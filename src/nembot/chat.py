"""Provider-neutral NEM analyst: prompt, DuckDB tools, history and the turn loop.

History is stored in a neutral format so a conversation can switch freely
between models and providers. Each provider module renders it into its own
wire format and streams back UI events plus one assistant turn:

- ``{'role': 'user', 'text': str, 'context': str}`` (``context`` is the frozen <data_status> note)
- ``{'role': 'assistant', 'model': ref, 'model_name': str, 'text': str, 'tool_calls': [{'id', 'name', 'input'}],
  'raw': {provider: provider-specific replay data}, 'stop': 'end' | 'tool_use' | 'pause'}``
- ``{'role': 'tool', 'results': [{'id', 'name', 'text', 'image': base64 PNG | None, 'is_error': bool}]}``
"""
import asyncio
import base64
import concurrent.futures
import csv
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import io
import json
import time as time_module  # `time` is the datetime type imported above
from typing import AsyncIterator

import altair
import duckdb
import vl_convert

from . import catalog, downloads, explorer, guides, live
from .notebook_files import LIVE_TOOL as RUN_IN_MARIMO, READ_TOOL as READ_NOTEBOOK, WRITE_TOOL as WRITE_NOTEBOOK
from .ingest import SUPPORTED, _month_shift
from .models import ModelInfo, public as model_public

TABLE_ROWS = 200
MAX_ROUNDS = 25
NO_VISION_BLURB = ('I am unable to visually sense-check or comment on the results of this visualization. '
                   'For that, you will need a model that can read images.')

_PROMPT_HEAD = """You are a data analyst for Australia's National Electricity Market (NEM).

Your tools work on a local DuckDB copy of AEMO's public MMS data (the `nem` database):
- `query_mms(sql)` returns a short CSV preview and displays a table to the user.
- `describe_table(table)` returns AEMO's data-model documentation for any MMS table
  (description, keys, columns and their meaning) and which months are downloaded.
- `request_download(table, start_month, end_month, reason, recent)` asks the user to approve
  downloading months of a table from AEMO. The user sees a popup and may change the
  months. It returns once the download has finished, failed or been declined.
- `create_report(format, title, content)` exports a report (html, pdf, docx, pptx, xlsx) or a
  runnable notebook (jupyter, marimo) that simply re-runs this conversation's results. Only use
  it when the user asks for a report, document, deck, workbook or a notebook of these results.
  Every query/plot result has an id (shown as "Result id: R3" in tool output). Write the
  content in markdown and put a result on its own line as [[R3]] to include it. Use ##
  headings for sections (each section becomes a slide in pptx). Include only results that
  support the narrative; the data is re-queried when the file is built.
- `create_document(title, content)` creates a shared document that opens beside the chat,
  where the user can edit it and highlight passages to ask you about them. Use it when the
  user asks for a document, write-up, brief or draft to work on together (create_report is
  for exported files). Content is markdown with [[R3]] result lines, as for reports; each
  becomes a snapshot table or chart.
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
- The database holds only the tables and months the user has downloaded. Each user
  message ends with a <data_status> note listing them. Trust it instead of running
  coverage checks (no preliminary MIN/MAX or count-by-date queries).
- Write queries as though every archive table listed below were available. If a query
  fails because a table is missing, or the question needs months that <data_status>
  shows are not downloaded, call `request_download` for that table and the months the
  question needs (keep the range as small as the question allows; never more than 12
  months unless the user asks), then re-run the query once the download succeeds.
  If the user declines or the download fails, answer from the data that is available
  and say what is missing.
- Use `describe_table` when you need the columns of a table you haven't used yet.
- If the user gives no date range, use the downloaded data rather than assuming a
  recent calendar window. Do not apologize for, or repeatedly caveat, limited data.
- Report what the query results and plots actually show. Keep conclusions scoped to
  the observed data; do not extrapolate to unobserved periods.
- Honor explicit date filters.
"""

# Shared with the document assistant (docs.py).
_SCHEMA_GUIDE = """Schema highlights (all tables in the `main` schema):
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


def _archive_tables() -> str:
    """Static, cache-friendly list of every table the monthly archive publishes."""
    lines = ['', '', 'Tables published in the MMS monthly archive (downloadable on request), by package:']
    for package in catalog.load()['packages']:
        names = [n for n in package['tables'] if catalog.tables()[n]['in_archive']]
        if names:
            lines.append(f"- {package['name']}: {', '.join(names)}")
    return '\n'.join(lines)


_SCHEMA_GUIDE += _archive_tables()
_PROMPT_TAIL += '\n' + _SCHEMA_GUIDE


_WEB_SEARCH = """

Web search:
- You can also search the web. Use it for context the database can't give: market
  events, rule changes, outages, weather, AEMO reports and news that explain the data.
- Answer data questions from the database tools, not the web. Mention which facts
  came from the web; citations are shown to the user automatically."""


_NOTEBOOK_TOOLS = """- `write_notebook(name, format, code | cells)` writes a marimo or Jupyter notebook with any Python
  (widgets, custom analysis, charts) when the user wants a notebook to explore or reuse. nembot checks
  and runs it before saving. `read_notebook(name)` reads one back. Read the marimo or jupyter guide first.
- `run_in_marimo(notebook, code)` works inside a marimo notebook the user has open: inspect its variables,
  or add, edit and run cells live. Read the marimo guide's LIVE section first.
"""


def system_prompt(vision: bool, web_search: bool = False) -> str:
    prompt = (_PROMPT_HEAD + _NOTEBOOK_TOOLS + guides.prompt_index() + (_VISION_TOOLS if vision else _NO_VISION_TOOLS)
              + _PROMPT_TAIL)
    return prompt + _WEB_SEARCH if web_search else prompt


def text_from_blocks(blocks: list[tuple[str, str]]) -> str:
    """Join (type, text) blocks: adjacent text blocks (citation splits) join directly."""
    parts, joined = [], False
    for kind, text in blocks:
        if kind != 'text':
            joined = False
        elif joined:
            parts[-1] += text
        else:
            parts.append(text)
            joined = True
    return '\n\n'.join(parts)


VISION_PLOT_NOTE = ('Inspect this plot of the SQL result and describe its key patterns. '
                    'The interactive Vega-Lite plot is displayed when this tool completes.')
NO_VISION_PLOT_NOTE = ('The interactive plot is now displayed to the user. You cannot see it: do not describe '
                       f'its visual content, and include this sentence in your reply: "{NO_VISION_BLURB}"')

# Tool definitions in Anthropic's shape; providers convert as needed.
TOOLS = [
    {
        'name': 'describe_table',
        'description': ('Return AEMO data-model documentation for an MMS table: description, primary key, '
                        'columns (type and meaning) and which months are downloaded locally.'),
        'input_schema': {
            'type': 'object',
            'properties': {'table': {'type': 'string', 'description': 'MMS table name, e.g. DISPATCHLOAD.'}},
            'required': ['table'],
        },
    },
    {
        'name': 'create_report',
        'description': ('Export a report or notebook from this conversation. content is markdown; put a result on '
                        'its own line as [[R3]] to include that table or plot. ## headings start sections '
                        '(slides in pptx). xlsx gets one sheet per included result.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'format': {'type': 'string', 'enum': ['html', 'pdf', 'docx', 'pptx', 'xlsx', 'jupyter', 'marimo']},
                'title': {'type': 'string'},
                'content': {'type': 'string', 'description': 'Markdown with [[R<n>]] result markers on their own lines.'},
            },
            'required': ['format', 'title', 'content'],
        },
    },
    {
        'name': 'create_document',
        'description': ('Create a shared document that opens beside the chat for the user to edit with you. content '
                        'is markdown; put a result on its own line as [[R3]] to include a snapshot of that table or '
                        'plot. Use ## headings for sections.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'title': {'type': 'string'},
                'content': {'type': 'string', 'description': 'Markdown with [[R<n>]] result markers on their own lines.'},
            },
            'required': ['title', 'content'],
        },
    },
    {
        'name': 'request_download',
        'description': ('Ask the user to approve downloading whole months of an MMS table from AEMO. The user '
                        'sees the table, months and estimated size and may change the months. Returns when '
                        'the download has finished, failed, or was declined; then re-run your query.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'table': {'type': 'string', 'description': 'MMS table name from the archive list.'},
                'start_month': {'type': 'string', 'description': 'First month, YYYY-MM.'},
                'end_month': {'type': 'string', 'description': 'Last month, YYYY-MM (inclusive).'},
                'reason': {'type': 'string', 'description': 'One sentence for the user: why this data is needed.'},
                'recent': {'type': 'boolean', 'description': (
                    'Also fetch AEMO\'s recent daily/5-minute reports published after the latest monthly '
                    'archive (only for: ' + ', '.join(sorted(live.FEEDS)) + '). Use when the question needs '
                    'data newer than the latest monthly archive month.')},
            },
            'required': ['table', 'start_month', 'end_month', 'reason'],
        },
    },
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
    guides.TOOL,
    WRITE_NOTEBOOK,
    READ_NOTEBOOK,
    RUN_IN_MARIMO,
]
REQUIRED_INPUTS = {tool['name']: tuple(tool['input_schema']['required']) for tool in TOOLS}


@dataclass
class Result:
    sql: str
    columns: list[str]
    rows: list[tuple]
    seconds: float = 0.0  # wall-clock time to run the query and fetch every row


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
        started = time_module.perf_counter()
        relation = cursor.sql(sql)
        rows = relation.fetchall()
        return Result(sql, relation.columns, rows, time_module.perf_counter() - started)
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
        'query_seconds': round(result.seconds, 3),
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
        started = time_module.perf_counter()
        full = chart_spec(spec, _records(result))
        png = vl_convert.vegalite_to_png(full, scale=1)
        render_seconds = time_module.perf_counter() - started
    except Exception as err:
        return f'Plot rendering failed: {err}. Revise the specification and retry.', None, []
    text = f'Plot created from all {len(result.rows)} rows. First 10 rows:\n{_preview(result)}'
    return text, base64.standard_b64encode(png).decode(), [plot_event(sql, spec, full, result, render_seconds)]


def chart_spec(plot_spec: dict, records: list[dict]) -> dict:
    """A complete Vega-Lite spec: ``plot_spec`` with ``records`` as its data.

    Validated with one row, then given them all: altair's schema check walks every data row
    and grows much faster than linearly (26k rows took ~2 minutes), while the structure is
    checked in prepare_spec and Vega-Lite accepts any objects as rows.
    """
    full = altair.Chart.from_dict({**plot_spec, 'data': {'values': records[:1]}}, validate=True).to_dict()
    (name,) = full['datasets']  # the data, under altair's hashed dataset name
    full['datasets'][name] = records
    return full


def plot_event(sql: str, plot_spec: dict, full: dict, result: Result, render_seconds: float | None = None) -> dict:
    """``render_seconds``: building the chart spec with the data (and, in the chat, the PNG the model sees)."""
    # The data travels with the plot (shown under its SQL), not as a separate table at the end of the turn.
    table = table_event(result)
    return {'type': 'plot', 'sql': sql, 'spec': full, 'plot_spec': plot_spec,
            'columns': table['columns'], 'rows': table['rows'], 'total': table['total'],
            'query_seconds': table['query_seconds'],
            'render_seconds': round(render_seconds, 3) if render_seconds is not None else None}


def rerun_chart(db, sql: str, plot_spec) -> dict:
    """Redraw a saved chat's chart from its SQL (saved chats keep no chart data)."""
    try:
        spec = prepare_spec(json.dumps(plot_spec))
    except Exception as err:
        return {'error': f"Invalid plot specification: {getattr(err, 'message', str(err))}"}
    result = run_query(db, sql)
    if isinstance(result, str):
        return {'error': result}
    try:
        started = time_module.perf_counter()
        full = chart_spec(spec, _records(result))
        return plot_event(sql, spec, full, result, time_module.perf_counter() - started)
    except Exception as err:
        return {'error': f'Plot failed: {err}'}


def earlier_reply_label(entry: dict, model: ModelInfo) -> str:
    """Prefix for assistant turns written by a different model than ``model``."""
    if entry['model'] == model.ref:
        return ''
    return f"[Earlier reply by {entry.get('model_name') or entry['model']}]"


def _month_ranges(months: list[str]) -> str:
    """['2026-06', '2026-07', '2026-09'] -> '2026-06 to 2026-07, 2026-09'."""
    spans = []
    for month in months:
        if spans and downloads.next_month(spans[-1][1]) == month:
            spans[-1][1] = month
        else:
            spans.append([month, month])
    return ', '.join(a if a == b else f'{a} to {b}' for a, b in spans)


_status_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)


def data_status(db) -> str:
    """What is downloaded right now, frozen into each user message."""
    lines = ['<data_status>', 'Downloaded tables:']
    overview = explorer.overview(db)
    tables = [t for p in overview['packages'] for t in p['tables'] if t['downloaded']]
    for t in tables:
        detail = explorer.table(db, t['name'])
        parts = [f"{detail['rows']:,} rows"]
        if detail['range']:
            parts.append(f"{detail['time_column']} {detail['range'][0][:16]} to {detail['range'][1][:16]}")
        months = downloads.loaded_months(db, t['name'])
        if months:
            parts.append(f'archive months {_month_ranges(months)}')
        lines.append(f"- {t['name']}: {'; '.join(parts)}")
    if not tables:
        lines.append('- none')
    try:  # don't let a slow or offline NEMWEB hold up the chat
        latest = _status_pool.submit(downloads.latest_published, 'DISPATCHPRICE').result(timeout=5)
    except Exception:
        latest = catalog.load()['archive']['month']
    lines += [f'Latest month published by AEMO: {latest}. Any other archive table can be requested.', '</data_status>']
    return '\n'.join(lines)


def describe_table(db, name: str) -> str:
    entry = catalog.tables().get(name.upper())
    if entry is None:
        return f'Unknown table {name!r}. Use a name from the archive table list.'
    detail = explorer.table(db, entry['name'])
    months = downloads.loaded_months(db, entry['name'])
    text = [f"{entry['name']} (package {entry['package']}): {entry['comment']}",
            f"Kind: {catalog.kind(entry['name'])}; time column: {catalog.time_column(entry['name']) or 'none'}",
            f"Primary key: {', '.join(entry['primary_key'])}",
            f"In the monthly archive: {'yes' if entry['in_archive'] else 'no'}",
            f"Downloaded: {'yes, ' + str(detail['rows']) + ' rows, months ' + _month_ranges(months) if detail['downloaded'] else 'no'}"]
    description = ' '.join(b['text'] for b in entry['description'] if b['type'] == 'text')
    if description:
        text.append(f'Description: {description[:1500]}')
    text.append('Columns:')
    text += [f"- {c['name']} {c['type']}{' (PK)' if c['name'] in entry['primary_key'] else ''}: {c['comment']}"
             for c in entry['columns']]
    return '\n'.join(text)


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
    if name == 'describe_table':
        result.update(text=await asyncio.to_thread(describe_table, db, args['table']))
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
    pending: dict = field(default_factory=dict)  # download request id -> Future for the user's decision
    results: dict = field(default_factory=dict)  # 'R1' -> {'sql', 'plot_spec'} for reports
    mode: str = 'analyst'  # or 'creator' (plugin building); fixed for the conversation
    # After a compare turn: each model's {'model', 'history', 'results', 'base'}, until the next turn starts.
    branches: list | None = None
    # While a turn runs: branch (None for a single-model turn) -> (task, the Conversation it runs on), for stop_turn.
    running: dict = field(default_factory=dict)
    stopping: bool = False  # set by stop_turn just before it cancels this conversation's reply


DECISION_TIMEOUT = 1800  # seconds to wait for the user to approve or decline a download

# ---- questionnaire: multiple-choice questions for the user (interactive chats only) ----
QUESTIONNAIRE = {
    'name': 'questionnaire',
    'description': (
        'Ask the user 1-4 multiple-choice questions and wait for the answers. Use it when a choice is genuinely '
        "the user's and changes what you do, e.g. which region, period, measure or presentation they mean. Don't "
        'use it for things the other tools can find out, or to confirm routine steps. Each question has 2-4 '
        'distinct options; the user can always write their own answer instead, so no "Other" option is needed.'),
    'input_schema': {
        'type': 'object',
        'properties': {
            'questions': {
                'type': 'array', 'minItems': 1, 'maxItems': 4,
                'items': {
                    'type': 'object',
                    'properties': {
                        'question': {'type': 'string', 'description': 'The full question, ending with a question mark.'},
                        'header': {'type': 'string', 'description': 'A very short label for it (at most 12 characters).'},
                        'options': {
                            'type': 'array', 'minItems': 2, 'maxItems': 4,
                            'items': {'type': 'object', 'properties': {
                                'label': {'type': 'string', 'description': 'The choice, in 1-5 words.'},
                                'description': {'type': 'string', 'description': 'What choosing it means, if not obvious.'}},
                                'required': ['label']},
                        },
                        'multi_select': {'type': 'boolean', 'description': 'Allow choosing several options.'},
                    },
                    'required': ['question', 'options'],
                },
            },
        },
        'required': ['questions'],
    },
}
QUESTIONNAIRE_NOTE = """
- `questionnaire(questions)` shows the user 1-4 multiple-choice questions and waits for the answers.
  Ask only when the answer is genuinely the user's to give and changes what you do (which region,
  period, measure or presentation they mean). Otherwise make a sensible choice and say what you assumed.
"""


def parse_questions(args) -> list[dict] | str:
    """Normalised questions, or why the input is unusable (sent back to the model)."""
    questions = args.get('questions') if isinstance(args, dict) else None
    if isinstance(questions, str):
        try:
            questions = json.loads(questions)
        except json.JSONDecodeError:
            questions = None
    if not isinstance(questions, list) or not 1 <= len(questions) <= 4:
        return 'questions must be a list of 1-4 questions.'
    parsed = []
    for i, q in enumerate(questions, 1):
        if not isinstance(q, dict) or not str(q.get('question') or '').strip():
            return f'Question {i} has no question text.'
        options = [o if isinstance(o, dict) else {'label': str(o)} for o in q.get('options') or []]
        options = [{'label': str(o.get('label') or '').strip()[:80], 'description': str(o.get('description') or '').strip()[:300]}
                   for o in options if str(o.get('label') or '').strip()]
        if not 2 <= len(options) <= 4 or len({o['label'] for o in options}) != len(options):
            return f'Question {i} needs 2-4 options with different labels.'
        parsed.append({'question': str(q['question']).strip()[:500], 'header': str(q.get('header') or '').strip()[:24],
                       'options': options, 'multi_select': bool(q.get('multi_select'))})
    return parsed


def clean_answers(questions: list[dict], answers) -> list[dict]:
    """The user's answers, keeping only offered labels and a short free-text answer."""
    answers = answers if isinstance(answers, list) else []
    cleaned = []
    for i, q in enumerate(questions):
        answer = answers[i] if i < len(answers) and isinstance(answers[i], dict) else {}
        labels = {o['label'] for o in q['options']}
        selected = [str(x) for x in answer.get('selected') or [] if str(x) in labels]
        cleaned.append({'selected': selected if q['multi_select'] else selected[:1],
                        'other': str(answer.get('other') or '').strip()[:2000]})
    return cleaned


def answers_text(questions: list[dict], answers: list[dict]) -> str:
    lines = ['The user answered:']
    for i, (q, a) in enumerate(zip(questions, answers), 1):
        chosen = a['selected'] + ([f'(in their own words) {a["other"]}'] if a['other'] else [])
        lines.append(f'{i}. {q["question"]}\n   -> ' + ('; '.join(chosen) if chosen else '(no answer)'))
    return '\n'.join(lines)


async def approve_code(conversation: 'Conversation', call: dict, code: str, reason: str) -> AsyncIterator[dict]:
    """Ask before running agent-written Python (unless turned off in Settings). Ends with
    ``{'type': 'approval', 'approved': bool}``."""
    from . import credentials
    if not credentials.setting('confirm_code', True):
        yield {'type': 'approval', 'approved': True}
        return
    request_id = call['id']
    future = asyncio.get_running_loop().create_future()
    conversation.pending[request_id] = future
    yield {'type': 'code_request', 'id': request_id, 'reason': reason, 'code': code}
    try:
        reply = await asyncio.wait_for(future, DECISION_TIMEOUT)
    except TimeoutError:
        reply = {'approve': False, 'timeout': True}
    finally:
        conversation.pending.pop(request_id, None)
    approved = bool(reply.get('approve'))
    if approved and reply.get('remember'):
        await asyncio.to_thread(credentials.set_setting, 'confirm_code', False)
    yield {'type': 'code_decision', 'id': request_id, 'approved': approved, 'timeout': bool(reply.get('timeout'))}
    yield {'type': 'approval', 'approved': approved}


async def questionnaire(conversation: 'Conversation', call: dict) -> AsyncIterator[dict]:
    """Show the questions in the chat and wait for the user's answers (or a skip)."""
    def result(text, is_error=False):
        return {'type': 'result', 'text': text, 'is_error': is_error}

    questions = parse_questions(call['input'])
    if isinstance(questions, str):
        yield result(f'The questionnaire could not be shown: {questions}', True)
        return
    request_id = call['id']
    future = asyncio.get_running_loop().create_future()
    conversation.pending[request_id] = future
    yield {'type': 'questions', 'id': request_id, 'questions': questions}
    try:
        reply = await asyncio.wait_for(future, DECISION_TIMEOUT)
    except TimeoutError:
        yield {'type': 'questions_answered', 'id': request_id, 'skipped': True, 'timeout': True}
        yield result('The user did not answer. Make sensible choices and say what you assumed.')
        return
    finally:
        conversation.pending.pop(request_id, None)
    if reply.get('skipped'):
        yield {'type': 'questions_answered', 'id': request_id, 'skipped': True}
        yield result('The user skipped the questions. Make sensible choices and say what you assumed.')
        return
    answers = clean_answers(questions, reply.get('answers'))
    yield {'type': 'questions_answered', 'id': request_id, 'answers': answers}
    yield result(answers_text(questions, answers))



async def request_download(db, jobs, conversation: Conversation, call: dict) -> AsyncIterator[dict]:
    """Ask the user (via a chat popup) to approve a download, run it, and report back.

    Yields UI events, then a final ``{'type': 'result', 'text', 'is_error'}``.
    """
    args, request_id = call['input'], call['id']
    table = args['table'].strip().upper()

    def result(text, is_error=False):
        return {'type': 'result', 'text': text, 'is_error': is_error}

    if jobs is None:
        yield result('Downloads are not available in this app.', True)
        return
    if table not in SUPPORTED:
        yield result(f'{table} is not published in the MMS monthly archive, so it cannot be downloaded.', True)
        return
    try:
        downloads.month_range(args['start_month'], args['end_month'])
        plan = await asyncio.to_thread(downloads.plan, db, table)
    except (ValueError, RuntimeError) as exc:
        yield result(f'Cannot request that download: {exc}', True)
        return
    future = asyncio.get_running_loop().create_future()
    conversation.pending[request_id] = future
    yield {'type': 'download_request', 'id': request_id, 'table': table, 'start': args['start_month'],
           'end': args['end_month'], 'reason': args.get('reason', ''), 'comment': catalog.tables()[table]['comment'],
           'recent': bool(args.get('recent')) and table in live.FEEDS,
           'plan': plan}
    try:
        decision = await asyncio.wait_for(future, DECISION_TIMEOUT)
    except TimeoutError:
        yield {'type': 'download_declined', 'id': request_id, 'timeout': True}
        yield result('The user did not respond to the download request. Answer with the data available.')
        return
    finally:
        conversation.pending.pop(request_id, None)
    if not decision.get('approve'):
        yield {'type': 'download_declined', 'id': request_id}
        yield result('The user declined the download. Answer with the data that is available and say what is missing.')
        return
    try:
        start, end = downloads.month_range(decision.get('start') or args['start_month'],
                                           decision.get('end') or args['end_month'])
    except ValueError as exc:
        yield result(f'The months chosen were invalid: {exc}', True)
        return
    recent = bool(args.get('recent')) and table in live.FEEDS and decision.get('recent', True)
    latest = date.fromisoformat(plan['latest_month'] + '-01')
    end = min(end, _month_shift(latest, 1) - timedelta(days=1))  # monthly archive only goes this far
    jobs_run = []
    if start <= end:
        jobs_run.append(jobs.submit(table, start, end, reason='chat'))
    if recent:
        jobs_run.append(await asyncio.to_thread(downloads.submit_live, jobs, db, table, 'chat'))
    jobs_run = [j for j in jobs_run if j is not None]
    if not jobs_run:
        yield result(f'{table} already has everything requested. Re-run your query.')
        return
    for job in jobs_run:
        yield {'type': 'download_started', 'id': request_id, 'job': job.public()}
        while job.status in ('queued', 'running'):
            await asyncio.sleep(1)
            yield {'type': 'download_progress', 'id': request_id, 'job': job.public()}
        yield {'type': 'download_finished', 'id': request_id, 'job': job.public()}
        if job.status != 'done':
            break
    months = f'{start:%Y-%m} to {end:%Y-%m}' + (' plus recent reports' if recent else '')
    if job.status == 'done':
        status = await asyncio.to_thread(describe_table, db, table)
        yield result(f'Downloaded {table} for {months}. Re-run your query now.\n'
                     + status.split('Columns:')[0].strip())
    elif job.status == 'cancelled':
        yield result(f'The user cancelled the download of {table} ({months}). Files that finished were kept; '
                     'answer with the data available.')
    else:
        yield result(f'The download of {table} ({months}) failed: {job.error}', True)


def chosen_effort(model: ModelInfo, efforts: dict | None) -> str | None:
    """The reasoning level picked for ``model`` in the model picker, if it takes that level, else nembot's default."""
    picked = (efforts or {}).get(model.ref)
    return picked if picked in model.efforts else model.default_effort


def model_event(model: ModelInfo, efforts: dict | None) -> dict:
    """A model as the UI shows it on a reply, with the reasoning level it ran at."""
    return {**model_public(model), 'effort': chosen_effort(model, efforts)}


def mode_setup(mode: str, model: ModelInfo, web_search: bool, plugin_manager=None) -> tuple[str, list[dict]]:
    """System prompt and tools for a conversation mode.

    The analyst only sees plugin tools (and their one-line notes); how to build
    plugins is documented solely in Creator mode's prompt.
    """
    if mode == 'creator':
        from . import creator
        system = creator.PROMPT + (_WEB_SEARCH if web_search else '')
        return system, creator.TOOLS + [t for t in TOOLS if t['name'] in ('query_mms', 'describe_table')]
    if mode == 'doc':
        from . import docs
        return docs.system_prompt(web_search), docs.TOOLS + [t for t in TOOLS if t['name'] in ('query_mms', 'describe_table')]
    system = system_prompt(model.vision, web_search) + (plugin_manager.prompt_notes() if plugin_manager else '')
    return system, TOOLS + (plugin_manager.tool_definitions() if plugin_manager else [])


async def _plugin_tool(plugin_manager, tool, call) -> AsyncIterator[dict]:
    from . import plugins
    output = await plugins.call_tool(tool, call['input'] if isinstance(call['input'], dict) else {})
    table = output.get('table')
    if isinstance(table, dict) and table.get('columns'):
        rows = [[_jsonable(v) for v in row] for row in table.get('rows', [])][:TABLE_ROWS]
        yield {'type': 'table', 'sql': f'-- from plugin {tool.plugin}', 'columns': list(table['columns']),
               'rows': rows, 'total': table.get('total', len(rows)), 'plugin': tool.plugin}
    for event in output.get('events') or []:
        if isinstance(event, dict):
            yield {'type': 'plugin_event', 'plugin': tool.plugin, 'kind': str(event.get('kind', '')),
                   'data': event.get('data')}
    yield {'type': 'result', 'text': output['text'], 'is_error': output['is_error']}


async def run_turn(providers: dict, db, conversation: Conversation, text: str, model: ModelInfo,
                   web_search: bool = False, jobs=None, exporter=None, plugin_manager=None,
                   creator=None, documents=None, doc_session=None, interactive: bool = False,
                   efforts: dict | None = None, notebook_files=None) -> AsyncIterator[dict]:
    """Stream UI events for one user turn, running tools until the model finishes.

    Plots are emitted as soon as their tool finishes; only the latest query
    table is shown, at the end of the turn, as in the marimo notebook.
    ``documents(title, content, results)`` creates a shared document (create_document);
    ``doc_session`` runs the suggest_* tools when nembot is tagged in a document.
    ``interactive`` (a person is watching the chat) adds the questionnaire tool; unattended runs
    such as evals leave it out so they never wait for answers.
    ``efforts`` maps model refs to the reasoning level picked for them (see chosen_effort).
    ``notebook_files`` (notebook_files.NotebookFiles) runs write_notebook and read_notebook.
    """
    provider = providers[model.provider]
    web_search = web_search and getattr(provider, 'web_search', True)  # e.g. Ollama has no web search
    history = conversation.history
    start = len(history)
    partial = []
    history.append({'role': 'user', 'text': text, 'context': await asyncio.to_thread(data_status, db)})
    latest_table = None
    system, tools = mode_setup(conversation.mode, model, web_search, plugin_manager)
    reasoning = {'effort': chosen_effort(model, efforts)} if model.efforts else {}
    if interactive and conversation.mode in ('analyst', 'creator'):
        system, tools = system + QUESTIONNAIRE_NOTE, tools + [QUESTIONNAIRE]
    try:
        for _ in range(MAX_ROUNDS):
            turn = None
            partial = []  # this round's text so far, kept if the user stops the reply
            async for item in provider.respond(model, history, web_search=web_search, system=system, tools=tools,
                                               **reasoning):
                if item.get('role') == 'assistant':
                    turn = item
                else:
                    if item.get('type') == 'text':
                        partial.append(item.get('delta', ''))
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
                special = None
                if doc_session is not None and call['name'] in doc_session.tool_names:
                    special = doc_session.run(call)
                elif conversation.mode == 'creator' and creator is not None:
                    from . import creator as creator_mode
                    if call['name'] in creator_mode.TOOL_NAMES:
                        special = creator.run(call, conversation, [t['name'] for t in TOOLS])
                elif plugin_manager is not None and (tool := plugin_manager.find_tool(call['name'])):
                    special = _plugin_tool(plugin_manager, tool, call)
                if special is not None:
                    async for event in special:
                        if event['type'] == 'result':
                            results.append({'id': call['id'], 'name': call['name'], 'text': event['text'],
                                            'image': event.get('image'), 'is_error': event['is_error']})
                        elif event['type'] == 'table':
                            latest_table = event
                        else:
                            yield event
                    continue
                if call['name'] == 'questionnaire' and interactive:
                    async for event in questionnaire(conversation, call):
                        if event['type'] == 'result':
                            results.append({'id': call['id'], 'name': call['name'], 'text': event['text'],
                                            'image': None, 'is_error': event['is_error']})
                        else:
                            yield event
                    continue
                if call['name'] == 'read_guide' and valid_input(call['name'], call['input']):
                    text, is_error = guides.read(call['input']['name'], call['input'].get('section'))
                    results.append({'id': call['id'], 'name': call['name'], 'text': text, 'image': None,
                                    'is_error': is_error})
                    continue
                if call['name'] == 'run_in_marimo' and valid_input(call['name'], call['input']):
                    args = call['input']
                    result = {'id': call['id'], 'name': call['name'], 'image': None}
                    approved = True
                    if notebook_files is None:
                        result.update(text='Notebooks are not available here.', is_error=True)
                        approved = False
                    elif interactive:  # it runs in the user's own notebook kernel
                        reason = (f"It runs in your open marimo notebook {args['notebook']}, where it can also add, "
                                  'change or delete cells.')
                        async for event in approve_code(conversation, call, args['code'], reason):
                            if event['type'] == 'approval':
                                approved = event['approved']
                            else:
                                yield event
                        if not approved:
                            result.update(text='The user chose not to run this code in their notebook.', is_error=True)
                    if approved:
                        result['text'], result['is_error'] = await notebook_files.run_in_marimo(args['notebook'], args['code'])
                    results.append(result)
                    continue
                if call['name'] in ('write_notebook', 'read_notebook') and valid_input(call['name'], call['input']):
                    args = call['input']
                    result = {'id': call['id'], 'name': call['name'], 'image': None, 'is_error': False}
                    if notebook_files is None:
                        result.update(text='Notebooks are not available here.', is_error=True)
                    elif call['name'] == 'read_notebook':
                        result['text'], result['is_error'] = notebook_files.read(args['name'])
                    else:
                        cells = args.get('cells')
                        if isinstance(cells, str):  # some models send the list as a JSON string
                            try:
                                cells = json.loads(cells)
                            except json.JSONDecodeError:
                                cells = None
                        approved = True
                        if interactive:  # checking a notebook runs its code on the user's computer
                            code = args.get('code') if args['format'] == 'marimo' else '\n\n# ---- next cell ----\n\n'.join(
                                str(c.get('source', '')) for c in cells or [] if isinstance(c, dict) and c.get('type') != 'markdown')
                            kind = 'marimo' if args['format'] == 'marimo' else 'Jupyter'
                            reason = (f"To check the {kind} notebook {args['name']} works, nembot runs it once before "
                                      'saving it.')
                            async for event in approve_code(conversation, call, str(code or ''), reason):
                                if event['type'] == 'approval':
                                    approved = event['approved']
                                else:
                                    yield event
                        outcome = (await notebook_files.write(args['name'], args['format'], args.get('code'), cells,
                                                              bool(args.get('overwrite'))) if approved else
                                   {'ok': False, 'problems': 'The user chose not to run this code, so the notebook was '
                                                             'not checked or saved. Ask what they would like changed.'})
                        if outcome['ok']:
                            yield {'type': 'report', **{k: v for k, v in outcome.items() if k != 'ok'}}
                            result['text'] = (f"Saved {outcome['file']}; it checked and ran without errors. The user "
                                              'can open it from the link in the chat.')
                        else:
                            result.update(text=f"The notebook was not saved.\n{outcome['problems']}", is_error=True)
                    results.append(result)
                    continue
                if call['name'] == 'request_download' and valid_input(call['name'], call['input']):
                    async for event in request_download(db, jobs, conversation, call):
                        if event['type'] == 'result':
                            results.append({'id': call['id'], 'name': call['name'], 'text': event['text'],
                                            'image': None, 'is_error': event['is_error']})
                        else:
                            yield event
                    continue
                if call['name'] == 'create_document' and valid_input(call['name'], call['input']):
                    result = {'id': call['id'], 'name': call['name'], 'image': None, 'is_error': False}
                    try:
                        if documents is None:
                            raise RuntimeError('Documents are not available in this app.')
                        doc = await documents(call['input']['title'], call['input']['content'], conversation.results)
                        notes = ''.join([f" Unknown result ids: {', '.join(doc['missing'])}." if doc['missing'] else '',
                                         f" Results that failed: {doc['errors']}." if doc['errors'] else ''])
                        result['text'] = (f"Created document {doc['id']} \"{doc['title']}\". It is open beside the chat; "
                                          f"the user can edit it and highlight passages to ask you about them.{notes}")
                        yield {'type': 'document', **doc}
                    except Exception as exc:
                        result.update(text=f'Creating the document failed: {exc}', is_error=True)
                    results.append(result)
                    continue
                if call['name'] == 'create_report' and valid_input(call['name'], call['input']):
                    result = {'id': call['id'], 'name': call['name'], 'image': None, 'is_error': False}
                    try:
                        if exporter is None:
                            raise RuntimeError('Reports are not available in this app.')
                        report = await exporter(call['input']['format'], call['input']['title'],
                                                call['input']['content'], conversation.results)
                        notes = ''.join([f" Unknown result ids: {', '.join(report['missing'])}." if report['missing'] else '',
                                         f" Results that failed: {report['errors']}." if report['errors'] else ''])
                        result['text'] = f"Created {report['file']}. The user can open it from the link in the chat.{notes}"
                        yield {'type': 'report', **report}
                    except Exception as exc:
                        result.update(text=f'Report failed: {exc}', is_error=True)
                    results.append(result)
                    continue
                result, events = await execute_tool(db, call)
                if not result['is_error'] and any(e['type'] in ('table', 'plot') for e in events):
                    # Number the result so the model can place it in a report.
                    rid = f'R{len(conversation.results) + 1}'
                    plot = next((e for e in events if e['type'] == 'plot'), None)
                    conversation.results[rid] = {'sql': call['input']['sql'], 'plot_spec': plot and plot['plot_spec']}
                    result['text'] = f'Result id: {rid}\n{result["text"]}'
                    for event in events:
                        event['result_id'] = rid
                for event in events:
                    if event['type'] == 'table':
                        latest_table = event
                    else:
                        yield event
                results.append(result)
            history.append({'role': 'tool', 'results': results})
        else:
            yield {'type': 'error', 'message': f'Stopped after {MAX_ROUNDS} tool rounds.'}
    except asyncio.CancelledError:
        if conversation.stopping:  # the user pressed Stop: keep what was said, then a note
            _keep_stopped(history, start, ''.join(partial), model)
        else:  # the browser went away: forget the turn
            del history[start:]
        raise
    except BaseException:
        # Leave history ending on a complete turn (no tool call without its result).
        del history[start:]
        raise
    finally:
        # A provider that keeps a session open across tool rounds (subscription.py) ends it here.
        if close_turn := getattr(provider, 'close_turn', None):
            await close_turn(history)
    if latest_table is not None:
        yield latest_table


MAX_COMPARE = 4
STOPPED = '[Stopped by the user]'


def _keep_stopped(history: list, start: int, partial: str, model: ModelInfo) -> None:
    """End a stopped turn's history validly: drop a tool call whose results never came, then
    record what the model had written so far and that the user stopped it."""
    if len(history) <= start:
        return  # stopped before the user message was even recorded
    if history[-1]['role'] == 'assistant' and history[-1]['tool_calls']:
        partial = partial or history[-1]['text']
        history.pop()
    text = f'{partial.rstrip()}\n\n{STOPPED}' if partial.strip() else f'{STOPPED} (before it replied)'
    history.append({'role': 'assistant', 'model': model.ref, 'model_name': model.name, 'text': text,
                    'tool_calls': [], 'raw': {}, 'stop': 'end'})


def stop_turn(conversation: Conversation, branch: int | None = None) -> bool:
    """Stop the running reply: every model's, or (``branch``) one compared model's."""
    targets = list(conversation.running.items()) if branch is None else \
        [(branch, conversation.running[branch])] if branch in conversation.running else []
    for _, (task, runs_on) in targets:
        runs_on.stopping = True
        task.cancel()
    return bool(targets)


async def run_single(providers: dict, db, conversation: Conversation, text: str, model: ModelInfo,
                     describe_error=str, **options) -> AsyncIterator[dict]:
    """run_turn in a task that stop_turn can cancel; ends with a 'stopped' event if it was stopped."""
    queue = asyncio.Queue()
    conversation.stopping = False

    async def drive():
        try:
            async for event in run_turn(providers, db, conversation, text, model, **options):
                await queue.put(event)
        except asyncio.CancelledError:
            if conversation.stopping:
                await queue.put({'type': 'stopped'})
        except Exception as exc:
            await queue.put({'type': 'error', 'message': describe_error(exc)})
        finally:
            await queue.put(None)

    task = asyncio.create_task(drive())
    conversation.running[None] = (task, conversation)
    try:
        while (event := await queue.get()) is not None:
            yield event
    finally:  # also when the browser goes away mid-reply (that's not a Stop: the turn is forgotten)
        conversation.running.pop(None, None)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def run_compare(providers: dict, db, conversation: Conversation, text: str, models: list[ModelInfo],
                      describe_error=str, **options) -> AsyncIterator[dict]:
    """One user turn answered by several models at once, each on its own copy of the conversation.

    Events carry the index of their model as ``branch``; ``branch_done`` marks a model finishing,
    and stop_turn can stop one model (``stopped``) or all. The conversation then continues from
    the first model that replied without being stopped (a ``kept`` event says which);
    ``keep_branch`` switches to another until the next turn starts. Branches share ``pending``,
    so approval cards work.
    """
    branches = [Conversation(history=list(conversation.history), results=dict(conversation.results),
                             pending=conversation.pending, mode=conversation.mode) for _ in models]
    queue = asyncio.Queue()

    async def drive(index):
        try:
            async for event in run_turn(providers, db, branches[index], text, models[index], **options):
                await queue.put({**event, 'branch': index})
        except asyncio.CancelledError:
            if branches[index].stopping:
                await queue.put({'type': 'stopped', 'branch': index})
        except Exception as exc:
            await queue.put({'type': 'error', 'message': describe_error(exc), 'branch': index})
        finally:
            conversation.running.pop(index, None)
            await queue.put({'type': 'branch_done', 'branch': index})
            await queue.put(None)

    yield {'type': 'compare', 'models': [model_event(m, options.get('efforts')) for m in models]}
    tasks = [asyncio.create_task(drive(i)) for i in range(len(models))]
    conversation.running.update({i: (task, branches[i]) for i, task in enumerate(tasks)})
    try:
        for _ in tasks:
            while (event := await queue.get()) is not None:
                yield event
    finally:  # also when the browser goes away mid-reply
        conversation.running.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    base = len(conversation.history)
    conversation.branches = [{'model': m.ref, 'history': b.history, 'results': b.results, 'base': base}
                             for m, b in zip(models, branches)]
    replied = [i for i, b in enumerate(branches) if len(b.history) > base]
    kept = next((i for i in replied if not branches[i].stopping), replied[0] if replied else None)
    if kept is not None:
        keep_branch(conversation, kept)
    yield {'type': 'kept', 'branch': kept}


def keep_branch(conversation: Conversation, index: int) -> bool:
    """Continue the conversation from compare branch ``index``; False if that isn't possible."""
    branches = conversation.branches or []
    if not 0 <= index < len(branches):
        return False
    branch = branches[index]
    if not any(h['role'] == 'assistant' for h in branch['history'][branch['base']:]):
        return False  # that model failed before replying
    conversation.history = list(branch['history'])
    conversation.results = dict(branch['results'])
    return True

