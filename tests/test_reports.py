"""Report and notebook export from chat results (no network)."""
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import duckdb

from nemsplainer import reports
from nemsplainer.client import parquet_bytes

SPEC = {'mark': 'line', 'encoding': {'x': {'field': 'x', 'type': 'quantitative'},
                                     'y': {'field': 'y', 'type': 'quantitative'}}, 'width': 400, 'height': 200}
RESULTS = {'R1': {'sql': 'SELECT x, y FROM t', 'plot_spec': None},
           'R2': {'sql': 'SELECT x, y FROM t', 'plot_spec': SPEC},
           'R3': {'sql': 'SELECT * FROM missing', 'plot_spec': None}}
CONTENT = '## Findings\nPrices cost $5 and **rose**.\n\n[[R1]]\n\n## Trend\n- Up\n\n[[R2]]\n[[R9]]\n'


class Export(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.db = duckdb.connect(str(self.dir / 'nem.duckdb'))
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE t AS SELECT range AS x, range * 2 AS y FROM range(30)')

    def export(self, fmt, content=CONTENT):
        meta = reports.export(self.db, self.dir / 'reports', fmt, 'My report', content, RESULTS,
                              url='http://127.0.0.1:1', database=str(self.dir / 'nem.duckdb'))
        return meta, (self.dir / 'reports' / meta['file']).read_bytes()

    def test_blocks_split_markers(self):
        self.assertEqual(reports.blocks('a\n[[R1]]\nb'), [('text', 'a'), ('result', 'R1'), ('text', 'b')])

    def test_every_format_builds_and_reports_unknown_ids(self):
        for fmt, ext in reports.FORMATS.items():
            meta, data = self.export(fmt)
            self.assertTrue(meta['file'].endswith(ext))
            self.assertEqual(meta['missing'], ['R9'], fmt)
            self.assertGreater(len(data), 100, fmt)
        listed = reports.listing(self.dir / 'reports')
        self.assertEqual(sorted(r['format'] for r in listed), sorted(reports.FORMATS))

    def test_every_export_credits_aemo(self):
        from nemsplainer.attribution import CREDIT
        for fmt in reports.FORMATS:
            _, data = self.export(fmt)
            if fmt in ('docx', 'pptx', 'xlsx'):
                with zipfile.ZipFile(io.BytesIO(data)) as package:
                    text = ''.join(package.read(n).decode('utf-8', 'ignore') for n in package.namelist() if n.endswith('.xml'))
                self.assertIn('Australian Energy Market Operator', text, fmt)
            elif fmt == 'pdf':
                self.assertGreater(len(data), 1000)  # rendered from the HTML, which carries the credit
            else:
                self.assertIn(CREDIT.replace("'", "&#x27;") if fmt == 'html' else CREDIT, data.decode(), fmt)

    def test_html_has_table_plot_and_sql(self):
        _, data = self.export('html')
        page = data.decode()
        self.assertIn('<table>', page)
        self.assertIn('vegaEmbed("#vis-R2"', page)
        self.assertIn('SELECT x, y FROM t', page)
        self.assertIn('First 20 of 30 rows', page)

    def test_failed_results_are_reported(self):
        meta, _ = self.export('html', '[[R3]]')
        self.assertIn('missing', meta['errors']['R3'])

    def test_xlsx_includes_every_result_when_none_placed(self):
        _, data = self.export('xlsx', 'Just text.')
        with zipfile.ZipFile(io.BytesIO(data)) as book:
            workbook = book.read('xl/workbook.xml').decode()
        for sheet in ('Summary', 'SQL', 'R1', 'R2', 'R3'):
            self.assertIn(f'name="{sheet}"', workbook)

    def test_notebooks_query_through_the_client(self):
        _, data = self.export('jupyter')
        notebook = json.loads(data)
        code = '\n'.join(''.join(c['source']) for c in notebook['cells'] if c['cell_type'] == 'code')
        self.assertIn('from nemsplainer.client import query', code)
        self.assertIn("url='http://127.0.0.1:1'", code)
        self.assertIn('validate=False', code)
        markdown = ''.join(''.join(c['source']) for c in notebook['cells'] if c['cell_type'] == 'markdown')
        self.assertIn(r'\$5', markdown)  # dollars escaped so Jupyter doesn't render LaTeX
        _, data = self.export('marimo')
        compile(data.decode(), 'notebook.py', 'exec')  # valid Python

    def test_parquet_query_endpoint_is_select_only(self):
        self.assertTrue(parquet_bytes(self.db, 'SELECT * FROM t').startswith(b'PAR1'))
        with self.assertRaises(ValueError):
            parquet_bytes(self.db, 'DROP TABLE t')


if __name__ == '__main__':
    unittest.main()
