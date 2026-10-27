# Downloading NEMWEB data

Research checked 17 September 2026. Scope: public Australian National Electricity Market (NEM) data, especially MMSDM tables. This is not a guide to authenticated participant feeds or the separate Western Australian market.

## Recommendation

**Start with selected monthly table ZIPs, not the entire monthly bundle.** For a Polars-based notebook, direct downloads give explicit control over the raw files and schema. Use **NEMOSIS** when its supported tables and pandas interface save more work than maintaining a downloader/parser. Its `cache_compiler(..., fformat="parquet")` is also a useful bridge to Polars.

For this repository:

- `load_mms.py` currently imports Polars but contains no data-loading implementation.
- `pyproject.toml` requires Python >=3.14 and declares only marimo; Polars is not yet a declared dependency.
- A direct download and Polars parse was successfully exercised in an isolated Python 3.14 environment without changing project dependencies.
- Package comparisons below are based on documentation, not installation or download tests. A package's Python metadata is not proof that its full dependency stack works on Python 3.14.

## 1. Where the data lives

NEMWEB exposes browsable HTTP directory listings and downloadable files. These public URLs do not require an API key. The homepage redirects to AEMO's explanatory website; enter the data directories directly.

| Source | Entry point | Appropriate use |
|---|---|---|
| Current reports | [Reports/CURRENT](https://www.nemweb.com.au/Reports/CURRENT/) | Recent publications, including interval reports and next-day releases. Different report families have different publication schedules and retention. |
| Report archives | [Reports/ARCHIVE/Dispatch_SCADA](https://www.nemweb.com.au/Reports/ARCHIVE/Dispatch_SCADA/) | Older reports grouped by report family; some daily ZIPs contain many interval ZIPs. This is **not** organized as `/Reports/ARCHIVE/2026/`. |
| Monthly MMSDM history | [MMSDM/2026](https://www.nemweb.com.au/Data_Archive/Wholesale_Electricity/MMSDM/2026/) | Bulk historical backfills, organized by year, month and table. Both full-month bundles and individually downloadable table ZIPs exist. |
| Schema reference | [Electricity Data Model Report](https://nemweb.com.au/Reports/Current/MMSDataModelReport/Electricity/Electricity%20Data%20Model%20Report.htm) | Table definitions, column meanings and keys. Follow the current model rather than inferring semantics from filenames. |

Example current report directories include `DispatchIS_Reports`, `Dispatch_SCADA`, `Next_Day_Dispatch`, `PredispatchIS_Reports`, `P5_Reports` and `ROOFTOP_PV`. A report can contain multiple MMS tables: report-directory names and database-table names are not interchangeable.

Do not assume a universal “30 days current / one year archive” rule. The SCADA current listing inspected contained roughly two days; its archive contained daily files going back to September 2025. These are observations, not retention guarantees.

Monthly history is delayed rather than live. The latest month listed during research was August 2026, published in September. Discover available months from the listing rather than assuming the most recent calendar month is already present.

## 2. Downloading ZIPs directly

### Full monthly bundles: possible, but large

AEMO publishes actual whole-month ZIP files alongside browsable monthly directories. For example:

```text
https://www.nemweb.com.au/Data_Archive/Wholesale_Electricity/MMSDM/2026/MMSDM_2026_08.zip
```

Observed compressed sizes from the [2026 listing](https://www.nemweb.com.au/Data_Archive/Wholesale_Electricity/MMSDM/2026/):

| Month | Bytes | Decimal GB |
|---|---:|---:|
| January 2026 | 44,472,551,863 | 44.47 |
| July 2026 | 63,796,445,100 | 63.80 |
| August 2026 | 55,095,958,837 | 55.10 |

Use full bundles for a broad local mirror or a database covering many tables. They are poor defaults for an analysis needing only prices and demand. Allow additional disk space for extracted data and converted Parquet; ZIP size is not the required working-space budget.

An illustrative download command, **not executed here** because it transfers about 55 GB:

```sh
curl --fail --location --retry 3 \
  --output MMSDM_2026_08.zip \
  'https://www.nemweb.com.au/Data_Archive/Wholesale_Electricity/MMSDM/2026/MMSDM_2026_08.zip'
```

For interrupted large downloads, use a client with resume support, but verify the server accepts range requests and the remote object has not changed before resuming. Resume behavior was not tested here. The full bundle was neither downloaded nor unpacked.

### Per-table monthly ZIPs: the practical default

The same month exposes individual files under:

```text
https://www.nemweb.com.au/Data_Archive/Wholesale_Electricity/MMSDM/
  2026/MMSDM_2026_08/MMSDM_Historical_Data_SQLLoader/DATA/
```

(The line break above is for readability; actual URLs are continuous.)

A price ZIP is:

```text
https://www.nemweb.com.au/Data_Archive/Wholesale_Electricity/MMSDM/2026/MMSDM_2026_08/MMSDM_Historical_Data_SQLLoader/DATA/PUBLIC_ARCHIVE%23DISPATCHPRICE%23FILE01%23202608010000.zip
```

Important details:

- The visible filename contains `#`; the URL must contain `%23`. An unescaped `#` starts a URL fragment and is not sent as part of the HTTP path.
- Use the directory's actual links. Do not assume one naming convention works across all historical years.
- Download **all parts** for the selected table. `FILE01` is not necessarily the entire table: August 2026 `BIDOFFERPERIOD` has `FILE01` through `FILE63`.
- A monthly filename's timestamp identifies its archive partition, not necessarily the minimum or maximum timestamp in the records.
- `SQLLoader` in the directory name does not require an Oracle installation. The tested table ZIP contains a CSV that Python can read directly.

Selected compressed sizes from the [August table listing](https://www.nemweb.com.au/Data_Archive/Wholesale_Electricity/MMSDM/2026/MMSDM_2026_08/MMSDM_Historical_Data_SQLLoader/DATA/):

| Table | Likely analysis | ZIP size, decimal MB |
|---|---|---:|
| `DISPATCHPRICE` | Regional energy and FCAS prices | 2.02 |
| `DISPATCHREGIONSUM` | Regional demand and dispatch quantities | 6.44 |
| `DISPATCH_UNIT_SCADA` | Unit-level measured power | 34.41 |
| `DISPATCHLOAD` | Unit dispatch targets and related quantities | 129.34 |
| `DUDETAILSUMMARY` | Unit metadata and region mapping | 0.38 |

These are measured listing sizes for this month, not stable future size estimates. Only `DISPATCHPRICE` was downloaded and parsed during this research.

### Current versus daily archive packaging

A current SCADA filename looks like:

```text
PUBLIC_DISPATCHSCADA_202609151925_0000000538049524.zip
```

A daily archived SCADA file looks like:

```text
PUBLIC_DISPATCHSCADA_20250906.zip
```

The latter was downloaded from [this archive URL](https://www.nemweb.com.au/Reports/ARCHIVE/Dispatch_SCADA/PUBLIC_DISPATCHSCADA_20250906.zip). It was **1,106,409 bytes**, containing **288 inner ZIPs**. The first inner ZIP contained an interval CSV. Thus a downloader handling both monthly and report archives must not assume `ZIP → CSV` everywhere; this daily example is `ZIP → ZIP → CSV`.

### CSV parsing is the main complication

AEMO's CSV files are report envelopes, not necessarily one rectangular table:

- `C` records contain report/control metadata, including the end-of-report marker.
- `I` records introduce table headers and identify a report/table/version.
- `D` records contain data for a table/version.
- The first four fields identify the record and table/version; they are not the table's business columns.
- A multi-table report can have different row widths and multiple `I` sections. Blind `read_csv(skip_rows=1)` is not a general parser.

Use a CSV parser, not `split(',')`; quoted commas and empty values are valid CSV content. Match the table/version records, use the corresponding header, and cast columns explicitly. Preserve distinct run/intervention/version keys rather than deleting apparent duplicates by timestamp alone.

### Verified small-download example: monthly prices into Polars

This example is deliberately specific to the observed `DISPATCH/PRICE` version 5 file. It is not a universal MMS parser or a streaming bulk loader. It buffers the small ZIP and table in memory; use disk-backed downloads and batch conversion for large tables.

Requires Polars, for example in an isolated environment with `uv run --no-project --python 3.14 --with polars python example.py` after saving the code as `example.py`.

```python
import csv
import io
import urllib.request
import zipfile
import polars as pl

url = (
    "https://www.nemweb.com.au/Data_Archive/Wholesale_Electricity/MMSDM/"
    "2026/MMSDM_2026_08/MMSDM_Historical_Data_SQLLoader/DATA/"
    "PUBLIC_ARCHIVE%23DISPATCHPRICE%23FILE01%23202608010000.zip"
)
with urllib.request.urlopen(url, timeout=60) as response:
    payload = response.read()  # Appropriate for this 2 MB ZIP, not a full month bundle.
with zipfile.ZipFile(io.BytesIO(payload)) as archive:
    names = [n for n in archive.namelist() if n.upper().endswith(".CSV")]
    if len(names) != 1:
        raise ValueError("Expected one CSV in this particular table ZIP")
    with archive.open(names[0]) as member:
        rows = csv.reader(io.TextIOWrapper(member, encoding="utf-8-sig"))
        header = None
        records = []
        for row in rows:
            if row[:4] == ["I", "DISPATCH", "PRICE", "5"]:
                header = row[4:]
            elif row[:4] == ["D", "DISPATCH", "PRICE", "5"]:
                records.append(row[4:])
        if header is None or not records:
            raise ValueError("Expected DISPATCH/PRICE version 5 records")
prices = pl.DataFrame(records, schema=header, orient="row").select(
    pl.col("SETTLEMENTDATE").str.strptime(pl.Datetime, "%Y/%m/%d %H:%M:%S"),
    pl.col("REGIONID"),
    pl.col("INTERVENTION").cast(pl.Int64),
    pl.col("RRP").cast(pl.Float64),
)
print(prices.shape)
print(prices.select(pl.col("SETTLEMENTDATE").min().alias("first"),
                    pl.col("SETTLEMENTDATE").max().alias("last")))
print(prices["REGIONID"].unique().sort().to_list())
```

Observed result:

```text
HTTP 200; downloaded 2,017,570 bytes
ZIP CRC check: no failures
One CSV member: 17,180,711 uncompressed bytes
Record counts: C=2, I=1, D=44,640
Polars shape: (44640, 4)
First SETTLEMENTDATE: 2026-08-01 00:05:00
Last SETTLEMENTDATE:  2026-09-01 00:00:00
Regions: NSW1, QLD1, SA1, TAS1, VIC1
```

HTTP, CRC and raw record counts were checked separately from the displayed Polars example. The example itself was executed successfully on Python 3.14. The boundary illustrates period-ending timestamps: the final interval of August is labelled midnight on 1 September.

## 3. Existing Python packages

### NEMOSIS: first package to evaluate

Sources: [PyPI](https://pypi.org/project/nemosis/), [official repository](https://github.com/UNSW-CEEM/NEMOSIS), [supported tables wiki](https://github.com/UNSW-CEEM/NEMOSIS/wiki/AEMO-Tables).

The PyPI version observed was **3.8.1**, requiring Python **>=3.9**. NEMOSIS is purpose-built for historical AEMO data. Its documented API handles downloading, caching, selecting supported tables and columns, and filtering by dates or field values.

Main entry points:

- `dynamic_data_compiler(...)`: returns a pandas DataFrame for a time range and table.
- `cache_compiler(...)`: builds typed Feather/Parquet caches without returning a DataFrame; useful when Polars will do the analysis.
- `static_table(...)`: obtains supported static/reference datasets.
- `defaults.dynamic_tables` and `defaults.table_columns`: inspect the installed release's supported tables and configured columns.

Documentation-based usage, **not runtime-tested here**:

```python
from pathlib import Path
from nemosis import dynamic_data_compiler

cache = Path("data/nemosis")
cache.mkdir(parents=True, exist_ok=True)
prices = dynamic_data_compiler(
    "2026/08/01 00:00:00",
    "2026/08/02 00:00:00",
    "DISPATCHPRICE",
    str(cache),
    select_columns=["SETTLEMENTDATE", "REGIONID", "INTERVENTION", "RRP"],
    fformat="parquet",
    keep_csv=False,
)
```

Alternatively, use the documented cache API:

```python
from nemosis import cache_compiler

cache_compiler(
    "2026/08/01 00:00:00", "2026/08/02 00:00:00",
    "DISPATCHPRICE", str(cache), fformat="parquet",
)
```

After checking the generated paths and schema, Polars can scan the relevant Parquet files. Do not glob an entire mixed-table cache into one frame. Region/column filtering is local processing; it does not turn NEMWEB into a server-side query API or necessarily reduce bytes downloaded.

Caveats:

- It supports a configured subset of AEMO tables/columns, not automatically every NEMWEB report. Extra columns may require updating `defaults.table_columns` and rebuilding the cache, as documented.
- **Released docs and repository main differ.** PyPI 3.8.1 documents Feather as the default and `keep_csv=True`; the repository README describes Parquet defaults, retained ZIPs, and additional date-input behavior. Pin a release and explicitly set the options needed rather than assuming main-branch behavior is published.
- PyPI documentation warns of missing `BIDPEROFFER_D` / `BIDDAYOFFER_D` data from March 2021 through July 2024, and limited `FCAS_4_SECOND` history. Treat these as package/documentation caveats to investigate for the intended dataset, not proof that all underlying AEMO sources lack the data.
- No Python 3.14 dependency-resolution or live NEMOSIS download was tested here.

### nemdata: simple CLI, significant compatibility constraint

Source: [PyPI documentation](https://pypi.org/project/nemdata/).

Observed version **0.3.7**, MIT licence, Python requirement **>=3.10,<3.11**. That requirement excludes this repository's Python 3.14 environment.

It provides a CLI and Python API for a selected set of MMSDM tables plus NEMDE data, caches under `~/nem-data/data`, and exposes CSV/Parquet outputs. Documented CLI example:

```sh
nemdata -t trading-price -s 2020-01 -e 2020-02
```

Documented Python interface:

```python
import nemdata
nemdata.download(start="2020-01", end="2020-02", table="trading-price")
prices = nemdata.load()["trading-price"]
```

Its author describes it as a personal research tool, **not designed for production use**, and directs users toward NEMOSIS. It adds timezone-aware interval start/end columns; its trading-price loader resamples data to five-minute frequency even before the settlement transition. That transformation is material: it must not be mistaken for native historical five-minute settlement prices.

Verdict: useful as a small CLI/reference implementation, but not a sensible default dependency here without a separate Python 3.10 environment and careful checking of transformations.

### Specialist alternatives: nemseer and nempy

| Package | Released version / upload date (UTC) | Python requirement | Best fit |
|---|---|---|---|
| [NEMOSIS](https://pypi.org/pypi/nemosis/json) | 3.8.1 / 2025-02-07 | >=3.9 | General supported historical tables |
| [nemdata](https://pypi.org/pypi/nemdata/json) | 0.3.7 / 2024-10-20 | >=3.10,<3.11 | Small research CLI in a separate environment |
| [nemseer](https://pypi.org/pypi/nemseer/json) | 1.0.7 / 2023-11-21 | >=3.8,<3.12 | Historical forecast vintages |
| [nempy](https://pypi.org/pypi/nempy/json) | 3.0.3 / 2025-03-07 | >=3.9 | Dispatch modelling and reconstruction |

Dates and requirements come from PyPI release metadata, not repository commit dates. Release age alone does not establish abandonment or current compatibility.

**nemseer** supports P5MIN, PREDISPATCH, PDPASA, STPASA and MTPASA forecast data. Its distinguishing feature is filtering by both **forecast run time** and **forecast target time**. `download_raw_data(...)` builds a raw Parquet cache; `compile_data(...)` returns table-keyed pandas DataFrames or xarray Datasets, optionally caching processed results. A forecast-target month can require files from multiple run months. Discovery helpers include `get_data_daterange()` and `get_tables(...)`.

Its Python upper bound excludes this repository's interpreter. The docs recommend at least 8 GB RAM, preferably 16 GB+, and warn that xarray conversion can be expensive. The changelog says xarray conversion drops intervention periods; prefer DataFrame output when preserving those observations matters. This is a forecast-history companion, not a universal current-report downloader. Sources: [quick start](https://nemseer.readthedocs.io/en/latest/quick_start.html), [released API source](https://raw.githubusercontent.com/UNSW-CEEM/NEMSEER/v1.0.7/src/nemseer/nemseer.py), [changelog](https://github.com/UNSW-CEEM/NEMSEER/blob/master/CHANGELOG.md).

**nempy** is primarily a dispatch model. Its historical-input utilities download selected MMSDM tables into SQLite through `mms_db.DBManager`, and obtain NEMDE XML through `xml_cache.XMLCacheManager`. These caches feed modelling/preprocessing rather than a general data-download interface.

Important operational warning: the released `DBManager.populate(...)` calls `create_tables()`, dropping/recreating its default tables; it is not simply an incremental cache-safe refresh. XML `load_interval(...)` does check the disk cache. Do not run broad population against a database containing work you need to preserve. Sources: [historical-input docs](https://nempy.readthedocs.io/en/latest/historical.html), [released database implementation](https://raw.githubusercontent.com/UNSW-CEEM/nempy/v3.0.3/src/nempy/historical_inputs/mms_db.py), [PyPI](https://pypi.org/project/nempy/).

Neither specialist package was installed or exercised here. For prices/demand alone, they add scope without replacing the simpler direct ZIP or NEMOSIS approach. Also avoid installing all candidates together: nemdata declares pandas <2.0 whereas NEMOSIS 3.8.1 requires pandas >=2.2.3, so those declared constraints conflict.

## 4. Data semantics and operational pitfalls

The [MMS guide by Matthew Davis](https://www.mdavis.xyz/mms-guide/) is a useful secondary source for parsing and market semantics; resolve table-specific questions against AEMO's model documentation.

- **Market time:** NEM timestamps generally use fixed UTC+10 without daylight saving. Do not localize them to `Australia/Sydney`. The example intentionally leaves them naive; establish an explicit UTC+10 policy before combining with other data.
- **Interval boundaries:** many measurements are period-ending. Check range inclusion rules in whichever package is used, especially at midnight and month boundaries.
- **Five versus thirty minutes:** five-minute settlement began on 1 October 2021. Do not conflate pre-transition dispatch prices with thirty-minute settlement prices or silently upsample one into the other.
- **Units:** measured MW and interval energy MWh are different quantities. Confirm column meanings before integrating or aggregating.
- **Publication delay:** bids, next-day reports, forecasts and dispatch outputs have different release schedules. Monthly archives cannot provide live data.
- **Runs, revisions and intervention:** preserve the table's keys and understand intervention flags. Deduplicating solely by timestamp and region can discard meaningful records.
- **Changing schema:** handle table versions, optional columns and split files. Do not silently ignore a new version when building a general loader.
- **Forecasts:** retain both publication/run time and forecast target time. Otherwise a historical analysis can accidentally use information unavailable at the time.
- **Archive lag and overlap:** when combining current reports, report archives and monthly tables, define source precedence and check for gaps rather than concatenating blindly.
- **Public availability is not a licence grant:** review [AEMO's website terms](https://www.aemo.com.au/privacy-and-legal-notices) for attribution, use and redistribution requirements. This research does not establish redistribution rights.

For a reusable downloader, recommended engineering rather than tested features here: discover links with an HTML parser; use timeouts, bounded retries and modest concurrency; stream large files to temporary disk paths; validate ZIPs before marking downloads complete; retain source URL and retrieval metadata; cache raw inputs where practical; and convert in batches to table/month-partitioned Parquet. Avoid recursively mirroring all NEMWEB for a small analysis.

## 5. Suggested next step for this project

1. Choose the question and tables first. Start with `DISPATCHPRICE` and `DISPATCHREGIONSUM`; add `DISPATCH_UNIT_SCADA` and `DUDETAILSUMMARY` for unit-level analysis.
2. Download one month of those tables, retaining every file part. Prices plus regional quantities are only about 8.45 MB compressed in the observed month, versus 55.10 GB for the complete bundle.
3. Either adapt the direct Polars path with explicit schemas or evaluate a pinned NEMOSIS release in an isolated environment. Prefer `cache_compiler` when pandas results are unnecessary.
4. Validate expected regions, interval coverage, table keys and timestamp semantics before scaling to years.
5. Add current/report-archive ingestion only if the application actually needs data newer than monthly MMSDM history.

## Verification scope

Completed: directory inspection; one monthly price ZIP download and CRC check; its CSV record counts and Polars conversion; one daily SCADA archive download and inspection of its nested ZIP structure.

Not completed or claimed: downloading a full monthly bundle; testing HTTP resume; validating every table or historical year; runtime testing NEMOSIS/nemdata; building a production ingestion pipeline. Only this research document was added to the project; no dependencies or application code were changed.

Additional SCADA smoke check: a temporary script downloaded the same daily archive, validated outer/inner ZIP CRCs, and parsed every CSV. Its initial three-column schema assumption failed because the actual header also includes `LASTCHANGED`; after matching the observed four-column schema, validation passed for **137,083 data rows, 476 DUIDs and 288 five-minute timestamps**, spanning **2025-09-06 00:05:00 to 2025-09-07 00:00:00**. Every data row matched `DISPATCH/UNIT_SCADA` version 1, had the expected width, a parseable timestamp, nonempty DUID and numeric SCADAVALUE. The temporary script was removed automatically. This tests that sample day, not all historical schema versions.

## Download speed and politeness (2026-10-20)
Research findings:
- None of the six NEMWEB tools reviewed sleeps between requests. OpenElectricity downloads 3 files in parallel; the others mostly run sequentially.
- NEMWEB has no robots.txt and no published rate limit. It sits behind Azure Front Door with caching disabled, so every request reaches AEMO's server.

What changed:
- Connections are now reused.
- 2 concurrent requests by default (cap 3), with AutoThrottle-style pacing.
- Errors drop the downloader to one request at a time with backoff; repeated 403s stop the run.

Result: 120 five-minute DispatchIS files took **4.9 s, down from about 2.5 minutes** (0.04 s per file).
