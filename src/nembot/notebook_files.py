"""Notebooks written by the chat agent (write_notebook / read_notebook), in the reports directory.

Before a notebook is saved it must pass a check, so the user never gets one that doesn't run:
- marimo: ``marimo check`` (breaking issues only), then the notebook runs once as a script.
- Jupyter: every cell runs once in a fresh kernel (nbconvert --execute); outputs are kept.
Both run in a subprocess of nembot's Python with ``NEMBOT_URL``/``NEMBOT_DATABASE`` set, so
``nembot.client.query`` reaches this app. Failures go back to the agent to fix; nothing is written.
The AEMO credit line is added as a final markdown cell when the notebook doesn't carry it.
"""
import asyncio
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from .attribution import CREDIT

FORMATS = {'marimo': '.py', 'jupyter': '.ipynb'}
RUN_TIMEOUT = 300  # seconds for the check run (queries over big tables can be slow)
_ANSI = re.compile(r'\x1b\[[0-9;]*m')


def slug(name: str) -> str:
    stem = re.sub(r'[^A-Za-z0-9_-]+', '_', str(name).strip()).strip('_')[:80]
    return stem or 'notebook'


def _tail(text: str, lines: int = 40) -> str:
    return '\n'.join(_ANSI.sub('', text).strip().splitlines()[-lines:])


def with_credit_marimo(code: str) -> str:
    if CREDIT in code:
        return code
    cell = ('@app.cell(hide_code=True)\ndef _(mo):\n'
            f'    mo.md(r"""*{CREDIT}*""")\n    return\n\n\n')
    marker = 'if __name__ == "__main__":'
    if marker in code:
        head, tail = code.rsplit(marker, 1)
        return head.rstrip() + '\n\n\n' + cell + marker + tail
    return code.rstrip() + '\n\n\n' + cell


def jupyter_notebook(cells: list[dict]):
    import nbformat
    from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook
    built = []
    for cell in cells:
        source = str(cell.get('source', ''))
        built.append(new_markdown_cell(source) if cell.get('type') == 'markdown' else new_code_cell(source))
    if not any(CREDIT in str(c.get('source', '')) for c in cells):
        built.append(new_markdown_cell(f'*{CREDIT}*'))
    return new_notebook(cells=built, metadata={'kernelspec': {'name': 'python3', 'display_name': 'Python 3',
                                                              'language': 'python'}})


class NotebookFiles:
    def __init__(self, directory: Path, app_url: str, database: str, servers=None):
        self.directory, self.app_url, self.database = Path(directory), app_url, database
        self.servers = servers  # notebooks.NotebookServers, for run_in_marimo

    async def run_in_marimo(self, notebook: str, code: str) -> tuple[str, bool]:
        """Run Python in the live kernel of a notebook open in nembot's marimo server (marimo pair's
        transport: GET /api/sessions, then POST /api/kernel/execute, an SSE stream). (output, is_error)."""
        import httpx2 as httpx
        running = self.servers.running('marimo') if self.servers else None
        if running is None:
            return ('No notebook is open in marimo. Ask the user to open it from its "Open in marimo" link '
                    '(in the chat or on the Reports page), then try again.'), True
        port, token = running
        base, headers = f'http://127.0.0.1:{port}', {'Authorization': f'Bearer {token}'}
        wanted = Path(str(notebook)).name
        wanted = wanted if wanted.endswith('.py') else slug(wanted) + '.py'
        async with httpx.AsyncClient(timeout=httpx.Timeout(RUN_TIMEOUT, connect=10)) as client:
            sessions = (await client.get(f'{base}/api/sessions', headers=headers)).json()
            matches = [sid for sid, s in sessions.items() if Path(s.get('path') or s.get('filename') or '').name == wanted]
            if not matches:
                open_now = sorted({Path(s.get('path') or s.get('filename') or '').name for s in sessions.values()})
                return (f'{wanted} is not open in marimo. Open notebooks: {", ".join(open_now) or "none"}. Ask the '
                        'user to open it from its link (a notebook must be open in a browser tab).'), True
            out, done = [], None
            async with client.stream('POST', f'{base}/api/kernel/execute', json={'code': code},
                                     headers={**headers, 'Marimo-Session-Id': matches[-1]}) as response:
                if response.status_code >= 400:
                    return f'marimo refused the request ({response.status_code}): {(await response.aread())[:500]!r}', True
                event = None
                async for line in response.aiter_lines():
                    if line.startswith('event:'):
                        event = line[6:].strip()
                    elif line.startswith('data:'):
                        data = json.loads(line[5:])
                        if event in ('stdout', 'stderr'):
                            out.append(data.get('data', ''))
                        elif event == 'done':
                            done = data
                            break
        text = _ANSI.sub('', ''.join(out)).strip()
        if done and (done.get('output') or {}).get('data'):
            text += '\n' + str(done['output']['data'])
        text = text[-12_000:] or '(no output)'
        if done is None:
            return 'The code did not finish: marimo ended the stream without a result.\n' + text, True
        return text, not done.get('success', False)

    def _env(self) -> dict:
        return {**os.environ, 'NEMBOT_URL': self.app_url, 'NEMBOT_DATABASE': self.database}

    def find(self, name: str) -> Path | None:
        base = Path(str(name)).name
        candidates = [self.directory / base] if Path(base).suffix in FORMATS.values() else []
        candidates += [self.directory / (slug(Path(base).stem if Path(base).suffix else base) + ext)
                       for ext in FORMATS.values()]
        return next((p for p in candidates if p.is_file()), None)

    def read(self, name: str) -> tuple[str, bool]:
        path = self.find(name)
        if path is None:
            names = sorted(p.name for ext in FORMATS.values() for p in self.directory.glob(f'*{ext}'))
            return f'No notebook named {name!r}. Notebooks: {", ".join(names) or "none yet"}.', True
        if path.suffix == '.py':
            return f'{path.name} (marimo):\n\n{path.read_text()}', False
        cells = json.loads(path.read_text()).get('cells', [])
        plain = [{'type': c['cell_type'] if c['cell_type'] == 'markdown' else 'code',
                  'source': ''.join(c.get('source', ''))} for c in cells]
        return f'{path.name} (Jupyter) cells:\n\n{json.dumps(plain, indent=1)}', False

    async def _run(self, *args: str) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            sys.executable, *args, cwd=self.directory, env=self._env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(process.communicate(), RUN_TIMEOUT)
        except TimeoutError:
            process.kill()
            await process.wait()
            return -1, '', f'The notebook did not finish within {RUN_TIMEOUT} s.'
        return process.returncode, out.decode(errors='replace'), err.decode(errors='replace')

    async def write(self, name: str, fmt: str, code: str | None = None, cells: list | None = None,
                    overwrite: bool = False) -> dict:
        """Check and save a notebook. Returns {'ok': True, file, format, title} or {'ok': False, 'problems'}."""
        if fmt not in FORMATS:
            return {'ok': False, 'problems': f'format must be one of {", ".join(FORMATS)}'}
        filename = slug(Path(str(name)).stem) + FORMATS[fmt]
        target = self.directory / filename
        if target.exists() and not overwrite:
            return {'ok': False, 'problems': f'{filename} already exists. Read it with read_notebook (the user may '
                                             'have edited it), then write it again with overwrite=true.'}
        self.directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.directory, prefix='.check-') as scratch:
            draft = Path(scratch) / filename
            if fmt == 'marimo':
                if not isinstance(code, str) or 'marimo.App' not in code:
                    return {'ok': False, 'problems': 'code must be a whole marimo notebook file (import marimo, '
                                                     'app = marimo.App(...), @app.cell functions).'}
                draft.write_text(with_credit_marimo(code))
                status, out, err = await self._run('-m', 'marimo', 'check', '--format', 'json', str(draft))
                try:
                    report = json.loads(out)
                except ValueError:
                    report = {'issues': [], 'summary': {'errored': status != 0}}
                breaking = [i for i in report.get('issues', []) if i.get('severity') != 'formatting']
                if report.get('summary', {}).get('errored') or breaking:
                    problems = '\n'.join(f"- line {i.get('line')}: {i.get('message')}"
                                         + (f" ({i['fix']})" if i.get('fix') else '') for i in breaking)
                    return {'ok': False, 'problems': 'marimo check found problems:\n' + (problems or _tail(err))}
                status, out, err = await self._run(str(draft))
                if status != 0:
                    return {'ok': False, 'problems': 'Running the notebook failed:\n' + _tail(err or out)}
            else:
                if not isinstance(cells, list) or not cells:
                    return {'ok': False, 'problems': 'cells must be a list of {"type": "markdown"|"code", "source"}.'}
                import nbformat
                nbformat.write(jupyter_notebook(cells), draft)
                status, out, err = await self._run(
                    '-m', 'nbconvert', '--to', 'notebook', '--execute', '--inplace',
                    f'--ExecutePreprocessor.timeout={RUN_TIMEOUT}', '--ExecutePreprocessor.kernel_name=python3',
                    str(draft))
                if status != 0:
                    return {'ok': False, 'problems': 'Running the notebook failed:\n' + _tail(err or out, 50)}
            os.replace(draft, target)
        return {'ok': True, 'file': filename, 'format': fmt, 'title': Path(filename).stem,
                'bytes': target.stat().st_size, 'missing': [], 'errors': {}}


WRITE_TOOL = {
    'name': 'write_notebook',
    'description': (
        'Write a marimo or Jupyter notebook for the user, with any Python (widgets, charts, analysis). '
        'Read the matching guide first. nembot checks and runs it once before saving; problems come back '
        'for you to fix. marimo: pass the whole .py file as code. Jupyter: pass cells.'),
    'input_schema': {
        'type': 'object',
        'properties': {
            'name': {'type': 'string', 'description': 'File name without extension, e.g. "vwap_by_region".'},
            'format': {'type': 'string', 'enum': list(FORMATS)},
            'code': {'type': 'string', 'description': 'marimo only: the complete notebook file.'},
            'cells': {'type': 'array', 'description': 'Jupyter only: the cells in order.',
                      'items': {'type': 'object', 'properties': {
                          'type': {'type': 'string', 'enum': ['markdown', 'code']},
                          'source': {'type': 'string'}}, 'required': ['type', 'source']}},
            'overwrite': {'type': 'boolean', 'description': 'Replace an existing notebook of this name.'},
        },
        'required': ['name', 'format'],
    },
}

LIVE_TOOL = {
    'name': 'run_in_marimo',
    'description': (
        'Run Python in the live kernel of a marimo notebook the user has open (from nembot), to inspect its '
        'variables or add, edit, run and delete cells with marimo\'s code mode. Read the marimo guide\'s LIVE '
        'section first. Use it when the user asks you to work in a notebook they have open.'),
    'input_schema': {
        'type': 'object',
        'properties': {
            'notebook': {'type': 'string', 'description': 'Notebook file name, e.g. "vwap_by_region_fuel.py".'},
            'code': {'type': 'string', 'description': 'Python to run in the notebook\'s scratchpad.'},
        },
        'required': ['notebook', 'code'],
    },
}

READ_TOOL = {
    'name': 'read_notebook',
    'description': 'Read a notebook in nembot\'s notebooks folder (marimo source, or Jupyter cells without outputs).',
    'input_schema': {
        'type': 'object',
        'properties': {'name': {'type': 'string', 'description': 'Notebook name, with or without extension.'}},
        'required': ['name'],
    },
}
