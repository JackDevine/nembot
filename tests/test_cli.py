import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from nemsplainer.cli import main


class ConfigErrors(unittest.TestCase):
    def test_bad_config_fails_before_database_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / 'never.duckdb'
            config = root / 'input.toml'
            for contents in ('watch = true', 'tables = "DISPATCHPRICE"', 'start = 2026-08-01T12:00:00', 'database = 9', 'start = "broken"', 'tables = ['):
                with self.subTest(contents=contents):
                    config.write_text(contents)
                    with patch.object(sys, 'argv', ['nemsplainer', '--config', str(config), '--database', str(database)]), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                        main()
                    self.assertEqual(result.exception.code, 2)
                    self.assertFalse(database.exists())

    def test_no_watch_flag_exists(self):
        with patch.object(sys, 'argv', ['nemsplainer', '--watch']), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
            main()
        self.assertEqual(result.exception.code, 2)

class ConfigLoad(unittest.TestCase):
    def test_flags_replace_config_and_connection_is_released(self):
        import duckdb
        from test_ingest import MonthlyArchive
        from nemsplainer import nemweb
        nemweb.clear_cache()
        self.addCleanup(nemweb.clear_cache)

        network = MonthlyArchive()
        network.add_file('DISPATCHPRICE', 2026, 8, [dict(
            SETTLEMENTDATE='2026/08/01 00:05:00', RUNNO='1', REGIONID='NSW1',
            DISPATCHINTERVAL='20260731241', INTERVENTION='0', RRP='12.34',
            LASTCHANGED='2026/08/01 00:00:00')])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'load.toml'
            unused = root / 'unused.duckdb'
            config.write_text(f'tables = ["DISPATCH_UNIT_SCADA"]\nstart = 2026-07-01\nend = 2026-07-31\ndatabase = "{unused}"\ncache_dir = "{root / "cache"}"\n')
            observations = []
            for config_flags in (['--config', str(config)], ['--cache-dir', str(root / 'cache')]):
                database = root / f'actual{len(observations)}.duckdb'
                argv = ['nemsplainer', *config_flags, '--tables', 'DISPATCHPRICE', '--start', '2026-08-01', '--end', '2026-08-01', '--database', str(database)]
                with patch.object(sys, 'argv', argv), patch('urllib.request.urlopen', side_effect=network.open), patch('nemsplainer.nemweb.REQUEST_INTERVAL', 0), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    main()
                with duckdb.connect(str(database)) as c:
                    observations.append(c.sql('SELECT REGIONID, RRP FROM DISPATCHPRICE').fetchall())
            self.assertEqual(observations[0], observations[1])
            self.assertEqual(str(observations[0][0][1]), '12.34000')
            self.assertFalse(unused.exists())



if __name__ == '__main__':
    unittest.main()
