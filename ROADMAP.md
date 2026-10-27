# Roadmap

Open work and known limitations as of v0.1.0. Suggestions are welcome as GitHub issues.

## Data

- **Older data-model versions.** nembot uses one version of AEMO's data model (5.7) for every month. Older
  archives that follow an earlier version (renamed, added or dropped columns) fail with a clear error rather than
  loading; converting them to the current schema is still to do, guided by those errors.
- **Recent data beyond 600 files.** Downloads of AEMO's recent report files (newer than the monthly archive)
  stop after 600 requests, which can cut off long ranges for the 5-minute TradingIS and rooftop PV feeds. A
  slow, opt-in way to fetch everything may follow.
- **Reading while downloading.** Chat, Explorer and downloads share one database connection, which works in
  practice. A client–server setup (MotherDuck, or DuckDB's client–server protocol, see
  https://motherduck.com/blog/duckdb-client-server/) would allow more concurrent use.

## Assistants

- **Standard sources for web search.** A list of key documents (such as AEMO's Quarterly Energy Dynamics) to
  favour in web search, or to keep in nembot's database.
- **Choosing tools.** A way to choose which tools the assistant may use; it would replace the web search toggle.
- **Claude through a Claude subscription** exists in the code but is off and unsupported until Anthropic
  approves offering it.

## Notebooks

- **Live editing of open Jupyter notebooks** (marimo already works), which would need the jupyter-collaboration
  extension.
- **marimo's AI background** lists the tables downloaded when nembot started its marimo server; refreshing it
  after downloads without a restart.
- `run_in_marimo` relies on marimo's private code-mode API, so marimo is pinned to the tested 0.24 series.

## Sharing

- **Multiple users** of one database: snippets (and other saved items) have no owner yet.

## Known issues

See "Known issues" in the README.
