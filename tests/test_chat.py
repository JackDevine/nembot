"""Provider-neutral chat history, rendering and the turn loop (no network)."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import duckdb

from nemsplainer import claude, openrouter
from nemsplainer.chat import NO_VISION_BLURB, Conversation, query_tool, run_turn, system_prompt, text_from_blocks
from nemsplainer.models import ModelInfo

OPUS = ModelInfo('claude:claude-opus-5-5', 'claude', 'claude-opus-5-5', 'Claude Opus 5.5', True, True, True)
GLM = ModelInfo('openrouter:z-ai/glm', 'openrouter', 'z-ai/glm', 'GLM', True, True, True)
TEXT = ModelInfo('openrouter:x/text', 'openrouter', 'x/text', 'Text Only', False, True, True)

HISTORY = [
    {'role': 'user', 'text': 'plot prices'},
    {'role': 'assistant', 'model': OPUS.ref, 'model_name': OPUS.name, 'text': 'Plotting.',
     'tool_calls': [{'id': 'toolu_1', 'name': 'visualize_mms', 'input': {'sql': 'SELECT 1', 'plot_spec': '{}'}}],
     'raw': {'claude': [{'type': 'thinking', 'thinking': '', 'signature': 'sig'},
                        {'type': 'text', 'text': 'Plotting.'},
                        {'type': 'tool_use', 'id': 'toolu_1', 'name': 'visualize_mms',
                         'input': {'sql': 'SELECT 1', 'plot_spec': '{}'}}]},
     'stop': 'tool_use'},
    {'role': 'tool', 'results': [{'id': 'toolu_1', 'name': 'visualize_mms', 'text': 'Plot created',
                                  'image': 'iVBORw0KGgo=', 'is_error': False}]},
    {'role': 'assistant', 'model': GLM.ref, 'model_name': GLM.name, 'text': 'Prices rose.', 'tool_calls': [],
     'raw': {'openrouter': {'reasoning_details': [{'type': 'reasoning.text', 'text': 'hm', 'index': 0}]}},
     'stop': 'end'},
]


class Rendering(unittest.TestCase):
    def test_claude_replays_own_turns_and_labels_others(self):
        messages = claude.render(HISTORY, OPUS)
        self.assertEqual(messages[1]['content'], HISTORY[1]['raw']['claude'])  # exact replay, thinking kept
        image = [b for b in messages[2]['content'][0]['content'] if b['type'] == 'image']
        self.assertEqual(len(image), 1)
        self.assertEqual(messages[3]['content'][0]['text'], '[Earlier reply by GLM]\n\nPrices rose.')
        self.assertEqual(claude.render(HISTORY, OPUS), messages)  # deterministic

    def test_openrouter_vision_model_gets_plot_as_user_image(self):
        messages = openrouter.render(HISTORY, GLM)
        self.assertEqual(messages[0]['content'], system_prompt(vision=True))
        assistant = messages[2]
        self.assertTrue(assistant['content'].startswith('[Earlier reply by Claude Opus 5.5]'))
        self.assertEqual(assistant['tool_calls'][0]['function']['name'], 'visualize_mms')
        self.assertNotIn('reasoning_details', assistant)
        self.assertEqual(messages[3]['role'], 'tool')
        self.assertEqual(messages[4]['content'][1]['type'], 'image_url')
        self.assertEqual(messages[5]['reasoning_details'], HISTORY[3]['raw']['openrouter']['reasoning_details'])

    def test_text_only_model_gets_no_image_and_the_blurb(self):
        messages = openrouter.render(HISTORY, TEXT)
        self.assertIn(NO_VISION_BLURB, messages[0]['content'])
        self.assertIn(NO_VISION_BLURB, messages[3]['content'])
        self.assertFalse(any(m['role'] == 'user' and isinstance(m['content'], list) for m in messages))
        self.assertNotIn('reasoning_details', messages[4])  # GLM reasoning is not sent to another model


class Helpers(unittest.TestCase):
    def test_reasoning_fragments_merge_by_index(self):
        merged = []
        openrouter._merge_reasoning(merged, [{'type': 'reasoning.text', 'index': 0, 'text': 'Hel', 'signature': None}])
        openrouter._merge_reasoning(merged, [{'type': 'reasoning.text', 'index': 0, 'text': 'lo', 'signature': 'abc'}])
        openrouter._merge_reasoning(merged, [{'type': 'reasoning.encrypted', 'index': 1, 'data': 'xyz'}])
        self.assertEqual(merged, [{'type': 'reasoning.text', 'index': 0, 'text': 'Hello', 'signature': 'abc'},
                                  {'type': 'reasoning.encrypted', 'index': 1, 'data': 'xyz'}])

    def test_call_ids_fit_anthropic_pattern(self):
        self.assertEqual(openrouter._call_id('functions.query_mms:0'), 'functions_query_mms_0')
        self.assertTrue(openrouter._call_id(None).startswith('call_'))

    def test_citation_split_text_rejoins_but_tool_separated_text_does_not(self):
        blocks = [('text', 'Prices rose '), ('text', 'sharply.'), ('tool_use', ''), ('text', 'Done.')]
        self.assertEqual(text_from_blocks(blocks), 'Prices rose sharply.\n\nDone.')

    def test_claude_sources_prefer_citations_then_consulted_pages(self):
        search = NS(type='web_search_tool_result', content=[NS(url='https://a', title='A'), NS(url='https://b', title='B')])
        uncited = NS(type='text', text='x', citations=None)
        self.assertEqual(claude.sources([search, uncited]),
                         ('consulted', [{'url': 'https://a', 'title': 'A'}, {'url': 'https://b', 'title': 'B'}]))
        cited = NS(type='text', text='x', citations=[NS(url='https://b', title='B')])
        self.assertEqual(claude.sources([search, cited]), ('cited', [{'url': 'https://b', 'title': 'B'}]))

    def test_web_search_changes_prompt_and_tools(self):
        self.assertIn('Web search:', system_prompt(True, web_search=True))
        self.assertNotIn('Web search:', system_prompt(True))
        self.assertEqual(claude.web_tools('claude-opus-5-5')[0]['type'], 'web_search_20260209')
        self.assertEqual(claude.web_tools('claude-haiku-5-5')[0]['type'], 'web_search_20250305')

    def test_claude_request_options_per_model(self):
        opus = claude.request_options('claude-opus-5-5')
        self.assertEqual(opus['fallbacks'], 'default')
        self.assertEqual(opus['thinking']['display'], 'updates')
        self.assertNotIn('fallbacks', claude.request_options('claude-haiku-5-5'))
        self.assertEqual(claude.request_options('claude-haiku-4-5-20251001'), {})


class FakeProvider:
    """Calls query_mms once, then answers; optionally fails on the second request."""

    def __init__(self, fail=False):
        self.fail, self.calls = fail, 0

    async def respond(self, model, history, web_search=False, **options):
        self.calls += 1
        if self.calls == 1:
            yield {'role': 'assistant', 'model': model.ref, 'model_name': model.name, 'text': '', 'raw': {},
                   'tool_calls': [{'id': 'c1', 'name': 'query_mms', 'input': {'sql': 'SELECT 42 AS answer'}}],
                   'stop': 'tool_use'}
            return
        if self.fail:
            raise RuntimeError('provider down')
        yield {'type': 'text', 'delta': 'It is 42.'}
        yield {'role': 'assistant', 'model': model.ref, 'model_name': model.name, 'text': 'It is 42.',
               'raw': {}, 'tool_calls': [], 'stop': 'end'}


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        latest = patch('nemsplainer.chat.downloads.latest_published', return_value='2026-09')
        latest.start()
        self.addCleanup(latest.stop)
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / 'nem.duckdb'
        duckdb.connect(str(path)).close()
        self.db = duckdb.connect(str(path), read_only=True)

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def run_turn(self, provider, conversation, text='question'):
        async def collect():
            return [e async for e in run_turn({'openrouter': provider}, self.db, conversation, text, GLM)]
        return asyncio.run(collect())


class TurnLoop(DatabaseCase):
    def test_tool_round_trip_records_history_and_final_table(self):
        conversation = Conversation()
        events = self.run_turn(FakeProvider(), conversation)
        self.assertEqual([e['type'] for e in events], ['tool_running', 'text', 'table'])
        self.assertEqual(events[-1]['rows'], [[42]])
        roles = [e['role'] for e in conversation.history]
        self.assertEqual(roles, ['user', 'assistant', 'tool', 'assistant'])
        self.assertEqual(conversation.history[2]['results'][0]['text'], 'Result id: R1\nanswer\n42\n')
        self.assertEqual(conversation.results, {'R1': {'sql': 'SELECT 42 AS answer', 'plot_spec': None}})
        self.assertEqual(events[-1]['result_id'], 'R1')

    def test_failed_turn_is_rolled_back(self):
        conversation = Conversation()
        self.run_turn(FakeProvider(), conversation, 'first')
        with self.assertRaises(RuntimeError):
            self.run_turn(FakeProvider(fail=True), conversation, 'second')
        self.assertEqual(len(conversation.history), 4)

    def test_query_tool_rejects_writes(self):
        text, image, events = query_tool(self.db, 'CREATE TABLE x (a INT)')
        self.assertEqual((text, image, events), ('Only one SELECT query is allowed.', None, []))


if __name__ == '__main__':
    unittest.main()


class FakeJobs:
    """Stands in for downloads.Downloads: jobs finish immediately."""

    def __init__(self, status='done'):
        self.status, self.submitted = status, []

    def submit(self, table, start, end, reason='explorer'):
        from nemsplainer.downloads import Job
        job = Job(table, start, end, reason)
        job.status = self.status
        job.error = 'boom' if self.status == 'failed' else None
        self.submitted.append(job)
        return job


class DownloadRequester:
    """Asks to download DISPATCHLOAD, then answers."""

    async def respond(self, model, history, web_search=False, **options):
        if history[-1]['role'] == 'user':
            yield {'role': 'assistant', 'model': model.ref, 'model_name': model.name, 'text': '', 'raw': {},
                   'tool_calls': [{'id': 'd1', 'name': 'request_download', 'input': {
                       'table': 'dispatchload', 'start_month': '2026-08', 'end_month': '2026-09', 'reason': 'Need it.'}}],
                   'stop': 'tool_use'}
            return
        yield {'role': 'assistant', 'model': model.ref, 'model_name': model.name, 'text': 'ok', 'raw': {},
               'tool_calls': [], 'stop': 'end'}


class DownloadRequests(DatabaseCase):
    def converse(self, jobs, decision):
        conversation = Conversation()

        async def collect():
            events = []
            async for event in run_turn({'openrouter': DownloadRequester()}, self.db, conversation, 'q', GLM, jobs=jobs):
                events.append(event)
                if event['type'] == 'download_request':
                    conversation.pending[event['id']].set_result(decision)
            return events
        plan = {'table': 'DISPATCHLOAD', 'loaded_months': [], 'latest_month': '2026-09', 'month_size': 1}
        with patch('nemsplainer.chat.downloads.plan', return_value=plan):
            events = asyncio.run(collect())
        return events, conversation.history[2]['results'][0]

    def test_approved_request_runs_a_job_with_the_users_months(self):
        jobs = FakeJobs()
        events, result = self.converse(jobs, {'approve': True, 'start': '2026-09', 'end': '2026-09'})
        types = [e['type'] for e in events]
        self.assertIn('download_request', types)
        self.assertIn('download_finished', types)
        job = jobs.submitted[0]
        self.assertEqual((job.table, str(job.start), str(job.end)), ('DISPATCHLOAD', '2026-09-01', '2026-09-30'))
        self.assertTrue(result['text'].startswith('Downloaded DISPATCHLOAD for 2026-09 to 2026-09'))
        self.assertFalse(result['is_error'])

    def test_declined_request_tells_the_model(self):
        jobs = FakeJobs()
        events, result = self.converse(jobs, {'approve': False})
        self.assertEqual(jobs.submitted, [])
        self.assertIn('declined', result['text'])

    def test_failed_download_is_reported_as_an_error(self):
        events, result = self.converse(FakeJobs('failed'), {'approve': True})
        self.assertTrue(result['is_error'])
        self.assertIn('boom', result['text'])

    def test_user_messages_carry_a_frozen_data_status(self):
        conversation = Conversation()
        self.run_turn(FakeProvider(), conversation)
        context = conversation.history[0]['context']
        self.assertTrue(context.startswith('<data_status>'))
        self.assertIn('Latest month published by AEMO: 2026-09', context)
