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
models still make plots but can't see them. A **Web search** toggle lets the
model search the web for context (billed per search); sources are listed under
the reply. Flags: `--database`, `--host`,
`--port`.

If a question needs a table or months you haven't downloaded, the assistant asks
first: a card in the chat shows the table, why it's needed, the proposed months
(editable) and the download size. Approve it and the download runs in the
chat, after which the assistant re-runs its query.

## Snippets

Every table and plot in the chat has **☆ Add to snippets**. It saves the SQL
(and the Vega-Lite plot spec) to the `snippets` schema in your database, with
a name suggested by the current model that you can edit. The **Snippets** tab
lists them, and each one re-runs its SQL when you open it, so it always shows
the latest data.

## Reports and notebooks

Ask the chat for a report ("make this a PowerPoint deck", "export a PDF",
"give me a Jupyter notebook") and it builds one from the conversation's tables
and plots, re-running their SQL: HTML, PDF, Word, PowerPoint, Excel, Jupyter or
marimo. Files are saved in `data/reports/` and listed on the **Reports** tab.
Notebooks open in JupyterLab or marimo, which the app starts locally on
demand; they query the database through the running app.

## Explorer

The **Explorer** tab lists every table published in the MMSDM monthly archive,
grouped by package, using documentation parsed from AEMO's Electricity Data
Model Report (the version and a link to the PDF are shown). Tables you haven't
downloaded are faded. Open a table to see its description, keys and column
docs (hover over a column header), its row count and date range, when it was
downloaded, and a filterable, paged view of the data.

Use **Download…** on any table to fetch months from AEMO (with a size estimate;
some tables are many GB per month). **Refresh** fetches the months published
since your last download; tables with newer published data are marked with ●.
Downloads run in the background with progress and a Cancel button. The web app
holds the database open for writing, so stop it before using the CLI.

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
