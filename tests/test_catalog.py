"""The generated data-model catalogue and schemas derived from it (no network)."""
import csv
from io import StringIO
from pathlib import Path
import tempfile
import unittest
import zipfile

from nemsplainer import catalog, nemweb
from nemsplainer.tables import DEFAULT_TABLES, TABLES, Table

# The original hand-written DISPATCHPRICE schema (from the HTML report).
DISPATCHPRICE = [
    ('SETTLEMENTDATE', 'TIMESTAMP'), ('RUNNO', 'DECIMAL(3,0)'), ('REGIONID', 'VARCHAR'),
    ('DISPATCHINTERVAL', 'VARCHAR'), ('INTERVENTION', 'DECIMAL(2,0)'), ('RRP', 'DECIMAL(15,5)'),
    ('EEP', 'DECIMAL(15,5)'), ('ROP', 'DECIMAL(15,5)'), ('APCFLAG', 'DECIMAL(3,0)'),
    ('MARKETSUSPENDEDFLAG', 'DECIMAL(3,0)'), ('LASTCHANGED', 'TIMESTAMP'),
]


class Catalogue(unittest.TestCase):
    def test_source_and_size(self):
        source = catalog.source()
        self.assertEqual(source['format'], 'pdf')
        self.assertTrue(source['version'].startswith('5.7'))
        self.assertGreater(len(catalog.tables()), 500)
        self.assertEqual(catalog.load()['problems'], [])

    def test_every_table_is_consistent(self):
        for name, table in catalog.tables().items():
            columns = [c['name'] for c in table['columns']]
            self.assertTrue(columns, name)
            self.assertEqual(len(columns), len(set(columns)), name)
            self.assertLessEqual(set(table['primary_key']), set(columns), name)
            self.assertTrue(all(c['type'] for c in table['columns']), name)

    def test_generated_schema_matches_the_hand_written_one(self):
        self.assertEqual(list(TABLES['DISPATCHPRICE'].columns.items())[:len(DISPATCHPRICE)], DISPATCHPRICE)
        self.assertEqual(TABLES['DISPATCHPRICE'].primary_key,
                         ('DISPATCHINTERVAL', 'INTERVENTION', 'REGIONID', 'RUNNO', 'SETTLEMENTDATE'))
        for name in DEFAULT_TABLES:
            self.assertTrue(catalog.tables()[name]['in_archive'], name)

    def test_type_mapping(self):
        cases = {'DATE': 'TIMESTAMP', 'TIMESTAMP(3)': 'TIMESTAMP', 'VARCHAR2(10)': 'VARCHAR', 'CHAR(1)': 'VARCHAR',
                 'NUMBER(15,5)': 'DECIMAL(15,5)', 'NUMBER(3)': 'DECIMAL(3,0)', 'NUMBER': 'DOUBLE', 'BLOB': None}
        for oracle, expected in cases.items():
            self.assertEqual(catalog.duckdb_type(oracle), expected, oracle)


def archive(path: Path, identities: list[tuple[str, str]]) -> Path:
    out = StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(['C', 'NEMWEB', 'PUBLIC', 'MONTHLY'])
    count = 1
    for dataset, csv_table in identities:
        writer.writerow(['I', dataset, csv_table, 1, 'DUID', 'MW'])
        writer.writerow(['D', dataset, csv_table, 1, 'UNIT1', '5'])
        count += 2
    writer.writerow(['C', 'END OF REPORT', count + 1])
    with zipfile.ZipFile(path, 'w') as target:
        target.writestr('PUBLIC_ARCHIVE#X#FILE01#202609010000.CSV', out.getvalue().encode('utf-8-sig'))
    return path


class IdentityInference(unittest.TestCase):
    table = Table('X', None, None, {'DUID': 'VARCHAR', 'MW': 'DECIMAL(6,0)'}, ('DUID',))

    def test_identity_comes_from_the_first_i_record(self):
        with tempfile.TemporaryDirectory() as directory:
            zip_path = archive(Path(directory) / 'x.zip', [('SOME', 'THING')])
            outputs = nemweb.normalize(zip_path, self.table, Path(directory), 'https://example/x.zip')
            self.assertEqual(Path(outputs[0][0]).read_text().splitlines(), ['DUID,MW', 'UNIT1,5'])

    def test_archives_with_two_tables_fail_loudly(self):
        with tempfile.TemporaryDirectory() as directory:
            zip_path = archive(Path(directory) / 'x.zip', [('SOME', 'THING'), ('OTHER', 'THING')])
            with self.assertRaisesRegex(RuntimeError, 'more than one MMS table'):
                nemweb.normalize(zip_path, self.table, Path(directory), 'https://example/x.zip')


if __name__ == '__main__':
    unittest.main()
