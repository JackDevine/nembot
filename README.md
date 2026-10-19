# nemsplainer

Load public AEMO MMS data into a local DuckDB database.

`load_mms` downloads monthly AEMO MMSDM archive ZIPs (June–August 2026 was the
initial verified window), parses the MMS C/I/D CSV envelope and loads four
tables — DISPATCHPRICE, DISPATCHREGIONSUM, DISPATCH_UNIT_SCADA and
DUDETAILSUMMARY — into a persistent DuckDB database. DUDETAILSUMMARY is loaded
as effective-dated reference history, not interval data.

## Python API

```python
from nemsplainer import load_mms

connection = load_mms(
    ["DISPATCHPRICE", "DISPATCHREGIONSUM", "DISPATCH_UNIT_SCADA", "DUDETAILSUMMARY"],
    "2026-06-01",
    "2026-08-31",
    database="data/nem.duckdb",
    cache_dir="data/cache",
)
# connection is an open duckdb.DuckDBPyConnection you own; close it when done.
connection.close()
```

- `tables` — list of MMS table names (the four above).
- `start_date` / `end_date` — inclusive calendar dates (UTC+10 market days) as
  `YYYY-MM-DD` strings or `datetime.date`; omit both to load the latest three
  complete published months.
- `database` / `cache_dir` — file paths; DuckDB allows only one writable
  connection per database file per process.

## CLI

```bash
uv run nemsplainer --start 2026-06-01 --end 2026-08-31
uv run nemsplainer --config nemsplainer.toml
```

Flags: `--tables`, `--start`, `--end`, `--database`, `--cache-dir`, `--config`.
TOML config accepts the same keys (`tables`, `start`, `end`, `database`,
`cache_dir`); CLI flags override config values.

## Web chat

```bash
uv run nemsplainer-chat   # http://127.0.0.1:8000
```

A localhost chat analyst over `data/nem.duckdb` (opened read-only) that
answers with SQL tables and interactive Vega-Lite plots. Pick the model next to
the chat input: any Claude model via the Anthropic API (credentials from
`ant auth login` or `ANTHROPIC_API_KEY`), or any tool-capable OpenRouter model
(`OPENROUTER_API_KEY`). You can switch models mid-conversation; text-only
models still make plots but can't see them. Flags: `--database`, `--host`,
`--port`.

## Behavior

- Data is appended cumulatively; reruns of the same window are idempotent.
- Downloads are cached in `cache_dir` and reused on reruns.
- Rows are keyed by each table's official primary key (e.g. DISPATCHPRICE:
  `DISPATCHINTERVAL, INTERVENTION, REGIONID, RUNNO, SETTLEMENTDATE`); later
  `LASTCHANGED` corrections update existing rows, equal-timestamp conflicts
  abort the affected file's transaction.
- Source URLs, SHA256 hashes and per-request completion windows are recorded in
  the `_nemsplainer.files` table.
- This build performs a single run and exits; there is no automatic polling.
  Data freshness is bounded by AEMO's monthly publication schedule.
