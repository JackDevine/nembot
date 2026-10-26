"""Reasoning levels: read from each service, defaulted, and sent with the request (no network)."""
import asyncio
import unittest
from unittest.mock import patch

from nembot import claude, presets
from nembot.chat import Conversation, chosen_effort, run_turn
from nembot.models import ModelInfo, default_effort, efforts

from test_chat import DatabaseCase
from test_onboarding import mock_client

KIMI = ModelInfo('openrouter:moonshotai/kimi', 'openrouter', 'moonshotai/kimi', 'Kimi', True, True, True,
                 efforts=('none', 'low', 'high', 'max'), default_effort='high')


class Levels(unittest.TestCase):
    def test_levels_are_ordered_and_default_to_medium_or_the_nearest(self):
        self.assertEqual(efforts(['max', 'low', 'medium', 'bogus']), ('low', 'medium', 'max'))
        self.assertEqual(default_effort(('low', 'medium', 'high')), 'medium')
        self.assertEqual(default_effort(('none', 'low', 'high', 'max')), 'high')  # Kimi: no medium
        self.assertEqual(default_effort(('off', 'on')), 'on')  # Ollama thinking models
        self.assertIsNone(default_effort(()))

    def test_openrouter_catalogue_levels(self):
        routes = {('GET', '/api/v1/models'): {'data': [
            {'id': 'moonshotai/kimi-k3.1-pro', 'supported_parameters': ['tools', 'reasoning'],
             'reasoning': {'supported_efforts': ['max', 'high', 'low', 'none'], 'default_effort': 'max'}},
            {'id': 'plain/model', 'supported_parameters': ['tools']}]}}
        with patch('nembot.presets.httpx.AsyncClient', mock_client(routes)):
            kimi, plain = asyncio.run(presets.list_models(presets.PRESETS['openrouter'], None))
        self.assertEqual((kimi.efforts, kimi.default_effort), (('none', 'low', 'high', 'max'), 'high'))
        self.assertEqual((plain.efforts, plain.default_effort), ((), None))

    def test_a_picked_level_must_be_one_the_model_takes(self):
        self.assertEqual(chosen_effort(KIMI, {KIMI.ref: 'low'}), 'low')
        self.assertEqual(chosen_effort(KIMI, {KIMI.ref: 'medium'}), 'high')
        self.assertEqual(chosen_effort(KIMI, None), 'high')

    def test_claude_request_uses_the_picked_effort(self):
        self.assertEqual(claude.request_options('claude-opus-5-5', 'max')['output_config'], {'effort': 'max'})
        self.assertEqual(claude.request_options('claude-opus-5-5')['output_config'], {'effort': 'medium'})


class Recorder:
    def __init__(self):
        self.options = None

    async def respond(self, model, history, web_search=False, **options):
        self.options = options
        yield {'role': 'assistant', 'model': model.ref, 'model_name': model.name, 'text': 'ok', 'tool_calls': [],
               'raw': {}, 'stop': 'end'}


class TurnEffort(DatabaseCase):
    def run_with(self, model, efforts):
        provider = Recorder()

        async def go():
            return [e async for e in run_turn({model.provider: provider}, self.db, Conversation(), 'q', model,
                                              efforts=efforts)]
        asyncio.run(go())
        return provider.options

    def test_the_level_reaches_the_provider_only_for_models_with_levels(self):
        self.assertEqual(self.run_with(KIMI, {KIMI.ref: 'low'})['effort'], 'low')
        plain = ModelInfo('openrouter:x/y', 'openrouter', 'x/y', 'Y', False, True, True)
        self.assertNotIn('effort', self.run_with(plain, {plain.ref: 'low'}))


if __name__ == '__main__':
    unittest.main()
