"""Shared documents: rich text with tables, charts and images, edited together with nembot.

A document lives in the `docs` schema as Tiptap JSON (``content``) plus its comment
threads (``threads``, JSON). The browser is the source of truth for both and saves them
whole. Documents made by the chat's create_document tool start with a ``seed``: JSON
parts, markdown or a table/chart snapshot, which the editor converts once (markdown with
the same renderer as the chat) and then replaces with JSON. nembot's suggestions also
carry markdown, so everything it writes is formatted the way the chat shows it.

Tables and charts are snapshots: a ``dataBlock`` node holds its SQL, an optional
Vega-Lite spec and the rows the SQL returned when it was added or last refreshed.

Tagging nembot on a highlighted passage runs a one-off turn in 'doc' mode with a
``DocSession``: nembot replies in the thread and proposes changes with the suggest_*
tools, which the browser shows as tracked suggestions the user accepts or rejects.
"""
import asyncio
from datetime import datetime
import base64
import html
import json
import uuid

import vl_convert

from . import reports
from .chat import _SCHEMA_GUIDE, _WEB_SEARCH, _jsonable, chart_spec, prepare_spec, run_query

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS docs;
CREATE SEQUENCE IF NOT EXISTS docs.doc_id;
CREATE TABLE IF NOT EXISTS docs.docs (
    id INTEGER PRIMARY KEY DEFAULT nextval('docs.doc_id'),
    title VARCHAR NOT NULL,
    content VARCHAR,                      -- Tiptap document JSON; NULL until the editor first saves
    seed VARCHAR,                         -- create_document's JSON parts, converted by the editor once
    threads VARCHAR NOT NULL DEFAULT '[]',
    conversation VARCHAR,                 -- the chat that created it, if any
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS docs.images (
    id VARCHAR PRIMARY KEY,
    doc_id INTEGER NOT NULL,
    mime VARCHAR NOT NULL,
    data BLOB NOT NULL,
    created_at TIMESTAMP NOT NULL
);
"""
MAX_TITLE = 120
TABLE_ROWS = 1000     # rows kept in a table snapshot (the total is recorded too)
CHART_ROWS = 20000    # charts plot every row, so larger results must be aggregated first
MAX_IMAGE = 10 * 1024 * 1024
IMAGE_TYPES = ('image/png', 'image/jpeg', 'image/gif', 'image/webp')  # no SVG: it can carry scripts
MAX_DOCUMENT_TEXT = 60000  # characters of document sent to the model


def _run(db, sql: str, params=()):
    cursor = db.cursor()
    try:
        return cursor.execute(sql, params).fetchall()
    finally:
        cursor.close()


def ensure(db) -> None:
    cursor = db.cursor()
    try:
        cursor.execute(SCHEMA)
    finally:
        cursor.close()


def clean_title(title) -> str:
    return ' '.join(str(title or '').split())[:MAX_TITLE] or 'Untitled document'


def _stamp(value: datetime) -> str:
    return value.isoformat(sep=' ', timespec='seconds')


def list_all(db) -> list[dict]:
    rows = _run(db, 'SELECT id, title, created_at, updated_at FROM docs.docs ORDER BY updated_at DESC')
    return [{'id': i, 'title': t, 'created_at': _stamp(c), 'updated_at': _stamp(u)} for i, t, c, u in rows]


def get(db, doc_id: int) -> dict | None:
    rows = _run(db, 'SELECT id, title, content, seed, threads, conversation, created_at, updated_at '
                    'FROM docs.docs WHERE id = ?', [doc_id])
    if not rows:
        return None
    i, title, content, seed, threads, conversation, created, updated = rows[0]
    return {'id': i, 'title': title, 'content': json.loads(content) if content else None,
            'seed': json.loads(seed) if seed else None,
            'threads': json.loads(threads), 'conversation': conversation,
            'created_at': _stamp(created), 'updated_at': _stamp(updated)}


def create(db, title: str, *, seed: list | None = None, conversation: str | None = None) -> dict:
    now = datetime.now()
    (doc_id,), = _run(db, 'INSERT INTO docs.docs (title, seed, conversation, created_at, updated_at) '
                          'VALUES (?, ?, ?, ?, ?) RETURNING id',
                      [clean_title(title), json.dumps(seed) if seed else None, conversation, now, now])
    return get(db, doc_id)


def save(db, doc_id: int, *, title, content, threads) -> dict | None:
    """Store the editor's whole state. Returns the new timestamps, or None for an unknown document."""
    if not isinstance(content, dict) or content.get('type') != 'doc':
        raise ValueError('content must be a Tiptap document')
    if not isinstance(threads, list):
        raise ValueError('threads must be a list')
    now = datetime.now()
    rows = _run(db, 'UPDATE docs.docs SET title = ?, content = ?, seed = NULL, threads = ?, updated_at = ? '
                    'WHERE id = ? RETURNING updated_at',
                [clean_title(title), json.dumps(content), json.dumps(threads), now, doc_id])
    return {'id': doc_id, 'title': clean_title(title), 'updated_at': _stamp(rows[0][0])} if rows else None


def delete(db, doc_id: int) -> None:
    _run(db, 'DELETE FROM docs.images WHERE doc_id = ?', [doc_id])
    _run(db, 'DELETE FROM docs.docs WHERE id = ?', [doc_id])


def add_image(db, doc_id: int, mime: str, data: bytes) -> str:
    if mime not in IMAGE_TYPES:
        raise ValueError(f'Unsupported image type {mime or "(none)"}; use PNG, JPEG, GIF or WebP.')
    if len(data) > MAX_IMAGE:
        raise ValueError(f'Images are limited to {MAX_IMAGE // (1024 * 1024)} MB.')
    if not _run(db, 'SELECT 1 FROM docs.docs WHERE id = ?', [doc_id]):
        raise LookupError('unknown document')
    key = uuid.uuid4().hex
    _run(db, 'INSERT INTO docs.images VALUES (?, ?, ?, ?, ?)', [key, doc_id, mime, data, datetime.now()])
    return key


def get_image(db, key: str) -> tuple[str, bytes] | None:
    rows = _run(db, 'SELECT mime, data FROM docs.images WHERE id = ?', [key])
    return (rows[0][0], bytes(rows[0][1])) if rows else None


# ---- Table and chart snapshots ----

def snapshot(db, sql: str, plot_spec=None, title: str = '') -> dict | str:
    """Run ``sql`` now and return dataBlock attributes, or an error message."""
    spec = None
    if plot_spec:
        try:
            spec = prepare_spec(plot_spec if isinstance(plot_spec, str) else json.dumps(plot_spec))
        except Exception as err:
            return f"Invalid plot specification: {getattr(err, 'message', str(err))}"
    result = run_query(db, sql)
    if isinstance(result, str):
        return result
    if spec and len(result.rows) > CHART_ROWS:
        return f'The chart query returned {len(result.rows)} rows; aggregate it to at most {CHART_ROWS}.'
    if spec and not result.rows:
        return 'The query returned no rows. Adjust the SQL before charting.'
    limit = CHART_ROWS if spec else TABLE_ROWS
    return {'kind': 'chart' if spec else 'table', 'title': ' '.join(str(title or '').split())[:MAX_TITLE],
            'sql': sql, 'plot_spec': spec, 'columns': list(result.columns),
            'rows': [[_jsonable(v) for v in row] for row in result.rows[:limit]],
            'total': len(result.rows), 'taken_at': _stamp(datetime.now()), 'suggestion': None}


def chart_png(attrs: dict) -> str | None:
    """A PNG of a chart snapshot (base64), for models that can read images."""
    records = [dict(zip(attrs['columns'], row)) for row in attrs['rows']]
    try:
        full = chart_spec(attrs['plot_spec'], records)
        return base64.standard_b64encode(vl_convert.vegalite_to_png(full, scale=1)).decode()
    except Exception:
        return None


def seed_from_markdown(db, content: str, results: dict) -> tuple[list[dict], list[str], dict]:
    """Parts for a new document: markdown, with each [[R3]] line turned into a table/chart snapshot."""
    parts, missing, errors = [], [], {}
    for kind, text in reports.blocks(content):
        if kind == 'text':
            parts.append({'markdown': text})
        elif text not in results:
            missing.append(text)
        else:
            stored = results[text]
            attrs = snapshot(db, stored['sql'], stored.get('plot_spec'))
            if isinstance(attrs, str):
                errors[text] = attrs
            else:
                parts.append({'block': attrs})
    return parts, missing, errors


# ---- nembot in a document ('doc' mode) ----

PROMPT = """You are nembot, a data analyst for Australia's National Electricity Market (NEM), working
with the user on a shared document. Your tools work on a local DuckDB copy of AEMO's public
MMS data.

The user highlights a passage and asks you about it. Each request contains the whole
document as numbered blocks ([B1], [B2], ...), the highlighted passage, the comment
thread so far and the request. A block's type, when it isn't a plain paragraph, is shown
in parentheses after its id, e.g. "[B1] (heading 2) Summary"; it is not part of the text.

How to respond:
- Your text reply appears in the comment thread beside the highlight. Keep it short
  (a few sentences); markdown is fine.
- Never change the document by writing new text in your reply. Use the suggest_* tools.
  Each suggestion appears in the document as a tracked change that the user accepts or
  rejects:
  - `suggest_edit(block, find, replace)` rewrites text inside one block. Copy `find`
    exactly from that block (plain text as shown), with enough words to be unique there.
    `replace` may use inline markdown: **bold**, *italic*, `code` and [links](https://...).
    Keep edits as small as the request allows; an empty `replace` deletes the text.
  - `suggest_insert(after, content)` adds markdown (paragraphs, headings, lists, links
    and GitHub-style pipe tables for short text tables, but no images) after a block, or at
    the top with after="START". Use suggest_table for tables of query results.
  - `suggest_table(after, sql, title)` and `suggest_chart(after, sql, plot_spec, title)`
    add a table or a Vega-Lite chart built from one SELECT. They are snapshots of the
    data with their SQL; the user can refresh them later. For charts, plot_spec is a
    JSON-encoded Vega-Lite spec without data (fields must match SQL aliases; use explicit
    encoding types and label axes with units).
- If the request is only a question, answer in the thread without suggestions.
- Check facts with `query_mms` (it returns a 10-row CSV preview) and `describe_table`
  before writing numbers into the document. Tables and charts in the document show
  their SQL, which you can reuse.
- Only downloaded data is available (see the <data_status> note). If the request needs
  data that isn't downloaded, say which table and months are needed; the user can
  download them from the chat.
- After suggesting changes, say in one sentence what you suggested.

"""

_AFTER = {'type': 'string', 'description': 'Block id to insert after, e.g. "B3", or "START" for the top.'}
TOOLS = [
    {
        'name': 'suggest_edit',
        'description': 'Suggest replacing text inside one block of the document (shown as a tracked change).',
        'input_schema': {'type': 'object', 'properties': {
            'block': {'type': 'string', 'description': 'Block id, e.g. "B4".'},
            'find': {'type': 'string', 'description': 'Exact text from that block, unique within it.'},
            'replace': {'type': 'string', 'description': 'Replacement text (inline markdown allowed); empty to delete.'},
        }, 'required': ['block', 'find', 'replace']},
    },
    {
        'name': 'suggest_insert',
        'description': 'Suggest new markdown (paragraphs, headings, lists, pipe tables) after a block.',
        'input_schema': {'type': 'object', 'properties': {
            'after': _AFTER, 'content': {'type': 'string', 'description': 'GitHub-flavoured markdown, without images.'},
        }, 'required': ['after', 'content']},
    },
    {
        'name': 'suggest_table',
        'description': 'Suggest a table of a SQL query result (a snapshot) after a block.',
        'input_schema': {'type': 'object', 'properties': {
            'after': _AFTER, 'sql': {'type': 'string', 'description': 'One read-only DuckDB SELECT.'},
            'title': {'type': 'string', 'description': 'Short caption shown above the table.'},
        }, 'required': ['after', 'sql', 'title']},
    },
    {
        'name': 'suggest_chart',
        'description': 'Suggest a Vega-Lite chart of a SQL query result (a snapshot) after a block.',
        'input_schema': {'type': 'object', 'properties': {
            'after': _AFTER, 'sql': {'type': 'string', 'description': 'One read-only DuckDB SELECT.'},
            'plot_spec': {'type': 'string', 'description': 'JSON-encoded Vega-Lite spec without data.'},
            'title': {'type': 'string', 'description': 'Short caption shown above the chart.'},
        }, 'required': ['after', 'sql', 'plot_spec', 'title']},
    },
]
TOOL_NAMES = {t['name'] for t in TOOLS}
_REQUIRED = {t['name']: t['input_schema']['required'] for t in TOOLS}


def system_prompt(web_search: bool) -> str:
    return PROMPT + _SCHEMA_GUIDE + (_WEB_SEARCH if web_search else '')


def ask_prompt(title: str, blocks: list[dict], quote: str, quote_block: str | None,
               messages: list[dict], request: str) -> str:
    """The user message for one tag: the document, the highlight, the thread and the request."""
    lines, used = [], 0
    for block in blocks:
        label = block.get('label')
        line = f"[{block['id']}] " + (f'({label}) ' if isinstance(label, str) and label else '') + block['text']
        used += len(line)
        if used > MAX_DOCUMENT_TEXT:
            lines.append('[… the rest of the document is omitted]')
            break
        lines.append(line)
    thread = '\n'.join(f"{'nembot' if m.get('author') == 'nembot' else 'User'}: {m.get('text', '')}"
                       for m in messages)
    where = f' in [{quote_block}]' if quote_block else ''
    return (f'<document title="{title}">\n' + '\n'.join(lines) + '\n</document>\n\n'
            + (f'<thread>\n{thread}\n</thread>\n\n' if thread else '')
            + f'Highlighted passage{where}: «{quote}»\n\nRequest: {request}')


class DocSession:
    """Runs the suggest_* tools for one tag, checking them against the document the browser sent."""

    tool_names = TOOL_NAMES

    def __init__(self, db, blocks: list[dict]):
        self.db = db
        self.text = {b['id']: b['text'] for b in blocks if b.get('kind') == 'text'}
        self.ids = {b['id'] for b in blocks}
        self.claimed: dict[str, list[tuple[int, int]]] = {}  # block -> character ranges already suggested

    def _after(self, after: str) -> str | None:
        return None if after == 'START' or after in self.ids else f'Unknown block {after!r}; use a block id such as B3, or START.'

    async def run(self, call: dict):
        name, args = call['name'], call['input']
        if not isinstance(args, dict) or not all(isinstance(args.get(k), str) for k in _REQUIRED[name]):
            yield {'type': 'result', 'text': f'Invalid input for {name}.', 'is_error': True}
            return
        sid = uuid.uuid4().hex[:12]
        if name == 'suggest_edit':
            block, find = args['block'], args['find']
            text = self.text.get(block)
            error = None
            if text is None:
                error = f'{block!r} is not a text block of the document.'
            elif not find:
                error = 'find must not be empty.'
            elif (count := text.count(find)) != 1:
                error = (f'find does not appear in {block}; copy it exactly.' if count == 0
                         else f'find appears {count} times in {block}; include more words so it is unique.')
            else:
                start = text.index(find)
                span = (start, start + len(find))
                if any(span[0] < b and a < span[1] for a, b in self.claimed.get(block, [])):
                    error = 'That text overlaps an earlier suggestion; combine them into one edit.'
                else:
                    self.claimed.setdefault(block, []).append(span)
            if error:
                yield {'type': 'result', 'text': error, 'is_error': True}
                return
            yield {'type': 'doc_suggestion', 'id': sid, 'kind': 'replace', 'block': block, 'find': find,
                   'replace': args['replace']}
            yield {'type': 'result', 'text': 'Suggested; the user will accept or reject it.', 'is_error': False}
            return
        if error := self._after(args['after']):
            yield {'type': 'result', 'text': error, 'is_error': True}
            return
        if name == 'suggest_insert':
            if not args['content'].strip():
                yield {'type': 'result', 'text': 'content must not be empty.', 'is_error': True}
                return
            yield {'type': 'doc_suggestion', 'id': sid, 'kind': 'insert', 'after': args['after'],
                   'markdown': args['content']}
            yield {'type': 'result', 'text': 'Suggested; the user will accept or reject it.', 'is_error': False}
            return
        plot_spec = args.get('plot_spec') if name == 'suggest_chart' else None
        attrs = await asyncio.to_thread(snapshot, self.db, args['sql'], plot_spec, args.get('title', ''))
        if isinstance(attrs, str):
            yield {'type': 'result', 'text': attrs, 'is_error': True}
            return
        attrs['suggestion'] = sid
        yield {'type': 'doc_suggestion', 'id': sid, 'kind': 'block', 'after': args['after'], 'attrs': attrs}
        preview = '\n'.join(','.join(map(str, row)) for row in [attrs['columns'], *attrs['rows'][:10]])
        text = f"Suggested a {attrs['kind']} of {attrs['total']} rows. First rows:\n{preview}"
        image = await asyncio.to_thread(chart_png, attrs) if attrs['kind'] == 'chart' else None
        yield {'type': 'result', 'text': text, 'is_error': False, 'image': image}
