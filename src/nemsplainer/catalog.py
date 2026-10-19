"""The MMS table catalogue generated from AEMO's Electricity Data Model Report.

`catalog.json` is built by `scripts/build_catalog.py` from the report PDF and
committed; this module only reads it. One data-model version is used for both
documentation and loader schemas, so archive files from months that follow an
older version can fail validation (by design, with a clear error).
"""
from functools import cache
import json
from pathlib import Path
import re

PATH = Path(__file__).with_name('catalog.json')


def duckdb_type(oracle: str) -> str | None:
    """Map a data-model (Oracle) column type to the DuckDB type the loader uses."""
    kind = oracle.upper().replace(' ', '')
    if kind == 'DATE' or re.fullmatch(r'TIMESTAMP(\(\d+\))?', kind):
        return 'TIMESTAMP'  # MMS DATE values carry a time of day
    if re.fullmatch(r'(N?VARCHAR2?|N?CHAR)(\(\d+(CHAR|BYTE)?\))?|N?CLOB|LONG', kind):
        return 'VARCHAR'
    if kind in ('NUMBER', 'FLOAT') or re.fullmatch(r'FLOAT\(\d+\)', kind):
        return 'DOUBLE'  # no declared precision
    if match := re.fullmatch(r'NUMBER\((\d+)(?:,(-?\d+))?\)', kind):
        precision, scale = int(match[1]), int(match[2] or 0)
        if 0 < precision <= 38 and 0 <= scale <= precision:
            return f'DECIMAL({precision},{scale})'
        return 'DOUBLE'
    return None


@cache
def load() -> dict:
    return json.loads(PATH.read_text())


def tables() -> dict[str, dict]:
    return load()['tables']


def source() -> dict:
    return load()['source']


# Timestamp keys that mark when a reference row takes effect, rather than an interval.
_EFFECTIVE = {'EFFECTIVEDATE', 'VALIDFROM', 'START_DATE', 'STARTDATE', 'EFFECTIVE_END_DATETIME'}


def time_column(name: str) -> str | None:
    """The table's main timestamp: its first TIMESTAMP primary-key column (not LASTCHANGED)."""
    entry = tables()[name]
    types = {c['name']: c['type'] for c in entry['columns']}
    return next((k for k in entry['primary_key'] if types.get(k) == 'TIMESTAMP' and k != 'LASTCHANGED'), None)


def kind(name: str) -> str:
    """'interval' (time series), 'effective' (effective-dated reference) or 'reference'."""
    column = time_column(name)
    if column is None:
        return 'reference'
    return 'effective' if column in _EFFECTIVE else 'interval'
