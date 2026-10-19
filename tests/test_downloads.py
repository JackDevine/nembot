"""Generic-table loading and the web app's download jobs (fake NEMWEB, real DuckDB)."""
import csv
from datetime import date
from io import StringIO
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import duckdb

from nemsplainer import downloads, nemweb
from nemsplainer.ingest import load_mms
from test_ingest import MonthlyArchive

COLUMNS = ('DUID', 'BIDTYPE', 'TRADINGDATE', 'OFFERDATETIME', 'DIRECTION', 'PERIODID', 'MAXAVAIL')


def bid_csv(rows):
    """BIDOFFERPERIOD: no LASTCHANGED, millisecond OFFERDATETIME, identity not known in advance."""
    out = StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(['C', 'NEMWEB', 'PUBLIC', 'MONTHLY'])
    writer.writerow(['I', 'BID', 'BIDOFFERPERIOD', 1, *COLUMNS])
    for row in rows:
        writer.writerow(['D', 'BID', 'BIDOFFERPERIOD', 1, *row])
    writer.writerow(['C', 'END OF REPORT', len(rows) + 3])
    return out.getvalue().encode('utf-8-sig')


def bid(maxavail, period='1', offered='2026/08/31 12:00:00.123'):
    return ['UNIT1', 'ENERGY', '2026/09/01 00:00:00', offered, 'GEN', period, maxavail]


class Fixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database = self.root / 'nem.duckdb'
        self.cache = self.root / 'cache'
        self.network = MonthlyArchive()
        for target in (patch("nemsplainer.nemweb._open", side_effect=self.network.open),
                       patch('nemsplainer.nemweb.REQUEST_INTERVAL', 0)):
            target.start()
            self.addCleanup(target.stop)
        nemweb.clear_cache()
        self.addCleanup(nemweb.clear_cache)

    def publish(self, month, rows, part=1):
        filename = f'PUBLIC_ARCHIVE#BIDOFFERPERIOD#FILE{part:02d}#2026{month:02d}010000'
        self.network.add_file('BIDOFFERPERIOD', 2026, month, [], members={filename + '.CSV': bid_csv(rows)}, part=part)


class GenericTables(Fixture):
    def load(self, start, end, **kwargs):
        nemweb.clear_cache()
        with patch('sys.stdout', StringIO()):
            return load_mms(['BIDOFFERPERIOD'], start, end, database=self.database, cache_dir=self.cache, **kwargs)

    def test_whole_months_load_with_inferred_identity_and_milliseconds(self):
        self.publish(9, [bid('100'), bid('50', period='2')])
        events = []
        self.load('2026-09-10', '2026-09-10', progress=events.append).close()  # one day still loads the month
        with duckdb.connect(str(self.database)) as c:
            rows = c.execute('SELECT PERIODID, MAXAVAIL, OFFERDATETIME FROM BIDOFFERPERIOD ORDER BY 1').fetchall()
            window = c.execute('SELECT window_start, window_end FROM _nemsplainer.files').fetchone()
        self.assertEqual([(int(p), float(m)) for p, m, _ in rows], [(1, 100.0), (2, 50.0)])
        self.assertEqual(rows[0][2].microsecond, 123000)
        self.assertEqual([w.date() for w in window], [date(2026, 9, 1), date(2026, 10, 1)])
        self.assertEqual([e['stage'] for e in events][0], 'listing')
        self.assertIn('download', [e['stage'] for e in events])
        self.assertEqual(events[-1]['stage'], 'done')

    def test_tables_without_lastchanged_take_the_latest_archive(self):
        self.publish(8, [bid('100')])
        self.publish(9, [bid('70')])
        self.load('2026-08-01', '2026-08-31').close()
        self.network.files.clear(); self.network.months.clear()
        self.publish(8, [bid('100')])
        self.publish(9, [bid('70')])
        self.load('2026-09-01', '2026-09-30').close()
        with duckdb.connect(str(self.database)) as c:
            self.assertEqual(float(c.execute('SELECT MAXAVAIL FROM BIDOFFERPERIOD').fetchone()[0]), 70.0)

    def test_conflicting_duplicates_in_one_file_fail_loudly(self):
        self.publish(9, [bid('100'), bid('90')])
        with self.assertRaisesRegex(RuntimeError, 'conflicting'):
            self.load('2026-09-01', '2026-09-30')

    def test_shared_connection_is_left_open_and_cancel_keeps_finished_files(self):
        self.publish(8, [bid('100')])
        self.publish(9, [bid('70')])
        connection = duckdb.connect(str(self.database))
        self.addCleanup(connection.close)
        cancel = threading.Event()

        def progress(event):
            if event['stage'] == 'download' and event.get('month') == '2026-09':
                cancel.set()  # cancel while the second month is downloading
        with self.assertRaises(nemweb.Cancelled):
            self.load('2026-08-01', '2026-09-30', connection=connection, progress=progress, cancel=cancel)
        self.assertEqual(connection.execute('SELECT count(*) FROM BIDOFFERPERIOD').fetchone()[0], 1)
        self.assertEqual(downloads.loaded_months(connection, 'BIDOFFERPERIOD'), ['2026-08'])


class Jobs(Fixture):
    def wait(self, job, timeout=10):
        deadline = time.monotonic() + timeout
        while job.status in ('queued', 'running') and time.monotonic() < deadline:
            time.sleep(0.02)
        return job

    def test_jobs_run_in_order_and_report_failures(self):
        self.publish(9, [bid('100')])
        connection = duckdb.connect(str(self.database))
        self.addCleanup(connection.close)
        manager = downloads.Downloads(connection, self.database, self.cache)
        with patch('sys.stdout', StringIO()), patch.object(downloads.log, 'exception'):
            good = self.wait(manager.submit('BIDOFFERPERIOD', date(2026, 9, 1), date(2026, 9, 30)))
            bad = self.wait(manager.submit('BIDOFFERPERIOD', date(2026, 7, 1), date(2026, 7, 31)))
        self.assertEqual(good.status, 'done', good.error)
        self.assertEqual(bad.status, 'failed')
        self.assertIn('2026-07', bad.error)
        self.assertEqual(downloads.loaded_months(connection, 'BIDOFFERPERIOD'), ['2026-09'])

    def test_month_range_validation(self):
        self.assertEqual(downloads.month_range('2026-02', '2026-03'), (date(2026, 2, 1), date(2026, 3, 31)))
        for start, end in (('2026-3', '2026-03'), ('2026-04', '2026-03'), ('2008-01', '2008-02')):
            with self.assertRaises(ValueError):
                downloads.month_range(start, end)


if __name__ == '__main__':
    unittest.main()
