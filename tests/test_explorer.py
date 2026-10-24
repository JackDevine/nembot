"""Explorer API over the catalogue and a small local database (no network)."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

import duckdb

from nembot import explorer
from nembot.ingest import _create_table
from nembot.tables import TABLES


class Explorer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        path = Path(cls.directory.name) / 'nem.duckdb'
        with duckdb.connect(str(path)) as c:
            _create_table(c, TABLES['DISPATCHPRICE'])
            start = datetime(2026, 6, 1)
            for i in range(1, 289 * 2):  # two days of 5-minute intervals
                for region in ('NSW1', 'SA1'):
                    c.execute('INSERT INTO DISPATCHPRICE (SETTLEMENTDATE, RUNNO, REGIONID, DISPATCHINTERVAL, INTERVENTION, RRP) '
                              'VALUES (?, 1, ?, ?, 0, ?)', [start + timedelta(minutes=5 * i), region, str(i), i])
        cls.db = duckdb.connect(str(path), read_only=True)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.directory.cleanup()

    def test_overview_lists_archive_tables_and_marks_downloaded_ones(self):
        overview = explorer.overview(self.db)
        tables = {t['name']: t for p in overview['packages'] for t in p['tables']}
        self.assertTrue(tables['DISPATCHPRICE']['downloaded'])
        self.assertFalse(tables['DISPATCHREGIONSUM']['downloaded'])
        self.assertEqual(overview['source']['format'], 'pdf')

    def test_table_details_for_downloaded_and_missing_tables(self):
        detail = explorer.table(self.db, 'DISPATCHPRICE')
        self.assertEqual(detail['rows'], 577 * 2)
        self.assertEqual(detail['time_column'], 'SETTLEMENTDATE')
        self.assertEqual(detail['range'], ['2026-06-01 00:05:00', '2026-06-03 00:05:00'])
        self.assertEqual(detail['filters'], {'REGIONID': ['NSW1', 'SA1']})
        missing = explorer.table(self.db, 'DISPATCHLOAD')
        self.assertFalse(missing['downloaded'])
        self.assertTrue(missing['columns'])
        self.assertIsNone(explorer.table(self.db, 'NOT_A_TABLE'))

    def test_rows_filter_by_day_and_region_newest_first(self):
        page = explorer.rows(self.db, 'DISPATCHPRICE', start='2026-06-02', end='2026-06-02',
                             values={'REGIONID': 'SA1'}, limit=10)
        self.assertEqual(page['total'], 288)
        self.assertEqual(page['rows'][0][:3], ['2026-06-02 23:55:00', 1.0, 'SA1'])
        self.assertEqual(len(page['rows']), 10)

    def test_filter_values_are_parameters_not_sql(self):
        page = explorer.rows(self.db, 'DISPATCHPRICE', values={'REGIONID': "SA1' OR '1'='1"})
        self.assertEqual(page['total'], 0)
        # Unknown filter columns are ignored rather than interpolated.
        page = explorer.rows(self.db, 'DISPATCHPRICE', values={'1=1; DROP TABLE x; --': 'y'})
        self.assertEqual(page['total'], 577 * 2)



class SqlEditor(Explorer):
    def test_runs_one_select_and_returns_the_first_rows(self):
        data = explorer.run_sql(self.db, "SELECT REGIONID, max(RRP) AS top FROM DISPATCHPRICE GROUP BY 1 ORDER BY 1")
        self.assertEqual((data['columns'], data['rows'], data['truncated']), (['REGIONID', 'top'], [['NSW1', 577.0], ['SA1', 577.0]], False))
        capped = explorer.run_sql(self.db, 'SELECT * FROM DISPATCHPRICE', limit=10)
        self.assertEqual((len(capped['rows']), capped['truncated']), (10, True))
        self.assertIsInstance(capped['rows'][0][0], str)  # timestamps as text for the browser

    def test_rejects_anything_but_one_select(self):
        for sql in ('DELETE FROM DISPATCHPRICE', 'SELECT 1; SELECT 2', 'CREATE TABLE x AS SELECT 1'):
            with self.assertRaises(ValueError):
                explorer.run_sql(self.db, sql)
        with self.assertRaises(duckdb.Error):
            explorer.run_sql(self.db, 'SELECT nope FROM DISPATCHPRICE')

    def test_check_underlines_what_duckdb_rejects_without_running_it(self):
        def check(sql):
            return [(sql[p['from']:p['to']], p['message'].split(':')[0]) for p in explorer.check_sql(self.db, sql)]
        self.assertEqual(check('SELECT REGIONID, max(RRP) FROM DISPATCHPRICE GROUP BY ALL'), [])  # DuckDB-only syntax
        self.assertEqual(check('SELECT * FROM DISPATCHPRICE QUALIFY row_number() OVER (PARTITION BY REGIONID) = 1'), [])
        self.assertEqual(check('SELECT REGIONID,\n  PRICE\nFROM DISPATCHPRICE'), [('PRICE', 'Binder Error')])
        self.assertEqual(check('SELECT avgg(RRP) FROM DISPATCHPRICE'), [('avgg', 'Catalog Error')])
        self.assertEqual(check('SELECT * FROM DISPATCHPRICES'), [('DISPATCHPRICES', 'Catalog Error')])
        self.assertEqual(check('SELEC * FROM DISPATCHPRICE'), [('SELEC', 'Parser Error')])
        self.assertEqual(check('SELECT * FROM DISPATCHPRICE WHERE'), [('WHERE', 'Parser Error')])
        self.assertEqual(check('SELECT 1; SELECT 2'), [('SELECT 2', 'Only one query at a time')])
        self.assertEqual(check('DELETE FROM DISPATCHPRICE')[0][1], 'Only SELECT queries can run here (the editor is read-only).')
        self.assertEqual(check('  '), [])

    def test_functions_have_signatures_and_help(self):
        functions = {f['name']: f for f in explorer.functions(self.db)}
        self.assertIn('date_trunc(part VARCHAR, timestamp TIMESTAMP) → TIMESTAMP', functions['date_trunc']['signatures'])
        self.assertTrue(functions['avg']['description'])
        self.assertEqual(functions['avg']['type'], 'aggregate')
        self.assertFalse([name for name in functions if not name[0].isalpha()])  # no operators or internals

    def test_rows_allow_the_chart_page_size_but_snap_others(self):
        self.assertEqual(explorer.rows(self.db, 'DISPATCHPRICE', limit=explorer.CHART_ROWS)['limit'], explorer.CHART_ROWS)
        self.assertEqual(len(explorer.rows(self.db, 'DISPATCHPRICE', limit=explorer.CHART_ROWS)['rows']), 1154)
        self.assertEqual(explorer.rows(self.db, 'DISPATCHPRICE', limit=777)['limit'], 25)

    def test_schema_lists_downloaded_tables_with_catalogue_columns(self):
        schema = explorer.schema(self.db)
        self.assertEqual(list(schema), ['DISPATCHPRICE'])
        self.assertEqual(schema['DISPATCHPRICE'][:3], ['SETTLEMENTDATE', 'RUNNO', 'REGIONID'])


class Filters(Explorer):
    def total(self, filters=None, search=None):
        return explorer.rows(self.db, 'DISPATCHPRICE', filters=filters, search=search)['total']

    def test_operators_follow_marimo(self):
        self.assertEqual(self.total([{'column': 'REGIONID', 'operator': 'in', 'values': ['SA1']}]), 577)
        self.assertEqual(self.total([{'column': 'REGIONID', 'operator': 'not_in', 'values': ['SA1']}]), 577)
        self.assertEqual(self.total([{'column': 'RRP', 'operator': 'between', 'min': 1, 'max': 10}]), 20)
        self.assertEqual(self.total([{'column': 'RRP', 'operator': '>', 'value': 570}]), 14)
        self.assertEqual(self.total([{'column': 'REGIONID', 'operator': 'starts_with', 'value': 'NS'}]), 577)
        self.assertEqual(self.total([{'column': 'REGIONID', 'operator': 'contains', 'value': 'sa'}]), 0)  # case-sensitive
        self.assertEqual(self.total([{'column': 'REGIONID', 'operator': 'regex', 'value': '^(SA|NSW)1$'}]), 1154)
        self.assertEqual(self.total([{'column': 'EEP', 'operator': 'is_null'}]), 1154)
        self.assertEqual(self.total([{'column': 'SETTLEMENTDATE', 'operator': '>=', 'value': '2026-06-02'}]), 580)  # intervals 288..577, two regions
        self.assertEqual(self.total([{'column': 'RRP', 'operator': '<=', 'value': 5},
                                     {'column': 'REGIONID', 'operator': 'equals', 'value': 'SA1'}]), 5)

    def test_search_is_case_insensitive_and_literal(self):
        self.assertEqual(self.total(search='sa1'), 577)
        self.assertEqual(self.total(search='%'), 0)

    def test_top_values_ignore_the_columns_own_filter(self):
        result = explorer.top_values(self.db, 'DISPATCHPRICE', 'REGIONID',
                                     filters=[{'column': 'REGIONID', 'operator': 'in', 'values': ['SA1']},
                                              {'column': 'RRP', 'operator': '<=', 'value': 10}])
        self.assertEqual(result['values'], [['NSW1', 10], ['SA1', 10]])

    def test_bad_filters_are_rejected(self):
        for bad in ([{'column': 'RRP; DROP', 'operator': '=='}], [{'column': 'RRP', 'operator': 'contains', 'value': '1'}],
                    [{'column': 'REGIONID', 'operator': 'in', 'values': []}], ['x']):
            with self.assertRaises(ValueError, msg=bad):
                self.total(bad)


class Summaries(Explorer):
    def test_stats_and_histograms(self):
        result = explorer.summaries(self.db, 'DISPATCHPRICE')
        self.assertTrue(result['enabled'])
        rrp = result['columns']['RRP']
        self.assertEqual((rrp['min'], rrp['max'], rrp['nulls'], rrp['unique']), (1.0, 577.0, 0, 577))
        self.assertEqual(len(rrp['bins']), explorer.SUMMARY_BINS)
        self.assertEqual(sum(b[2] for b in rrp['bins']), 1154)
        self.assertAlmostEqual(rrp['median'], 289.0)
        when = result['columns']['SETTLEMENTDATE']
        self.assertEqual((when['min'], when['bins'][0][0]), ('2026-06-01 00:05:00', '2026-06-01 00:05:00'))
        self.assertEqual(result['columns']['REGIONID'], {'nulls': 0, 'unique': 2})
        self.assertEqual(result['columns']['EEP']['nulls'], 1154)  # all-null column: no bins
        self.assertNotIn('bins', result['columns']['EEP'])

    def test_summaries_follow_filters_and_the_row_limit(self):
        filtered = explorer.summaries(self.db, 'DISPATCHPRICE', filters=[{'column': 'REGIONID', 'operator': 'in', 'values': ['SA1']}])
        self.assertEqual(filtered['total'], 577)
        from unittest.mock import patch
        with patch.object(explorer, 'SUMMARY_ROW_LIMIT', 100):
            self.assertFalse(explorer.summaries(self.db, 'DISPATCHPRICE')['enabled'])


if __name__ == '__main__':
    unittest.main()
