"""Saved chats in the `chats` schema, so conversations survive restarts and can be reopened.

A chat is saved after every turn. ``history`` is chat.py's neutral history, replayed to
the model when the chat continues; ``results`` maps result ids (R1, ...) to their SQL for
reports and documents; ``transcript`` is what the browser showed, as a list of turns
(``{'text': user message, 'events': [UI events]}``) that the browser replays to redraw
the chat. Charts are saved as their SQL and spec without data (``strip_chart_data``): the
browser re-runs them when the chat is reopened. ``web_search`` is restored on reopening because Claude's preserved thinking
only replays against the same system prompt and tools.

Titles are suggested by the chat's model from the first message; a title the user
typed (``title_by_user``) is never replaced by a suggestion.
"""
from datetime import datetime
import json
import re

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS chats;
CREATE TABLE IF NOT EXISTS chats.chats (
    id VARCHAR PRIMARY KEY,                     -- the browser-generated conversation id
    title VARCHAR,                              -- NULL until named
    title_by_user BOOLEAN NOT NULL DEFAULT false,
    mode VARCHAR NOT NULL,                      -- 'analyst' or 'creator'
    web_search BOOLEAN NOT NULL DEFAULT true,
    model VARCHAR,                              -- the model of the latest turn
    history VARCHAR NOT NULL,                   -- neutral history (JSON)
    results VARCHAR NOT NULL,                   -- result id -> {'sql', 'plot_spec'} (JSON)
    transcript VARCHAR NOT NULL,                -- turns as displayed (JSON)
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);
"""
MAX_TITLE = 80
# Progress indicators that only matter while a reply streams in.
TRANSIENT = {'tool', 'tool_running', 'notice', 'done', 'download_progress', 'branch_done'}


def _run(db, sql: str, params=()):
    cursor = db.cursor()
    try:
        return cursor.execute(sql, params).fetchall()
    finally:
        cursor.close()


def ensure(db) -> None:
    _run(db, SCHEMA)


def _stamp(value: datetime) -> str:
    return value.isoformat(sep=' ', timespec='seconds')


def clean_title(title) -> str:
    title = re.sub(r'\s+', ' ', str(title or '')).strip().strip('"\'`*#').strip().rstrip('.')
    return title[:MAX_TITLE]


def fallback_title(text: str) -> str:
    words = text.split()
    return clean_title(' '.join(words[:8]) + ('…' if len(words) > 8 else '')) or 'New chat'


def title_prompt(text: str) -> str:
    return ('Suggest a short title (at most 6 words, sentence case, no quotes or trailing punctuation) for a '
            'chat about NEM electricity market data that starts with this message. Reply with the title only.'
            f'\n\nMessage:\n{text[:2000]}')


def compact(events: list[dict]) -> list[dict]:
    """The UI events worth replaying: streamed text joined up, progress-only events dropped."""
    out = []
    for event in events:
        kind = event.get('type')
        if kind in TRANSIENT:
            continue
        # Compare turns interleave several models' events; only join text from the same one.
        if kind in ('text', 'progress') and out and out[-1].get('type') == kind \
                and out[-1].get('branch') == event.get('branch'):
            out[-1] = {**out[-1], 'delta': out[-1]['delta'] + event.get('delta', '')}
        else:
            out.append(event)
    return out


# A chart's full data (``spec`` holds every row) and the preview rows under its SQL. A
# 100k-point chart is several MB, so saved chats keep only the SQL, the plot spec and the row
# count; the browser re-runs the chart on reopening (POST /api/chart). HTML exports inline data.
CHART_DATA = ('spec', 'rows')


def strip_chart_data(transcript: list) -> list:
    return [{**turn, 'events': [{k: v for k, v in e.items() if k not in CHART_DATA} if e.get('type') == 'plot' else e
                                for e in turn.get('events', [])]} for turn in transcript]


def list_all(db) -> list[dict]:
    rows = _run(db, 'SELECT id, title, mode, updated_at FROM chats.chats ORDER BY updated_at DESC')
    return [{'id': i, 'title': t, 'mode': m, 'updated_at': _stamp(u)} for i, t, m, u in rows]


def get(db, chat_id: str) -> dict | None:
    rows = _run(db, 'SELECT id, title, title_by_user, mode, web_search, model, history, results, transcript, '
                    'created_at, updated_at FROM chats.chats WHERE id = ?', [chat_id])
    if not rows:
        return None
    i, title, by_user, mode, web_search, model, history, results, transcript, created, updated = rows[0]
    return {'id': i, 'title': title, 'title_by_user': by_user, 'mode': mode, 'web_search': web_search, 'model': model,
            'history': json.loads(history), 'results': json.loads(results), 'transcript': json.loads(transcript),
            'created_at': _stamp(created), 'updated_at': _stamp(updated)}


def save(db, chat_id: str, *, mode: str, web_search: bool, model: str | None, history: list, results: dict,
         transcript: list) -> None:
    now = datetime.now()
    dump = lambda value: json.dumps(value, default=str)
    _run(db, 'INSERT INTO chats.chats (id, mode, web_search, model, history, results, transcript, created_at, updated_at) '
             'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET web_search = excluded.web_search, '
             'model = excluded.model, history = excluded.history, results = excluded.results, '
             'transcript = excluded.transcript, updated_at = excluded.updated_at',
         [chat_id, mode, web_search, model, dump(history), dump(results), dump(strip_chart_data(transcript)), now, now])


def set_title(db, chat_id: str, title: str, *, by_user: bool) -> str | None:
    """Rename a chat. A suggested title never replaces one the user typed. Returns the stored title."""
    title = clean_title(title)
    if not title:
        return None
    if by_user:
        rows = _run(db, 'UPDATE chats.chats SET title = ?, title_by_user = true WHERE id = ? RETURNING title',
                    [title, chat_id])
    else:
        rows = _run(db, 'UPDATE chats.chats SET title = ? WHERE id = ? AND NOT title_by_user RETURNING title',
                    [title, chat_id])
    return rows[0][0] if rows else None


def delete(db, chat_id: str) -> None:
    _run(db, 'DELETE FROM chats.chats WHERE id = ?', [chat_id])
