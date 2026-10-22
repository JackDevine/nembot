"""Example nembot plugin: a chat tool, an HTTP route and (in client.js) a tab."""

REGIONS = {'NSW1', 'QLD1', 'SA1', 'TAS1', 'VIC1'}


def setup(nem):
    @nem.tool(
        'List the dispatch intervals where the energy price (RRP) exceeded a threshold, optionally for one region.',
        {'type': 'object', 'properties': {
            'threshold': {'type': 'number', 'description': 'Price threshold in $/MWh, e.g. 1000.'},
            'region': {'type': 'string', 'description': 'Optional region: NSW1, QLD1, SA1, TAS1 or VIC1.'}},
         'required': ['threshold']})
    def find(args):
        threshold = float(args['threshold'])
        region = args.get('region')
        if region and region not in REGIONS:
            return f'Unknown region {region!r}; use one of {sorted(REGIONS)}.'
        where = f"AND REGIONID = '{region}'" if region else ''
        frame = nem.query(f"""
            SELECT SETTLEMENTDATE, REGIONID, RRP FROM DISPATCHPRICE
            WHERE INTERVENTION = 0 AND RRP > {threshold} {where}
            ORDER BY RRP DESC""")
        if frame.is_empty():
            return f'No intervals above ${threshold:,.0f}/MWh.'
        top = frame.row(0, named=True)
        return {
            'text': f"{frame.height} intervals above ${threshold:,.0f}/MWh; highest ${top['RRP']:,.2f} in "
                    f"{top['REGIONID']} at {top['SETTLEMENTDATE']}. First rows:\n{frame.head(10)}",
            'table': {'columns': frame.columns, 'rows': [list(r) for r in frame.head(200).rows()], 'total': frame.height},
            'events': [{'kind': 'summary', 'data': {'count': frame.height, 'threshold': threshold}}],
        }

    @nem.route('GET', '/daily')
    def daily(request):
        threshold = float(request.query_params.get('threshold', 300))
        frame = nem.query(f"""
            SELECT CAST(SETTLEMENTDATE - INTERVAL 5 MINUTE AS DATE) AS day, REGIONID, count(*) AS spikes, max(RRP) AS max_rrp
            FROM DISPATCHPRICE WHERE INTERVENTION = 0 AND RRP > {threshold}
            GROUP BY ALL ORDER BY day, REGIONID""")
        return {'threshold': threshold, 'columns': frame.columns, 'rows': [list(r) for r in frame.rows()]}

    nem.prompt('Use price_spikes_find to list intervals above a price threshold (e.g. "how many spikes over $1000?").')
