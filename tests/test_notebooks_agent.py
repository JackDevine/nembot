"""Guides, agent-written notebooks, code approval, run_in_marimo and marimo's AI background (no network)."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import duckdb
import httpx2 as httpx

from nembot import credentials, guides, notebook_context
from nembot.attribution import CREDIT
from nembot.chat import Conversation, run_turn, system_prompt
from nembot.notebook_files import NotebookFiles, slug, with_credit_marimo

from test_chat import GLM, DatabaseCase

GOOD_MARIMO = '''import marimo

__generated_with = "0.24.2"
app = marimo.App()


@app.cell
def _():
    import marimo as mo
    return (mo,)


@app.cell
def _(mo):
    pick = mo.ui.dropdown(options=["a", "b"], value="a")
    pick
    return (pick,)


@app.cell
def _(pick):
    print(pick.value)
    return


if __name__ == "__main__":
    app.run()
'''


class Guides(unittest.TestCase):
    def test_index_and_sections(self):
        self.assertIn('`marimo`', guides.prompt_index())
        self.assertIn('`jupyter`', guides.prompt_index())
        self.assertIn('read_guide', system_prompt(True))
        page, error = guides.read('marimo')
        self.assertFalse(error)
        self.assertIn('nembot.client import query', page)
        self.assertIn('Sections: ', page)
        live, error = guides.read('MARIMO', 'live')
        self.assertFalse(error)
        self.assertIn('cm.get_context', live)
        self.assertTrue(guides.read('marimo', 'nope')[1])
        self.assertTrue(guides.read('excel')[1])

    def test_guide_examples_are_valid_python(self):
        import ast
        import re
        for name, guide in guides.available().items():
            # Sections copied from marimo-team/skills show bare signatures, so only nembot's own text is checked.
            for text in [guide['body'], *(t for t in guide['sections'].values() if 'marimo-team/skills' not in t)]:
                for block in re.findall(r'```python\n(.*?)```', text, re.S):
                    compile(block, name, 'exec', ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)  # code mode: top-level async


class Writing(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        with duckdb.connect(str(root / 'nem.duckdb')) as c:
            c.execute("CREATE TABLE T AS SELECT 1 AS x")
        self.files = NotebookFiles(root / 'reports', 'http://127.0.0.1:9', str(root / 'nem.duckdb'))

    def write(self, **kwargs):
        return asyncio.run(self.files.write(**kwargs))

    def test_marimo_is_checked_run_and_credited(self):
        result = self.write(name='My notebook!', fmt='marimo', code=GOOD_MARIMO)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['file'], 'My_notebook.py')
        saved = (Path(self.directory.name) / 'reports' / 'My_notebook.py').read_text()
        self.assertIn(CREDIT, saved)
        self.assertTrue(saved.rstrip().endswith('app.run()'))
        self.assertIn('(marimo)', self.files.read('My_notebook')[0])

    def test_problems_come_back_and_nothing_is_saved(self):
        twice = GOOD_MARIMO.replace('    print(pick.value)\n    return', '    pick = 2\n    return (pick,)')
        result = self.write(name='bad', fmt='marimo', code=twice)
        self.assertFalse(result['ok'])
        self.assertIn('defined in multiple cells', result['problems'])
        crash = GOOD_MARIMO.replace('print(pick.value)', 'print(1 / 0)')
        self.assertIn('ZeroDivisionError', self.write(name='crash', fmt='marimo', code=crash)['problems'])
        self.assertFalse(any(Path(self.directory.name, 'reports').glob('*.py')))

    def test_jupyter_runs_with_outputs_and_reads_back(self):
        result = self.write(name='j', fmt='jupyter', cells=[{'type': 'code', 'source': 'from nembot.client import query\n'
                                                                                       "query('SELECT x FROM T')"}])
        self.assertTrue(result['ok'], result)
        notebook = json.loads((Path(self.directory.name) / 'reports' / 'j.ipynb').read_text())
        self.assertTrue(notebook['cells'][0]['outputs'])  # executed, via the file fallback of nembot.client
        self.assertIn(CREDIT, ''.join(notebook['cells'][-1]['source']))
        text, error = self.files.read('j.ipynb')
        self.assertFalse(error)
        self.assertIn("query('SELECT x FROM T')", text)

    def test_overwrite_must_be_asked_for(self):
        self.assertTrue(self.write(name='m', fmt='marimo', code=GOOD_MARIMO)['ok'])
        self.assertIn('overwrite=true', self.write(name='m', fmt='marimo', code=GOOD_MARIMO)['problems'])
        self.assertTrue(self.write(name='m', fmt='marimo', code=GOOD_MARIMO, overwrite=True)['ok'])

    def test_helpers(self):
        self.assertEqual(slug('../../etc/passwd'), 'etc_passwd')
        self.assertEqual(with_credit_marimo('x = 1').count(CREDIT), 1)
        self.assertEqual(with_credit_marimo(f'"{CREDIT}"'), f'"{CREDIT}"')


class Writer:
    """Asks to write a notebook once, then finishes."""

    async def respond(self, model, history, web_search=False, **options):
        if history[-1]['role'] == 'tool':
            self.result = history[-1]['results'][0]
            yield {'role': 'assistant', 'model': model.ref, 'model_name': model.name, 'text': 'done',
                   'tool_calls': [], 'raw': {}, 'stop': 'end'}
            return
        yield {'role': 'assistant', 'model': model.ref, 'model_name': model.name, 'text': '', 'raw': {}, 'stop': 'tool_use',
               'tool_calls': [{'id': 'w1', 'name': 'write_notebook',
                               'input': {'name': 'n', 'format': 'marimo', 'code': GOOD_MARIMO}}]}


class FakeFiles:
    def __init__(self):
        self.written = False

    async def write(self, *args):
        self.written = True
        return {'ok': True, 'file': 'n.py', 'format': 'marimo', 'title': 'n', 'bytes': 1, 'missing': [], 'errors': {}}


class Approval(DatabaseCase):
    def setUp(self):
        super().setUp()
        config = tempfile.TemporaryDirectory()
        self.addCleanup(config.cleanup)
        env = patch.dict(os.environ, {'NEMBOT_CONFIG_DIR': config.name})
        env.start()
        self.addCleanup(env.stop)

    def turn(self, reply):
        provider, files, conversation = Writer(), FakeFiles(), Conversation()

        async def go():
            events = []
            async for event in run_turn({'openrouter': provider}, self.db, conversation, 'q', GLM,
                                        interactive=True, notebook_files=files):
                events.append(event)
                if event['type'] == 'code_request':
                    conversation.pending[event['id']].set_result(reply)
            return events
        return asyncio.run(asyncio.wait_for(go(), 10)), provider, files

    def test_declining_runs_nothing(self):
        events, provider, files = self.turn({'approve': False})
        self.assertFalse(files.written)
        self.assertIn({'type': 'code_decision', 'id': 'w1', 'approved': False, 'timeout': False}, events)
        self.assertIn('chose not to run', provider.result['text'])

    def test_approving_runs_and_remember_turns_the_warning_off(self):
        events, provider, files = self.turn({'approve': True, 'remember': True})
        self.assertTrue(files.written)
        self.assertTrue(any(e['type'] == 'report' for e in events))
        self.assertIn(GOOD_MARIMO.splitlines()[0], next(e for e in events if e['type'] == 'code_request')['code'])
        self.assertFalse(credentials.setting('confirm_code', True))
        events, _, files = self.turn(None)  # no card this time
        self.assertTrue(files.written)
        self.assertFalse(any(e['type'] == 'code_request' for e in events))


class FakeServers:
    def __init__(self, port):
        self.port = port

    def running(self, kind):
        return (self.port, 'tok') if self.port else None


class Live(unittest.TestCase):
    def run_live(self, port, notebook, handler):
        files = NotebookFiles(Path('/tmp'), 'http://x', 'db', servers=FakeServers(port))
        real = httpx.AsyncClient
        with patch('httpx2.AsyncClient', lambda **kw: real(transport=httpx.MockTransport(handler), **kw)):
            return asyncio.run(files.run_in_marimo(notebook, 'print(1)'))

    def test_streams_the_output_of_the_matching_session(self):
        seen = {}

        def handler(request):
            if request.url.path == '/api/sessions':
                assert request.headers['authorization'] == 'Bearer tok'
                return httpx.Response(200, json={'s_1': {'path': '/r/other.py'}, 's_2': {'path': '/r/vwap.py'}})
            seen['session'] = request.headers['marimo-session-id']
            seen['code'] = json.loads(request.content)['code']
            body = ('event: stdout\ndata: {"data": "1\\n"}\n\nevent: done\n'
                    'data: {"success": true, "output": {"data": ""}}\n\n')
            return httpx.Response(200, text=body, headers={'content-type': 'text/event-stream'})
        text, error = self.run_live(1234, 'vwap', handler)
        self.assertEqual((text, error), ('1', False))
        self.assertEqual(seen, {'session': 's_2', 'code': 'print(1)'})

    def test_explains_when_the_notebook_is_not_open(self):
        text, error = self.run_live(None, 'vwap.py', None)
        self.assertTrue(error)
        self.assertIn('Open in marimo', text)
        text, error = self.run_live(1, 'vwap.py', lambda r: httpx.Response(200, json={}))
        self.assertTrue(error)
        self.assertIn('not open in marimo', text)


class Background(DatabaseCase):
    def test_marimo_settings_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = notebook_context.write_marimo_project(Path(directory), self.db)
            config = tomllib.loads(path.read_text())['tool']['marimo']
        self.assertTrue(config['runtime']['auto_instantiate'])
        self.assertIn('nembot.client import query', config['ai']['rules'])
        self.assertIn('- none yet', config['ai']['rules'])


if __name__ == '__main__':
    unittest.main()
