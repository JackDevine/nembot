"""The Claude plan provider: nembot's turn loop driving an Agent SDK session (no Claude Code process)."""
import asyncio
import unittest
from unittest.mock import patch

import claude_agent_sdk as sdk

from nembot import subscription
from nembot.chat import Conversation, run_single, run_turn, stop_turn
from nembot.models import ModelInfo

from test_chat import DatabaseCase, GLM, HISTORY

PLAN = ModelInfo('subscription:claude-opus-5-5', 'subscription', 'claude-opus-5-5', 'Claude Opus 5.5', True, True, True)


def stream(kind, **fields):
    return sdk.StreamEvent(uuid='u', session_id='s', event={'type': kind, **fields}, parent_tool_use_id=None)


def assistant(*blocks):
    return sdk.AssistantMessage(content=list(blocks), model='claude-opus-5-5')


class FakeClaudeCode:
    """Plays Claude Code: asks for query_mms, calls nembot's handler as the CLI would, then answers with its result."""
    instances = []

    def __init__(self, options, rate_limited=False):
        self.options, self.rate_limited = options, rate_limited
        self.prompt, self.result, self.disconnected = None, None, False
        FakeClaudeCode.instances.append(self)

    async def connect(self):
        pass

    async def query(self, prompt):
        self.prompt = prompt

    async def disconnect(self):
        self.disconnected = True

    async def receive_messages(self):
        if self.rate_limited:
            yield sdk.RateLimitEvent(rate_limit_info=sdk.RateLimitInfo(status='rejected', resets_at=None),
                                     uuid='u', session_id='s')
        yield stream('message_start', message={'usage': {'input_tokens': 10, 'cache_read_input_tokens': 5}})
        yield stream('content_block_start', content_block={'type': 'text', 'text': ''})
        yield stream('content_block_delta', delta={'type': 'text_delta', 'text': 'Let me check.'})
        yield assistant(sdk.TextBlock('Let me check.'))
        yield stream('content_block_start', content_block={'type': 'tool_use', 'name': 'mcp__nembot__query_mms'})
        args = {'sql': 'SELECT 42 AS answer'}
        yield assistant(sdk.ToolUseBlock('toolu_1', 'mcp__nembot__query_mms', args))
        handler = self.options.mcp_servers['nembot']['tools']['query_mms'].handler
        call = asyncio.ensure_future(handler(args))  # the CLI runs the tool while the stream ends
        yield stream('message_delta', usage={'output_tokens': 7})
        yield stream('message_stop')
        self.result = await call
        answer = 'The answer is 42.' if '42' in self.result['content'][0]['text'] else 'No answer.'
        yield stream('message_start', message={'usage': {'input_tokens': 3}})
        yield stream('content_block_start', content_block={'type': 'text', 'text': ''})
        yield stream('content_block_delta', delta={'type': 'text_delta', 'text': answer})
        yield assistant(sdk.TextBlock(answer))
        yield stream('message_stop')
        yield sdk.ResultMessage(subtype='success', duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2,
                                session_id='s')


def fake_server(name, tools):
    return {'tools': {tool.name: tool for tool in tools}}


class PlanTurns(DatabaseCase):
    def setUp(self):
        super().setUp()
        FakeClaudeCode.instances = []
        server = patch('nembot.subscription.sdk.create_sdk_mcp_server', fake_server)
        server.start()
        self.addCleanup(server.stop)

    def turn(self, provider, conversation, text='question'):
        async def collect():
            return [e async for e in run_turn({'subscription': provider}, self.db, conversation, text, PLAN)]
        return asyncio.run(asyncio.wait_for(collect(), 10))

    def test_tool_calls_go_through_nembots_loop_and_back_to_the_session(self):
        provider = subscription.SubscriptionProvider(FakeClaudeCode)
        conversation = Conversation()
        events = self.turn(provider, conversation)
        self.assertEqual([h['role'] for h in conversation.history], ['user', 'assistant', 'tool', 'assistant'])
        asked = conversation.history[1]
        self.assertEqual((asked['text'], asked['stop']), ('Let me check.', 'tool_use'))
        self.assertEqual(asked['tool_calls'], [{'id': 'toolu_1', 'name': 'query_mms', 'input': {'sql': 'SELECT 42 AS answer'}}])
        self.assertEqual(conversation.history[-1]['text'], 'The answer is 42.')
        fake = FakeClaudeCode.instances[0]
        self.assertTrue(fake.result['content'][0]['text'].startswith('Result id: R1'))
        self.assertTrue(fake.disconnected)
        self.assertEqual(provider._sessions, {})
        self.assertEqual(fake.options.tools, [])  # Claude Code's own tools stay off
        self.assertEqual(fake.options.setting_sources, [])
        usage = [e for e in events if e['type'] == 'usage']
        self.assertEqual([(u['input'], u['cache_read'], u['output'], u['plan'], u['cost']) for u in usage],
                         [(10, 5, 7, True, 0), (3, 0, 0, True, 0)])
        self.assertIn({'type': 'tool', 'name': 'query_mms'}, events)
        self.assertEqual(events[-1]['type'], 'table')

    def test_web_search_enables_only_claude_codes_web_tools(self):
        provider = subscription.SubscriptionProvider(FakeClaudeCode)

        async def go():
            return [e async for e in run_turn({'subscription': provider}, self.db, Conversation(), 'q', PLAN,
                                              web_search=True)]
        asyncio.run(asyncio.wait_for(go(), 10))
        options = FakeClaudeCode.instances[0].options
        self.assertEqual(options.tools, ['WebSearch', 'WebFetch'])
        self.assertIn('mcp__nembot__query_mms', options.allowed_tools)

    def test_stop_during_a_tool_ends_the_session(self):
        async def slow_tool(db, call):
            await asyncio.Event().wait()
        provider = subscription.SubscriptionProvider(FakeClaudeCode)
        conversation = Conversation()

        async def go():
            events = []
            async for event in run_single({'subscription': provider}, self.db, conversation, 'q', PLAN):
                events.append(event)
                if event['type'] == 'tool_running':
                    stop_turn(conversation)
            return events
        with patch('nembot.chat.execute_tool', slow_tool):
            events = asyncio.run(asyncio.wait_for(go(), 10))
        self.assertEqual(events[-1], {'type': 'stopped'})
        self.assertTrue(FakeClaudeCode.instances[0].disconnected)
        self.assertEqual(provider._sessions, {})
        self.assertEqual(conversation.history[-1]['text'], 'Let me check.\n\n[Stopped by the user]')

    def test_plan_limit_is_reported(self):
        provider = subscription.SubscriptionProvider(lambda options: FakeClaudeCode(options, rate_limited=True))
        conversation = Conversation()
        with self.assertRaisesRegex(subscription.PlanError, 'usage limit'):
            self.turn(provider, conversation)
        self.assertEqual(conversation.history, [])
        self.assertTrue(FakeClaudeCode.instances[0].disconnected)


class Prompt(unittest.TestCase):
    def test_earlier_turns_are_sent_as_a_transcript(self):
        history = HISTORY + [{'role': 'user', 'text': 'and now?', 'context': '<data_status>x</data_status>'}]
        prompt = subscription.render_prompt(history, PLAN)
        self.assertTrue(prompt.startswith('<earlier_conversation>'))
        self.assertIn('[Earlier reply by GLM]\nPrices rose.', prompt)
        self.assertIn('[Tool call visualize_mms: {"sql": "SELECT 1", "plot_spec": "{}"}]', prompt)
        self.assertIn('(The plot image is not repeated here.)', prompt)
        self.assertTrue(prompt.endswith('and now?\n\n<data_status>x</data_status>'))

    def test_first_message_is_sent_as_is(self):
        self.assertEqual(subscription.render_prompt([{'role': 'user', 'text': 'hi'}], PLAN), 'hi')

    def test_plot_results_carry_the_image(self):
        result = subscription.tool_result({'text': 'Plot created', 'image': 'iVBORw0KGgo=', 'is_error': False})
        self.assertEqual([c['type'] for c in result['content']], ['text', 'text', 'image'])
        self.assertFalse(result['is_error'])


if __name__ == '__main__':
    unittest.main()
