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
from typing import AsyncIterator

import altair
import duckdb
import vl_convert

from . import catalog, downloads, explorer, live
from .ingest import SUPPORTED, _month_shift
from .models import ModelInfo

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
  runnable notebook (jupyter, marimo) built from this conversation's results. Only use it
  when the user asks for a report, document, deck, workbook or notebook.
  Every query/plot result has an id (shown as "Result id: R3" in tool output). Write the
  content in markdown and put a result on its own line as [[R3]] to include it. Use ##
  headings for sections (each section becomes a slide in pptx). Include only results that
  support the narrative; the data is re-queried when the file is built.
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


def _archive_tables() -> str:
    """Static, cache-friendly list of every table the monthly archive publishes."""
    lines = ['', '', 'Tables published in the MMS monthly archive (downloadable on request), by package:']
    for package in catalog.load()['packages']:
        names = [n for n in package['tables'] if catalog.tables()[n]['in_archive']]
        if names:
            lines.append(f"- {package['name']}: {', '.join(names)}")
    return '\n'.join(lines)


_PROMPT_TAIL += _archive_tables()


_WEB_SEARCH = """

Web search:
- You can also search the web. Use it for context the database can't give: market
  events, rule changes, outages, weather, AEMO reports and news that explain the data.
- Answer data questions from the database tools, not the web. Mention which facts
  came from the web; citations are shown to the user automatically."""


def system_prompt(vision: bool, web_search: bool = False) -> str:
    prompt = _PROMPT_HEAD + (_VISION_TOOLS if vision else _NO_VISION_TOOLS) + _PROMPT_TAIL
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


DECISION_TIMEOUT = 1800  # seconds to wait for the user to approve or decline a download


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


def mode_setup(mode: str, model: ModelInfo, web_search: bool, plugin_manager=None) -> tuple[str, list[dict]]:
    """System prompt and tools for a conversation mode.

    The analyst only sees plugin tools (and their one-line notes); how to build
    plugins is documented solely in Creator mode's prompt.
    """
    if mode == 'creator':
        from . import creator
        system = creator.PROMPT + (_WEB_SEARCH if web_search else '')
        return system, creator.TOOLS + [t for t in TOOLS if t['name'] in ('query_mms', 'describe_table')]
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
                   creator=None) -> AsyncIterator[dict]:
    """Stream UI events for one user turn, running tools until the model finishes.

    Plots are emitted as soon as their tool finishes; only the latest query
    table is shown, at the end of the turn, as in the marimo notebook.
    """
    provider = providers[model.provider]
    history = conversation.history
    start = len(history)
    history.append({'role': 'user', 'text': text, 'context': await asyncio.to_thread(data_status, db)})
    latest_table = None
    system, tools = mode_setup(conversation.mode, model, web_search, plugin_manager)
    try:
        for _ in range(MAX_ROUNDS):
            turn = None
            async for item in provider.respond(model, history, web_search=web_search, system=system, tools=tools):
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
                special = None
                if conversation.mode == 'creator' and creator is not None:
                    from . import creator as creator_mode
                    if call['name'] in creator_mode.TOOL_NAMES:
                        special = creator.run(call, conversation, [t['name'] for t in TOOLS])
                elif plugin_manager is not None and (tool := plugin_manager.find_tool(call['name'])):
                    special = _plugin_tool(plugin_manager, tool, call)
                if special is not None:
                    async for event in special:
                        if event['type'] == 'result':
                            results.append({'id': call['id'], 'name': call['name'], 'text': event['text'],
                                            'image': None, 'is_error': event['is_error']})
                        elif event['type'] == 'table':
                            latest_table = event
                        else:
                            yield event
                    continue
                if call['name'] == 'request_download' and valid_input(call['name'], call['input']):
                    async for event in request_download(db, jobs, conversation, call):
                        if event['type'] == 'result':
                            results.append({'id': call['id'], 'name': call['name'], 'text': event['text'],
                                            'image': None, 'is_error': event['is_error']})
                        else:
                            yield event
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
                if not result['is_error'] and any(e['type'] == 'table' for e in events):
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
    except BaseException:
        # Leave history ending on a complete turn (no tool call without its result).
        del history[start:]
        raise
    if latest_table is not None:
        yield latest_table
