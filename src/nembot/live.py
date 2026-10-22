"""Recent data from NEMWEB's report feeds, newer than the monthly MMSDM archives.

AEMO publishes recent data per report family (e.g. DispatchIS_Reports) as
ARCHIVE files (daily, weekly or monthly ZIPs, often ZIP-in-ZIP) plus CURRENT
files (5-minute, half-hourly or daily). One family carries several MMS
tables. `FEEDS` lists the tables supported so far, with the CSV identity
each uses in the report (checked against live files on 19 Oct 2026).

An archive file covers from its date to the next archive file's date. After
the newest archive file, CURRENT files fill the gap, but only when that's a
polite number of requests (`CURRENT_LIMIT`); otherwise the data stops at the
archive's end and the plan says so.
"""
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
import re
import tempfile
import threading
import urllib.parse
from pathlib import Path
from collections.abc import Callable

import duckdb

from . import catalog, nemweb
from .ingest import _create_table, _completed, _load_file, _mark, _q
from .tables import TABLES

REPORTS = 'https://www.nemweb.com.au/Reports/'
CURRENT_LIMIT = 600     # at most this many CURRENT files per refresh (~10 minutes at 1 request/s)
CURRENT_TTL = 300       # CURRENT listings change every few minutes


@dataclass(frozen=True)
class Feed:
    family: str         # directory under Reports/ARCHIVE and Reports/CURRENT
    prefix: str         # file name prefix within that directory
    identity: tuple[str, str]


FEEDS = {
    'DISPATCHPRICE': Feed('DispatchIS_Reports', 'PUBLIC_DISPATCHIS_', ('DISPATCH', 'PRICE')),
    'DISPATCHREGIONSUM': Feed('DispatchIS_Reports', 'PUBLIC_DISPATCHIS_', ('DISPATCH', 'REGIONSUM')),
    'DISPATCHINTERCONNECTORRES': Feed('DispatchIS_Reports', 'PUBLIC_DISPATCHIS_', ('DISPATCH', 'INTERCONNECTORRES')),
    'DISPATCHCASESOLUTION': Feed('DispatchIS_Reports', 'PUBLIC_DISPATCHIS_', ('DISPATCH', 'CASE_SOLUTION')),
    'DISPATCHCONSTRAINT': Feed('DispatchIS_Reports', 'PUBLIC_DISPATCHIS_', ('DISPATCH', 'CONSTRAINT')),
    'DISPATCH_LOCAL_PRICE': Feed('DispatchIS_Reports', 'PUBLIC_DISPATCHIS_', ('DISPATCH', 'LOCAL_PRICE')),
    'DISPATCH_INTERCONNECTION': Feed('DispatchIS_Reports', 'PUBLIC_DISPATCHIS_', ('DISPATCH', 'INTERCONNECTION')),
    'DISPATCH_UNIT_SCADA': Feed('Dispatch_SCADA', 'PUBLIC_DISPATCHSCADA_', ('DISPATCH', 'UNIT_SCADA')),
    'TRADINGPRICE': Feed('TradingIS_Reports', 'PUBLIC_TRADINGIS_', ('TRADING', 'PRICE')),
    'TRADINGINTERCONNECT': Feed('TradingIS_Reports', 'PUBLIC_TRADINGIS_', ('TRADING', 'INTERCONNECTORRES')),
    'DISPATCHLOAD': Feed('Next_Day_Dispatch', 'PUBLIC_NEXT_DAY_DISPATCH_', ('DISPATCH', 'UNIT_SOLUTION')),
    'DISPATCHOFFERTRK': Feed('Next_Day_Dispatch', 'PUBLIC_NEXT_DAY_DISPATCH_', ('DISPATCH', 'OFFERTRK')),
    'ROOFTOP_PV_ACTUAL': Feed('ROOFTOP_PV/ACTUAL', 'PUBLIC_ROOFTOP_PV_ACTUAL_', ('ROOFTOP', 'ACTUAL')),
}


@dataclass
class File:
    url: str
    kind: str           # 'archive' or 'current'
    start: datetime     # first moment the file covers
    end: datetime       # end of its coverage (exclusive)
    size: int | None


def _stamp(digits: str) -> datetime:
    return datetime.strptime(digits.ljust(14, '0')[:14], '%Y%m%d%H%M%S')


def _files(feed: Feed, area: str) -> list[File]:
    url = f'{REPORTS}{area}/{feed.family}/'
    links = nemweb._listing(url, ttl=CURRENT_TTL if area == 'CURRENT' else nemweb.LISTING_TTL)
    series: dict[str, list[tuple[datetime, datetime | None, str]]] = {}
    for link in links:
        name = urllib.parse.unquote(urllib.parse.urlsplit(link).path).rsplit('/', 1)[-1]
        if not name.upper().startswith(feed.prefix) or not name.lower().endswith('.zip'):
            continue
        rest = name[len(feed.prefix):-4]
        stamps = re.findall(r'(?<![0-9])(\d{8}(?:\d{4}(?:\d{2})?)?)(?![0-9])', rest)
        if not stamps:
            continue
        key = re.sub(r'\d', '', rest)  # e.g. MEASUREMENT_ vs SATELLITE_ series
        start = _stamp(stamps[0])
        # A second 8-digit date is an explicit inclusive end (weekly archives); long numbers are sequence ids.
        end = _stamp(stamps[1]) + timedelta(days=1) if area == 'ARCHIVE' and len(stamps) > 1 and len(stamps[1]) == 8 else None
        series.setdefault(key, []).append((start, end, link))
    files = []
    for entries in series.values():
        entries.sort()
        for i, (start, end, link) in enumerate(entries):
            if end is None:
                # Covers until the next archive file; the last one by the typical gap.
                gap = (entries[-1][0] - entries[-2][0]) if len(entries) > 1 else timedelta(days=1)
                end = entries[i + 1][0] if i + 1 < len(entries) else start + gap
            files.append(File(link, area.lower(), start, end, nemweb.file_size(link)))
    return sorted(files, key=lambda f: f.start)


def latest_loaded(db: duckdb.DuckDBPyConnection, table: str) -> datetime | None:
    column = catalog.time_column(table)
    cursor = db.cursor()
    try:
        exists = cursor.execute("SELECT count(*) FROM duckdb_tables() WHERE schema_name = 'main' AND table_name = ?",
                                [table]).fetchone()[0]
        if not exists or column is None:
            return None
        return cursor.execute(f'SELECT max({_q(column)}) FROM {_q(table)}').fetchone()[0]
    finally:
        cursor.close()


def plan(db: duckdb.DuckDBPyConnection, table: str, monthly_latest: str) -> dict:
    """Files needed to bring ``table`` up to date after the monthly archive (or its latest row)."""
    feed = FEEDS.get(table)
    if feed is None:
        return {'available': False}
    month_end = datetime.combine(date.fromisoformat(monthly_latest + '-01'), time())
    month_end = (month_end + timedelta(days=32)).replace(day=1)  # first day after the latest monthly archive
    loaded = latest_loaded(db, table)
    since = max(loaded, month_end) if loaded else month_end
    archive = [f for f in _files(feed, 'ARCHIVE') if f.end > since]
    archive_end = max((f.end for f in archive), default=since)
    current = [f for f in _files(feed, 'CURRENT') if f.start >= max(archive_end, since) - timedelta(minutes=5)]
    skipped = len(current) > CURRENT_LIMIT
    files = archive + ([] if skipped else current)
    through = max((f.end for f in files), default=None)
    return {
        'available': True, 'family': feed.family, 'since': since.isoformat(sep=' '), 'loaded': loaded and loaded.isoformat(sep=' '),
        'archive_files': len(archive), 'current_files': 0 if skipped else len(current),
        'skipped_current': len(current) if skipped else 0, 'through': through and through.isoformat(sep=' '),
        'size': sum(f.size or 0 for f in files), 'up_to_date': not files,
        'files': [{'url': f.url, 'kind': f.kind, 'start': f.start.isoformat(sep=' '), 'end': f.end.isoformat(sep=' ')}
                  for f in files],
    }


def load(db: duckdb.DuckDBPyConnection, table: str, files: list[dict], cache_dir: Path,
         progress: Callable[[dict], None] | None = None, cancel: threading.Event | None = None) -> None:
    """Load the planned report files for ``table``, one transaction per file (resumable)."""
    report = progress or (lambda event: None)
    feed = FEEDS[table]
    schema = replace(TABLES[table], dataset=feed.identity[0], csv_table=feed.identity[1])
    cache_dir.mkdir(parents=True, exist_ok=True)
    c = db.cursor()
    downloads = None
    try:
        c.execute('CREATE SCHEMA IF NOT EXISTS _nembot')
        c.execute('CREATE TABLE IF NOT EXISTS _nembot.files (url VARCHAR, sha256 VARCHAR, table_name VARCHAR, window_start TIMESTAMP, window_end TIMESTAMP, processed_at TIMESTAMP, PRIMARY KEY(url,sha256,table_name,window_start,window_end))')
        _create_table(c, schema, table)
        current = {}

        def on_bytes(url, n):  # only the file being waited for reports progress
            if current.get('url') == url:
                report({**current['event'], 'bytes': n})
        downloads = nemweb.download_many([f['url'] for f in files], cache_dir, progress=on_bytes, cancel=cancel)
        with tempfile.TemporaryDirectory(prefix='live-', dir=cache_dir) as scratch:
            for index, file in enumerate(files, 1):
                if cancel is not None and cancel.is_set():
                    raise nemweb.Cancelled('Download cancelled')
                url = file['url']
                event = {'stage': 'download', 'table': table, 'url': url, 'month': file['start'][:10],
                         'file': index, 'files': len(files), 'bytes': 0, 'size': nemweb.file_size(url)}
                current.update(url=url, event=event)
                report(event)
                _, path, digest = next(downloads)
                window = (datetime.fromisoformat(file['start']), datetime.fromisoformat(file['end']))
                if _completed(c, table, url, digest, *window):
                    continue
                report({**event, 'stage': 'load'})
                c.execute('BEGIN')
                try:
                    with tempfile.TemporaryDirectory(dir=scratch) as normalized:
                        _load_file(c, schema, table, path, Path(normalized), url, required=False)
                    _mark(c, table, url, digest, *window)
                    c.execute('COMMIT')
                except BaseException:
                    c.execute('ROLLBACK')
                    raise
        report({'stage': 'done'})
    finally:
        if downloads is not None:
            downloads.close()
        c.close()
