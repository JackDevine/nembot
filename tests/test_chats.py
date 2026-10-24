"""Saved chats: storage, titles and transcript compaction (no network)."""
from pathlib import Path
import tempfile
import unittest

import duckdb

from nembot import chats

HISTORY = [{'role': 'user', 'text': 'prices?', 'context': '<data_status/>'},
           {'role': 'assistant', 'model': 'claude:x', 'model_name': 'X', 'text': 'Up.', 'tool_calls': [], 'raw': {}, 'stop': 'end'}]


class Chats(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = duckdb.connect(str(Path(self.directory.name) / 'nem.duckdb'))
        chats.ensure(self.db)

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def save(self, chat_id='c1', transcript=(), **overrides):
        args = dict(mode='analyst', web_search=True, model='claude:x', history=HISTORY, results={'R1': {'sql': 'SELECT 1'}},
                    transcript=list(transcript))
        chats.save(self.db, chat_id, **{**args, **overrides})

    def test_charts_are_saved_without_their_data(self):
        plot = {'type': 'plot', 'sql': 'SELECT 1 AS x', 'plot_spec': {'mark': 'bar'}, 'spec': {'datasets': {'d': [{'x': 1}]}},
                'columns': ['x'], 'rows': [[1]], 'total': 1, 'result_id': 'R1'}
        table = {'type': 'table', 'sql': 'SELECT 1 AS x', 'columns': ['x'], 'rows': [[1]], 'total': 1}
        self.save('c1', transcript=[{'text': 'plot it', 'events': [plot, table]}])
        saved = chats.get(self.db, 'c1')['transcript'][0]['events']
        self.assertEqual(saved[0], {'type': 'plot', 'sql': 'SELECT 1 AS x', 'plot_spec': {'mark': 'bar'}, 'columns': ['x'],
                                    'total': 1, 'result_id': 'R1'})
        self.assertEqual(saved[1], table)  # tables keep their (at most 200) preview rows

    def test_save_is_an_upsert_and_round_trips(self):
        self.save(transcript=[{'text': 'prices?', 'events': [{'type': 'text', 'delta': 'Up.'}]}])
        self.save(web_search=False, transcript=[{'text': 'a', 'events': []}, {'text': 'b', 'events': []}])
        chat = chats.get(self.db, 'c1')
        self.assertEqual((chat['mode'], chat['web_search'], chat['history'], chat['results']),
                         ('analyst', False, HISTORY, {'R1': {'sql': 'SELECT 1'}}))
        self.assertEqual([t['text'] for t in chat['transcript']], ['a', 'b'])
        self.assertEqual([c['id'] for c in chats.list_all(self.db)], ['c1'])
        self.assertIsNone(chats.get(self.db, 'missing'))

    def test_a_suggested_title_never_replaces_the_users(self):
        self.save()
        self.assertEqual(chats.set_title(self.db, 'c1', '"Regional prices."', by_user=False), 'Regional prices')
        self.assertEqual(chats.set_title(self.db, 'c1', 'My name', by_user=True), 'My name')
        self.assertIsNone(chats.set_title(self.db, 'c1', 'Another suggestion', by_user=False))
        self.assertEqual(chats.get(self.db, 'c1')['title'], 'My name')
        self.assertIsNone(chats.set_title(self.db, 'c1', '   ', by_user=True))
        self.assertIsNone(chats.set_title(self.db, 'nope', 'x', by_user=True))

    def test_list_is_newest_first_and_delete_removes(self):
        self.save('old')
        self.save('new')
        self.assertEqual([c['id'] for c in chats.list_all(self.db)], ['new', 'old'])
        chats.delete(self.db, 'new')
        self.assertEqual([c['id'] for c in chats.list_all(self.db)], ['old'])

    def test_compact_keeps_compared_models_text_apart(self):
        events = [{'type': 'text', 'delta': 'A1', 'branch': 0}, {'type': 'text', 'delta': 'B1', 'branch': 1},
                  {'type': 'text', 'delta': 'B2', 'branch': 1}, {'type': 'text', 'delta': 'A2', 'branch': 0}]
        self.assertEqual([(e['branch'], e['delta']) for e in chats.compact(events)], [(0, 'A1'), (1, 'B1B2'), (0, 'A2')])

    def test_compact_joins_streamed_text_and_drops_progress(self):
        events = [{'type': 'model', 'model': {}}, {'type': 'tool_running', 'name': 'query_mms'},
                  {'type': 'text', 'delta': 'Prices '}, {'type': 'text', 'delta': 'rose.'}, {'type': 'block'},
                  {'type': 'text', 'delta': 'Next.'}, {'type': 'download_progress', 'job': {}}, {'type': 'done'}]
        self.assertEqual(chats.compact(events), [{'type': 'model', 'model': {}}, {'type': 'text', 'delta': 'Prices rose.'},
                                                 {'type': 'block'}, {'type': 'text', 'delta': 'Next.'}])

    def test_fallback_title(self):
        self.assertEqual(chats.fallback_title('what were prices'), 'what were prices')
        self.assertEqual(chats.fallback_title('one two three four five six seven eight nine'),
                         'one two three four five six seven eight…')


if __name__ == '__main__':
    unittest.main()
