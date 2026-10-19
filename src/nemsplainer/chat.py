"""Provider-neutral NEM analyst: prompt, DuckDB tools, history and the turn loop.

History is stored in a neutral format so a conversation can switch freely
between models and providers. Each provider module renders it into its own
wire format and streams back UI events plus one assistant turn:

- ``{'role': 'user', 'text': str}``
- ``{'role': 'assistant', 'model': ref, 'model_name': str, 'text': str, 'tool_calls': [{'id', 'name', 'input'}],
  'raw': {provider: provider-specific replay data}, 'stop': 'end' | 'tool_use' | 'pause'}``
- ``{'role': 'tool', 'results': [{'id', 'name', 'text', 'image': base64 PNG | None, 'is_error': bool}]}``
"""
import asyncio
import base64
import csv
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
import io
import json
from typing import AsyncIterator

import altair
import duckdb
import vl_convert

from .models import ModelInfo

TABLE_ROWS = 200
MAX_ROUNDS = 25
NO_VISION_BLURB = ('I am unable to visually sense-check or comment on the results of this visualization. '
                   'For that, you will need a model that can read images.')

_PROMPT_HEAD = """You are a data analyst for Australia's National Electricity Market (NEM).

You have two tools that execute DuckDB SQL against the `nem` database:
- `query_mms(sql)` returns a short CSV preview and displays a table to the user.
"""

_VISION_TOOLS = """- `visualize_mms(sql, plot_spec)` takes SQL and a JSON-encoded Vega-Lite spec
  (omit data/datasets), plots ALL query rows, and returns a PNG for you to inspect.
  Use it when asked for a plot/chart, or when a visual comparison or trend helps.
  Use explicit encoding types and fields matching SQL aliases; label axes and units.
  Inspect the returned image before commenting; never claim to see a failed plot.
"""

_NO_VISION_TOOLS = f"""- `visualize_mms(sql, plot_spec)` takes SQL and a JSON-encoded Vega-Lite spec
  (omit data/datasets) and plots ALL query rows for the user.
  Use it when asked for a plot/chart, or when a visual comparison or trend helps.
  Use explicit encoding types and fields matching SQL aliases; label axes and units.
  You cannot see images, so you never see the plots you make. Do not describe or
  comment on a plot's visual content; base any insight only on the CSV preview.
  Whenever you produce a plot, include this sentence verbatim in your reply:
  "{NO_VISION_BLURB}"
"""

_PROMPT_TAIL = """  Interactive plots are displayed automatically as soon as the tool finishes,
  before your interpretation; do not duplicate them in your written response.
  Do not emit image links, HTML, or Vega-Lite JSON in your final answer.
ALWAYS call at least one of these tools before answering factual market questions.
The user can switch models mid-conversation. Earlier assistant turns that start with
"[Earlier reply by <model>]" were written by that other model, not by you: build on or
correct them as needed, but don't apologise for them as your own.

Data coverage:
- This database intentionally contains only a short date range. This is expected,
  not a data-quality problem to investigate or a reason to delay answering.
- Do NOT run preliminary date-range or coverage checks (such as MIN/MAX dates
  or counts by date) unless the user explicitly asks about data coverage.
- Go directly to the query or plot needed to answer the question. If the user
  gives no date range, use the available data rather than assuming a recent
  calendar window or checking which dates exist first.
- Report what the query results and plots actually show. Do not apologize for,
  speculate about, or repeatedly caveat the short date range, and do not seek
  additional history. Keep conclusions scoped to the observed data; do not
  extrapolate to unobserved periods.
- Honor explicit date filters. If the requested query returns no rows, simply
  say so rather than launching a separate coverage investigation.

Schema highlights (all tables in the `main` schema):
- DISPATCHPRICE: 5-min dispatch prices per region — SETTLEMENTDATE (TIMESTAMP), REGIONID,
  RRP (energy price $/MWh), plus FCAS prices (RAISE6SECRRP, RAISEREGRRP, RRP per MWh), ...
- DISPATCHREGIONSUM: regional supply/demand — TOTALDEMAND, AVAILABLEGENERATION,
  NETINTERCHANGE, DEMANDFORECAST, ...
- DISPATCH_UNIT_SCADA: unit output — SETTLEMENTDATE, DUID, SCADAVALUE (MW).
- DUDETAILSUMMARY: unit registry — DUID, REGIONID, DISPATCHTYPE (GENERATOR/LOAD),
  PARTICIPANTID, START_DATE, END_DATE.

SQL style rules:
- ALWAYS spread SQL across multiple lines with proper indentation (never a single-line query).
- NEVER append any comments to the SQL (no '-- ...' trailing annotations of any kind).
- Prefer aggregations (GROUP BY, AVG, ...) instead of fetching raw rows.
- Both tools return only a 10-row CSV preview of the result. That is intentional: you never need the full result set to answer, since your reply is a short insight from an aggregate query. Do NOT try to squeeze the whole series into 10 rows or pivot rows into columns to fit the preview, and do NOT say the data is limited/hidden — the user sees the full result as an interactive table.
- For monthly aggregates use DATE_TRUNC('month', SETTLEMENTDATE).
- Regions are NSW1, QLD1, SA1, TAS1, VIC1.
- After receiving tool output, reply with a concise 1-2 sentence insight about the data.
- Prices are fine to write as usual, e.g. $52.30/MWh."""


def system_prompt(vision: bool) -> str:
    return _PROMPT_HEAD + (_VISION_TOOLS if vision else _NO_VISION_TOOLS) + _PROMPT_TAIL


VISION_PLOT_NOTE = ('Inspect this plot of the SQL result and describe its key patterns. '
                    'The interactive Vega-Lite plot is displayed when this tool completes.')
NO_VISION_PLOT_NOTE = ('The interactive plot is now displayed to the user. You cannot see it: do not describe '
                       f'its visual content, and include this sentence in your reply: "{NO_VISION_BLURB}"')

# Tool definitions in Anthropic's shape; providers convert as needed.
TOOLS = [
    {
        'name': 'query_mms',
        'description': (
            'Run one read-only DuckDB SELECT against the NEM MMS database. Returns a '
            '10-row CSV preview; the user sees the full query result in a table. '
            'Prefer aggregate queries; do not pivot to fit the preview.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'sql': {'type': 'string', 'description': 'A DuckDB SELECT query against the main schema.'},
            },
            'required': ['sql'],
        },
    },
    {
        'name': 'visualize_mms',
        'description': (
            'Query MMS and create a Vega-Lite plot for the user from all result rows. Returns a '
            'short CSV preview (and, for models that can read images, a PNG of the plot). The '
            'interactive plot is automatically displayed as soon as this tool finishes; do not '
            'embed it yourself.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'sql': {
                    'type': 'string',
                    'description': 'One read-only DuckDB SELECT query against the main schema. '
                                   'Aggregate/filter in SQL to produce a useful, manageable plot.',
                },
                'plot_spec': {
                    'type': 'string',
                    'description': (
                        'A JSON-encoded Vega-Lite specification, WITHOUT data or datasets (SQL '
                        'results are injected automatically). Use explicit field names matching '
                        'SQL aliases and explicit encoding types. For example: {"mark":"line",'
                        '"encoding":{"x":{"field":"month","type":"temporal"},"y":{"field":'
                        '"avg_rrp","type":"quantitative"},"color":{"field":"REGIONID","type":'
                        '"nominal"}},"height":320}. Layered/faceted plots are supported. Width '
                        'defaults to 640 pixels; "container" is converted to a fixed width for '
                        'PNG rendering.'
                    ),
                },
            },
            'required': ['sql', 'plot_spec'],
        },
    },
]
REQUIRED_INPUTS = {tool['name']: tuple(tool['input_schema']['required']) for tool in TOOLS}


@dataclass
class Result:
    sql: str
    columns: list[str]
    rows: list[tuple]


def _jsonable(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _preview(result: Result) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow(result.columns)
    writer.writerows(result.rows[:10])
    return out.getvalue()


def _records(result: Result) -> list[dict]:
    return [{c: _jsonable(v) for c, v in zip(result.columns, row)} for row in result.rows]


def run_query(db: duckdb.DuckDBPyConnection, sql: str) -> Result | str:
    """Return the full result, or an error string for the model."""
    cursor = db.cursor()  # one cursor per call: tools run in worker threads
    try:
        statements = cursor.extract_statements(sql)
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            return 'Only one SELECT query is allowed.'
        relation = cursor.sql(sql)
        return Result(sql, relation.columns, relation.fetchall())
    except Exception as err:
        return f'Query failed: {err}'
    finally:
        cursor.close()


def prepare_spec(plot_spec: str) -> dict:
    spec = json.loads(plot_spec)
    if not isinstance(spec, dict):
        raise ValueError('plot_spec must be a JSON object')

    def prepare(node):
        # All views must use the SQL result, not embedded or remote data.
        if isinstance(node, dict):
            if 'data' in node or 'datasets' in node:
                raise ValueError('Omit data/datasets; SQL results supply the plot data')
            mark = node.get('mark')
            if mark == 'image' or (isinstance(mark, dict) and mark.get('type') == 'image'):
                raise ValueError('Image marks are not supported; plot the SQL data directly')
            for key, value in node.items():
                if key in ('width', 'height') and value == 'container':
                    node[key] = 640 if key == 'width' else 320
                else:
                    prepare(value)
        elif isinstance(node, list):
            for value in node:
                prepare(value)

    prepare(spec)
    # Composite charts determine their size in their child views.
    if not any(key in spec for key in ('hconcat', 'vconcat', 'concat', 'facet', 'repeat')):
        spec.setdefault('width', 640)
        spec.setdefault('height', 320)
    # Validate before running potentially expensive SQL.
    altair.Chart.from_dict({**spec, 'data': {'values': []}}, validate=True)
    return spec


def table_event(result: Result) -> dict:
    return {
        'type': 'table', 'sql': result.sql, 'columns': result.columns, 'total': len(result.rows),
        'rows': [[_jsonable(v) for v in row] for row in result.rows[:TABLE_ROWS]],
    }


def query_tool(db, sql: str) -> tuple[str, str | None, list[dict]]:
    """Run query_mms, returning (result text, PNG base64, UI events)."""
    result = run_query(db, sql)
    if isinstance(result, str):
        return result, None, []
    return _preview(result), None, [table_event(result)]


def visualize_tool(db, sql: str, plot_spec: str) -> tuple[str, str | None, list[dict]]:
    """Run visualize_mms, returning (result text, PNG base64, UI events).

    The PNG is always rendered and stored; providers only send it to models
    that can read images.
    """
    try:
        spec = prepare_spec(plot_spec)
    except Exception as err:
        return f"Invalid plot specification: {getattr(err, 'message', str(err))}", None, []
    result = run_query(db, sql)
    if isinstance(result, str):
        return result, None, []
    if not result.rows:
        return 'The query returned no rows. Adjust the SQL before plotting.', None, [table_event(result)]
    try:
        full = altair.Chart.from_dict({**spec, 'data': {'values': _records(result)}}, validate=True).to_dict()
        png = vl_convert.vegalite_to_png(full, scale=1)
    except Exception as err:
        return f'Plot rendering failed: {err}. Revise the specification and retry.', None, []
    text = f'Plot created from all {len(result.rows)} rows. First 10 rows:\n{_preview(result)}'
    events = [{'type': 'plot', 'sql': sql, 'spec': full, 'plot_spec': spec}, table_event(result)]
    return text, base64.standard_b64encode(png).decode(), events


def earlier_reply_label(entry: dict, model: ModelInfo) -> str:
    """Prefix for assistant turns written by a different model than ``model``."""
    if entry['model'] == model.ref:
        return ''
    return f"[Earlier reply by {entry.get('model_name') or entry['model']}]"


def valid_input(name: str, args) -> bool:
    # Inputs may arrive unvalidated (eager streaming, OpenAI-style JSON strings).
    required = REQUIRED_INPUTS.get(name)
    return required is not None and isinstance(args, dict) and all(isinstance(args.get(k), str) for k in required)


async def execute_tool(db, call: dict) -> tuple[dict, list[dict]]:
    """Run one tool call, returning its neutral result and UI events."""
    name, args = call['name'], call['input']
    result = {'id': call['id'], 'name': name, 'image': None, 'is_error': False}
    if not valid_input(name, args):
        result.update(text=json.dumps({'INVALID_JSON': json.dumps(args)}), is_error=True)
        return result, []
    if name == 'query_mms':
        text, image, events = await asyncio.to_thread(query_tool, db, args['sql'])
    else:
        text, image, events = await asyncio.to_thread(visualize_tool, db, args['sql'], args['plot_spec'])
    result.update(text=text, image=image)
    return result, events


@dataclass
class Conversation:
    history: list = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


async def run_turn(providers: dict, db, conversation: Conversation, text: str, model: ModelInfo) -> AsyncIterator[dict]:
    """Stream UI events for one user turn, running tools until the model finishes.

    Plots are emitted as soon as their tool finishes; only the latest query
    table is shown, at the end of the turn, as in the marimo notebook.
    """
    provider = providers[model.provider]
    history = conversation.history
    start = len(history)
    history.append({'role': 'user', 'text': text})
    latest_table = None
    try:
        for _ in range(MAX_ROUNDS):
            turn = None
            async for item in provider.respond(model, history):
                if item.get('role') == 'assistant':
                    turn = item
                else:
                    yield item
            if turn is None:  # refused; the provider already reported it
                return
            history.append(turn)
            if turn['stop'] == 'pause':
                continue
            if not turn['tool_calls']:
                break
            results = []
            for call in turn['tool_calls']:  # sequential: tools share the DuckDB connection
                yield {'type': 'tool_running', 'name': call['name']}
                result, events = await execute_tool(db, call)
                for event in events:
                    if event['type'] == 'table':
                        latest_table = event
                    else:
                        yield event
                results.append(result)
            history.append({'role': 'tool', 'results': results})
        else:
            yield {'type': 'error', 'message': f'Stopped after {MAX_ROUNDS} tool rounds.'}
    except BaseException:
        # Leave history ending on a complete turn (no tool call without its result).
        del history[start:]
        raise
    if latest_table is not None:
        yield latest_table
