"""Plugins and Creator mode (no network)."""
import asyncio
import logging
from pathlib import Path
import shutil
import tempfile
import unittest

import duckdb
import polars

from nemsplainer import creator as creator_mode, plugins
from nemsplainer.chat import Conversation, TOOLS, mode_setup, run_turn
from nemsplainer.models import ModelInfo

MODEL = ModelInfo('claude:claude-haiku-5-5', 'claude', 'claude-haiku-5-5', 'Claude Haiku 5.5', True, True, True)
logging.getLogger('nemsplainer.plugins').setLevel(logging.ERROR)  # broken plugins are expected here
EXAMPLE = Path(__file__).resolve().parents[1] / 'plugins' / 'price-spikes'

GOOD = '''
def setup(nem):
    @nem.tool("Double a number.", {"type": "object", "properties": {"x": {"type": "number"}}, "required": ["x"]})
    def double(args):
        frame = nem.query("SELECT 1")
        return {"text": str(args["x"] * 2), "table": {"columns": ["x"], "rows": [[args["x"] * 2]]},
                "events": [{"kind": "doubled", "data": {"x": args["x"]}}]}

    @nem.route("GET", "/hello")
    def hello(request):
        return {"hello": "world"}

    nem.prompt("Use demo_double to double numbers.")
'''


def write_plugin(root: Path, name: str, code: str | None = GOOD, manifest_name: str | None = None):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'plugin.toml').write_text(f'name = "{manifest_name or name}"\ntitle = "Demo"\nversion = "0.1.0"\n')
    if code is not None:
        (folder / 'plugin.py').write_text(code)
    return folder


def fake_query(sql):
    return polars.DataFrame({'one': [1]})


class Manager(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / 'plugins'
        self.manager = plugins.PluginManager(self.root, fake_query)

    def test_loads_tools_routes_and_notes(self):
        write_plugin(self.root, 'demo')
        self.manager.reload()
        plugin = self.manager.plugins['demo']
        self.assertEqual(plugin.status, 'loaded', plugin.error)
        self.assertEqual([t['name'] for t in self.manager.tool_definitions()], ['demo_double'])
        self.assertIn('demo_double', self.manager.prompt_notes())
        self.assertEqual(self.manager.route('demo', 'GET', '/hello')(None), {'hello': 'world'})
        result = asyncio.run(plugins.call_tool(self.manager.find_tool('demo_double'), {'x': 21}))
        self.assertEqual((result['text'], result['is_error']), ('42', False))

    def test_broken_plugins_are_reported_not_fatal(self):
        write_plugin(self.root, 'demo')
        write_plugin(self.root, 'broken', 'def setup(nem):\n    raise RuntimeError("nope")\n')
        write_plugin(self.root, 'mismatch', manifest_name='other')
        self.manager.reload()
        self.assertEqual(self.manager.plugins['demo'].status, 'loaded')
        self.assertIn('nope', self.manager.plugins['broken'].error)
        self.assertIn('folder name', self.manager.plugins['mismatch'].error)
        self.assertEqual(len(self.manager.tools()), 1)

    def test_disable_and_tool_errors(self):
        write_plugin(self.root, 'demo', 'def setup(nem):\n    @nem.tool("x")\n    def fail(args):\n        raise ValueError("bad")\n')
        self.manager.reload()
        result = asyncio.run(plugins.call_tool(self.manager.find_tool('demo_fail'), {}))
        self.assertTrue(result['is_error'])
        self.assertIn('bad', result['text'])
        self.manager.set_enabled('demo', False)
        self.assertEqual(self.manager.plugins['demo'].status, 'disabled')
        self.assertEqual(self.manager.tools(), [])

    def test_safe_paths(self):
        for bad in ('../x.py', '/etc/passwd', 'a/../../b.py', 'x.exe', ''):
            with self.assertRaises(ValueError, msg=bad):
                plugins.safe_path(self.root, bad)
        self.assertEqual(plugins.safe_path(self.root, 'lib/util.py'), (self.root / 'lib' / 'util.py').resolve())

    def test_install_backs_up_and_keeps_data(self):
        installed = write_plugin(self.root, 'demo')
        (installed / 'data').mkdir()
        (installed / 'data' / 'keep.txt').write_text('mine')
        draft = self.manager.start_draft('demo')  # seeded from the installed copy
        self.assertTrue((draft / 'plugin.py').exists())
        self.assertFalse((draft / 'data').exists())
        (draft / 'plugin.py').write_text(GOOD.replace('* 2', '* 3'))
        plugin = self.manager.install('demo')
        self.assertEqual(plugin.status, 'loaded')
        self.assertEqual((installed / 'data' / 'keep.txt').read_text(), 'mine')
        self.assertEqual(len(list((self.root / '.backup').iterdir())), 1)
        self.assertIn('* 3', (installed / 'plugin.py').read_text())

    def test_example_plugin_loads(self):
        shutil.copytree(EXAMPLE, self.root / 'price-spikes')
        self.manager.reload()
        self.assertEqual(self.manager.plugins['price-spikes'].status, 'loaded', self.manager.plugins['price-spikes'].error)


class Modes(unittest.TestCase):
    def test_analyst_prompt_stays_clean(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        write_plugin(root, 'demo')
        manager = plugins.PluginManager(root, fake_query)
        manager.reload()
        system, tools = mode_setup('analyst', MODEL, False, manager)
        self.assertNotIn('plugin.toml', system)
        self.assertIn('demo_double', [t['name'] for t in tools])
        self.assertNotIn('write_plugin_file', [t['name'] for t in tools])
        system, tools = mode_setup('creator', MODEL, False, manager)
        self.assertIn('plugin.toml', system)
        names = [t['name'] for t in tools]
        self.assertIn('write_plugin_file', names)
        self.assertIn('query_mms', names)
        self.assertNotIn('demo_double', names)


class FakeProvider:
    """Calls one tool, then finishes."""

    def __init__(self, call):
        self.call = call

    async def respond(self, model, history, web_search=False, **options):
        self.options = options
        if history[-1]['role'] == 'user':
            yield {'role': 'assistant', 'model': model.ref, 'model_name': model.name, 'text': '', 'raw': {},
                   'tool_calls': [{'id': 't1', **self.call}], 'stop': 'tool_use'}
            return
        yield {'role': 'assistant', 'model': model.ref, 'model_name': model.name, 'text': 'done', 'raw': {},
               'tool_calls': [], 'stop': 'end'}


class Turns(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        db_path = self.root / 'nem.duckdb'
        duckdb.connect(str(db_path)).close()
        self.db = duckdb.connect(str(db_path), read_only=True)
        self.addCleanup(self.db.close)
        self.manager = plugins.PluginManager(self.root / 'plugins', fake_query)

    def run_turn(self, provider, conversation, **kwargs):
        from unittest.mock import patch
        async def collect():
            return [e async for e in run_turn({'claude': provider}, self.db, conversation, 'q', MODEL, **kwargs)]
        with patch('nemsplainer.chat.downloads.latest_published', return_value='2026-09'):
            return asyncio.run(collect())

    def test_plugin_tool_runs_in_analyst_mode(self):
        write_plugin(self.manager.directory, 'demo')
        self.manager.reload()
        conversation = Conversation()
        events = self.run_turn(FakeProvider({'name': 'demo_double', 'input': {'x': 4}}), conversation,
                               plugin_manager=self.manager)
        self.assertIn({'type': 'plugin_event', 'plugin': 'demo', 'kind': 'doubled', 'data': {'x': 4}}, events)
        self.assertEqual(events[-1]['type'], 'table')
        self.assertEqual(conversation.history[2]['results'][0]['text'], '8')

    def test_creator_writes_tests_and_installs_with_approval(self):
        manager = self.manager
        studio = creator_mode.Creator(manager, self.db, 'http://127.0.0.1:1', str(self.root / 'nem.duckdb'))
        conversation = Conversation(mode='creator')
        files = {'plugin.toml': 'name = "demo"\ntitle = "Demo"\nversion = "0.1.0"\n', 'plugin.py': GOOD}
        for path, content in files.items():
            events = self.run_turn(FakeProvider({'name': 'write_plugin_file', 'input': {
                'plugin': 'demo', 'path': path, 'content': content}}), conversation, plugin_manager=manager, creator=studio)
            self.assertEqual(events[1]['type'], 'plugin_file')
        events = self.run_turn(FakeProvider({'name': 'test_plugin', 'input': {'plugin': 'demo'}}), conversation,
                               plugin_manager=manager, creator=studio)
        report = next(e for e in events if e['type'] == 'plugin_test')['report']
        self.assertTrue(report['ok'], report)
        self.assertEqual([t['name'] for t in report['plugin']['tools']], ['demo_double'])
        # Writing outside the draft is refused.
        events = self.run_turn(FakeProvider({'name': 'write_plugin_file', 'input': {
            'plugin': 'demo', 'path': '../../evil.py', 'content': 'x'}}), conversation, plugin_manager=manager, creator=studio)
        self.assertIn('escapes the plugin folder', conversation.history[-2]['results'][0]['text'])

        async def install():
            out = []
            async for event in run_turn({'claude': FakeProvider({'name': 'install_plugin', 'input': {
                    'plugin': 'demo', 'summary': 'Doubles numbers.'}})}, self.db, conversation, 'install', MODEL,
                    plugin_manager=manager, creator=studio):
                out.append(event)
                if event['type'] == 'plugin_install_request':
                    conversation.pending[event['id']].set_result({'approve': True})
            return out
        from unittest.mock import patch
        with patch('nemsplainer.chat.downloads.latest_published', return_value='2026-09'):
            events = asyncio.run(install())
        result = next(e for e in events if e['type'] == 'plugin_install_result')
        self.assertEqual(result['status'], 'loaded')
        self.assertEqual(manager.plugins['demo'].status, 'loaded')


if __name__ == '__main__':
    unittest.main()
