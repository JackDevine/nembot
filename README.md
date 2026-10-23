<img src="src/nembot/static/logo.svg" alt="nembot logo: a robot head with three lightning bolts" width="96">

# nembot

Load public AEMO MMS data into a local DuckDB database.

`load_mms` downloads monthly AEMO MMSDM archive ZIPs (June–August 2026 was the
initial verified window), parses the MMS C/I/D CSV envelope and loads four
tables — DISPATCHPRICE, DISPATCHREGIONSUM, DISPATCH_UNIT_SCADA and
DUDETAILSUMMARY — into a persistent DuckDB database. DUDETAILSUMMARY is loaded
as effective-dated reference history, not interval data.

## Python API

```python
from nembot import load_mms

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
uv run nembot --start 2026-06-01 --end 2026-08-31
uv run nembot --config nembot.toml
```

Flags: `--tables`, `--start`, `--end`, `--database`, `--cache-dir`, `--config`.
TOML config accepts the same keys (`tables`, `start`, `end`, `database`,
`cache_dir`); CLI flags override config values.

## Web chat

```bash
uv run nembot-chat   # http://127.0.0.1:8000
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

## Tables that feel like marimo

Every table in nembot, whether chat results, the Explorer, Snippets or
plugins, uses a port of [marimo](https://marimo.io)'s data table. If you like
how marimo shows a dataframe, you get the same experience here:
- bold column names with the data type underneath
- sort on hover, plus a column menu (Asc / Desc / Clear sort / Copy column name)
- right-aligned tabular numbers at full precision, and `None` pills
- search, and Export to CSV / JSON / Markdown
- the familiar "1,234 rows, 5 columns · 10 / page · Page 1 of N" footer
- **column summaries** under each header:
  - for numbers: a small histogram (9 bins; hover a bar for its range and count,
    with axis labels at the min, quartiles, p95 and max)
  - for dates: a histogram with the date range underneath
  - `unique: N` for text, and true/false counts for booleans

  They follow your search and filters. As in marimo they appear for tables of
  11 rows or more. marimo stops drawing charts above 20,000 rows; here they're
  computed in DuckDB, so you get them for tables up to 1,000,000 rows.
- **drag across cells** to highlight them in green, with Count / Sum / Average
  for the selection in the footer. Shift-click and Shift+arrows extend the
  selection, Ctrl/Cmd+A selects the page, and Ctrl/Cmd+C copies it as
  tab-separated text.
- **filters** as pills above the table:
  - set them from the column menu (Filter / Filter by values) or by
    right-clicking a cell (Filter by this value / Remove rows with this value)
  - marimo's operators per column type: between, comparisons, contains, starts
    with, regex, is in / not in with value counts, is null…
- a **Columns** button to show and hide columns, **Wrap text** per column, and
  an **Explore** panel that shows one row at a time

The table was copied from marimo's open-source code (Apache-2.0) and rewritten
as plain JavaScript, with no React; see [Acknowledgements](#acknowledgements).

## Plugins and Creator mode

nembot can be extended with **plugins**: new chat tools, tabs, buttons on
results and HTTP endpoints. A plugin is a folder in `plugins/` with a
`plugin.toml` and any of `plugin.py` (server: tools, routes, a short note for the
assistant), `client.js` (browser: tabs, renderers, buttons) and `style.css`. The
**Plugins** tab lists them, with their status, errors and an on/off switch.
`plugins/price-spikes` is a hand-written example, and `plugins/negative-prices`
was built entirely in Creator mode.

To build one, switch the chat to **Creator** mode (the toggle next to the model
button) and describe the feature. Creator is a separate mode, so the analyst's
prompt never fills up with app-building instructions. In Creator mode the
assistant:
1. inspects the running app and your data
2. writes the plugin as a draft in `plugins/.drafts/`
3. tests it in a separate process (catching load errors, tool errors and
   JavaScript syntax errors)
4. asks you before installing it

Plugins run on your computer with nembot's permissions, so only install
code you trust. Installed plugins are backed up before being replaced.

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
For some tables (dispatch prices, regional
summaries, interconnector flows, unit SCADA and dispatch targets, trading
prices, rooftop PV) **Get latest** also fetches AEMO's recent daily and
5-minute reports, so you can have data up to the last few minutes rather than
the end of the last monthly archive.
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
  the `_nembot.files` table.
- This build performs a single run and exits; there is no automatic polling.
  Data freshness is bounded by AEMO's monthly publication schedule.

## Data source and terms

> Contains data from the Australian Energy Market Operator (AEMO), published on
> NEMWEB, used under AEMO's Copyright Permissions
> (aemo.com.au/privacy-and-legal-notices/copyright-permissions). Processed by
> nembot; not endorsed by AEMO. AEMO gives no warranty of accuracy,
> completeness or currency; use at your own risk.

The same credit appears in the app (**Data: AEMO** in the header) and in
every exported report and notebook.

Downloads are polite but not slow:
- 2 connections at most by default (`NEMBOT_CONCURRENCY`, capped at 3),
  reused rather than reopened for every file
- requests paced by NEMWEB's response time (AutoThrottle-style), with
  `NEMBOT_REQUEST_INTERVAL` as an optional minimum gap
- on errors it drops to one request at a time with backoff (honouring
  `Retry-After`), and it stops after repeated HTTP 403s
- listings are cached for an hour and files are never downloaded twice

## Acknowledgements

- **marimo**: nembot's data tables are ported from
  [marimo](https://github.com/marimo-team/marimo)'s data table, Copyright 2026
  Marimo, licensed under the Apache License 2.0. The look, layout, colours,
  formatting and interactions come from marimo; the code was rewritten without
  React, TanStack Table or Tailwind. The notebook export also targets marimo.
  See `LICENSES/marimo-LICENSE.txt` and the header of
  `src/nembot/static/datatable.js`.
- **marimo, again**: SQL and plot specifications are syntax-highlighted with
  marimo's editor colours (its light theme and One Dark-style dark theme, read
  from marimo's code editor). SQL is parsed with the open-source parser
  marimo's editor uses, CodeMirror's Lezer SQL grammar (MIT), loaded from a CDN.
- **Lucide**: table icons from [Lucide](https://lucide.dev) (ISC licence), as
  used by marimo. See `LICENSES/lucide-LICENSE.txt`.
- **DeepSeek Harness**: the plugin architecture and the separate Creator mode
  for building plugins are modelled on [DeepSeek Harness](https://deepseek.com/en/harness/).
  No code was taken from it.
