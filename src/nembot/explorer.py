"""Read-only explorer API over the catalogue and the local DuckDB database."""
from datetime import date, datetime, time
from decimal import Decimal
import json
import re
from time import perf_counter

import duckdb

from . import catalog

PAGE_SIZES = (10, 25, 50, 100)
CHART_ROWS = 5000  # the data table's "Chart this" asks for this many rows at once
SQL_ROWS = 1000  # rows the SQL editor shows; it streams them, so big tables stay cheap
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


# Column summaries (marimo's header stats and histograms). marimo computes stats up to
# 1,000,000 rows and charts up to 20,000; DuckDB makes both cheap up to a million.
SUMMARY_ROW_LIMIT = 1_000_000
SUMMARY_BINS = 9  # marimo's DEFAULT_BIN_SIZE


def summaries(db: duckdb.DuckDBPyConnection, name: str, *, filters: list | None = None, search: str | None = None,
              start: str | None = None, end: str | None = None) -> dict | None:
    """Per-column stats and 9-bin histograms over the filtered/searched rows."""
    entry = catalog.tables().get(name)
    if entry is None:
        return None
    where, params = _filters(entry, start, end, {})
    extra, extra_params = conditions(entry, filters or [], search)
    clauses = ([where.removeprefix(' WHERE ')] if where else []) + extra
    sql_where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    params = params + extra_params
    cursor = db.cursor()
    try:
        if name not in _downloaded(cursor):
            return {'total': 0, 'enabled': False, 'columns': {}}
        total = cursor.execute(f'SELECT count(*) FROM {_q(name)}{sql_where}', params).fetchone()[0]
        if total == 0 or total > SUMMARY_ROW_LIMIT:
            return {'total': total, 'enabled': False, 'limit': SUMMARY_ROW_LIMIT, 'columns': {}}
        kinds = {c['name']: filter_type(c['type']) for c in entry['columns']}
        aggregates, layout = [], []
        for column, kind in kinds.items():
            col = _q(column)
            parts = {'nulls': f'count(*) - count({col})', 'unique': f'count(DISTINCT {col})'}
            if kind == 'number':
                parts |= {'min': f'min({col})::DOUBLE', 'max': f'max({col})::DOUBLE',
                          'quantiles': f'quantile_cont({col}::DOUBLE, [0.25, 0.5, 0.75, 0.95])'}
            elif kind == 'datetime':
                parts |= {'min': f'min({col})', 'max': f'max({col})'}
            elif kind == 'boolean':
                parts |= {'true': f'count(*) FILTER (WHERE {col})', 'false': f'count(*) FILTER (WHERE NOT {col})'}
            for key, sql in parts.items():
                aggregates.append(sql)
                layout.append((column, key))
        values = cursor.execute(f'SELECT {", ".join(aggregates)} FROM {_q(name)}{sql_where}', params).fetchone()
        stats: dict[str, dict] = {column: {} for column in kinds}
        for (column, key), value in zip(layout, values):
            if key == 'quantiles':
                if value:
                    stats[column] |= dict(zip(('p25', 'median', 'p75', 'p95'), value))
            else:
                stats[column][key] = _jsonable(value)
        # One statement for every histogram: equal-width bins between each column's min and max.
        selects, bin_params, ranges = [], [], {}
        for column, kind in kinds.items():
            info = stats[column]
            if kind not in ('number', 'datetime') or info.get('min') is None:
                continue
            col = _q(column)
            value_sql = f'{col}::DOUBLE' if kind == 'number' else f'epoch({col})'
            if kind == 'number':
                low, high = info['min'], info['max']
            else:
                low, high = cursor.execute(f'SELECT min({value_sql}), max({value_sql}) FROM {_q(name)}{sql_where}', params).fetchone()
            ranges[column] = (low, high)
            step = (high - low) / SUMMARY_BINS if high > low else 1
            selects.append(f"SELECT ? AS column_name, least(floor(({value_sql} - ?) / ?)::INTEGER, {SUMMARY_BINS - 1}) AS bucket, "
                           f"count(*) AS n FROM {_q(name)}{sql_where}{' AND ' if sql_where else ' WHERE '}{col} IS NOT NULL GROUP BY 2")
            bin_params += [column, low, step] + params
        counts: dict[str, dict[int, int]] = {}
        if selects:
            for column, bucket, n in cursor.execute(' UNION ALL '.join(selects), bin_params).fetchall():
                counts.setdefault(column, {})[bucket] = n
        for column, (low, high) in ranges.items():
            if high == low:
                bins = [[low, high, sum(counts.get(column, {}).values())]]
            else:
                step = (high - low) / SUMMARY_BINS
                bins = [[low + i * step, low + (i + 1) * step, counts.get(column, {}).get(i, 0)] for i in range(SUMMARY_BINS)]
            if kinds[column] == 'datetime':  # epoch seconds -> ISO strings
                bins = [[_epoch_iso(a), _epoch_iso(b), n]
                        for a, b, n in bins]
            stats[column]['bins'] = bins
        return {'total': total, 'enabled': True, 'columns': stats}
    finally:
        cursor.close()


def _epoch_iso(seconds: float) -> str:
    """Epoch seconds from DuckDB's epoch(TIMESTAMP) back to a naive ISO timestamp."""
    from datetime import timezone
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None).isoformat(sep=' ', timespec='seconds')


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
        has_files = cursor.execute("SELECT count(*) FROM duckdb_tables() WHERE schema_name = '_nembot' "
                                   "AND table_name = 'files'").fetchone()[0]
        if has_files:
            files = cursor.execute('SELECT count(DISTINCT url), min(processed_at), max(processed_at), '
                                   'min(window_start), max(window_end) FROM _nembot.files WHERE table_name = ?',
                                   [name]).fetchone()
            if files[0]:
                result['files'] = {'count': files[0], 'first': _jsonable(files[1]), 'last': _jsonable(files[2]),
                                   'window': [_jsonable(files[3]), _jsonable(files[4])]}
        if cursor.execute("SELECT count(*) FROM duckdb_tables() WHERE schema_name = '_nembot' "
                          "AND table_name = 'notes'").fetchone()[0]:
            result['notes'] = [{'file': url.rsplit('/', 1)[-1], 'note': note} for url, note in cursor.execute(
                'SELECT url, note FROM _nembot.notes WHERE table_name = ? ORDER BY url, note', [name]).fetchall()]
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
    limit = limit if limit in PAGE_SIZES or limit == CHART_ROWS else 25
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


def run_sql(db: duckdb.DuckDBPyConnection, sql: str, limit: int = SQL_ROWS) -> dict:
    """The SQL editor: run one read-only SELECT and return its first ``limit`` rows."""
    cursor = db.cursor()
    try:
        statements = cursor.extract_statements(sql)
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            raise ValueError('Only one SELECT query is allowed.')
        started = perf_counter()
        cursor.execute(sql)
        columns = [d[0] for d in cursor.description]
        rows = cursor.fetchmany(limit + 1)
        seconds = perf_counter() - started
    finally:
        cursor.close()
    return {'columns': columns, 'rows': [[_jsonable(v) for v in row] for row in rows[:limit]],
            'truncated': len(rows) > limit, 'seconds': round(seconds, 3)}


def schema(db: duckdb.DuckDBPyConnection) -> dict[str, list[str]]:
    """Downloaded tables and their columns (from the catalogue), for SQL autocompletion."""
    cursor = db.cursor()
    try:
        downloaded = _downloaded(cursor)
    finally:
        cursor.close()
    tables = catalog.tables()
    return {name: [c['name'] for c in tables[name]['columns']] for name in sorted(downloaded) if name in tables}


def check_sql(db: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
    """Problems with an editor query, found by DuckDB's own parser and binder without running it.

    Each is ``{'message', 'from', 'to'}`` (character offsets). Binding a SELECT relation doesn't
    execute it, so this stays fast on any table. Binder errors carry no position, but they quote
    the name at fault (column "X", Table with name X), which is underlined where it first appears.
    """
    if not sql.strip():
        return []
    cursor = db.cursor()
    try:
        try:
            statements = cursor.extract_statements(sql)
        except duckdb.Error as exc:
            serialized = json.loads(cursor.execute('SELECT json_serialize_sql(?)', [sql]).fetchone()[0])
            position = serialized.get('position')
            start = min(int(position), len(sql)) if position is not None else 0
            if not sql[start:].strip():  # "at end of input": the last word typed
                start = len(sql.rstrip())
                start -= len(re.search(r'\w*$', sql[:start]).group()) or 1
            word = re.match(r'\w+', sql[start:])
            return [_problem(str(exc), sql, start, start + (len(word.group()) if word else 1))]
        if len(statements) > 1:
            start = sql.find(statements[1].query.strip()[:20]) if statements[1].query.strip() else -1
            return [_problem('Only one query at a time: remove everything after the first statement.', sql,
                             max(start, 0), len(sql))]
        if not statements or statements[0].type != duckdb.StatementType.SELECT:
            return [_problem('Only SELECT queries can run here (the editor is read-only).', sql, 0, len(sql))]
        try:
            cursor.sql(sql)
        except duckdb.Error as exc:
            start, end = _culprit(str(exc), sql)
            return [_problem(str(exc), sql, start, end)]
        return []
    finally:
        cursor.close()


def _problem(message: str, sql: str, start: int, end: int) -> dict:
    start = min(max(start, 0), len(sql))
    end = min(max(end, start), len(sql))
    if start == end and start == len(sql) and start > 0:  # "syntax error at end of input"
        start -= 1
    return {'message': message.strip(), 'from': start, 'to': max(end, start + 1) if sql else 0}


def _culprit(message: str, sql: str) -> tuple[int, int]:
    """Where the name a binder/catalog error quotes first appears in the query (else everything)."""
    first = message.split('\n', 1)[0]
    names = re.findall(r'"([^"]+)"', first) + re.findall(r'with name (\S+?)(?: does not exist|!|$)', first)
    for name in names:
        for candidate in (name, name.split('.')[-1]):
            match = re.search(r'(?<![\w"])' + re.escape(candidate) + r'(?![\w"])', sql, re.IGNORECASE) \
                or re.search(re.escape(candidate), sql, re.IGNORECASE)
            if match:
                return match.start(), match.end()
    return 0, len(sql)


_functions_cache: list[dict] | None = None


def functions(db: duckdb.DuckDBPyConnection) -> list[dict]:
    """DuckDB's functions for editor completion and hover help: one entry per name with its
    description, signatures (up to six overloads) and examples, from ``duckdb_functions()``."""
    global _functions_cache
    if _functions_cache is None:
        cursor = db.cursor()
        try:
            rows = cursor.execute("""
                SELECT function_name, function_type, description, return_type, parameters, parameter_types,
                       varargs, examples
                FROM duckdb_functions()
                WHERE function_type <> 'pragma' AND regexp_full_match(function_name, '[a-z][a-z0-9_]*')
                ORDER BY function_name, len(parameters), function_oid""").fetchall()
        finally:
            cursor.close()
        entries: dict[str, dict] = {}
        for name, kind, description, returns, params, types, varargs, examples in rows:
            entry = entries.setdefault(name, {'name': name, 'type': kind, 'description': None, 'signatures': [],
                                              'examples': []})
            entry['description'] = entry['description'] or description
            args = [f'{p} {t}' if t else p for p, t in zip(params or [], (types or []) + [None] * len(params or []))]
            if varargs:
                args.append(f'{varargs}...')
            signature = f"{name}({', '.join(args)})" + (f' → {returns}' if returns else '')
            if signature not in entry['signatures'] and len(entry['signatures']) < 6:
                entry['signatures'].append(signature)
            for example in examples or []:
                if example not in entry['examples'] and len(entry['examples']) < 2:
                    entry['examples'].append(example)
        _functions_cache = list(entries.values())
    return _functions_cache
