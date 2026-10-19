"""Explorer API over the catalogue and a small local database (no network)."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

import duckdb

from nemsplainer import explorer
from nemsplainer.ingest import _create_table
from nemsplainer.tables import TABLES


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


if __name__ == '__main__':
    unittest.main()
