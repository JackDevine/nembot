import marimo

__generated_with = "0.24.2"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo

    return (mo,)


@app.cell
def _():
    import polars as pl

    return


@app.cell
def _():
    import duckdb

    return (duckdb,)


@app.cell
def _():
    import altair
    import io
    import json
    from pydantic_ai import BinaryContent, ToolReturn

    return BinaryContent, ToolReturn, altair, io, json


@app.cell
def _(duckdb):
    mms = duckdb.connect("data/nem.duckdb")
    mms
    return (mms,)


@app.cell
def _():
    SYSTEM_PROMPT = """You are a data analyst for Australia's National Electricity Market (NEM).

    You have two tools that execute DuckDB SQL against the `nem` database:
    - `query_mms(sql)` returns a short CSV preview and displays a table to the user.
    - `visualize_mms(sql, plot_spec)` takes SQL and a JSON-encoded Vega-Lite spec
      (omit data/datasets), plots ALL query rows, and returns a PNG for you to inspect.
      Use it when asked for a plot/chart, or when a visual comparison or trend helps.
      Use explicit encoding types and fields matching SQL aliases; label axes and units.
      Inspect the returned image before commenting; never claim to see a failed plot.
      Interactive plots are displayed automatically as soon as the tool finishes,
      before your interpretation; do not duplicate them in your written response.
      Do not emit image links, HTML, or Vega-Lite JSON in your final answer.
    ALWAYS call at least one of these tools before answering factual market questions.

    Data coverage:
    - This database intentionally contains only a short date range. This is expected,
      not a data-quality problem to investigate or a reason to delay answering.
    - Do NOT run preliminary date-range or coverage checks (such as MIN/MAX dates
      or counts by date) unless the user explicitly asks about data coverage.
    - Go directly to the query or plot needed to answer the question. If the user
      gives no date range, use the available data rather than assuming a recent
      calendar window or checking which dates exist first.
    - Report what the query results and plots actually show. Do not apologize for,
      speculate about, or repeatedly caveat the short date range, and do not seek
      additional history. Keep conclusions scoped to the observed data; do not
      extrapolate to unobserved periods.
    - Honor explicit date filters. If the requested query returns no rows, simply
      say so rather than launching a separate coverage investigation.

    Schema highlights (all tables in the `main` schema):
    - DISPATCHPRICE: 5-min dispatch prices per region — SETTLEMENTDATE (TIMESTAMP), REGIONID,
      RRP (energy price $/MWh), plus FCAS prices (RAISE6SECRRP, RAISEREGRRP, RRP per MWh), ...
    - DISPATCHREGIONSUM: regional supply/demand — TOTALDEMAND, AVAILABLEGENERATION,
      NETINTERCHANGE, DEMANDFORECAST, ...
    - DISPATCH_UNIT_SCADA: unit output — SETTLEMENTDATE, DUID, SCADAVALUE (MW).
    - DUDETAILSUM: unit registry — DUID, REGIONID, DISPATCHTYPE (GENERATOR/LOAD),
      PARTICIPANTID, START_DATE, END_DATE.

    SQL style rules:
    - ALWAYS spread SQL across multiple lines with proper indentation (never a single-line query).
    - NEVER append any comments to the SQL (no '-- ...' trailing annotations of any kind).
    - Prefer aggregations (GROUP BY, AVG, ...) instead of fetching raw rows.
    - Both tools return only a 10-row CSV preview of the result. That is intentional: you never need the full result set to answer, since your reply is a short insight from an aggregate query. Do NOT try to squeeze the whole series into 10 rows or pivot rows into columns to fit the preview, and do NOT say the data is limited/hidden — the user sees the full result as an interactive table.
    - For monthly aggregates use DATE_TRUNC('month', SETTLEMENTDATE).
    - Regions are NSW1, QLD1, SA1, TAS1, VIC1.
    - After receiving tool output, reply with a concise 1-2 sentence insight about the data.
    - Prices are fine to write as usual, e.g. $52.30/MWh."""
    return (SYSTEM_PROMPT,)


@app.cell
def _(duckdb, mms):
    # Per-turn results for the rich response. Plots accumulate across tool calls;
    # only the latest query table is kept. The chat model clears these each turn.
    query_state = {"sql": None, "df": None, "plots": []}


    def query_mms(sql: str) -> str:
        """Run one read-only DuckDB SELECT against the NEM MMS database.

        Returns a 10-row CSV preview; the user sees the full query result in a
        table. Prefer aggregate queries; do not pivot to fit the preview.

        Args:
            sql: A DuckDB SELECT query against the main schema.
        """
        query_state["sql"] = None
        query_state["df"] = None
        try:
            statements = mms.extract_statements(sql)
            if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                return "Only one SELECT query is allowed."
            result = mms.sql(sql).pl()
        except Exception as err:
            return f"Query failed: {err}"

        query_state["sql"] = sql
        query_state["df"] = result
        return result.head(10).write_csv()


    return query_mms, query_state


@app.cell
def _(BinaryContent, ToolReturn, altair, io, json, query_mms, query_state):
    def visualize_mms(sql: str, plot_spec: str) -> ToolReturn:
        """Query MMS and create a Vega-Lite plot for you and the user.

        You receive a PNG of the full plot and a short CSV preview, so inspect
        the image before commenting. The interactive plot is automatically
        displayed as soon as this tool finishes; do not embed it yourself.

        Args:
            sql: One read-only DuckDB SELECT query against the main schema.
                Aggregate/filter in SQL to produce a useful, manageable plot.
            plot_spec: A JSON-encoded Vega-Lite specification, WITHOUT data or
                datasets (SQL results are injected automatically). Use explicit
                field names matching SQL aliases and explicit encoding types.
                For example: {"mark":"line","encoding":{"x":{"field":"month",
                "type":"temporal"},"y":{"field":"avg_rrp","type":"quantitative"},
                "color":{"field":"REGIONID","type":"nominal"}},"height":320}.
                Layered/faceted plots are supported. Width defaults to 640 pixels;
                "container" is converted to a fixed width for PNG rendering.
        """
        try:
            spec = json.loads(plot_spec)
            if not isinstance(spec, dict):
                raise ValueError("plot_spec must be a JSON object")

            def prepare(node):
                # All views must use the SQL result, not embedded or remote data.
                if isinstance(node, dict):
                    if "data" in node or "datasets" in node:
                        raise ValueError("Omit data/datasets; SQL results supply the plot data")
                    if node.get("mark") == "image" or (
                        isinstance(node.get("mark"), dict)
                        and node["mark"].get("type") == "image"
                    ):
                        raise ValueError("Image marks are not supported; plot the SQL data directly")
                    for key, value in node.items():
                        if key in ("width", "height") and value == "container":
                            node[key] = 640 if key == "width" else 320
                        else:
                            prepare(value)
                elif isinstance(node, list):
                    for value in node:
                        prepare(value)

            prepare(spec)
            # Composite charts determine their size in their child views.
            if not any(key in spec for key in ("hconcat", "vconcat", "concat", "facet", "repeat")):
                spec.setdefault("width", 640)
                spec.setdefault("height", 320)
            # Validate before running potentially expensive SQL.
            spec["data"] = {"values": []}
            altair.Chart.from_dict(spec, validate=True)
        except Exception as err:
            return ToolReturn(return_value=f"Invalid plot specification: {getattr(err, 'message', str(err))}")

        preview = query_mms(sql)
        result = query_state["df"]
        if result is None:
            return ToolReturn(return_value=preview)
        if result.is_empty():
            return ToolReturn(return_value="The query returned no rows. Adjust the SQL before plotting.")

        try:
            # Polars JSON handles dates/datetimes and nulls for Vega-Lite.
            spec["data"] = {"values": json.loads(result.write_json())}
            plot = altair.Chart.from_dict(spec, validate=True)
            png = io.BytesIO()
            plot.save(png, format="png", engine="vl-convert", scale_factor=1)
            png_bytes = png.getvalue()
        except Exception as err:
            return ToolReturn(return_value=f"Plot rendering failed: {err}. Revise the specification and retry.")

        query_state["plots"].append({"sql": sql, "chart": plot})
        return ToolReturn(
            return_value=f"Plot created from all {result.height} rows. First 10 rows:\n{preview}",
            # ToolReturn.content becomes a multimodal model message, not a string
            # representation of an image or just a user-facing UI attachment.
            content=[
                "Inspect this plot of the SQL result and describe its key patterns. "
                "The interactive Vega-Lite plot is displayed when this tool completes.",
                BinaryContent(data=png_bytes, media_type="image/png"),
            ],
        )


    return (visualize_mms,)


@app.cell
def _(SYSTEM_PROMPT, mo, query_mms, query_state, visualize_mms):
    import os
    import re
    import uuid
    from pydantic_ai import Agent, Tool

    # Route marimo's OpenAI client through OpenRouter
    os.environ["OPENAI_API_KEY"] = os.environ.get("OPENROUTER_API_KEY", "sk-or-v1-...")  # or hardcode your key
    os.environ["OPENAI_BASE_URL"] = "https://openrouter.ai/api/v1"

    # The one object every tool call pushes its SQL + result into (see query_mms).
    _agent = mo.ai.llm.pydantic_ai(
        Agent(
            "openrouter:z-ai/glm-5.3-flash",
            system_prompt=SYSTEM_PROMPT,
            # The tools share a DuckDB connection and per-turn result state.
            tools=[
                Tool(query_mms, sequential=True),
                Tool(visualize_mms, sequential=True),
            ],
        )
    )
    async def _chat_model(messages, config):
        """Stream each plot on tool completion, then the agent's interpretation."""
        # Attach only queries and plots produced DURING this turn.
        query_state["sql"] = None
        query_state["df"] = None
        query_state["plots"].clear()
        displayed_plots = 0

        def rich_chunks(content):
            rich_id = f"rich_{uuid.uuid4().hex}"
            rich_html = mo.as_html(content).text
            yield {"type": "text-start", "id": rich_id}
            yield {"type": "text-delta", "id": rich_id, "delta": rich_html}
            yield {"type": "text-end", "id": rich_id}

        def pending_plot_chunks():
            nonlocal displayed_plots
            # Tools append only successfully rendered plots. A cursor makes each
            # plot appear exactly once, even across multiple tool/model steps.
            while displayed_plots < len(query_state["plots"]):
                plot_result = query_state["plots"][displayed_plots]
                plot_spec = plot_result["chart"].to_dict()
                plot_spec.pop("data", None)
                plot_spec.pop("datasets", None)
                content = mo.vstack([
                    mo.accordion({
                        "Plot SQL": mo.md(f"```sql\n{plot_result['sql']}\n```"),
                        "Plot specification": mo.vstack([
                            mo.md("Data is supplied by the plot SQL above."),
                            mo.json(plot_spec),
                        ]),
                    }),
                    plot_result["chart"],
                ])
                yield from rich_chunks(content)
                displayed_plots += 1

        finish_chunk = None
        try:
            async for chunk in _agent(messages, config):
                ctype = chunk.get("type")
                if ctype == "text-delta":
                    # Escape prices without modifying our own rich HTML chunks.
                    delta = chunk.get("delta", "")
                    chunk["delta"] = re.sub(r"(?<!\\)\$", r"\\$", delta)
                if ctype == "finish":
                    finish_chunk = chunk
                else:
                    yield chunk
                # The adapter emits tool-output-available immediately after a tool
                # returns, before waiting for the model's next response. Flush here
                # rather than waiting until the entire agent run has finished.
                for plot_chunk in pending_plot_chunks():
                    yield plot_chunk
        except Exception as err:
            # Keep any completed plots visible even if interpretation fails.
            for plot_chunk in pending_plot_chunks():
                yield plot_chunk
            err_id = f"text_{uuid.uuid4().hex}"
            yield {"type": "text-start", "id": err_id}
            yield {"type": "text-delta", "id": err_id, "delta": f"⚠️ The model call failed: {err}"}
            yield {"type": "text-end", "id": err_id}
            if finish_chunk is not None:
                yield finish_chunk
            return

        for plot_chunk in pending_plot_chunks():
            yield plot_chunk

        # Preserve the latest query table at the end; plots were already streamed.
        sql = query_state["sql"]
        df = query_state["df"]
        if sql is not None and df is not None:
            for table_chunk in rich_chunks(mo.vstack([
                mo.accordion({"SQL query": mo.md(f"```sql\n{sql}\n```")}),
                mo.ui.table(df.head(200), page_size=10, selection=None),
            ])):
                yield table_chunk

        if finish_chunk is not None:
            yield finish_chunk


    chat = mo.ui.chat(_chat_model)
    chat
    return


@app.cell
def _():
    return


if __name__ == "__main__":
    app.run()
