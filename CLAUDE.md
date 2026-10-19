# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Python >=3.14, managed with `uv`. Tests use stdlib `unittest` (no pytest).

```bash
uv run python -m unittest discover -s tests                  # all tests (plain `-m unittest` finds none)
uv run python -m unittest discover -s tests -k test_name      # single test by name substring
uv run nemsplainer --start 2026-06-01 --end 2026-08-31       # load data (defaults: data/nem.duckdb, data/cache)
uv run nemsplainer-chat                                      # web chat at http://127.0.0.1:8000 (--port, --database)
uv run marimo edit load_mms.py                               # original notebook chat (OpenRouter)
```

`load_mms.py` imports `polars` and `pydantic_ai`, which are **not** declared in `pyproject.toml`; supply them (e.g. `uv run --with polars --with pydantic-ai ...`) to run the notebook. The web chat needs only declared deps plus Anthropic credentials (`ant auth login` profile or `ANTHROPIC_API_KEY`).

## Architecture

Three pieces share the DuckDB file `data/nem.duckdb` (`data/` and `__marimo__/` are gitignored):

1. **`src/nemsplainer/` — the ingest package** (public API: `load_mms`, CLI: `nemsplainer`).
   - `tables.py` — hand-written `Table` schemas (ordered columns → DuckDB types, MMS dataset/csv_table identity, official primary key) transcribed from AEMO's Electricity Data Model Report. Supporting a new MMS table means adding an entry here.
   - `nemweb.py` — scrapes MMSDM HTML directory listings to discover monthly per-table ZIPs, downloads them into `cache_dir` (validated, SHA256'd, reused on rerun), and `normalize()` strictly validates the MMS C/I/D CSV envelope (opening C record, END OF REPORT count, integer versions, known columns, PK present), emitting one clean CSV per `I` header segment.
   - `ingest.py` — orchestration. Market time is fixed UTC+10. Each (table, file) is loaded in its own transaction and recorded in `_nemsplainer.files` keyed by (url, sha256, table, window) so reruns skip completed work and interrupted runs resume. `_merge` stages typed CSVs, picks the latest `LASTCHANGED` per PK, raises on equal-`LASTCHANGED` conflicts (both within a file and against stored rows), and only updates rows with strictly newer `LASTCHANGED`.
   - `DUDETAILSUMMARY` is special-cased: it's effective-dated reference data, so all monthly snapshots are reconciled together in a temp `_reference` table *before* the date-window overlap filter is applied (otherwise later closures could resurrect old rows). Interval tables are window-filtered per file.
   - `cli.py` — argparse + TOML config (`--config`); config keys mirror flags, flags override config, and config is validated even when overridden.
   - `load_mms()` returns an open writable connection the caller must close; DuckDB allows only one writable connection per file per process.

2. **`load_mms.py` — a marimo notebook** (despite the name, it does not load data). It opens the DB `read_only=True` and runs a pydantic-ai agent (via OpenRouter, `OPENROUTER_API_KEY`) inside `mo.ui.chat`, exposing two tools: `query_mms(sql)` and `visualize_mms(sql, plot_spec)` (Vega-Lite validated with Altair before running SQL, rendered to PNG for the model). The `SYSTEM_PROMPT` cell defines agent behavior.

3. **`chat.py` + `web.py` + `static/index.html` — the web chat** (`nemsplainer-chat`), a port of the notebook to the Anthropic API. `chat.py` holds the system prompt, tool schemas and a manual streaming tool loop (`run_turn`) on `claude-opus-5-5` (adaptive thinking with `display: "updates"`, effort `medium`, `fallbacks: "default"` — both via beta headers — and auto prompt caching); it yields UI events (`text`, `progress`, `tool`, `plot`, `table`, `error`) that `web.py` (Starlette) sends as SSE to a single-page frontend (vega-embed, marked, DOMPurify from CDN). Conversations live in memory keyed by a browser-generated id; a turn that fails midway is rolled back so history never ends on an unanswered `tool_use`. The system prompt and tool behavior are duplicated from `load_mms.py` — keep them in sync if one changes.

## Tests

Tests run against real DuckDB in temp dirs with the network faked by patching `urllib.request.urlopen` to an in-memory NEMWEB server (`tests/test_ingest.py`: `MonthlyArchive` builds synthetic MMS ZIPs and listings). No test hits the real network.

## Reference

`NEMWEB_RESEARCH.md` documents NEMWEB layout, URL patterns, MMS CSV format and publication timing (monthly MMSDM data lags ~1 month; discover months from listings rather than assuming).
