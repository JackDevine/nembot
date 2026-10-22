"""Shared documents: storage, data snapshots, seeding from markdown and nembot's suggestions (no network)."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import duckdb

from nembot import docs
from nembot.chat import Conversation, mode_setup, run_turn
from nembot.models import ModelInfo

GLM = ModelInfo('openrouter:z-ai/glm', 'openrouter', 'z-ai/glm', 'GLM', True, True, True)
BLOCKS = [{'id': 'B1', 'kind': 'text', 'label': 'heading 2', 'text': 'Prices'},
          {'id': 'B2', 'kind': 'text', 'text': 'Prices rose sharply in June, and prices fell in July.'},
          {'id': 'B3', 'kind': 'block', 'text': '[table: Monthly prices]'}]
SPEC = json.dumps({'mark': 'bar', 'encoding': {'x': {'field': 'region', 'type': 'nominal'},
                                               'y': {'field': 'price', 'type': 'quantitative'}}})


class DocsCase(unittest.TestCase):
    def setUp(self):
        latest = patch('nembot.chat.downloads.latest_published', return_value='2026-09')
        latest.start()
        self.addCleanup(latest.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.db = duckdb.connect(str(Path(self.directory.name) / 'nem.duckdb'))
        docs.ensure(self.db)

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()


class Storage(DocsCase):
    def test_create_save_list_delete(self):
        doc = docs.create(self.db, '  Price   brief ')
        self.assertEqual(doc['title'], 'Price brief')
        self.assertIsNone(doc['content'])
        self.assertEqual(doc['threads'], [])
        content = {'type': 'doc', 'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': 'Hi'}]}]}
        threads = [{'id': 't1', 'quote': 'Hi', 'messages': [{'author': 'user', 'text': 'Shorter?'}]}]
        docs.save(self.db, doc['id'], title='Brief', content=content, threads=threads)
        stored = docs.get(self.db, doc['id'])
        self.assertEqual((stored['title'], stored['content'], stored['threads']), ('Brief', content, threads))
        self.assertEqual([d['id'] for d in docs.list_all(self.db)], [doc['id']])
        docs.delete(self.db, doc['id'])
        self.assertIsNone(docs.get(self.db, doc['id']))

    def test_saving_clears_the_seed_and_rejects_bad_content(self):
        doc = docs.create(self.db, 'Seeded', seed=[{'markdown': 'Hi'}])
        self.assertEqual(doc['seed'], [{'markdown': 'Hi'}])
        with self.assertRaises(ValueError):
            docs.save(self.db, doc['id'], title='x', content={'type': 'paragraph'}, threads=[])
        docs.save(self.db, doc['id'], title='x', content={'type': 'doc', 'content': []}, threads=[])
        self.assertIsNone(docs.get(self.db, doc['id'])['seed'])
        self.assertIsNone(docs.save(self.db, 999, title='x', content={'type': 'doc'}, threads=[]))

    def test_images_are_typed_sized_and_deleted_with_their_document(self):
        doc = docs.create(self.db, 'Pictures')
        key = docs.add_image(self.db, doc['id'], 'image/png', b'\x89PNG')
        self.assertEqual(docs.get_image(self.db, key), ('image/png', b'\x89PNG'))
        with self.assertRaises(ValueError):
            docs.add_image(self.db, doc['id'], 'image/svg+xml', b'<svg/>')
        with self.assertRaises(LookupError):
            docs.add_image(self.db, 999, 'image/png', b'x')
        docs.delete(self.db, doc['id'])
        self.assertIsNone(docs.get_image(self.db, key))


class Snapshots(DocsCase):
    def test_table_snapshot_keeps_sql_rows_and_total(self):
        attrs = docs.snapshot(self.db, 'SELECT range AS n FROM range(3)', None, 'Numbers')
        self.assertEqual((attrs['kind'], attrs['title'], attrs['columns'], attrs['rows'], attrs['total']),
                         ('table', 'Numbers', ['n'], [[0], [1], [2]], 3))
        self.assertIsNone(attrs['suggestion'])

    def test_chart_snapshot_validates_spec_and_rows(self):
        attrs = docs.snapshot(self.db, "SELECT 'NSW1' AS region, 50 AS price", SPEC)
        self.assertEqual(attrs['kind'], 'chart')
        self.assertEqual(attrs['plot_spec']['mark'], 'bar')
        self.assertIn('Invalid plot specification', docs.snapshot(self.db, 'SELECT 1', '{"mark": "nope"}'))
        self.assertIn('no rows', docs.snapshot(self.db, 'SELECT 1 AS region, 2 AS price WHERE false', SPEC))
        self.assertIn('Only one SELECT', docs.snapshot(self.db, 'DROP TABLE x'))

    def test_seed_turns_result_markers_into_blocks(self):
        results = {'R1': {'sql': 'SELECT 1 AS one', 'plot_spec': None}, 'R2': {'sql': 'SELECT * FROM missing'}}
        seed, missing, errors = docs.seed_from_markdown(self.db, '## Summary\nPrices **rose**.\n[[R1]]\n[[R2]]\n[[R9]]', results)
        self.assertEqual(seed[0], {'markdown': '## Summary\nPrices **rose**.'})  # the browser renders it like the chat
        self.assertEqual((seed[1]['block']['kind'], seed[1]['block']['rows']), ('table', [[1]]))
        self.assertEqual(len(seed), 2)
        self.assertEqual(missing, ['R9'])
        self.assertIn('R2', errors)


def collect(gen):
    async def run():
        return [e async for e in gen]
    return asyncio.run(run())


class Suggestions(DocsCase):
    def suggest(self, session, name, **args):
        return collect(session.run({'id': 'c', 'name': name, 'input': args}))

    def test_edit_must_match_once_in_a_text_block(self):
        session = docs.DocSession(self.db, BLOCKS)
        ok = self.suggest(session, 'suggest_edit', block='B2', find='rose sharply', replace='rose')
        self.assertEqual((ok[0]['kind'], ok[0]['find'], ok[0]['replace']), ('replace', 'rose sharply', 'rose'))
        linked = self.suggest(session, 'suggest_edit', block='B2', find='June', replace='June ([AEMO](https://aemo.com.au))')
        self.assertEqual(linked[0]['replace'], 'June ([AEMO](https://aemo.com.au))')  # markdown, rendered by the browser
        self.assertFalse(ok[-1]['is_error'])
        for args, message in [({'block': 'B2', 'find': 'Prices rose', 'replace': 'x'}, 'overlaps'),
                              ({'block': 'B2', 'find': 'August', 'replace': 'x'}, 'does not appear'),
                              ({'block': 'B3', 'find': 'table', 'replace': 'x'}, 'not a text block'),
                              ({'block': 'B9', 'find': 'x', 'replace': 'y'}, 'not a text block')]:
            events = self.suggest(session, 'suggest_edit', **args)
            self.assertTrue(events[-1]['is_error'], args)
            self.assertIn(message, events[-1]['text'])
        # A separate span of the same block is fine (matching is case-sensitive).
        self.assertFalse(self.suggest(session, 'suggest_edit', block='B2', find='prices fell', replace='x')[-1]['is_error'])

    def test_repeated_text_needs_more_context(self):
        session = docs.DocSession(self.db, [{'id': 'B1', 'kind': 'text', 'text': 'up and up'}])
        events = self.suggest(session, 'suggest_edit', block='B1', find='up', replace='down')
        self.assertIn('appears 2 times', events[-1]['text'])

    def test_insert_and_data_blocks(self):
        session = docs.DocSession(self.db, BLOCKS)
        insert = self.suggest(session, 'suggest_insert', after='B2', content='A **new** point.')
        self.assertEqual((insert[0]['kind'], insert[0]['after'], insert[0]['markdown']), ('insert', 'B2', 'A **new** point.'))
        self.assertTrue(self.suggest(session, 'suggest_insert', after='B7', content='x')[-1]['is_error'])
        table = self.suggest(session, 'suggest_table', after='START', sql='SELECT 2 AS two', title='Two')
        self.assertEqual(table[0]['attrs']['suggestion'], table[0]['id'])
        self.assertIn('table of 1 rows', table[-1]['text'])
        chart = self.suggest(session, 'suggest_chart', after='B3', sql="SELECT 'SA1' AS region, 9 AS price",
                             plot_spec=SPEC, title='Prices')
        self.assertEqual(chart[0]['attrs']['kind'], 'chart')
        self.assertTrue(chart[-1]['image'])  # a PNG for models that can see
        bad = self.suggest(session, 'suggest_table', after='B1', sql='SELECT * FROM nowhere', title='x')
        self.assertTrue(bad[-1]['is_error'])


class DocTurn(DocsCase):
    def test_doc_mode_offers_suggest_tools_and_runs_them(self):
        system, tools = mode_setup('doc', GLM, False)
        self.assertIn('shared document', system)
        self.assertEqual({t['name'] for t in tools}, docs.TOOL_NAMES | {'query_mms', 'describe_table'})

        class Provider:
            async def respond(self, model, history, **options):
                if history[-1]['role'] == 'user':
                    yield {'role': 'assistant', 'model': model.ref, 'model_name': model.name, 'text': '', 'raw': {},
                           'tool_calls': [{'id': 'e1', 'name': 'suggest_edit',
                                           'input': {'block': 'B2', 'find': 'sharply', 'replace': 'steeply'}}],
                           'stop': 'tool_use'}
                    return
                yield {'type': 'text', 'delta': 'Suggested a word change.'}
                yield {'role': 'assistant', 'model': model.ref, 'model_name': model.name, 'text': 'done', 'raw': {},
                       'tool_calls': [], 'stop': 'end'}

        prompt = docs.ask_prompt('Brief', BLOCKS, 'rose sharply', 'B2',
                                 [{'author': 'user', 'text': 'Tone it down'}], 'Make it calmer')
        self.assertIn('[B2] Prices rose sharply', prompt)
        self.assertIn('[B1] (heading 2) Prices\n', prompt)
        self.assertIn('Highlighted passage in [B2]: «rose sharply»', prompt)
        self.assertIn('User: Tone it down', prompt)
        conversation = Conversation(mode='doc')
        events = collect(run_turn({'openrouter': Provider()}, self.db, conversation, prompt, GLM,
                                  doc_session=docs.DocSession(self.db, BLOCKS)))
        suggestion = next(e for e in events if e['type'] == 'doc_suggestion')
        self.assertEqual((suggestion['find'], suggestion['replace']), ('sharply', 'steeply'))
        self.assertFalse(conversation.history[2]['results'][0]['is_error'])

    def test_create_document_tool(self):
        calls = []

        async def documents(title, content, results):
            calls.append((title, content))
            return {'id': 7, 'title': title, 'missing': [], 'errors': {}}

        class Provider:
            async def respond(self, model, history, **options):
                if history[-1]['role'] == 'user':
                    yield {'role': 'assistant', 'model': model.ref, 'model_name': model.name, 'text': '', 'raw': {},
                           'tool_calls': [{'id': 'd1', 'name': 'create_document',
                                           'input': {'title': 'Brief', 'content': '# Hi'}}], 'stop': 'tool_use'}
                    return
                yield {'role': 'assistant', 'model': model.ref, 'model_name': model.name, 'text': 'ok', 'raw': {},
                       'tool_calls': [], 'stop': 'end'}

        conversation = Conversation()
        events = collect(run_turn({'openrouter': Provider()}, self.db, conversation, 'write it up', GLM,
                                  documents=documents))
        self.assertEqual(calls, [('Brief', '# Hi')])
        self.assertEqual(next(e for e in events if e['type'] == 'document')['id'], 7)
        self.assertIn('Created document 7', conversation.history[2]['results'][0]['text'])


if __name__ == '__main__':
    unittest.main()
