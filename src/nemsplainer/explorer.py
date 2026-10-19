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


# Filter operators per column type, as in marimo's data table (filters/operators.ts).
OPERATORS = {
    'number': {'between', '==', '!=', '>', '>=', '<', '<=', 'in', 'not_in', 'is_null', 'is_not_null'},
    'text': {'contains', 'equals', 'does_not_equal', 'regex', 'starts_with', 'ends_with', 'in', 'not_in',
             'is_empty', 'is_null', 'is_not_null'},
    'datetime': {'between', '==', '!=', '>', '>=', '<', '<=', 'is_null', 'is_not_null'},
    'boolean': {'is_true', 'is_false', 'is_null', 'is_not_null'},
}
_COMPARE = {'==': '=', '!=': '<>', '>': '>', '>=': '>=', '<': '<', '<=': '<=', 'equals': '=', 'does_not_equal': '<>'}


def filter_type(duckdb_type: str) -> str:
    if duckdb_type.startswith(('DECIMAL', 'DOUBLE', 'BIGINT', 'INTEGER')):
        return 'number'
    if duckdb_type in ('TIMESTAMP', 'DATE'):
        return 'datetime'
    if duckdb_type == 'BOOLEAN':
        return 'boolean'
    return 'text'


def conditions(entry: dict, filters: list, search: str | None, exclude: str | None = None) -> tuple[list[str], list]:
    """SQL conditions for marimo-style column filters (ANDed) plus a search over every column.

    Column names come only from the catalogue; every value is a parameter.
    Semantics follow marimo: text `contains` is case-sensitive and literal,
    `between` is inclusive, `not_in` keeps nulls, search is case-insensitive.
    """
    types = {c['name']: filter_type(c['type']) for c in entry['columns']}
    clauses, params = [], []
    for item in filters or []:
        if not isinstance(item, dict):
            raise ValueError('each filter must be an object')
        column, op = item.get('column'), item.get('operator')
        if column not in types or column == exclude:
            if column not in types:
                raise ValueError(f'unknown filter column {column!r}')
            continue
        kind = types[column]
        if op not in OPERATORS[kind]:
            raise ValueError(f'operator {op!r} does not apply to {kind} column {column}')
        col = _q(column)
        cast = 'TIMESTAMP' if kind == 'datetime' else None
        if op == 'is_null':
            clauses.append(f'{col} IS NULL')
        elif op == 'is_not_null':
            clauses.append(f'{col} IS NOT NULL')
        elif op == 'is_true':
            clauses.append(f'{col} = true')
        elif op == 'is_false':
            clauses.append(f'{col} = false')
        elif op == 'is_empty':
            clauses.append(f"{col} = ''")
        elif op == 'between':
            clauses.append(f'{col} BETWEEN ?{"::" + cast if cast else ""} AND ?{"::" + cast if cast else ""}')
            params += [item.get('min'), item.get('max')]  # DuckDB casts parameters to the column type
        elif op in _COMPARE:
            clauses.append(f'{col} {_COMPARE[op]} ?{"::" + cast if cast else ""}')
            params.append(item.get('value'))
        elif op == 'contains':
            clauses.append(f"contains(coalesce({col}, ''), ?)")
            params.append(str(item.get('value', '')))
        elif op == 'starts_with':
            clauses.append(f"starts_with(coalesce({col}, ''), ?)")
            params.append(str(item.get('value', '')))
        elif op == 'ends_with':
            clauses.append(f"suffix(coalesce({col}, ''), ?)")
            params.append(str(item.get('value', '')))
        elif op == 'regex':
            clauses.append(f"regexp_matches(coalesce({col}, ''), ?)")
            params.append(str(item.get('value', '')))
        elif op in ('in', 'not_in'):
            values = item.get('values') or []
            if not isinstance(values, list) or not values:
                raise ValueError(f'{op} needs a non-empty list of values')
            present = [v for v in values if v is not None]
            inside = f"{col} IN ({', '.join('?' * len(present))})" if present else 'false'
            if op == 'in':
                clauses.append(f'({inside} OR {col} IS NULL)' if None in values else f'({inside})')
            else:
                clauses.append(f'(NOT ({inside}) AND {col} IS NOT NULL)' if None in values else f'(NOT ({inside}) OR {col} IS NULL)')
            params += present
    if search:
        pattern = '%' + search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        clauses.append('(' + ' OR '.join(f"CAST({_q(c['name'])} AS VARCHAR) ILIKE ? ESCAPE '\\'"
                                         for c in entry['columns']) + ')')
        params += [pattern] * len(entry['columns'])
    return clauses, params


def top_values(db: duckdb.DuckDBPyConnection, name: str, column: str, *, filters: list | None = None,
               search: str | None = None, start: str | None = None, end: str | None = None, k: int = 30) -> dict | None:
    """The most common values of ``column`` with counts (marimo's filter-by-values picker).

    Filters on the same column are ignored, so the picker shows what you could choose.
    """
    entry = catalog.tables().get(name)
    if entry is None:
        return None
    if column not in {c['name'] for c in entry['columns']}:
        raise ValueError(f'unknown column {column!r}')
    where, params = _filters(entry, start, end, {})
    extra, extra_params = conditions(entry, filters or [], search, exclude=column)
    clauses = ([where.removeprefix(' WHERE ')] if where else []) + extra
    sql_where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    cursor = db.cursor()
    try:
        if name not in _downloaded(cursor):
            return {'values': []}
        rows = cursor.execute(f'SELECT {_q(column)}, count(*) AS n FROM {_q(name)}{sql_where} GROUP BY 1 '
                              f'ORDER BY n DESC, 1 NULLS LAST LIMIT ?', params + extra_params + [k]).fetchall()
    finally:
        cursor.close()
    return {'values': [[_jsonable(v), n] for v, n in rows], 'k': k}


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
         start: str | None = None, end: str | None = None, values: dict[str, str] | None = None,
         sort: str | None = None, descending: bool = False, filters: list | None = None,
         search: str | None = None) -> dict | None:
    entry = catalog.tables().get(name)
    if entry is None:
        return None
    limit = limit if limit in PAGE_SIZES else 25
    offset = max(0, offset)
    where, params = _filters(entry, start, end, values or {})
    extra, extra_params = conditions(entry, filters or [], search)
    if extra:
        where = (where + ' AND ' if where else ' WHERE ') + ' AND '.join(extra)
        params = params + extra_params
    column = catalog.time_column(name)
    # Newest first for time series; otherwise in primary-key order.
    keys = [column] if column else []
    keys += [k for k in entry['primary_key'] if k != column]
    order = ', '.join(f'{_q(k)} DESC' if k == column else _q(k) for k in keys) or '1'
    if sort in {c['name'] for c in entry['columns']}:  # a user-chosen sort column (from the catalogue only)
        order = f"{_q(sort)} {'DESC' if descending else 'ASC'} NULLS LAST, {order}"
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
