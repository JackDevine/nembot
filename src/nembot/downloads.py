"""Background download jobs for the web app: one at a time, cancellable, with progress.

Jobs run `ingest.load_mms` on a cursor of the app's single read-write DuckDB
connection, so chat and explorer reads keep working (they see each archive
file once it commits). Jobs live in memory only: after a restart nothing
resumes until the user asks again (finished files are skipped on rerun).
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import itertools
import logging
from pathlib import Path
import queue
import re
import threading
import time

import duckdb

from . import catalog, live, nemweb
from .ingest import EXACT_WINDOW, MARKET_TIME, _month_shift, load_mms

log = logging.getLogger(__name__)
_ids = itertools.count(1)
FIRST_MONTH = date(2009, 7, 1)  # earliest MMSDM monthly archive


@dataclass
class Job:
    table: str
    start: date
    end: date
    reason: str = 'explorer'
    kind: str = 'archive'   # 'archive' (monthly MMSDM) or 'live' (recent report files)
    files: list = field(default_factory=list)  # live jobs: the planned report files
    id: int = field(default_factory=lambda: next(_ids))
    status: str = 'queued'  # queued, running, done, failed, cancelled
    progress: dict = field(default_factory=dict)
    error: str | None = None
    notes: list = field(default_factory=list)  # e.g. decimals rounded to the data model's scale
    created: float = field(default_factory=time.time)
    finished: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event)

    def public(self) -> dict:
        return {'id': self.id, 'table': self.table, 'start': self.start.isoformat(), 'end': self.end.isoformat(),
                'reason': self.reason, 'kind': self.kind, 'status': self.status, 'progress': self.progress, 'error': self.error,
                'notes': self.notes, 'created': self.created, 'finished': self.finished}


def month_range(start: str, end: str) -> tuple[date, date]:
    """'YYYY-MM' bounds -> first day of start month, last day of end month (not after today)."""
    if not (re.fullmatch(r'\d{4}-\d{2}', start or '') and re.fullmatch(r'\d{4}-\d{2}', end or '')):
        raise ValueError('months must be YYYY-MM')
    first = date.fromisoformat(start + '-01')
    last = _month_shift(date.fromisoformat(end + '-01'), 1) - timedelta(days=1)
    today = datetime.now(MARKET_TIME).date()
    if first > last:
        raise ValueError('the start month must not be after the end month')
    if first < FIRST_MONTH:
        raise ValueError(f'the monthly archive starts in {FIRST_MONTH:%Y-%m}')
    return first, min(last, today)


class Downloads:
    def __init__(self, db: duckdb.DuckDBPyConnection, database: Path, cache_dir: Path):
        self.db, self.database, self.cache_dir = db, database, cache_dir
        self.jobs: dict[int, Job] = {}
        self.queue: queue.Queue[Job] = queue.Queue()
        self.lock = threading.Lock()
        threading.Thread(target=self._worker, name='downloads', daemon=True).start()

    def submit(self, table: str, start: date, end: date, reason: str = 'explorer', *,
               kind: str = 'archive', files: list | None = None) -> Job:
        job = Job(table, start, end, reason, kind, files or [])
        with self.lock:
            self.jobs[job.id] = job
        self.queue.put(job)
        return job

    def cancel(self, job_id: int) -> Job | None:
        job = self.jobs.get(job_id)
        if job and job.status in ('queued', 'running'):
            job.cancel.set()
            if job.status == 'queued':
                job.status, job.finished = 'cancelled', time.time()
        return job

    def dismiss(self, job_id: int) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job and job.status not in ('queued', 'running'):
                del self.jobs[job_id]

    def list(self) -> list[dict]:
        with self.lock:
            return [job.public() for job in self.jobs.values()]

    def busy(self) -> bool:
        return any(job.status in ('queued', 'running') for job in self.jobs.values())

    def _worker(self):
        while True:
            job = self.queue.get()
            if job.cancel.is_set():
                continue
            job.status = 'running'

            def progress(event, job=job):
                if event.get('stage') == 'note':
                    job.notes.append(f"{event['url'].rsplit('/', 1)[-1]}: {event['note']}")
                    return
                job.progress = event

            try:
                if job.kind == 'live':
                    live.load(self.db, job.table, job.files, self.cache_dir, progress=progress, cancel=job.cancel)
                else:
                    load_mms([job.table], job.start, job.end, database=self.database, cache_dir=self.cache_dir,
                             connection=self.db, progress=progress, cancel=job.cancel)
                job.status = 'done'
            except nemweb.Cancelled:
                job.status = 'cancelled'
            except Exception as exc:  # shown prominently in the UI
                log.exception('download job %s failed', job.id)
                job.status, job.error = 'failed', str(exc)
            job.finished = time.time()


def next_month(month: str) -> str:
    return f"{_month_shift(date.fromisoformat(month + '-01'), 1):%Y-%m}"


def loaded_months(db: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    """Archive months ('YYYY-MM') already loaded for ``table``, from the file log."""
    cursor = db.cursor()
    try:
        exists = cursor.execute("SELECT count(*) FROM duckdb_tables() WHERE schema_name = '_nembot' "
                                "AND table_name = 'files'").fetchone()[0]
        if not exists:
            return []
        urls = [r[0] for r in cursor.execute('SELECT DISTINCT url FROM _nembot.files WHERE table_name = ?',
                                             [table]).fetchall()]
    finally:
        cursor.close()
    months = {f'{m[1]}-{m[2]}' for url in urls if (m := re.search(r'MMSDM_(\d{4})_(\d{2})', url))}
    return sorted(months)


def latest_published(table: str) -> str:
    month = nemweb.latest_month([table])  # listings are cached for an hour
    return f'{month:%Y-%m}'


def month_size(table: str, month: str) -> int | None:
    """Bytes of the table's archive files for one month (from the cached listing)."""
    year, number = map(int, month.split('-'))
    try:
        urls = nemweb.month_files(year, number, [table])[table]
    except RuntimeError:
        return None
    sizes = [nemweb.file_size(url) for url in urls]
    return sum(sizes) if all(s is not None for s in sizes) else None


def plan(db: duckdb.DuckDBPyConnection, table: str) -> dict:
    """What can be downloaded for ``table``: loaded months, latest month, refresh range, sizes."""
    loaded = loaded_months(db, table)
    latest = latest_published(table)
    refresh = None
    if loaded and loaded[-1] < latest:
        after = _month_shift(date.fromisoformat(loaded[-1] + '-01'), 1)
        refresh = {'start': f'{after:%Y-%m}', 'end': latest}
    return {
        'table': table, 'kind': catalog.kind(table), 'exact_days': table in EXACT_WINDOW,
        'loaded_months': loaded, 'latest_month': latest, 'first_month': f'{FIRST_MONTH:%Y-%m}',
        'refresh': refresh, 'up_to_date': bool(loaded) and refresh is None,
        'month_size': month_size(table, latest),
        'live': live_plan(db, table, latest),
    }


def live_plan(db, table: str, latest: str) -> dict:
    """Recent report files after the monthly archive, without the (long) file list."""
    if table not in live.FEEDS:
        return {'available': False}
    try:
        result = live.plan(db, table, latest)
    except RuntimeError as exc:
        return {'available': False, 'error': str(exc)}
    result.pop('files')
    return result


def submit_live(jobs: 'Downloads', db, table: str, reason: str = 'explorer') -> Job | None:
    """Queue a job for every report file newer than what's loaded; None if up to date."""
    result = live.plan(db, table, latest_published(table))
    if not result['files']:
        return None
    start = datetime.fromisoformat(result['files'][0]['start']).date()
    end = datetime.fromisoformat(result['files'][-1]['end']).date()
    return jobs.submit(table, start, end, reason, kind='live', files=result['files'])
