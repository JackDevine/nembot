"""What marimo's own AI is told about the data, for notebooks in nembot's notebooks folder.

marimo reads settings from the nearest ``pyproject.toml`` above a notebook, so when nembot starts its
marimo server it writes one into the notebooks folder: ``[tool.marimo.ai] rules`` carries a short NEM
background and the tables downloaded right now (marimo adds the rules to every AI prompt), and
``[tool.marimo.runtime] auto_instantiate`` makes notebooks run when they open. The user's own marimo
settings (models, keys) are untouched; the file is rewritten each time nembot starts marimo.
"""
import json
from pathlib import Path

from . import catalog, explorer

MAX_COLUMNS = 40
BUDGET = 24_000  # characters; past this, columns are listed without their descriptions

PRIMER = """This notebook works with nembot, a local copy of AEMO's public MMS data for Australia's National
Electricity Market (NEM).

Getting data: `from nembot.client import query`, then `query(sql)` runs one read-only DuckDB SELECT against
nembot's database and returns a polars DataFrame. (`mo.sql` uses marimo's own in-memory DuckDB and does not
see these tables.) Only the tables listed below are downloaded; more can be downloaded from nembot's chat or
Explorer.

NEM basics: five regions (NSW1, QLD1, SA1, TAS1, VIC1). Dispatch runs every 5 minutes; timestamps such as
SETTLEMENTDATE and INTERVAL_DATETIME mark the END of the interval, in market time (AEST, UTC+10, no daylight
saving). RRP is the regional reference price in $/MWh (it can be negative, and is capped at the market price
cap). DUID identifies a dispatchable unit; SCADAVALUE is a unit's measured output in MW, so MWh in an interval
is MW / 12. Effective-dated tables (e.g. DUDETAILSUMMARY with START_DATE/END_DATE) hold one row per period a
setting applied. A volume weighted price is sum(RRP * MW) / sum(MW) over the same intervals.
"""


def background(db, describe_columns: bool = True) -> str:
    lines = [PRIMER, 'Downloaded tables (name: description; rows; time range; columns):']
    cursor = db.cursor()
    try:
        overview = explorer.overview(cursor)
        tables = [t['name'] for p in overview['packages'] for t in p['tables'] if t['downloaded']]
        for name in tables:
            info, detail = catalog.tables()[name], explorer.table(cursor, name)
            span = f"; {detail['time_column']} {detail['range'][0][:16]} to {detail['range'][1][:16]}" if detail['range'] else ''
            columns = info['columns'][:MAX_COLUMNS]
            described = ', '.join(f"{c['name']} {c['type']}" + (f" ({c['comment'][:70]})" if describe_columns
                                                                  and c.get('comment') else '') for c in columns)
            more = f', and {len(info["columns"]) - MAX_COLUMNS} more columns' if len(info['columns']) > MAX_COLUMNS else ''
            lines.append(f"- {name}: {info['comment']}; {detail['rows']:,} rows{span}.\n  Columns: {described}{more}.")
        if not tables:
            lines.append('- none yet')
    finally:
        cursor.close()
    text = '\n'.join(lines)
    return background(db, describe_columns=False) if describe_columns and len(text) > BUDGET else text


def write_marimo_project(directory: Path, db) -> Path:
    """Write the notebooks folder's pyproject.toml (marimo settings for nembot's notebooks)."""
    path = Path(directory) / 'pyproject.toml'
    path.parent.mkdir(parents=True, exist_ok=True)
    rules = background(db)
    path.write_text(
        '# Written by nembot when it starts marimo, for the notebooks in this folder; edits are overwritten.\n'
        '[tool.marimo.runtime]\nauto_instantiate = true\n\n'
        f'[tool.marimo.ai]\nrules = {json.dumps(rules)}\n')  # a JSON string is a valid TOML basic string
    return path
