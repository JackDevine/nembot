"""Recent report feeds: file coverage, planning and nested-ZIP loading (no network)."""
import csv
from datetime import datetime
import hashlib
from io import BytesIO, StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import duckdb

from nemsplainer import live

REPORTS = live.REPORTS


def listing(area, family, names):
    return [f'{REPORTS}{area}/{family}/{name}' for name in names]


def fake_listings(mapping):
    def _listing(url, ttl=None):
        return mapping.get(url, [])
    return patch('nemsplainer.live.nemweb._listing', side_effect=_listing)


def interval_csv(stamp, regions):
    """A DispatchIS-style CSV with PRICE and REGIONSUM tables for one interval."""
    out = StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(['C', 'NEMP.WORLD', 'DISPATCHIS', 'AEMO', 'PUBLIC'])
    writer.writerow(['I', 'DISPATCH', 'PRICE', 5, 'SETTLEMENTDATE', 'RUNNO', 'REGIONID', 'DISPATCHINTERVAL',
                     'INTERVENTION', 'RRP', 'LASTCHANGED'])
    for region, price in regions:
        writer.writerow(['D', 'DISPATCH', 'PRICE', 5, stamp, 1, region, '1', 0, price, stamp])
    writer.writerow(['I', 'DISPATCH', 'CASE_SOLUTION', 2, 'SETTLEMENTDATE', 'RUNNO', 'INTERVENTION'])
    writer.writerow(['D', 'DISPATCH', 'CASE_SOLUTION', 2, stamp, 1, 0])
    writer.writerow(['C', 'END OF REPORT', len(regions) + 5])
    return out.getvalue().encode('utf-8-sig')


def zipped(members):
    out = BytesIO()
    with zipfile.ZipFile(out, 'w') as target:
        for name, data in members.items():
            target.writestr(name, data)
    return out.getvalue()


class Coverage(unittest.TestCase):
    def test_archive_and_current_file_coverage(self):
        family = 'DispatchIS_Reports'
        feed = live.FEEDS['DISPATCHPRICE']
        with fake_listings({
            f'{REPORTS}ARCHIVE/{family}/': listing('ARCHIVE', family, ['PUBLIC_DISPATCHIS_20261016.zip',
                                                                       'PUBLIC_DISPATCHIS_20261017.zip', 'OTHER.zip']),
            f'{REPORTS}CURRENT/{family}/': listing('CURRENT', family, [
                'PUBLIC_DISPATCHIS_202610180005_0000000543400015.zip',
                'PUBLIC_DISPATCHIS_202610180010_0000000543400587.zip']),
        }):
            archive = live._files(feed, 'ARCHIVE')
            current = live._files(feed, 'CURRENT')
        self.assertEqual([(f.start.day, f.end.day) for f in archive], [(16, 17), (17, 18)])
        self.assertEqual([f.start.minute for f in current], [5, 10])
        self.assertEqual(current[0].end, datetime(2026, 10, 18, 0, 10))

    def test_weekly_archives_and_separate_series(self):
        trading = live.FEEDS['TRADINGPRICE']
        rooftop = live.FEEDS['ROOFTOP_PV_ACTUAL']
        with fake_listings({
            f'{REPORTS}ARCHIVE/TradingIS_Reports/': listing('ARCHIVE', 'TradingIS_Reports',
                                                            ['PUBLIC_TRADINGIS_20261004_20261010.zip']),
            f'{REPORTS}ARCHIVE/ROOFTOP_PV/ACTUAL/': listing('ARCHIVE', 'ROOFTOP_PV/ACTUAL', [
                'PUBLIC_ROOFTOP_PV_ACTUAL_MEASUREMENT_20260924.zip', 'PUBLIC_ROOFTOP_PV_ACTUAL_SATELLITE_20260924.zip',
                'PUBLIC_ROOFTOP_PV_ACTUAL_MEASUREMENT_20261001.zip', 'PUBLIC_ROOFTOP_PV_ACTUAL_SATELLITE_20261001.zip']),
        }):
            week = live._files(trading, 'ARCHIVE')[0]
            pv = live._files(rooftop, 'ARCHIVE')
        self.assertEqual((week.start.day, week.end.day), (4, 11))  # inclusive end date + 1 day
        self.assertEqual(sorted((f.start.day, f.end.day) for f in pv), [(1, 8), (1, 8), (24, 1), (24, 1)])

    def test_plan_skips_current_files_beyond_the_politeness_limit(self):
        family = 'TradingIS_Reports'
        current = [f'PUBLIC_TRADINGIS_202610{11 + i // 288:02d}{(i % 288) * 5 // 60:02d}{(i % 288) * 5 % 60:02d}_0001.zip'
                   for i in range(live.CURRENT_LIMIT + 10)]
        with fake_listings({
            f'{REPORTS}ARCHIVE/{family}/': listing('ARCHIVE', family, ['PUBLIC_TRADINGIS_20261004_20261010.zip']),
            f'{REPORTS}CURRENT/{family}/': listing('CURRENT', family, current),
        }):
            result = live.plan(duckdb.connect(), 'TRADINGPRICE', '2026-09')
        self.assertEqual((result['archive_files'], result['current_files']), (1, 0))
        self.assertEqual(result['skipped_current'], live.CURRENT_LIMIT + 10)
        self.assertEqual(result['through'], '2026-10-11 00:00:00')


class Loading(unittest.TestCase):
    def test_daily_zip_of_interval_zips_loads_only_the_requested_table(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            daily = root / 'daily.zip'
            daily.write_bytes(zipped({
                f'PUBLIC_DISPATCHIS_2026101700{m:02d}.zip': zipped({'interval.CSV': interval_csv(
                    f'2026/10/17 00:{m:02d}:00', [('NSW1', '50.5'), ('SA1', '80')])}) for m in (5, 10)}))
            digest = hashlib.sha256(daily.read_bytes()).hexdigest()
            db = duckdb.connect(str(root / 'nem.duckdb'))
            files = [{'url': 'https://example/PUBLIC_DISPATCHIS_20261017.zip', 'kind': 'archive',
                      'start': '2026-10-17 00:00:00', 'end': '2026-10-18 00:00:00'}]
            with patch('nemsplainer.live.nemweb.download', return_value=(daily, digest)), patch('sys.stdout', StringIO()):
                live.load(db, 'DISPATCHPRICE', files, root / 'cache')
                live.load(db, 'DISPATCHPRICE', files, root / 'cache')  # rerun: already recorded, skipped
            rows = db.execute('SELECT SETTLEMENTDATE, REGIONID, RRP FROM DISPATCHPRICE ORDER BY 1, 2').fetchall()
            self.assertEqual([(r[0].minute, r[1], float(r[2])) for r in rows],
                             [(5, 'NSW1', 50.5), (5, 'SA1', 80.0), (10, 'NSW1', 50.5), (10, 'SA1', 80.0)])
            self.assertEqual(db.execute('SELECT count(*) FROM _nemsplainer.files').fetchone()[0], 1)
            self.assertEqual(live.latest_loaded(db, 'DISPATCHPRICE'), datetime(2026, 10, 17, 0, 10))
            db.close()


if __name__ == '__main__':
    unittest.main()
