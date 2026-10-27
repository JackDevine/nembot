"""Persistent, cumulative monthly MMS ingestion. Connections belong to callers."""
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import re
import sys
import tempfile
import threading

import duckdb

from . import nemweb
from . import catalog
from .tables import DEFAULT_TABLES, TABLES, Table

# Any table the monthly archive publishes can be loaded.
SUPPORTED = tuple(name for name, entry in catalog.tables().items() if entry['in_archive'])
# Interval tables whose rows are filtered to the exact requested market days.
# Every other table is loaded a whole archive month at a time, because the
# meaning of its timestamp (interval ending, trading day, run time...) varies.
EXACT_WINDOW = ('DISPATCHPRICE', 'DISPATCHREGIONSUM', 'DISPATCH_UNIT_SCADA')
TIMESTAMP_FORMATS = "['%Y/%m/%d %H:%M:%S', '%Y/%m/%d %H:%M:%S.%g']"  # TIMESTAMP(n) has milliseconds

MARKET_TIME = timezone(timedelta(hours=10))


def _date(value: date | str | None, label: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        raise ValueError(f"{label} must be a calendar date, not datetime")
    if isinstance(value, str):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError(f"{label} must be YYYY-MM-DD")
        value = date.fromisoformat(value)
    if not isinstance(value, date):
        raise ValueError(f"{label} must be a date or YYYY-MM-DD string")
    if value > datetime.now(MARKET_TIME).date():
        raise ValueError(f"{label} cannot be in the future")
    return value


def validate_inputs(tables, start_date=None, end_date=None, *, database="data/nem.duckdb", cache_dir="data/cache"):
    """Validate caller/config values before filesystem or network effects."""
    if isinstance(tables, (str, bytes)) or not isinstance(tables, Sequence) or not tables:
        raise ValueError("tables must be a nonempty sequence of table names")
    names = []
    for name in tables:
        if not isinstance(name, str) or name.upper() not in SUPPORTED:
            raise ValueError(f"Unsupported MMS table: {name!r}; it is not published in the MMSDM monthly archive")
        if name.upper() not in names:
            names.append(name.upper())
    start, end = _date(start_date, "start"), _date(end_date, "end")
    if start and end and start > end:
        raise ValueError("start must not be after end")
    for label, path in (("database", database), ("cache_dir", cache_dir)):
        if not isinstance(path, (str, Path)) or not str(path).strip():
            raise ValueError(f"{label} must be a nonempty filesystem path")
    if str(database) == ":memory:" or str(database).startswith("md:"):
        raise ValueError("database must be a local file-backed DuckDB path")
    db, cache = Path(database).absolute(), Path(cache_dir).absolute()
    if db == cache or db in cache.parents:
        raise ValueError("database cannot be the cache directory or its parent")
    return names, start, end, db, cache


def _month_shift(day: date, offset: int) -> date:
    index = day.year * 12 + day.month - 1 + offset
    year, month = divmod(index, 12)
    return date(year, month + 1, 1)


def resolve_sources(names, start, end):
    """Resolve defaults and preflight every requested table-month."""
    if end is None:
        end = _month_shift(nemweb.latest_month(names), 1) - timedelta(days=1)
    if start is None:
        start = _month_shift(end, -2)
    if start > end:
        raise ValueError("start must not be after resolved published end")
    sources = []
    month = start.replace(day=1)
    while month <= end:
        files = nemweb.month_files(month.year, month.month, names)
        for name in names:
            for url in files[name]:
                sources.append((name, url, month))
        month = _month_shift(month, 1)
    return start, end, sources


def _q(name):
    return '"' + name.replace('"', '""') + '"'


def _create_table(c, table: Table, name=None, temporary=False):
    name = name or table.name
    columns = ', '.join(f'{_q(k)} {v}' for k, v in table.columns.items())
    keys = ', '.join(map(_q, table.primary_key))
    c.execute(f"CREATE {'TEMP ' if temporary else ''}TABLE IF NOT EXISTS {_q(name)} ({columns}, PRIMARY KEY ({keys}))")
    actual = [(r[1], r[2]) for r in c.execute(f"PRAGMA table_info({_q(name)})").fetchall()]
    if actual != list(table.columns.items()):
        raise RuntimeError(f"Incompatible existing schema for {name}; expected {list(table.columns.items())}, got {actual}")
    constraints = c.execute("SELECT constraint_column_names FROM duckdb_constraints() WHERE table_name=? AND constraint_type='PRIMARY KEY'", [name]).fetchall()
    if not any(tuple(r[0]) == table.primary_key for r in constraints):
        raise RuntimeError(f"Incompatible primary key for {name}; expected {table.primary_key}")


def _merge(c, table, target, csv_path, supplied, context):
    """Bulk cast, reject tied contradictions, and apply only newer supplied values."""
    # Timestamps are read as text and parsed with either format (with or without milliseconds).
    types = {name: 'VARCHAR' if table.columns[name] == 'TIMESTAMP' else table.columns[name] for name in supplied}
    casts = ', '.join(f'strptime({_q(n)}, {TIMESTAMP_FORMATS}) AS {_q(n)}' if table.columns[n] == 'TIMESTAMP' else _q(n)
                      for n in supplied)
    c.execute('DROP TABLE IF EXISTS _stage')
    try:
        c.read_csv(str(csv_path), header=True, columns=types, auto_detect=False,
                   delimiter=',', quotechar='"', escapechar='"', na_values=['']).create_view('_typed', replace=True)
        c.execute(f'CREATE TEMP TABLE _stage AS SELECT {casts} FROM _typed')
    except duckdb.Error as exc:
        raise RuntimeError(f"{context}: invalid typed CSV values: {exc}") from exc
    finally:
        c.execute('DROP VIEW IF EXISTS _typed')
    keys = ', '.join(map(_q, table.primary_key))
    tracked = 'LASTCHANGED' in table.columns  # a few tables have no LASTCHANGED at all
    if 'LASTCHANGED' not in supplied:
        c.execute('ALTER TABLE _stage ADD COLUMN LASTCHANGED TIMESTAMP')
    key_null = ' OR '.join(f'{_q(k)} IS NULL' for k in table.primary_key)
    if c.execute(f'SELECT 1 FROM _stage WHERE {key_null} LIMIT 1').fetchone():
        raise RuntimeError(f"{context}: NULL primary key")
    # Rank before filtering dates: reference closures outside the request still win.
    c.execute('DROP TABLE IF EXISTS _winners')
    c.execute(f'CREATE TEMP TABLE _winners AS SELECT DISTINCT * FROM _stage QUALIFY dense_rank() OVER (PARTITION BY {keys} ORDER BY LASTCHANGED DESC NULLS LAST)=1')
    conflict = c.execute(f'SELECT {keys} FROM _winners GROUP BY {keys} HAVING count(*)>1 LIMIT 1').fetchone()
    if conflict:
        raise RuntimeError(f"{context}: conflicting equal-LASTCHANGED records for key {conflict}")
    match = ' AND '.join(f't.{_q(k)}=s.{_q(k)}' for k in table.primary_key)
    differences = ' OR '.join(f't.{_q(k)} IS DISTINCT FROM s.{_q(k)}' for k in supplied if k not in table.primary_key) or 'false'
    if tracked:
        conflict = c.execute(f'SELECT {", ".join("s."+_q(k) for k in table.primary_key)} FROM _winners s JOIN {_q(target)} t ON {match} WHERE t.LASTCHANGED IS NOT DISTINCT FROM s.LASTCHANGED AND ({differences}) LIMIT 1').fetchone()
        if conflict:
            raise RuntimeError(f"{context}: conflicting equal-LASTCHANGED records against stored key {conflict}")
        newer = 's.LASTCHANGED IS NOT NULL AND (t.LASTCHANGED IS NULL OR s.LASTCHANGED>t.LASTCHANGED)'
    else:
        newer = 'true'  # no change tracking: the most recently loaded archive wins
    updates = ', '.join(f'{_q(k)}=s.{_q(k)}' for k in supplied if k not in table.primary_key)
    column_sql = ', '.join(map(_q, supplied))
    value_sql = ', '.join('s.' + _q(k) for k in supplied)
    update_sql = f'WHEN MATCHED AND {newer} THEN UPDATE SET {updates}' if updates else ''
    c.execute(f'MERGE INTO {_q(target)} t USING _winners s ON {match} {update_sql} WHEN NOT MATCHED THEN INSERT ({column_sql}) VALUES ({value_sql})')
    c.execute('DROP TABLE _stage')
    c.execute('DROP TABLE _winners')


def _completed(c, name, url, digest, lower, upper):
    return c.execute('SELECT 1 FROM _nembot.files WHERE url=? AND sha256=? AND table_name=? AND window_start=? AND window_end=?', [url, digest, name, lower, upper]).fetchone() is not None


def _mark(c, name, url, digest, lower, upper):
    c.execute('INSERT INTO _nembot.files VALUES (?, ?, ?, ?, ?, current_timestamp) ON CONFLICT DO NOTHING', [url, digest, name, lower, upper])


def _note_rounding(c, table, url, rounded: dict) -> list[str]:
    """Record decimals rounded to the data model's scale for this file (shown in the Explorer and jobs)."""
    c.execute('CREATE TABLE IF NOT EXISTS _nembot.notes (url VARCHAR, table_name VARCHAR, note VARCHAR, '
              'created_at TIMESTAMP, PRIMARY KEY(url, table_name, note))')
    notes = []
    for column, count in sorted(rounded.items()):
        note = (f"rounded {count:,} {column} value{'s' if count != 1 else ''} to {table.columns[column]}: "
                "AEMO's file has more decimal places than its data model documents")
        c.execute('INSERT INTO _nembot.notes VALUES (?, ?, ?, current_timestamp) ON CONFLICT DO NOTHING',
                  [url, table.name, note])
        print(f'{table.name}: {note} ({url})', flush=True)
        notes.append(note)
    return notes


def _load_file(c, table, target, path, directory, url, lower=None, upper=None, required=True, report=None):
    rounded = {}
    normalized = nemweb.normalize(path, table, directory, url, required=required, rounded=rounded)
    if rounded:
        for note in _note_rounding(c, table, url, rounded):
            if report is not None:
                report({'stage': 'note', 'table': table.name, 'url': url, 'note': note})
    for csv_path, supplied, context in normalized:
        if lower is None:
            _merge(c, table, target, csv_path, supplied, context)
        else:
            # Filter normalized data in DuckDB, not Python row-by-row timestamp parsing.
            selected = Path(directory) / (csv_path.name + '.selected.csv')
            relation = c.read_csv(str(csv_path), header=True, columns={n: 'VARCHAR' for n in supplied}, auto_detect=False, delimiter=',', quotechar='"', escapechar='"', na_values=[''])
            relation.create_view('_raw', replace=True)
            try:
                lower_sql, upper_sql = lower.isoformat(sep=' '), upper.isoformat(sep=' ')
                target_sql = str(selected).replace("'", "''")
                c.execute(f"COPY (SELECT * FROM _raw WHERE strptime(SETTLEMENTDATE, '%Y/%m/%d %H:%M:%S') > TIMESTAMP '{lower_sql}' AND strptime(SETTLEMENTDATE, '%Y/%m/%d %H:%M:%S') <= TIMESTAMP '{upper_sql}') TO '{target_sql}' (HEADER, FORMAT CSV)")
                _merge(c, table, target, selected, supplied, context)
            finally:
                c.execute('DROP VIEW _raw')


def _diagnostics(c, names, lower, upper):
    for name in names:
        timestamp = 'LASTCHANGED' if name == 'DUDETAILSUMMARY' else catalog.time_column(name)
        if timestamp is None:
            print(f'{name}: rows={c.execute(f"SELECT count(*) FROM {_q(name)}").fetchone()[0]:,}', flush=True)
            continue
        row = c.execute(f'SELECT count(*), min({_q(timestamp)}), max({_q(timestamp)}) FROM {_q(name)}').fetchone()
        print(f'{name}: rows={row[0]:,} {timestamp}={row[1]} .. {row[2]}', flush=True)
        if name not in EXACT_WINDOW:
            continue
        expected = "SELECT endpoint FROM generate_series(?::TIMESTAMP + INTERVAL 5 MINUTE, ?::TIMESTAMP, INTERVAL 5 MINUTE) x(endpoint)"
        if name in ('DISPATCHPRICE', 'DISPATCHREGIONSUM'):
            missing = c.execute(f"WITH expected AS ({expected}) SELECT count(*) FROM expected CROSS JOIN (VALUES ('NSW1'), ('QLD1'), ('SA1'), ('TAS1'), ('VIC1')) r(region) WHERE NOT EXISTS (SELECT 1 FROM {_q(name)} t WHERE t.SETTLEMENTDATE=endpoint AND t.REGIONID=region)", [lower, upper]).fetchone()[0]
            unit = 'region/interval pairs'
        else:
            missing = c.execute(f'WITH expected AS ({expected}) SELECT count(*) FROM expected WHERE NOT EXISTS (SELECT 1 FROM {_q(name)} t WHERE t.SETTLEMENTDATE=endpoint)', [lower, upper]).fetchone()[0]
            unit = 'interval endpoints'
        print(f'{name}: missing {unit}={missing}', file=sys.stderr if missing else sys.stdout, flush=True)


def _month_window(month: date) -> tuple[datetime, datetime]:
    return datetime.combine(month, time()), datetime.combine(_month_shift(month, 1), time())


def load_mms(
    tables: Sequence[str],
    start_date: date | str | None = None,
    end_date: date | str | None = None,
    *,
    database: str | Path = 'data/nem.duckdb',
    cache_dir: str | Path = 'data/cache',
    connection: duckdb.DuckDBPyConnection | None = None,
    progress: Callable[[dict], None] | None = None,
    cancel: threading.Event | None = None,
) -> duckdb.DuckDBPyConnection:
    """Load monthly public MMS archives for inclusive UTC+10 calendar dates.

    Omitted dates select three complete published months. Existing history is
    retained. `EXACT_WINDOW` tables are filtered to the requested days; other
    tables load whole archive months. Without ``connection`` a new writable
    connection is opened and returned (the caller must close it); with one,
    the load runs on a cursor of it and that connection is returned. Each
    file commits separately, so a cancelled (``cancel``) or failed load keeps
    the files already finished. ``progress`` receives event dicts.
    """
    report = progress or (lambda event: None)
    names, start, end, database, cache = validate_inputs(tables, start_date, end_date, database=database, cache_dir=cache_dir)
    report({'stage': 'listing'})
    start, end, sources = resolve_sources(names, start, end)
    lower, upper = datetime.combine(start, time()), datetime.combine(end + timedelta(days=1), time())
    print(f'Market dates: {start} through {end}; monthly sources only', flush=True)
    report({'stage': 'resolved', 'start': start.isoformat(), 'end': end.isoformat(), 'files': len(sources),
            'size': sum(nemweb.file_size(url) or 0 for _, url, _ in sources)})
    database.parent.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    owned = connection is None
    c = duckdb.connect(str(database)) if owned else connection.cursor()

    def check_cancel():
        if cancel is not None and cancel.is_set():
            raise nemweb.Cancelled('Download cancelled')

    try:
        c.execute("SET memory_limit='2GB'")
        c.execute('SET threads=4')
        c.execute('CREATE SCHEMA IF NOT EXISTS _nembot')
        c.execute('CREATE TABLE IF NOT EXISTS _nembot.files (url VARCHAR, sha256 VARCHAR, table_name VARCHAR, window_start TIMESTAMP, window_end TIMESTAMP, processed_at TIMESTAMP, PRIMARY KEY(url,sha256,table_name,window_start,window_end))')
        for name in names:
            _create_table(c, TABLES[name])
        references = []
        # Scratch space for unzipped CSVs. DuckDB spills to its own temp directory beside the
        # database: on the app's shared connection it can't be switched once it has been used.
        with tempfile.TemporaryDirectory(prefix='staging-', dir=cache) as scratch:
            current = {}

            def on_bytes(url, n):  # only the file being waited for reports progress
                if current.get('url') == url:
                    report({**current['event'], 'bytes': n})
            downloads = nemweb.download_many([url for _, url, _ in sources], cache, progress=on_bytes, cancel=cancel)
            try:
                for index, (name, url, month) in enumerate(sources, 1):
                    check_cancel()
                    print(f'{name}: {url}', flush=True)
                    event = {'stage': 'download', 'table': name, 'url': url, 'month': month.isoformat()[:7],
                             'file': index, 'files': len(sources), 'bytes': 0, 'size': nemweb.file_size(url)}
                    current.update(url=url, event=event)
                    report(event)
                    _, path, digest = next(downloads)
                    if name == 'DUDETAILSUMMARY':
                        references.append((path, digest, url))
                        continue
                    window = (lower, upper) if name in EXACT_WINDOW else _month_window(month)
                    if _completed(c, name, url, digest, *window):
                        continue
                    check_cancel()
                    report({**event, 'stage': 'load'})
                    c.execute('BEGIN')
                    try:
                        with tempfile.TemporaryDirectory(dir=scratch) as normalized:
                            if name in EXACT_WINDOW:
                                _load_file(c, TABLES[name], name, path, Path(normalized), url, lower, upper, report=report)
                            else:
                                _load_file(c, TABLES[name], name, path, Path(normalized), url, report=report)
                        _mark(c, name, url, digest, *window)
                        c.execute('COMMIT')
                    except BaseException:
                        c.execute('ROLLBACK')
                        raise
                # Reference snapshots must be reconciled across all months BEFORE
                # overlap selection, otherwise a later closure can resurrect an old row.
                if references and not all(_completed(c, 'DUDETAILSUMMARY', url, digest, lower, upper) for _, digest, url in references):
                    check_cancel()
                    report({'stage': 'load', 'table': 'DUDETAILSUMMARY'})
                    table = TABLES['DUDETAILSUMMARY']
                    c.execute('BEGIN')
                    try:
                        _create_table(c, table, '_reference', temporary=True)
                        c.execute('INSERT INTO _reference SELECT * FROM DUDETAILSUMMARY')
                        for path, digest, url in references:
                            with tempfile.TemporaryDirectory(dir=scratch) as normalized:
                                _load_file(c, table, '_reference', path, Path(normalized), url, report=report)
                        match = ' AND '.join(f't.{_q(k)}=s.{_q(k)}' for k in table.primary_key)
                        updates = ', '.join(f'{_q(k)}=s.{_q(k)}' for k in table.columns if k not in table.primary_key)
                        all_columns = ', '.join(map(_q, table.columns))
                        all_values = ', '.join('s.' + _q(k) for k in table.columns)
                        c.execute(f'MERGE INTO DUDETAILSUMMARY t USING _reference s ON {match} WHEN MATCHED THEN UPDATE SET {updates} WHEN NOT MATCHED AND s.START_DATE < ? AND (s.END_DATE IS NULL OR s.END_DATE > ?) THEN INSERT ({all_columns}) VALUES ({all_values})', [upper, lower])
                        for _, digest, url in references:
                            _mark(c, table.name, url, digest, lower, upper)
                        c.execute('DROP TABLE _reference')
                        c.execute('COMMIT')
                    except BaseException:
                        c.execute('ROLLBACK')
                        raise
                _diagnostics(c, names, lower, upper)
            finally:
                downloads.close()
        try:
            c.execute('CHECKPOINT')
        except duckdb.Error:
            if owned:
                raise  # a shared database may have readers mid-transaction; it checkpoints later
        report({'stage': 'done'})
        if owned:
            return c
        c.close()
        return connection
    except BaseException:
        c.close()
        raise
