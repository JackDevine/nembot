# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Python >=3.14, managed with `uv`. Tests use stdlib `unittest` (no pytest).

```bash
uv run python -m unittest discover -s tests                  # all tests (plain `-m unittest` finds none)
uv run python -m unittest discover -s tests -k test_name      # single test by name substring
uv run python scripts/build_catalog.py                       # rebuild catalog.json from the data-model PDF
uv run nemsplainer --start 2026-06-01 --end 2026-08-31       # load data (defaults: data/nem.duckdb, data/cache)
uv run nemsplainer-chat                                      # web chat at http://127.0.0.1:8000 (--port, --database)
uv run marimo edit load_mms.py                               # original notebook chat (OpenRouter)
```

`load_mms.py` imports `polars` and `pydantic_ai`, which are **not** declared in `pyproject.toml`; supply them (e.g. `uv run --with polars --with pydantic-ai ...`) to run the notebook. The web chat needs only declared deps plus Anthropic credentials (`ant auth login` profile or `ANTHROPIC_API_KEY`).

## Architecture

Three pieces share the DuckDB file `data/nem.duckdb` (`data/` and `__marimo__/` are gitignored):

1. **`src/nemsplainer/` — the ingest package** (public API: `load_mms`, CLI: `nemsplainer`).
   - `catalog.json` + `catalog.py` — the data-model catalogue for all 571 tables: package, comment, description, visibility, primary key, indexes, columns with Oracle and DuckDB types, and whether the latest MMSDM monthly archive publishes the table. It's generated from AEMO's Electricity Data Model Report **PDF** by `uv run python scripts/build_catalog.py` (pdfplumber is a dev dependency; the build takes about 2 minutes, needs network, and fails loudly via `problems`). Never hand-edit the JSON. One data-model version (currently 5.7) is used for both docs and loader schemas.
   - `tables.py` — `Table` schemas generated from the catalogue: ordered columns → DuckDB types, plus the primary key. The CSV envelope identity (dataset/csv_table) is only known for the four `DEFAULT_TABLES`; for other tables `nemweb.normalize` takes it from the archive's first `I` record and errors if an archive mixes tables. The loader accepts only `ingest.SUPPORTED` tables until its window filtering is generalised.
   - `nemweb.py` — all HTTP goes through `_polite()`: one request at a time process-wide, `REQUEST_INTERVAL` (default 1 s, env `NEMSPLAINER_REQUEST_INTERVAL`) between requests, an honest User-Agent, and backoff/`Retry-After` on 429/5xx/connection errors. Directory listings are cached in memory for an hour (tests call `nemweb.clear_cache()` and patch `REQUEST_INTERVAL` to 0). It scrapes MMSDM HTML directory listings to discover monthly per-table ZIPs, downloads them into `cache_dir` (validated, SHA256'd, reused on rerun), and `normalize()` strictly validates the MMS C/I/D CSV envelope (opening C record, END OF REPORT count, integer versions, known columns, PK present), emitting one clean CSV per `I` header segment.
   - `ingest.py` — orchestration. Market time is fixed UTC+10. Each (table, file) is loaded in its own transaction and recorded in `_nemsplainer.files` keyed by (url, sha256, table, window) so reruns skip completed work and interrupted runs resume. `_merge` stages typed CSVs, picks the latest `LASTCHANGED` per PK, raises on equal-`LASTCHANGED` conflicts (both within a file and against stored rows), and only updates rows with strictly newer `LASTCHANGED`.
   - `DUDETAILSUMMARY` is special-cased: it's effective-dated reference data, so all monthly snapshots are reconciled together in a temp `_reference` table *before* the date-window overlap filter is applied (otherwise later closures could resurrect old rows). Interval tables are window-filtered per file.
   - `cli.py` — argparse + TOML config (`--config`); config keys mirror flags, flags override config, and config is validated even when overridden.
   - `load_mms()` loads any table in `ingest.SUPPORTED` (every catalogue table the monthly archive publishes). `EXACT_WINDOW` tables (DISPATCHPRICE, DISPATCHREGIONSUM, DISPATCH_UNIT_SCADA) are filtered to the exact market days requested; every other table loads **whole archive months** (the file log records month windows), because timestamp meanings vary by table. Tables without `LASTCHANGED` are updated by the most recently loaded archive. Timestamps parse with or without milliseconds (`TIMESTAMP(n)` columns).
   - With no `connection=`, `load_mms()` opens and returns a writable connection the caller must close. With one (the web app), it runs on a cursor of that connection and returns it. `progress=` receives stage events and `cancel=` (a `threading.Event`) aborts with `nemweb.Cancelled`. Each file commits on its own, so finished files survive a cancel or a failure. DuckDB allows one writer per database file, so the CLI can't run while the web app has the database open.

2. **`load_mms.py` — a marimo notebook** (despite the name, it does not load data). It opens the DB `read_only=True` and runs a pydantic-ai agent (via OpenRouter, `OPENROUTER_API_KEY`) inside `mo.ui.chat`, exposing two tools: `query_mms(sql)` and `visualize_mms(sql, plot_spec)` (Vega-Lite validated with Altair before running SQL, rendered to PNG for the model). The `SYSTEM_PROMPT` cell defines agent behavior.

3. **The web chat** (`nemsplainer-chat`): a port of the notebook that can use any Claude model directly or any tool-capable OpenRouter model, switching freely mid-conversation.
   - `chat.py` — the provider-neutral core: system prompt (vision and text-only variants), tool schemas, DuckDB tool execution, and `run_turn` (the tool loop, sequential tool calls, rollback of a failed turn). History is stored in a **neutral format** (documented at the top of the file); plot PNGs are always stored and only sent to models that can read images.
   - `claude.py` / `openrouter.py` — each provider renders neutral history into its wire format and streams one assistant turn. Rendering must stay **deterministic per (history, current model)** so turns replay byte-identically (prompt caching, and Claude's preserved-thinking check). Same-model turns replay provider raw data (Claude content blocks with thinking; OpenRouter `reasoning_details`); turns from other models are rendered from neutral fields and prefixed `[Earlier reply by …]`. Claude per-model request features (adaptive thinking, `display: "updates"`, effort, `fallbacks: "default"`) are gated by `request_options()`.
   - Web search (UI toggle, default on) adds Anthropic's server-side `web_search`/`web_fetch` tools (`_20260209` dynamic-filtering versions where supported, gated in `claude.web_tools()`) or OpenRouter's `openrouter:web_search` server tool. Neither runs in our tool loop. Sources are emitted as a `sources` event: Claude citations, or the consulted pages when dynamic filtering leaves the text uncited.
   - `models.py` — the model catalogue (Anthropic models API + public OpenRouter `/models`, cached for an hour). Model refs are `claude:<id>` / `openrouter:<id>`.
   - `downloads.py` — in-memory download jobs run one at a time by a worker thread via `load_mms` on the app's single read-write connection (chat/explorer reads use their own cursors and see each committed file). It also provides `plan()` (loaded months from `_nemsplainer.files` URLs, latest published month, refresh range, size estimate from listing sizes) and the freshness flags shown in the explorer. Jobs are not resumed after a restart.
   - `web.py` (Starlette) streams UI events as SSE to `static/index.html` (vega-embed, marked, DOMPurify from CDN). Conversations live in memory keyed by a browser-generated id. OpenRouter needs `OPENROUTER_API_KEY`.
   - `explorer.py` — the read-only explorer API: overview (catalogue tables in the monthly archive, marked downloaded or not), table details (docs, row count, time-column range, `_nemsplainer.files` download history, filter options) and paged rows. Column names in SQL only ever come from the catalogue; filter values are always parameters. `catalog.time_column()`/`kind()` classify tables as interval, effective-dated or reference.
   - Front end: `static/index.html` is a shell with hash routing (`#chat`, `#explorer/<TABLE>`). The code lives in classic scripts that share globals, loaded in order: `common.js` (helpers), `downloads.js` (job polling, header indicator, progress/error panel, month-range dialog), `chat.js`, `explorer.js`, plus `app.css`.
   - Missing data: `describe_table` documents any catalogue table. `request_download` emits a `download_request` event (an approval card in chat), then waits on a Future in `Conversation.pending`, which `POST /api/chat/decision` resolves. On approval it submits a normal download job, streams progress and returns a result telling the model to re-run its query. Each user message stores a frozen `<data_status>` note (`chat.data_status`) of what's downloaded; it's rendered after the user's text and never recomputed, so history replays identically. The archive table list sits in the static system prompt.
   - `snippets.py` — saved SQL plus an optional Vega-Lite spec (no data) in the `snippets.snippets` table of the same database, created on startup. They're re-run on every open (`/api/snippets/{id}/run`). Names come from the current chat model via each provider's `complete()`, falling back to the table name. `static/snippets.js` adds the "☆ Add to snippets" button to chat results and the Snippets page.
   - The web chat's prompt and tools started as a copy of `load_mms.py` but have since moved on; the notebook is the legacy version.

## Tests

`tests/test_chat.py` covers history rendering and the turn loop with a fake provider (no network). Loader tests run against real DuckDB in temp dirs with the network faked by patching `urllib.request.urlopen` to an in-memory NEMWEB server (`tests/test_ingest.py`: `MonthlyArchive` builds synthetic MMS ZIPs and listings). No test hits the real network.

## Reference

`NEMWEB_RESEARCH.md` documents NEMWEB layout, URL patterns, MMS CSV format and publication timing (monthly MMSDM data lags ~1 month; discover months from listings rather than assuming).
