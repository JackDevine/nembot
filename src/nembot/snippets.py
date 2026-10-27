"""Saved query/plot snippets in the `snippets` schema, re-run on every load.

TODO: snippets from multiple users (ownership, per-user schemas or a user
column, sharing) — see plan.md.
"""
from datetime import datetime
import json
import re
import time

import duckdb

from .chat import chart_spec, run_query, table_event, _records

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS snippets;
CREATE SEQUENCE IF NOT EXISTS snippets.snippet_id;
CREATE TABLE IF NOT EXISTS snippets.snippets (
    id INTEGER PRIMARY KEY DEFAULT nextval('snippets.snippet_id'),
    name VARCHAR NOT NULL,
    sql VARCHAR NOT NULL,
    plot_spec VARCHAR,          -- Vega-Lite JSON without data, or NULL for a table snippet
    question VARCHAR,           -- the chat question it came from
    model VARCHAR,
    conversation VARCHAR,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);
"""
COLUMNS = ('id', 'name', 'sql', 'plot_spec', 'question', 'model', 'conversation', 'created_at', 'updated_at')
MAX_NAME = 80


def ensure(db: duckdb.DuckDBPyConnection) -> None:
    cursor = db.cursor()
    try:
        cursor.execute(SCHEMA)
    finally:
        cursor.close()


def _public(row) -> dict:
    snippet = dict(zip(COLUMNS, row))
    snippet['plot_spec'] = json.loads(snippet['plot_spec']) if snippet['plot_spec'] else None
    for key in ('created_at', 'updated_at'):
        snippet[key] = snippet[key].isoformat(sep=' ', timespec='seconds')
    return snippet


def clean_name(name: str) -> str:
    name = re.sub(r'\s+', ' ', str(name or '')).strip().strip('"\'`*#').strip()
    return name[:MAX_NAME] or 'Untitled snippet'


def fallback_name(sql: str, plot_spec: dict | None) -> str:
    tables = re.findall(r'\bFROM\s+"?([A-Za-z_][A-Za-z0-9_]*)', sql, re.IGNORECASE)
    kind = 'plot' if plot_spec else 'query'
    return f"{tables[0].upper()} {kind}" if tables else f'Saved {kind}'


def create(db, *, name: str, sql: str, plot_spec: dict | None, question: str | None, model: str | None,
           conversation: str | None) -> dict:
    now = datetime.now()
    cursor = db.cursor()
    try:
        row = cursor.execute(
            f"INSERT INTO snippets.snippets (name, sql, plot_spec, question, model, conversation, created_at, updated_at) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING {', '.join(COLUMNS)}",
            [clean_name(name), sql, json.dumps(plot_spec) if plot_spec else None, question, model, conversation, now, now],
        ).fetchone()
    finally:
        cursor.close()
    return _public(row)


def list_all(db) -> list[dict]:
    cursor = db.cursor()
    try:
        rows = cursor.execute(f"SELECT {', '.join(COLUMNS)} FROM snippets.snippets ORDER BY created_at DESC").fetchall()
    finally:
        cursor.close()
    return [_public(r) for r in rows]


def get(db, snippet_id: int) -> dict | None:
    cursor = db.cursor()
    try:
        row = cursor.execute(f"SELECT {', '.join(COLUMNS)} FROM snippets.snippets WHERE id = ?", [snippet_id]).fetchone()
    finally:
        cursor.close()
    return _public(row) if row else None


def rename(db, snippet_id: int, name: str) -> dict | None:
    cursor = db.cursor()
    try:
        cursor.execute('UPDATE snippets.snippets SET name = ?, updated_at = ? WHERE id = ?',
                       [clean_name(name), datetime.now(), snippet_id])
    finally:
        cursor.close()
    return get(db, snippet_id)


def delete(db, snippet_id: int) -> None:
    cursor = db.cursor()
    try:
        cursor.execute('DELETE FROM snippets.snippets WHERE id = ?', [snippet_id])
    finally:
        cursor.close()


def run(db, snippet: dict) -> dict:
    """Re-run the snippet's SQL now, returning UI events (table and, for plots, plot) or an error."""
    result = run_query(db, snippet['sql'])
    if isinstance(result, str):
        return {'error': result, 'events': []}
    events = [table_event(result)]
    if snippet['plot_spec']:
        try:
            started = time.perf_counter()
            full = chart_spec(snippet['plot_spec'], _records(result))
            events.insert(0, {'type': 'plot', 'sql': snippet['sql'], 'spec': full, 'plot_spec': snippet['plot_spec'],
                              'query_seconds': round(result.seconds, 3),
                              'render_seconds': round(time.perf_counter() - started, 3)})
        except Exception as exc:
            return {'error': f'Plot failed: {exc}', 'events': events}
    return {'error': None, 'events': events, 'ran_at': datetime.now().isoformat(sep=' ', timespec='seconds')}


def name_prompt(sql: str, plot_spec: dict | None, question: str | None) -> str:
    mark = plot_spec.get('mark') if plot_spec else None
    mark = mark.get('type') if isinstance(mark, dict) else mark
    return ('Suggest a short, descriptive name (at most 8 words, sentence case, no quotes or trailing '
            'punctuation) for this saved NEM data ' + ('chart' if plot_spec else 'query') + '. '
            'Reply with the name only.\n\n'
            + (f'User question: {question}\n' if question else '')
            + (f'Chart mark: {mark}\n' if mark else '')
            + f'SQL:\n{sql}')
