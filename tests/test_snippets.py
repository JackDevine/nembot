"""Snippets stored in the database and re-run on demand (no network)."""
from pathlib import Path
import tempfile
import unittest

import duckdb

from nemsplainer import snippets

SPEC = {'mark': 'line', 'encoding': {'x': {'field': 'x', 'type': 'quantitative'},
                                     'y': {'field': 'y', 'type': 'quantitative'}}, 'width': 640, 'height': 320}


class Snippets(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = duckdb.connect(str(Path(directory.name) / 'nem.duckdb'))
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE t AS SELECT range AS x, range * 2 AS y FROM range(5)')
        snippets.ensure(self.db)
        snippets.ensure(self.db)  # idempotent

    def test_create_list_rename_delete(self):
        made = snippets.create(self.db, name='  "Doubling line"  ', sql='SELECT x, y FROM t', plot_spec=SPEC,
                               question='plot it', model='claude:claude-haiku-5-5', conversation='c1')
        self.assertEqual(made['name'], 'Doubling line')
        self.assertEqual(made['plot_spec'], SPEC)
        self.assertEqual([s['id'] for s in snippets.list_all(self.db)], [made['id']])
        self.assertEqual(snippets.rename(self.db, made['id'], 'Renamed')['name'], 'Renamed')
        snippets.delete(self.db, made['id'])
        self.assertIsNone(snippets.get(self.db, made['id']))

    def test_run_uses_the_latest_data(self):
        made = snippets.create(self.db, name='n', sql='SELECT x, y FROM t', plot_spec=SPEC, question=None,
                               model=None, conversation=None)
        self.db.execute('INSERT INTO t VALUES (10, 20)')
        result = snippets.run(self.db, made)
        self.assertIsNone(result['error'])
        self.assertEqual([e['type'] for e in result['events']], ['plot', 'table'])
        self.assertEqual(result['events'][1]['total'], 6)

    def test_broken_snippets_report_an_error(self):
        made = snippets.create(self.db, name='n', sql='SELECT * FROM missing_table', plot_spec=None,
                               question=None, model=None, conversation=None)
        result = snippets.run(self.db, made)
        self.assertIn('missing_table', result['error'])
        self.assertEqual(result['events'], [])

    def test_fallback_names(self):
        self.assertEqual(snippets.fallback_name('SELECT * FROM dispatchprice', None), 'DISPATCHPRICE query')
        self.assertEqual(snippets.fallback_name('SELECT 1', SPEC), 'Saved plot')
        self.assertEqual(snippets.clean_name(''), 'Untitled snippet')


if __name__ == '__main__':
    unittest.main()
