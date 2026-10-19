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

3. **The web chat** (`nemsplainer-chat`): a port of the notebook that can use any Claude model directly or any tool-capable OpenRouter model, switching freely mid-conversation.
   - `chat.py` — the provider-neutral core: system prompt (vision and text-only variants), tool schemas, DuckDB tool execution, and `run_turn` (the tool loop, sequential tool calls, rollback of a failed turn). History is stored in a **neutral format** (documented at the top of the file); plot PNGs are always stored and only sent to models that can read images.
   - `claude.py` / `openrouter.py` — each provider renders neutral history into its wire format and streams one assistant turn. Rendering must stay **deterministic per (history, current model)** so turns replay byte-identically (prompt caching, and Claude's preserved-thinking check). Same-model turns replay provider raw data (Claude content blocks with thinking; OpenRouter `reasoning_details`); turns from other models are rendered from neutral fields and prefixed `[Earlier reply by …]`. Claude per-model request features (adaptive thinking, `display: "updates"`, effort, `fallbacks: "default"`) are gated by `request_options()`.
   - `models.py` — the model catalogue (Anthropic models API + public OpenRouter `/models`, cached for an hour). Model refs are `claude:<id>` / `openrouter:<id>`.
   - `web.py` (Starlette) streams UI events as SSE to `static/index.html` (vega-embed, marked, DOMPurify from CDN). Conversations live in memory keyed by a browser-generated id. OpenRouter needs `OPENROUTER_API_KEY`.
   - The system prompt and tool behaviour are duplicated from `load_mms.py`; keep them in sync if one changes.

## Tests

`tests/test_chat.py` covers history rendering and the turn loop with a fake provider (no network). Loader tests run against real DuckDB in temp dirs with the network faked by patching `urllib.request.urlopen` to an in-memory NEMWEB server (`tests/test_ingest.py`: `MonthlyArchive` builds synthetic MMS ZIPs and listings). No test hits the real network.

## Reference

`NEMWEB_RESEARCH.md` documents NEMWEB layout, URL patterns, MMS CSV format and publication timing (monthly MMSDM data lags ~1 month; discover months from listings rather than assuming).
