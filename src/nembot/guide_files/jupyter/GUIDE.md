---
title: Jupyter notebooks
summary: writing, checking and fixing Jupyter notebooks (cells, ipywidgets such as dropdowns and sliders, plots), and how a notebook gets nembot's data
---

# Jupyter notebooks in nembot

## How notebooks work here

- Write a notebook with `write_notebook(name, format="jupyter", cells=[...])`. Each cell is
  `{"type": "markdown" | "code", "source": "..."}`. Code cells can hold any Python.
- nembot runs every cell once in a fresh kernel before saving (widgets keep their default values, and no
  one clicks anything). If a cell raises, you get the error back and nothing is saved: fix it and call
  again. When it passes, the user gets an "Open in JupyterLab" link; outputs are saved with the notebook.
- To change an existing notebook, `read_notebook(name)` first (the user may have edited it), then write it
  again with `overwrite=true`.
- The kernel runs in nembot's own Python environment (polars, altair, duckdb, ipywidgets
  and nembot are installed; pandas and plotly are not). Don't `pip install` anything.

## Getting nembot's data

Notebooks don't open the DuckDB file themselves (the app holds it). Query through the app:

```python
from nembot.client import query

df = query("SELECT REGIONID, avg(RRP) AS avg_rrp FROM DISPATCHPRICE GROUP BY 1 ORDER BY 1")
df
```

`query(sql)` runs one read-only DuckDB `SELECT` against the same database as your `query_mms` tool and
returns a **polars** DataFrame. Use the same table and column
names. Only downloaded data is there; use `request_download` in the chat first if a notebook needs more.

## Widgets (ipywidgets)

The simplest pattern is `interact`, which re-runs a function whenever a widget changes and shows its
return value (it also runs once with the defaults, so the headless check covers it):

```python
import altair as alt
import ipywidgets as widgets
from ipywidgets import interact

regions = query("SELECT DISTINCT REGIONID FROM DISPATCHPRICE ORDER BY 1")["REGIONID"].to_list()

@interact(region=widgets.Dropdown(options=regions, value=regions[0], description="Region"),
          days=widgets.IntSlider(value=7, min=1, max=30, description="Days"))
def price_chart(region, days):
    prices = query(f"""
        SELECT date_trunc('hour', SETTLEMENTDATE) AS HOUR, avg(RRP) AS RRP FROM DISPATCHPRICE
        WHERE REGIONID = '{region}'
          AND SETTLEMENTDATE >= (SELECT max(SETTLEMENTDATE) FROM DISPATCHPRICE) - INTERVAL {days} DAY
        GROUP BY 1 ORDER BY 1""")
    return alt.Chart(prices).mark_line().encode(x="HOUR:T", y=alt.Y("RRP:Q", title="Hourly average, $/MWh"))
```

For layouts with several outputs, use widgets with an `Output` area and `observe`:

```python
region = widgets.Dropdown(options=regions, value=regions[0], description="Region")
out = widgets.Output()

def draw(change=None):
    with out:
        out.clear_output(wait=True)
        display(query(f"SELECT avg(RRP) AS avg_rrp FROM DISPATCHPRICE WHERE REGIONID = '{region.value}'"))

region.observe(draw, names="value")
draw()
display(widgets.VBox([region, out]))
```

Key points:
- Build option lists from the data, so the values put into SQL are ones you offered (`query` takes no
  parameters; quote strings with single quotes). `widgets.SelectMultiple` gives a tuple; build `IN (...)`.
- Give every widget a sensible default `value`.
- Return or `display()` charts; altair charts render on their own as the last expression.

## Charts and row limits

Altair refuses to chart more rows than its limit (5,000 by default, 20,000 in marimo), and the check run
can't catch this because charts aren't drawn headless. A region's 5-minute prices are 288 rows a day, so
aggregate in SQL for longer periods (`date_trunc('hour', SETTLEMENTDATE)` or `'day'`, with avg/min/max),
or choose the grain from a widget. Only if the user needs every point, call
`alt.data_transformers.disable_max_rows()` in the import cell (large charts are slow in the browser).

## Credit

nembot adds AEMO's credit line as the last cell when it saves the notebook. Don't write your own credit or
source line.
