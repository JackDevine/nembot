"""Provider-neutral chat history, rendering and the turn loop (no network)."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import duckdb

from nembot import claude, openrouter
from nembot.chat import (NO_VISION_BLURB, STOPPED, Conversation, keep_branch, query_tool, run_compare, run_single, run_turn,
                         stop_turn, system_prompt, rerun_chart, text_from_blocks, visualize_tool)
from nembot.models import ModelInfo

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
        latest = patch('nembot.chat.downloads.latest_published', return_value='2026-09')
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


class Timings(DatabaseCase):
    def test_queries_and_plots_report_wall_clock_times(self):
        _, _, (table,) = query_tool(self.db, 'SELECT 1 AS x')
        self.assertIsInstance(table['query_seconds'], float)
        self.assertGreaterEqual(table['query_seconds'], 0)
        spec = json.dumps({'mark': 'point', 'encoding': {'x': {'field': 'x', 'type': 'quantitative'}}})
        _, png, (plot,) = visualize_tool(self.db, 'SELECT range AS x FROM range(10)', spec)
        self.assertTrue(png)
        self.assertGreater(plot['render_seconds'], 0)  # building the chart and its PNG takes measurable time
        self.assertIsInstance(plot['query_seconds'], float)
        rerun = rerun_chart(self.db, 'SELECT range AS x FROM range(10)', json.loads(spec))
        self.assertIsInstance(rerun['render_seconds'], float)


class LargePlots(DatabaseCase):
    def test_every_row_is_plotted_without_validating_each_one(self):
        spec = json.dumps({'mark': 'line', 'encoding': {'x': {'field': 'i', 'type': 'quantitative'},
                                                        'y': {'field': 'v', 'type': 'quantitative'}}})
        started = time.monotonic()
        text, image, events = visualize_tool(self.db, 'SELECT i, i * 2 AS v FROM range(30000) t(i)', spec)
        self.assertLess(time.monotonic() - started, 20)  # validating every row took minutes
        self.assertTrue(image)
        self.assertEqual(text.split('.')[0], 'Plot created from all 30000 rows')
        (rows,) = events[0]['spec']['datasets'].values()
        self.assertEqual((len(rows), rows[-1]), (30000, {'i': 29999, 'v': 59998}))

    def test_saved_charts_are_rerun_from_sql(self):
        event = rerun_chart(self.db, 'SELECT i, i * 2 AS v FROM range(3) t(i)', {'mark': 'line', 'encoding': {
            'x': {'field': 'i', 'type': 'quantitative'}, 'y': {'field': 'v', 'type': 'quantitative'}}})
        self.assertEqual((event['type'], event['total'], event['rows']), ('plot', 3, [[0, 0], [1, 2], [2, 4]]))
        (rows,) = event['spec']['datasets'].values()
        self.assertEqual(len(rows), 3)
        self.assertIn('error', rerun_chart(self.db, 'DROP TABLE x', {'mark': 'line'}))
        self.assertIn('Omit data', rerun_chart(self.db, 'SELECT 1', {'mark': 'line', 'data': {'url': 'http://x'}})['error'])


class Hanging:
    """Streams some text (or asks for a tool), then hangs until cancelled, like a slow model."""

    def __init__(self, text='Prices peaked', tool=False):
        self.text, self.tool = text, tool

    async def respond(self, model, history, **options):
        if self.tool and history[-1]['role'] == 'user':
            yield {'role': 'assistant', 'model': model.ref, 'model_name': model.name, 'text': 'Let me check.', 'raw': {},
                   'tool_calls': [{'id': 't1', 'name': 'query_mms', 'input': {'sql': 'SELECT 1'}}], 'stop': 'tool_use'}
            return
        yield {'type': 'text', 'delta': self.text}
        await asyncio.Event().wait()  # never set: only cancellation ends this
        yield {'role': 'assistant', 'model': model.ref, 'model_name': model.name, 'text': self.text, 'raw': {},
               'tool_calls': [], 'stop': 'end'}


class StopTurns(DatabaseCase):
    def stop_after(self, events_before_stop, run, conversation, branch=None):
        """Collect a turn's events, stopping it (or one branch) once ``events_before_stop`` have arrived."""
        async def go():
            events = []
            async for event in run:
                events.append(event)
                if len(events) == events_before_stop:
                    self.assertTrue(stop_turn(conversation, branch))
            return events
        return asyncio.run(asyncio.wait_for(go(), 10))

    def test_stop_keeps_what_was_written_and_says_it_was_stopped(self):
        conversation = Conversation()
        events = self.stop_after(1, run_single({'openrouter': Hanging()}, self.db, conversation, 'q', GLM), conversation)
        self.assertEqual(events, [{'type': 'text', 'delta': 'Prices peaked'}, {'type': 'stopped'}])
        self.assertEqual([h['role'] for h in conversation.history], ['user', 'assistant'])
        self.assertEqual(conversation.history[-1]['text'], f'Prices peaked\n\n{STOPPED}')
        self.assertEqual(conversation.running, {})
        self.run_turn(FakeProvider(), conversation, 'carry on')  # the chat continues normally
        self.assertEqual([h['role'] for h in conversation.history][-3:], ['assistant', 'tool', 'assistant'])

    def test_stop_during_a_tool_drops_the_unanswered_call(self):
        async def slow_tool(db, call):
            await asyncio.Event().wait()
        conversation = Conversation()
        with patch('nembot.chat.execute_tool', slow_tool):
            events = self.stop_after(1, run_single({'openrouter': Hanging(tool=True)}, self.db, conversation, 'q', GLM),
                                     conversation)
        self.assertEqual([e['type'] for e in events], ['tool_running', 'stopped'])
        self.assertEqual([h['role'] for h in conversation.history], ['user', 'assistant'])
        self.assertEqual(conversation.history[-1]['tool_calls'], [])
        self.assertEqual(conversation.history[-1]['text'], f'Let me check.\n\n{STOPPED}')

    def test_leaving_without_stop_forgets_the_turn(self):
        conversation = Conversation()

        async def go():
            run = run_single({'openrouter': Hanging()}, self.db, conversation, 'q', GLM)
            await run.__anext__()
            await run.aclose()  # the browser went away
        asyncio.run(go())
        self.assertEqual(conversation.history, [])
        self.assertEqual(conversation.running, {})

    def test_stopping_one_compared_model_lets_the_others_finish(self):
        conversation = Conversation()
        providers = {'claude': Hanging('Opus says'), 'openrouter': FakeProvider()}
        run = run_compare(providers, self.db, conversation, 'q', [OPUS, GLM])

        async def go():
            events = []
            async for event in run:
                events.append(event)
                if event == {'type': 'text', 'delta': 'Opus says', 'branch': 0}:
                    self.assertTrue(stop_turn(conversation, 0))
            return events
        events = asyncio.run(asyncio.wait_for(go(), 10))
        self.assertIn({'type': 'stopped', 'branch': 0}, events)
        self.assertEqual(sorted(e['branch'] for e in events if e['type'] == 'branch_done'), [0, 1])
        self.assertEqual(events[-1], {'type': 'kept', 'branch': 1})  # the model that wasn't stopped
        self.assertEqual(conversation.branches[0]['history'][-1]['text'], f'Opus says\n\n{STOPPED}')
        self.assertTrue(keep_branch(conversation, 0))  # a stopped reply can still be chosen


class CompareTurns(DatabaseCase):
    def compare(self, conversation, providers, models, text='question'):
        async def collect():
            return [e async for e in run_compare(providers, self.db, conversation, text, models)]
        return asyncio.run(collect())

    def test_each_model_answers_on_its_own_branch_and_the_first_is_kept(self):
        conversation = Conversation()
        self.run_turn(FakeProvider(), conversation, 'first')  # R1 already taken
        events = self.compare(conversation, {'claude': FakeProvider(), 'openrouter': FakeProvider()}, [OPUS, GLM])
        self.assertEqual(events[0]['type'], 'compare')
        self.assertEqual([m['ref'] for m in events[0]['models']], [OPUS.ref, GLM.ref])
        self.assertEqual(events[-1], {'type': 'kept', 'branch': 0})
        tables = [e for e in events if e['type'] == 'table']
        self.assertEqual(sorted((e['branch'], e['result_id']) for e in tables), [(0, 'R2'), (1, 'R2')])
        self.assertEqual(len(conversation.history), 8)
        self.assertEqual(conversation.history[-1]['model'], OPUS.ref)
        self.assertTrue(keep_branch(conversation, 1))
        self.assertEqual(conversation.history[-1]['model'], GLM.ref)
        self.assertEqual(conversation.history[:4], conversation.branches[0]['history'][:4])
        self.assertFalse(keep_branch(conversation, 2))

    def test_a_failing_model_is_reported_and_never_kept(self):
        conversation = Conversation()
        events = self.compare(conversation, {'claude': FakeProvider(fail=True), 'openrouter': FakeProvider()}, [OPUS, GLM])
        self.assertIn({'type': 'error', 'message': 'provider down', 'branch': 0}, events)
        self.assertEqual(events[-1], {'type': 'kept', 'branch': 1})
        self.assertEqual(conversation.history[-1]['model'], GLM.ref)
        self.assertFalse(keep_branch(conversation, 0))


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

    def test_plot_carries_its_data_instead_of_a_final_table(self):
        spec = '{"mark": "bar", "encoding": {"x": {"field": "r", "type": "nominal"}, "y": {"field": "p", "type": "quantitative"}}}'

        class PlotProvider:
            async def respond(self, model, history, **options):
                if history[-1]['role'] == 'user':
                    yield {'role': 'assistant', 'model': model.ref, 'model_name': model.name, 'text': '', 'raw': {},
                           'tool_calls': [{'id': 'p1', 'name': 'visualize_mms', 'input': {
                               'sql': "SELECT 'SA1' AS r, 90 AS p UNION ALL SELECT 'NSW1', 70", 'plot_spec': spec}}],
                           'stop': 'tool_use'}
                    return
                yield {'role': 'assistant', 'model': model.ref, 'model_name': model.name, 'text': 'SA1 is higher.',
                       'raw': {}, 'tool_calls': [], 'stop': 'end'}

        conversation = Conversation()
        events = self.run_turn(PlotProvider(), conversation)
        self.assertEqual([e['type'] for e in events], ['tool_running', 'plot'])  # no table at the end
        plot = events[1]
        self.assertEqual((plot['columns'], plot['total'], plot['result_id']), (['r', 'p'], 2, 'R1'))
        self.assertCountEqual(plot['rows'], [['SA1', 90], ['NSW1', 70]])
        result = conversation.history[2]['results'][0]['text']
        self.assertTrue(result.startswith('Result id: R1\nPlot created from all 2 rows. First 10 rows:\nr,p\n'))
        self.assertEqual(conversation.results['R1']['plot_spec']['mark'], 'bar')

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
        from nembot.downloads import Job
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
        with patch('nembot.chat.downloads.plan', return_value=plan):
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
