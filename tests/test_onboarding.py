"""Onboarding: credential storage, service presets, the Ollama adapter and connection tests (no network)."""
import asyncio
import json
import os
from pathlib import Path
import stat
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import httpx2 as httpx

from nembot import credentials, presets, setup
from nembot.models import Catalog, ModelInfo
from nembot.ollama import OllamaProvider, native_messages
from nembot.openai_compat import render

GLM = ModelInfo('openrouter:z-ai/glm', 'openrouter', 'z-ai/glm', 'GLM', True, True, True)


class ConfigDir(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        env = patch.dict(os.environ, {'NEMBOT_CONFIG_DIR': self.directory.name})
        env.start()
        for name in ('OPENROUTER_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY'):
            os.environ.pop(name, None)
        self.addCleanup(env.stop)
        self.addCleanup(self.directory.cleanup)


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def get_password(self, service, name):
        return self.store.get((service, name))

    def delete_password(self, service, name):
        self.store.pop((service, name), None)


class Credentials(ConfigDir):
    def test_keys_are_saved_privately_and_read_back(self):
        credentials.save_provider('openrouter', api_key='sk-or-1')
        credentials.save_provider('ollama', enabled=True, base_url='http://box:11434/v1')
        path = credentials.path()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(credentials.api_key('openrouter', 'OPENROUTER_API_KEY'), ('sk-or-1', 'file'))
        self.assertEqual(tomllib.loads(path.read_text())['providers']['ollama'],
                         {'enabled': True, 'base_url': 'http://box:11434/v1'})

    def test_environment_variables_win(self):
        credentials.save_provider('openrouter', api_key='sk-or-file')
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'sk-or-env'}):
            self.assertEqual(credentials.api_key('openrouter', 'OPENROUTER_API_KEY'), ('sk-or-env', 'environment'))

    def test_awkward_values_round_trip(self):
        credentials.save_provider('lm "studio"', api_key='a"b\\c\n', base_url=None)
        credentials.set_setting('claude_plan', True)
        data = credentials.load()
        self.assertEqual(data['providers']['lm "studio"']['api_key'], 'a"b\\c\n')
        self.assertIs(data['settings']['claude_plan'], True)

    def test_forget_removes_a_provider(self):
        credentials.save_provider('openai', api_key='sk-1')
        credentials.forget_provider('openai')
        self.assertEqual(credentials.api_key('openai', 'OPENAI_API_KEY'), (None, None))

    def test_keys_move_to_the_keychain_and_back(self):
        keyring = FakeKeyring()
        with patch('nembot.credentials._keyring', return_value=keyring):
            credentials.save_provider('openai', api_key='sk-1')
            credentials.use_keyring(True)
            self.assertNotIn('sk-1', credentials.path().read_text())
            self.assertEqual(credentials.api_key('openai', None), ('sk-1', 'keychain'))
            credentials.save_provider('gemini', api_key='g-1')  # new keys go straight to the keychain
            self.assertEqual(keyring.store[('nembot', 'gemini')], 'g-1')
            credentials.use_keyring(False)
            self.assertEqual(credentials.api_key('openai', None), ('sk-1', 'file'))
            self.assertEqual(keyring.store, {})

    def test_keychain_needs_the_package(self):
        with patch('nembot.credentials._keyring', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'uv sync --extra keyring'):
                credentials.use_keyring(True)


def mock_client(routes):
    """httpx AsyncClient factory answering ``routes`` ({(method, path): json or status})."""
    def handler(request):
        answer = routes.get((request.method, request.url.path))
        if answer is None:
            return httpx.Response(404)
        if isinstance(answer, int):
            return httpx.Response(answer)
        return httpx.Response(200, json=answer)
    real = httpx.AsyncClient
    return lambda **kwargs: real(transport=httpx.MockTransport(handler))


class Presets(unittest.TestCase):
    def test_openai_style_listing_skips_non_chat_models_and_guesses_vision(self):
        routes = {('GET', '/v1/models'): {'data': [{'id': 'gpt-4o-mini'}, {'id': 'text-embedding-3-small'},
                                                   {'id': 'tts-1'}, {'id': 'deepseek-chat'}]}}
        with patch('nembot.presets.httpx.AsyncClient', mock_client(routes)):
            models = asyncio.run(presets.list_models(presets.PRESETS['openai'], 'sk'))
        self.assertEqual([(m.ref, m.vision, m.tools) for m in models],
                         [('openai:deepseek-chat', False, True), ('openai:gpt-4o-mini', True, True)])

    def test_gemini_ids_lose_their_models_prefix(self):
        routes = {('GET', '/v1beta/openai/models'): {'data': [{'id': 'models/gemini-2.5-flash'}]}}
        with patch('nembot.presets.httpx.AsyncClient', mock_client(routes)):
            models = asyncio.run(presets.list_models(presets.PRESETS['gemini'], 'g'))
        self.assertEqual(models[0].ref, 'gemini:gemini-2.5-flash')

    def test_openrouter_checks_the_key_because_its_list_is_public(self):
        routes = {('GET', '/api/v1/key'): 401, ('GET', '/api/v1/models'): {'data': []}}
        with patch('nembot.presets.httpx.AsyncClient', mock_client(routes)):
            with self.assertRaises(httpx.HTTPStatusError):
                asyncio.run(presets.list_models(presets.PRESETS['openrouter'], 'bad'))

    def test_ollama_capabilities_come_from_the_server(self):
        routes = {('GET', '/api/tags'): {'models': [{'name': 'llama3.2:3b'}, {'name': 'gemma3:4b'}, {'name': 'nomic'}]},
                  ('POST', '/api/show'): None}
        shows = {'llama3.2:3b': ['completion', 'tools'], 'gemma3:4b': ['completion', 'vision'], 'nomic': ['embedding']}

        def handler(request):
            if request.url.path == '/api/tags':
                return httpx.Response(200, json=routes[('GET', '/api/tags')])
            name = json.loads(request.content)['model']
            return httpx.Response(200, json={'capabilities': shows[name], 'model_info': {'llama.context_length': 131072}})
        real = httpx.AsyncClient
        with patch('nembot.presets.httpx.AsyncClient', lambda **kw: real(transport=httpx.MockTransport(handler))):
            models = asyncio.run(presets.list_models(presets.PRESETS['ollama'], None))
        self.assertEqual([(m.ref, m.tools, m.vision, m.context) for m in models],
                         [('ollama:llama3.2:3b', True, False, 131072), ('ollama:gemma3:4b', False, True, 131072)])


class Ollama(ConfigDir):
    def test_messages_use_ollamas_shapes(self):
        history = [
            {'role': 'user', 'text': 'plot', 'context': ''},
            {'role': 'assistant', 'model': GLM.ref, 'model_name': 'GLM', 'text': '', 'raw': {}, 'stop': 'tool_use',
             'tool_calls': [{'id': 'c1', 'name': 'visualize_mms', 'input': {'sql': 'SELECT 1', 'plot_spec': '{}'}}]},
            {'role': 'tool', 'results': [{'id': 'c1', 'name': 'visualize_mms', 'text': 'Plot created',
                                          'image': 'iVBORw0KGgo=', 'is_error': False}]},
        ]
        messages = native_messages(render(history, GLM, system='sys'))
        self.assertEqual(messages[2]['tool_calls'], [{'function': {'name': 'visualize_mms',
                                                                    'arguments': {'sql': 'SELECT 1', 'plot_spec': '{}'}}}])
        self.assertEqual((messages[3]['role'], messages[3]['tool_name']), ('tool', 'visualize_mms'))
        self.assertEqual(messages[4]['images'], ['iVBORw0KGgo='])
        self.assertIsInstance(messages[4]['content'], str)

    def test_context_size_is_capped_by_the_model_and_configurable(self):
        small = ModelInfo('ollama:tiny', 'ollama', 'tiny', 'tiny', False, True, True, context=8192)
        big = ModelInfo('ollama:big', 'ollama', 'big', 'big', False, True, True, context=131072)
        self.assertEqual((OllamaProvider.num_ctx(small), OllamaProvider.num_ctx(big)), (8192, 16384))
        credentials.save_provider('ollama', enabled=True, num_ctx=32768)
        self.assertEqual(OllamaProvider.num_ctx(big), 32768)


class ConnectionTest(ConfigDir):
    MODELS = [ModelInfo('openai:a', 'openai', 'a', 'A', False, False, True),
              ModelInfo('openai:gpt-4o-mini', 'openai', 'gpt-4o-mini', 'Mini', True, True, True)]

    def check(self, models=None, error=None, tool=(True, 'It called a tool correctly.'), **kwargs):
        async def listing(*args):
            if error:
                raise error
            return self.MODELS if models is None else models

        async def tool_test(*args):
            return tool
        with patch('nembot.setup.list_models', listing), patch('nembot.setup._tool_test_openai', tool_test):
            return asyncio.run(setup.check('openai', **kwargs))

    def test_a_missing_key_stops_before_any_request(self):
        result = self.check()
        self.assertEqual((result['listed'], result['steps'][0]['name']), (False, 'Key'))

    def test_success_lists_then_tests_the_preferred_model(self):
        result = self.check(key='sk')
        self.assertTrue(result['ok'] and result['listed'])
        self.assertEqual(result['test_model'], 'gpt-4o-mini')

    def test_a_rejected_key_is_explained(self):
        error = httpx.HTTPStatusError('401', request=httpx.Request('GET', 'http://x'), response=httpx.Response(401))
        result = self.check(key='bad', error=error)
        self.assertFalse(result['listed'])
        self.assertIn('rejected', result['steps'][0]['detail'])

    def test_no_tool_models_is_reported(self):
        result = self.check(key='sk', models=self.MODELS[:1])
        self.assertTrue(result['listed'])
        self.assertFalse(result['ok'])
        self.assertIn('call tools', result['steps'][-1]['detail'])

    def test_a_failed_tool_call_still_counts_as_listed(self):
        result = self.check(key='sk', tool=(False, 'It answered but did not call the tool.'))
        self.assertEqual((result['listed'], result['ok']), (True, False))

    def test_cheapest_priced_tool_model_when_none_is_preferred(self):
        models = [ModelInfo('o:x', 'o', 'x', 'X', False, True, True, price_in=3, price_out=15),
                  ModelInfo('o:y', 'o', 'y', 'Y', False, True, True, price_in=0.1, price_out=0.4),
                  ModelInfo('o:z:free', 'o', 'z:free', 'Z', False, True, True, price_in=0, price_out=0)]
        self.assertEqual(setup.pick_test_model(models, ['missing']).id, 'y')


class Guard(unittest.TestCase):
    OWN = {'http://127.0.0.1:8000'}

    def test_only_this_computer_and_this_app(self):
        self.assertIsNone(setup.refusal('127.0.0.1', None, self.OWN, 'POST', 'application/json'))
        self.assertIsNone(setup.refusal('127.0.0.1', 'http://127.0.0.1:8000', self.OWN, 'POST', 'application/json'))
        self.assertEqual(setup.refusal('192.168.1.5', None, self.OWN, 'GET', '')[0], 403)
        self.assertEqual(setup.refusal('127.0.0.1', 'https://evil.example', self.OWN, 'POST', 'application/json')[0], 403)
        self.assertEqual(setup.refusal('127.0.0.1', None, self.OWN, 'POST', 'text/plain')[0], 415)

    def test_openrouter_sign_in_link(self):
        from urllib.parse import parse_qs, urlsplit
        url = setup.openrouter_start('http://localhost:8000/setup/openrouter/callback')
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertNotIn('=', query['code_challenge'][0])  # base64url without padding
        with self.assertRaisesRegex(ValueError, 'expired'):
            asyncio.run(setup.openrouter_finish('not-a-state', 'code'))


class Catalogue(unittest.TestCase):
    def test_failing_sources_are_reported_and_defaults_prefer_listed_models(self):
        async def ok():
            return [ModelInfo('ollama:llama', 'ollama', 'llama', 'llama', False, True, True)]

        async def broken():
            raise RuntimeError('connection refused')
        catalog = Catalog(lambda: {'Ollama': ok, 'OpenAI': broken}, preferred=['claude:claude-opus-5-5'])
        models = asyncio.run(catalog.models())
        self.assertEqual(list(models), ['ollama:llama'])
        self.assertEqual(catalog.errors, ['OpenAI models unavailable: connection refused'])
        self.assertEqual(catalog.default(), 'ollama:llama')


if __name__ == '__main__':
    unittest.main()
