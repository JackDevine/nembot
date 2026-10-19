"""Claude-backed NEM analyst: DuckDB query/plot tools and a streaming agent loop."""
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
import anthropic
import duckdb
import vl_convert

MODEL = 'claude-opus-5-5'
TABLE_ROWS = 200

SYSTEM_PROMPT = """You are a data analyst for Australia's National Electricity Market (NEM).

You have two tools that execute DuckDB SQL against the `nem` database:
- `query_mms(sql)` returns a short CSV preview and displays a table to the user.
- `visualize_mms(sql, plot_spec)` takes SQL and a JSON-encoded Vega-Lite spec
  (omit data/datasets), plots ALL query rows, and returns a PNG for you to inspect.
  Use it when asked for a plot/chart, or when a visual comparison or trend helps.
  Use explicit encoding types and fields matching SQL aliases; label axes and units.
  Inspect the returned image before commenting; never claim to see a failed plot.
  Interactive plots are displayed automatically as soon as the tool finishes,
  before your interpretation; do not duplicate them in your written response.
  Do not emit image links, HTML, or Vega-Lite JSON in your final answer.
ALWAYS call at least one of these tools before answering factual market questions.

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

TOOLS = [
    {
        'name': 'query_mms',
        'description': (
            'Run one read-only DuckDB SELECT against the NEM MMS database. Returns a '
            '10-row CSV preview; the user sees the full query result in a table. '
            'Prefer aggregate queries; do not pivot to fit the preview.'
        ),
        'eager_input_streaming': True,
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
            'Query MMS and create a Vega-Lite plot for you and the user. You receive a PNG '
            'of the full plot and a short CSV preview, so inspect the image before '
            'commenting. The interactive plot is automatically displayed as soon as this '
            'tool finishes; do not embed it yourself.'
        ),
        'eager_input_streaming': True,
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


def _prepare_spec(plot_spec: str) -> dict:
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


def query_tool(db, sql: str) -> tuple[str, list[dict]]:
    """Run query_mms, returning (tool_result content, UI events)."""
    result = run_query(db, sql)
    if isinstance(result, str):
        return result, []
    return _preview(result), [_table_event(result)]


def visualize_tool(db, sql: str, plot_spec: str) -> tuple[str | list[dict], list[dict]]:
    """Run visualize_mms, returning (tool_result content, UI events)."""
    try:
        spec = _prepare_spec(plot_spec)
    except Exception as err:
        return f"Invalid plot specification: {getattr(err, 'message', str(err))}", []
    result = run_query(db, sql)
    if isinstance(result, str):
        return result, []
    if not result.rows:
        return 'The query returned no rows. Adjust the SQL before plotting.', [_table_event(result)]
    try:
        full = altair.Chart.from_dict({**spec, 'data': {'values': _records(result)}}, validate=True).to_dict()
        png = vl_convert.vegalite_to_png(full, scale=1)
    except Exception as err:
        return f'Plot rendering failed: {err}. Revise the specification and retry.', []
    content = [
        {'type': 'text', 'text': f'Plot created from all {len(result.rows)} rows. First 10 rows:\n{_preview(result)}'},
        {'type': 'text', 'text': 'Inspect this plot of the SQL result and describe its key patterns. '
                                 'The interactive Vega-Lite plot is displayed when this tool completes.'},
        {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png',
                                     'data': base64.standard_b64encode(png).decode()}},
    ]
    return content, [{'type': 'plot', 'sql': sql, 'spec': full, 'plot_spec': spec}, _table_event(result)]


def _table_event(result: Result) -> dict:
    return {
        'type': 'table', 'sql': result.sql, 'columns': result.columns, 'total': len(result.rows),
        'rows': [[_jsonable(v) for v in row] for row in result.rows[:TABLE_ROWS]],
    }


def _valid_input(name: str, args) -> bool:
    # Eager input streaming skips server-side validation, so check before running.
    required = {'query_mms': ('sql',), 'visualize_mms': ('sql', 'plot_spec')}.get(name)
    return required is not None and isinstance(args, dict) and all(isinstance(args.get(k), str) for k in required)


def _echoable(content: list) -> list:
    """Drop pre-fallback thinking/tool_use blocks, which must not be echoed back."""
    boundary = max((i for i, b in enumerate(content) if b.type == 'fallback'), default=-1)
    dropped = {'thinking', 'redacted_thinking', 'tool_use'}
    return [b for i, b in enumerate(content) if i > boundary or b.type not in dropped]


@dataclass
class Conversation:
    messages: list = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


async def run_turn(client: anthropic.AsyncAnthropic, db, conversation: Conversation, text: str) -> AsyncIterator[dict]:
    """Stream UI events for one user turn, running tools until Claude finishes.

    Plots are emitted as soon as their tool finishes; only the latest query
    table is shown, at the end of the turn, as in the marimo notebook.
    """
    messages = conversation.messages
    start = len(messages)
    messages.append({'role': 'user', 'content': text})
    latest_table = None
    json_retries = 0
    try:
        while True:
            try:
                async with client.beta.messages.stream(
                    model=MODEL,
                    max_tokens=64000,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=messages,
                    thinking={'type': 'adaptive', 'display': 'updates'},
                    output_config={'effort': 'medium'},
                    cache_control={'type': 'ephemeral'},
                    fallbacks='default',
                    betas=['server-side-fallback-2026-07-01', 'thinking-display-updates-2026-08-18'],
                ) as stream:
                    async for event in stream:
                        if event.type == 'content_block_start':
                            kind = event.content_block.type
                            if kind == 'tool_use':
                                yield {'type': 'tool', 'name': event.content_block.name}
                            elif kind in ('text', 'thinking'):
                                yield {'type': 'block', 'kind': kind}
                        elif event.type == 'content_block_delta':
                            if event.delta.type == 'text_delta':
                                yield {'type': 'text', 'delta': event.delta.text}
                            elif event.delta.type == 'thinking_delta':
                                yield {'type': 'progress', 'delta': event.delta.thinking}
                    response = await stream.get_final_message()
                json_retries = 0
            except ValueError:
                # Tool input JSON the SDK could not parse at all; re-issue the turn.
                json_retries += 1
                if json_retries > 2:
                    raise
                yield {'type': 'notice', 'message': 'Retrying after malformed tool input…'}
                continue

            if response.stop_reason == 'refusal':
                yield {'type': 'error', 'message': 'The model declined this request.'}
                return
            content = _echoable(response.content)
            messages.append({'role': 'assistant', 'content': content})
            if response.stop_reason == 'pause_turn':
                continue
            tool_uses = [b for b in content if b.type == 'tool_use']
            if not tool_uses:
                break
            if response.stop_reason == 'max_tokens':
                raise RuntimeError('Tool input was truncated by max_tokens.')

            results = []
            for block in tool_uses:  # sequential: tools share the DuckDB connection
                if not _valid_input(block.name, block.input):
                    results.append({'type': 'tool_result', 'tool_use_id': block.id, 'is_error': True,
                                    'content': json.dumps({'INVALID_JSON': json.dumps(block.input)})})
                    continue
                if block.name == 'query_mms':
                    output, events = await asyncio.to_thread(query_tool, db, block.input['sql'])
                else:
                    output, events = await asyncio.to_thread(visualize_tool, db, block.input['sql'], block.input['plot_spec'])
                for ui_event in events:
                    if ui_event['type'] == 'table':
                        latest_table = ui_event
                    else:
                        yield ui_event
                results.append({'type': 'tool_result', 'tool_use_id': block.id, 'content': output})
            messages.append({'role': 'user', 'content': results})
    except BaseException:
        # Leave history ending on a complete turn (no tool_use without its result).
        del messages[start:]
        raise
    if latest_table is not None:
        yield latest_table
