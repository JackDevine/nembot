"""Monthly MMS archive discovery, validated caching, and streaming CSV envelopes."""

from collections.abc import Callable, Sequence
from contextlib import ExitStack
import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
from html.parser import HTMLParser
from http.client import HTTPException
import io
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib

from .tables import Table

ROOT = 'https://www.nemweb.com.au/Data_Archive/Wholesale_Electricity/MMSDM/'

# Courtesy towards AEMO: every request goes through _polite(), which allows one
# request at a time process-wide, spaces requests out, identifies us honestly,
# and backs off on throttling or server errors. Listings are cached briefly and
# ZIPs indefinitely (see download), so reruns cost NEMWEB almost nothing.
USER_AGENT = 'nemsplainer/0.1 (personal NEM analysis tool; sequential, cached downloads)'
REQUEST_INTERVAL = float(os.environ.get('NEMSPLAINER_REQUEST_INTERVAL', '1.0'))  # seconds between requests
LISTING_TTL = 3600
MAX_ATTEMPTS = 4
BACKOFF = 5.0  # seconds; doubles each retry unless the server sends Retry-After
_RETRY_STATUS = {429, 500, 502, 503, 504}
_lock = threading.RLock()
_last_request = 0.0
_listings: dict[str, tuple[float, list[str]]] = {}


def clear_cache() -> None:
    _listings.clear()
    _sizes.clear()


def _retry_delay(exc: Exception, attempt: int) -> float | None:
    """Seconds to wait before retrying, or None if the error is not transient."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code not in _RETRY_STATUS:
            return None
        retry_after = exc.headers.get('Retry-After') if exc.headers else None
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), 300.0)
    elif not isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError, HTTPException)):
        return None
    return BACKOFF * 2 ** attempt


def _polite[T](url: str, consume: Callable[[object], T]) -> T:
    """Open ``url`` and hand the response to ``consume``, politely and with retries."""
    global _last_request
    with _lock:
        for attempt in range(MAX_ATTEMPTS):
            wait = _last_request + REQUEST_INTERVAL - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
                with urllib.request.urlopen(request, timeout=60) as response:
                    return consume(response)
            except Exception as exc:
                delay = _retry_delay(exc, attempt)
                if delay is None or attempt == MAX_ATTEMPTS - 1:
                    raise
                time.sleep(delay)
            finally:
                _last_request = time.monotonic()
        raise AssertionError('unreachable')


class _Unavailable(RuntimeError):
    """A listed month lacks one or more requested table archives."""


class Cancelled(Exception):
    """The caller cancelled a download (deliberately not a RuntimeError)."""


_sizes: dict[str, int] = {}  # file URL -> size in bytes, from directory listings


def file_size(url: str) -> int | None:
    return _sizes.get(url)


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []
        self.sizes = {}  # IIS listings print "<date> <time> <bytes> <A HREF=...>"
        self._text = ''

    def handle_data(self, data):
        self._text += data

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'a':
            for key, value in attrs:
                if key.lower() == 'href' and value is not None:
                    self.hrefs.append(value)
                    if match := re.search(r'(\d+)\s*$', self._text):
                        self.sizes[value] = int(match[1])
        self._text = ''


def _listing(url: str, ttl: float = LISTING_TTL) -> list[str]:
    cached = _listings.get(url)
    if cached and time.monotonic() - cached[0] < ttl:
        return cached[1]
    try:
        text = _polite(url, lambda response: response.read().decode('utf-8-sig'))
    except urllib.error.HTTPError as exc:
        error = _Unavailable if exc.code == 404 else RuntimeError
        raise error(f'{url}: cannot read monthly listing: {exc}') from exc
    except (OSError, UnicodeError, HTTPException, zlib.error) as exc:
        raise RuntimeError(f'{url}: cannot read monthly listing: {exc}') from exc
    parser = _Links()
    parser.feed(text)
    base = urllib.parse.urlsplit(url)
    base_path = urllib.parse.unquote(base.path)
    links = set()
    for href in parser.hrefs:
        # IIS listings sometimes include literal filename # characters.
        joined = urllib.parse.urljoin(url, href.replace('#', '%23'))
        size = parser.sizes.get(href)
        parts = urllib.parse.urlsplit(joined)
        path = urllib.parse.unquote(parts.path)
        if (parts.scheme != base.scheme or parts.netloc != base.netloc
                or parts.query or parts.fragment or not path.startswith(base_path)):
            continue
        child = path[len(base_path):]
        if not child or '/' in child.rstrip('/') or child.rstrip('/') in ('.', '..'):
            continue
        # Canonical escaping also prevents encoded slashes/traversal escaping scope.
        link = urllib.parse.urlunsplit((parts.scheme, parts.netloc, urllib.parse.quote(path, safe='/'), '', ''))
        links.add(link)
        if size is not None:
            _sizes[link] = size
    _listings[url] = (time.monotonic(), sorted(links))
    return sorted(links)


def _files(year: int, month: int, tables: Sequence[str]) -> dict[str, list[str]]:
    directory = (f'{ROOT}{year:04d}/MMSDM_{year:04d}_{month:02d}/'
                 'MMSDM_Historical_Data_SQLLoader/DATA/')
    result = {name: [] for name in tables}
    patterns = {
        name: re.compile(
            rf'(?:PUBLIC_ARCHIVE\#{re.escape(name)}\#FILE[0-9]+\#[0-9]+|'
            rf'PUBLIC_DVD_{re.escape(name)}_[0-9]+(?:_[0-9]+|_FILE[0-9]+)*)\.zip',
            re.IGNORECASE,
        )
        for name in tables
    }
    for url in _listing(directory):
        filename = urllib.parse.unquote(urllib.parse.urlsplit(url).path).rsplit('/', 1)[-1]
        for name, pattern in patterns.items():
            if pattern.fullmatch(filename):
                result[name].append(url)
    return result


def month_files(year: int, month: int, tables: Sequence[str]) -> dict[str, list[str]]:
    """Return every listed part, or fail before ingestion for unavailable tables."""
    date(year, month, 1)
    try:
        result = _files(year, month, tables)
    except _Unavailable as exc:
        raise _Unavailable(f'{year:04d}-{month:02d} {", ".join(tables)}: {exc}') from exc
    missing = [name for name, urls in result.items() if not urls]
    if missing:
        raise _Unavailable(f'{year:04d}-{month:02d}: no monthly files for {", ".join(missing)}')
    return result


def latest_month(tables: Sequence[str]) -> date:
    """Find the latest published month common to all requested tables."""
    years = []
    for url in _listing(ROOT):
        name = urllib.parse.urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1]
        if url.endswith('/') and re.fullmatch(r'[0-9]{4}', name):
            years.append((int(name), url))
    for year, url in sorted(years, reverse=True):
        months = []
        for month_url in _listing(url):
            name = urllib.parse.urlsplit(month_url).path.rstrip('/').rsplit('/', 1)[-1]
            match = re.fullmatch(rf'MMSDM_{year:04d}_([0-9]{{2}})', name)
            if month_url.endswith('/') and match and 1 <= int(match[1]) <= 12:
                months.append(int(match[1]))
        for month in sorted(set(months), reverse=True):
            try:
                month_files(year, month, tables)
            except _Unavailable:
                continue
            return date(year, month, 1)
    raise RuntimeError(f'{ROOT}: no published month common to {", ".join(tables)}')


def _validate_zip(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        bad = archive.testzip()
        if bad is not None:
            raise RuntimeError(f'ZIP CRC failed for member {bad}')
        if not archive.infolist():
            raise RuntimeError('empty ZIP archive')
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def download(url: str, cache_dir: Path, progress: Callable[[int], None] | None = None,
             cancel: threading.Event | None = None) -> tuple[Path, str]:
    """Stream to an incomplete path; publish only after ZIP CRC validation.

    ``progress`` receives the bytes written so far; setting ``cancel`` aborts
    the transfer with :class:`Cancelled` (the partial file is discarded).
    """
    path = Path(cache_dir) / (hashlib.sha256(url.encode('utf-8')).hexdigest() + '.zip')
    partial = path.with_suffix('.zip.part')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                return path, _validate_zip(path)
            except (OSError, RuntimeError, zipfile.BadZipFile, EOFError, zlib.error) as exc:
                path.unlink(missing_ok=True)
                raise RuntimeError(f'invalid cached ZIP removed: {exc}') from exc
        def save(response):
            written = 0
            with partial.open('wb') as target:
                while chunk := response.read(1024 * 1024):
                    if cancel is not None and cancel.is_set():
                        raise Cancelled(f'{url}: download cancelled')
                    target.write(chunk)
                    written += len(chunk)
                    if progress:
                        progress(written)

        _polite(url, save)  # a retry rewrites the partial file from scratch
        digest = _validate_zip(partial)
        partial.replace(path)
        return path, digest
    except (OSError, RuntimeError, zipfile.BadZipFile, EOFError, HTTPException, zlib.error) as exc:
        raise RuntimeError(f'{url}: download/cache failure: {exc}') from exc
    finally:
        partial.unlink(missing_ok=True)


def _value(value: str, kind: str) -> None:
    """Reject incompatible supplied values even outside the requested date range."""
    if not value or kind == 'VARCHAR':
        return
    if kind == 'DOUBLE':
        if not math.isfinite(float(value)):
            raise ValueError('non-finite number')
        return
    if kind == 'TIMESTAMP':
        # TIMESTAMP(n) columns carry fractional seconds, e.g. 2023/03/15 08:56:34.657.
        if not re.fullmatch(r'[0-9]{4}/[0-9]{2}/[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?', value):
            raise ValueError('expected YYYY/MM/DD HH:MM:SS[.fff]')
        datetime.fromisoformat(value.replace('/', '-'))
        return
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError('non-finite decimal')
    precision, scale = map(int, kind.removeprefix('DECIMAL(').removesuffix(')').split(','))
    if abs(number) >= Decimal(10) ** (precision - scale):
        raise ValueError(f'number exceeds {kind}')
    # No silent rounding of excess nonzero fractional digits.
    _, digits, exponent = number.as_tuple()
    excess = -exponent - scale
    if excess > 0 and any(digits[max(0, len(digits) - excess):]):
        raise ValueError(f'number has excess fractional digits for {kind}')


def _csv_members(source: zipfile.ZipFile, prefix: str = ''):
    """Yield (name, open function) for every CSV, descending into nested ZIPs.

    Daily report archives hold one inner ZIP per 5-minute interval.
    """
    for member in source.infolist():
        if member.is_dir():
            continue
        name = prefix + member.filename
        if member.filename.lower().endswith('.zip'):
            with zipfile.ZipFile(io.BytesIO(source.read(member))) as inner:
                yield from _csv_members(inner, name + ' / ')
        elif member.filename.lower().endswith('.csv'):
            yield name, (lambda member=member: source.open(member))
        else:
            raise RuntimeError(f'{name}: unexpected non-CSV ZIP member')


def normalize(archive: Path, table: Table, directory: Path, url: str,
              required: bool = True) -> list[tuple[Path, tuple[str, ...], str]]:
    """Validate complete C/I/D members, returning one CSV per selected table version and header.

    With ``required=False`` an archive without the table yields no outputs
    instead of failing (report files carry several tables, not always all).
    """
    outputs = []
    writers = {}  # (version, header columns) -> header details, shared across members
    context = f'{url} / {table.name}'
    # Tables without a known CSV identity take it from the archive's first I
    # record; archives for these tables are expected to hold one table only.
    identity = [table.dataset, table.csv_table] if table.dataset else None
    try:
        with zipfile.ZipFile(archive) as source, ExitStack() as handles:
            for member_name, open_member in _csv_members(source):
                context = f'{url} / {member_name} / {table.name}'
                headers = {}
                ended = False
                count = 0
                with open_member() as raw, io.TextIOWrapper(raw, encoding='utf-8-sig', newline='') as text:
                    rows = csv.reader(text, strict=True)
                    for count, row in enumerate(rows, 1):
                        context = f'{url} / {member_name} / {table.name} / record {count}'
                        if not row:
                            raise RuntimeError(f'{context}: empty envelope record')
                        if ended:
                            raise RuntimeError(f'{context}: record after END OF REPORT')
                        if count == 1 and (row[0] != 'C' or row[1:2] == ['END OF REPORT']):
                            raise RuntimeError(f'{context}: missing opening C record')
                        if row[0] == 'C':
                            if row[1:2] == ['END OF REPORT']:
                                if len(row) != 3 or not row[2].isascii() or not row[2].isdigit() or int(row[2]) != count:
                                    raise RuntimeError(f'{context}: invalid END OF REPORT count')
                                ended = True
                            continue
                        if identity is None:
                            if row[0] != 'I':
                                raise RuntimeError(f'{context}: data before the first I header')
                            identity = row[1:3]
                        if row[1:3] != identity:
                            if not table.dataset:
                                raise RuntimeError(f'{context}: archive holds more than one MMS table '
                                                   f'({"/".join(identity)} and {"/".join(row[1:3])}), so the '
                                                   f'{table.name} records cannot be identified')
                            continue
                        version = row[3] if len(row) > 3 else '<missing>'
                        context += f' / version {version}'
                        if not re.fullmatch(r'[0-9]+', version):
                            raise RuntimeError(f'{context}: non-integer table version')
                        if row[0] == 'I':
                            columns = tuple(row[4:])
                            if len(columns) != len(set(columns)):
                                raise RuntimeError(f'{context}: duplicate header columns')
                            unknown = set(columns) - table.columns.keys()
                            if unknown:
                                raise RuntimeError(f'{context}: unknown columns {sorted(unknown)}')
                            missing = set(table.primary_key) - set(columns)
                            if missing:
                                raise RuntimeError(f'{context}: missing primary key columns {sorted(missing)}')
                            if (version, columns) not in writers:
                                supplied = tuple(name for name in table.columns if name in columns)
                                order = tuple(columns.index(name) + 4 for name in supplied)
                                key_positions = tuple(columns.index(name) + 4 for name in table.primary_key)
                                output = handles.enter_context(tempfile.NamedTemporaryFile(
                                    mode='w', encoding='utf-8', newline='', suffix='.csv',
                                    prefix='normalized-', dir=directory, delete=False))
                                writer = csv.writer(output)
                                writer.writerow(supplied)
                                outputs.append((Path(output.name), supplied, context))
                                writers[version, columns] = (len(row), order, key_positions, writer, supplied)
                            headers[version] = writers[version, columns]
                        elif row[0] == 'D':
                            if version not in headers:
                                raise RuntimeError(f'{context}: selected data before matching I header')
                            width, order, keys, writer, supplied = headers[version]
                            if len(row) != width:
                                raise RuntimeError(f'{context}: wrong row width: expected {width}, got {len(row)}')
                            for position, key in zip(keys, table.primary_key):
                                if row[position] == '':
                                    raise RuntimeError(f'{context}: NULL primary key column {key}')
                            values = [row[position] for position in order]
                            for name, value in zip(supplied, values):
                                try:
                                    _value(value, table.columns[name])
                                except (ValueError, InvalidOperation) as exc:
                                    raise RuntimeError(f'{context} / column {name}: invalid {table.columns[name]} value {value!r}: {exc}') from exc
                            writer.writerow(values)
                        else:
                            raise RuntimeError(f'{context}: unexpected selected record kind {row[0]!r}')
                if not ended:
                    raise RuntimeError(f'{context}: missing END OF REPORT')
        if not outputs and required:
            raise RuntimeError(f'{url} / {table.name}: no matching I header in archive')
        return outputs
    except (OSError, UnicodeError, csv.Error, zipfile.BadZipFile, EOFError, RuntimeError, zlib.error) as exc:
        for path, _, _ in outputs:
            path.unlink(missing_ok=True)
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f'{context}: cannot normalize archive: {exc}') from exc
