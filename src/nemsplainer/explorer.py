"""Read-only explorer API over the catalogue and the local DuckDB database."""
from datetime import date, datetime, time
from decimal import Decimal

import duckdb

from . import catalog

PAGE_SIZES = (10, 25, 50, 100)
FILTER_COLUMNS = ('REGIONID', 'DUID')  # offered as dropdown/text filters when a table has them


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _jsonable(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat(sep=' ') if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _downloaded(cursor) -> dict[str, int]:
    """Non-empty tables in the database, with DuckDB's (cheap) estimated row counts.

    A cancelled first download leaves an empty table behind; that still counts
    as not downloaded.
    """
    rows = cursor.execute("SELECT table_name, estimated_size FROM duckdb_tables() "
                          "WHERE schema_name = 'main' AND NOT temporary AND estimated_size > 0").fetchall()
    return {name.upper(): size for name, size in rows}


def overview(db: duckdb.DuckDBPyConnection) -> dict:
    cursor = db.cursor()
    try:
        downloaded = _downloaded(cursor)
    finally:
        cursor.close()
    cat = catalog.load()
    packages = []
    for package in cat['packages']:
        tables = [{
            'name': name,
            'comment': cat['tables'][name]['comment'],
            'kind': catalog.kind(name),
            'downloaded': name in downloaded,
            'rows': downloaded.get(name),
        } for name in package['tables'] if cat['tables'][name]['in_archive']]
        if tables:
            packages.append({'name': package['name'], 'tables': tables})
    return {'source': cat['source'], 'archive': {k: cat['archive'][k] for k in ('month', 'directory')},
            'packages': packages}


def _filters(entry: dict, start: str | None, end: str | None, values: dict[str, str]) -> tuple[str, list]:
    """WHERE clause for the data view; column names come from the catalogue only."""
    clauses, params = [], []
    column = catalog.time_column(entry['name'])
    if column and start:
        clauses.append(f'{_q(column)} >= ?::TIMESTAMP')
        params.append(start)
    if column and end:
        clauses.append(f'{_q(column)} < ?::DATE + 1')
        params.append(end)
    names = {c['name'] for c in entry['columns']}
    for name, value in values.items():
        if name in names and name in FILTER_COLUMNS and value:
            clauses.append(f'{_q(name)} = ?')
            params.append(value)
    return (' WHERE ' + ' AND '.join(clauses)) if clauses else '', params


def table(db: duckdb.DuckDBPyConnection, name: str) -> dict | None:
    entry = catalog.tables().get(name)
    if entry is None:
        return None
    column = catalog.time_column(name)
    result = {**entry, 'kind': catalog.kind(name), 'time_column': column, 'downloaded': False,
              'rows': 0, 'range': None, 'files': None, 'filters': {}, 'source': catalog.source()}
    cursor = db.cursor()
    try:
        if name not in _downloaded(cursor):
            return result
        result['downloaded'] = True
        timing = f', min({_q(column)}), max({_q(column)})' if column else ''
        row = cursor.execute(f'SELECT count(*){timing} FROM {_q(name)}').fetchone()
        result['rows'] = row[0]
        if column and row[1] is not None:
            result['range'] = [_jsonable(row[1]), _jsonable(row[2])]
        has_files = cursor.execute("SELECT count(*) FROM duckdb_tables() WHERE schema_name = '_nemsplainer' "
                                   "AND table_name = 'files'").fetchone()[0]
        if has_files:
            files = cursor.execute('SELECT count(DISTINCT url), min(processed_at), max(processed_at), '
                                   'min(window_start), max(window_end) FROM _nemsplainer.files WHERE table_name = ?',
                                   [name]).fetchone()
            if files[0]:
                result['files'] = {'count': files[0], 'first': _jsonable(files[1]), 'last': _jsonable(files[2]),
                                   'window': [_jsonable(files[3]), _jsonable(files[4])]}
        for filter_column in FILTER_COLUMNS:
            if filter_column == 'REGIONID' and any(c['name'] == filter_column for c in entry['columns']):
                values = cursor.execute(f'SELECT DISTINCT {_q(filter_column)} FROM {_q(name)} '
                                        f'WHERE {_q(filter_column)} IS NOT NULL ORDER BY 1 LIMIT 50').fetchall()
                result['filters'][filter_column] = [v[0] for v in values]
            elif any(c['name'] == filter_column for c in entry['columns']):
                result['filters'][filter_column] = None  # free-text filter
        return result
    finally:
        cursor.close()


def rows(db: duckdb.DuckDBPyConnection, name: str, *, offset: int = 0, limit: int = 25,
         start: str | None = None, end: str | None = None, values: dict[str, str] | None = None) -> dict | None:
    entry = catalog.tables().get(name)
    if entry is None:
        return None
    limit = limit if limit in PAGE_SIZES else 25
    offset = max(0, offset)
    where, params = _filters(entry, start, end, values or {})
    column = catalog.time_column(name)
    # Newest first for time series; otherwise in primary-key order.
    keys = [column] if column else []
    keys += [k for k in entry['primary_key'] if k != column]
    order = ', '.join(f'{_q(k)} DESC' if k == column else _q(k) for k in keys) or '1'
    cursor = db.cursor()
    try:
        if name not in _downloaded(cursor):
            return {'columns': [c['name'] for c in entry['columns']], 'rows': [], 'total': 0}
        total = cursor.execute(f'SELECT count(*) FROM {_q(name)}{where}', params).fetchone()[0]
        relation = cursor.execute(f'SELECT * FROM {_q(name)}{where} ORDER BY {order} LIMIT ? OFFSET ?',
                                  params + [limit, offset])
        columns = [d[0] for d in relation.description]
        data = [[_jsonable(v) for v in row] for row in relation.fetchall()]
        return {'columns': columns, 'rows': data, 'total': total, 'offset': offset, 'limit': limit}
    finally:
        cursor.close()


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    return date.fromisoformat(value).isoformat()  # raises ValueError for bad input
