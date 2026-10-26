---
title: marimo notebooks
summary: writing, checking and fixing marimo notebooks (cells, reactivity, UI widgets such as dropdowns and sliders, SQL), and how a notebook gets nembot's data
---

# marimo notebooks in nembot

## How notebooks work here

- Write a notebook with `write_notebook(name, code)`: `code` is the whole marimo `.py` file. nembot runs
  `marimo check` and then runs the notebook once headless (as a script, so widgets keep their default
  values). If either fails you get the errors back and nothing is saved: fix them and call it again.
  When it passes, the user gets an "Open in marimo" link.
- To change an existing notebook, `read_notebook(name)` first (the user may have edited it in marimo),
  then `write_notebook(name, code, overwrite=true)`.
- Notebooks run in nembot's own Python environment (marimo, polars, altair, duckdb and nembot are
  installed; pandas and plotly are not). Don't add a PEP 723 `# /// script` header and don't install
  packages.

## Getting nembot's data

Notebooks don't open the DuckDB file themselves (the app holds it). Query through the app:

```python
@app.cell
def _():
    import marimo as mo
    import altair as alt
    from nembot.client import query
    return alt, mo, query
```

`query(sql)` runs one read-only DuckDB `SELECT` against the same database as your `query_mms` tool and
returns a **polars** DataFrame. Use the same table and column names. Only data that is already downloaded
is there; if a notebook needs more, use `request_download` in the chat first.

`mo.sql(...)` is marimo's own in-memory DuckDB: it does NOT see nembot's tables. Use it only on polars
DataFrames already in the notebook, or just use `query`.

## Widgets driving queries

A widget's `.value` is read in another cell; marimo re-runs that cell whenever the widget changes. Build
option lists from the data, so values in SQL are always ones you offered (`query` takes no parameters, so
the values go into the SQL text; quote strings with single quotes):

```python
@app.cell
def _(query):
    regions = query("SELECT DISTINCT REGIONID FROM DISPATCHPRICE ORDER BY 1")["REGIONID"].to_list()
    return (regions,)


@app.cell
def _(mo, regions):
    region = mo.ui.dropdown(options=regions, value=regions[0], label="Region")
    days = mo.ui.slider(1, 30, value=7, label="Days")
    mo.hstack([region, days])
    return days, region


@app.cell
def _(days, query, region):
    prices = query(f"""
        SELECT date_trunc('hour', SETTLEMENTDATE) AS HOUR, avg(RRP) AS RRP FROM DISPATCHPRICE
        WHERE REGIONID = '{region.value}'
          AND SETTLEMENTDATE >= (SELECT max(SETTLEMENTDATE) FROM DISPATCHPRICE) - INTERVAL {days.value} DAY
        GROUP BY 1 ORDER BY 1""")
    return (prices,)


@app.cell
def _(alt, prices):
    alt.Chart(prices).mark_line().encode(x="HOUR:T", y=alt.Y("RRP:Q", title="Hourly average, $/MWh"))
    return
```

Key points:
- Create a widget in one cell and read `.value` in a different cell (reading it in the defining cell
  gives no reactivity).
- Display widgets as the cell's last expression, or together with `mo.hstack`/`mo.vstack`.
- A widget needs a sensible `value=` so the notebook also runs headless.
- `mo.ui.multiselect(options)` gives a list in `.value`; build `IN (...)` from it.

## Charts and row limits

Altair refuses to chart more rows than its limit (5,000 by default, 20,000 in marimo), and the check run
can't catch this because charts aren't drawn headless. A region's 5-minute prices are 288 rows a day, so
aggregate in SQL for longer periods (`date_trunc('hour', SETTLEMENTDATE)` or `'day'`, with avg/min/max),
or choose the grain from a widget. Only if the user needs every point, call
`alt.data_transformers.disable_max_rows()` in the import cell (large charts are slow in the browser).

## Credit

nembot adds AEMO's credit line as the last cell when it saves the notebook. Don't write your own credit or
source line.

# Writing marimo notebooks

*Adapted from marimo's own agent skill, marimo-team/skills `skills/marimo-notebook` (Apache-2.0).*

marimo notebooks are Python files. Each cell is a function decorated with `@app.cell`; its parameters
are the variables it reads and its return tuple the variables it defines:

```python
import marimo

__generated_with = "0.24.2"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo
    import numpy as np
    return mo, np


@app.cell
def _(np, slider):
    np.array([1, 2, 3]) + slider.value
    return


@app.cell
def _(mo):
    slider = mo.ui.slider(1, 10, 1, label="number to add")
    slider
    return (slider,)


if __name__ == "__main__":
    app.run()
```

## State and reactivity

Variables between cells define reactivity for 99% of uses. Don't mutate objects across cells
(e.g. `my_list.append()`); create new objects instead. Avoid `mo.state()` unless you need bidirectional
UI sync or accumulated callback state. Every global name is defined by exactly one cell; use a `_` prefix
only for the odd cell-local name (agents tend to overdo underscores).

## Don't guard cells with `if` statements

marimo only runs a cell once its inputs exist, so guards like `if results:` just hide output:

```python
# BAD
@app.cell
def _(plt, training_results):
    if training_results:
        fig, ax = plt.subplots()
        ax.plot(training_results["losses"])
        fig
    return

# GOOD
@app.cell
def _(plt, training_results):
    fig, ax = plt.subplots()
    ax.plot(training_results["losses"])
    fig
    return
```

## Don't use try/except for control flow

Let errors surface; only catch specific, expected exceptions with a meaningful recovery.

## Cell output rendering

Only the cell's **final expression** renders. Indented or conditional expressions don't:

```python
# BAD
@app.cell
def _(mo, condition):
    if condition:
        mo.md("This won't show!")
    return

# GOOD
@app.cell
def _(mo, condition):
    result = mo.md("Shown!") if condition else mo.md("Also shown!")
    result
    return
```

## Script mode

`mo.app_meta().mode == "script"` is true in the headless check run. Always create and show widgets;
only change data sources in script mode if you must.

## More sections

Read one with `read_guide("marimo", section)`:
- `UI`: every `mo.ui` element and forms (`.batch().form()`)
- `SQL`: `mo.sql` and SQL engines
- `REACTIVITY`: the dataflow graph, variable uniqueness, mutations, lazy evaluation
- `STATE`: when and how to use `mo.state`
- `EXPENSIVE`: caching, `mo.stop`, run buttons for slow work
