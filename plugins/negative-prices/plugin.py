import re

REGIONS = ["NSW1", "QLD1", "SA1", "TAS1", "VIC1"]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Intervals are interval-ending, so shift back 5 min to assign to the trading day.
DAY = "CAST(SETTLEMENTDATE - INTERVAL 5 MINUTE AS DATE)"


def setup(nem):
    def daily_table():
        frame = nem.query(
            f"SELECT {DAY} AS day, REGIONID AS region, "
            "round(count(*) FILTER (WHERE RRP < 0) * 5.0 / 60, 2) AS hours "
            "FROM DISPATCHPRICE WHERE INTERVENTION = 0 GROUP BY 1, 2 ORDER BY 1")
        days = {}
        for day, region, hours in frame.rows():
            days.setdefault(str(day), {})[region] = float(hours)
        rows = [[d] + [days[d].get(r, 0.0) for r in REGIONS] for d in sorted(days)]
        return {"columns": ["Date"] + REGIONS, "rows": rows}

    @nem.route("GET", "/daily")
    def daily(request):
        return daily_table()

    @nem.tool("Share (percent) of 5-minute dispatch intervals with negative prices (RRP < 0) "
              "for a region over an inclusive date range.", {
        "type": "object",
        "properties": {
            "region": {"type": "string", "description": "NSW1, QLD1, SA1, TAS1 or VIC1"},
            "start_date": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
            "end_date": {"type": "string", "description": "YYYY-MM-DD, inclusive"}},
        "required": ["region", "start_date", "end_date"]})
    def share(args):
        region = str(args.get("region", "")).upper()
        start, end = str(args.get("start_date", "")), str(args.get("end_date", ""))
        if region not in REGIONS:
            return f"Invalid region; use one of {', '.join(REGIONS)}."
        if not (DATE_RE.match(start) and DATE_RE.match(end)):
            return "Dates must be YYYY-MM-DD."
        frame = nem.query(
            "SELECT count(*) AS total, count(*) FILTER (WHERE RRP < 0) AS neg "
            f"FROM DISPATCHPRICE WHERE INTERVENTION = 0 AND REGIONID = '{region}' "
            f"AND {DAY} BETWEEN DATE '{start}' AND DATE '{end}'")
        total, neg = int(frame["total"][0]), int(frame["neg"][0])
        if total == 0:
            return f"No dispatch prices for {region} between {start} and {end}."
        pct = 100.0 * neg / total
        return {"text": f"{region} {start} to {end}: {neg} of {total} intervals had negative prices "
                        f"({pct:.1f}%, about {neg * 5 / 60:.1f} hours).",
                "table": {"columns": ["region", "start", "end", "intervals", "negative", "share_pct"],
                          "rows": [[region, start, end, total, neg, round(pct, 2)]]}}

    nem.prompt("Use negative_prices_share for the share of intervals with negative prices; "
               "the Negative prices tab shows hours per day by region.")
