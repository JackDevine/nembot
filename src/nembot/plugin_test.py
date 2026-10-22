"""Test a plugin draft in a separate process and print a JSON report.

Usage: python -I -m nembot.plugin_test DRAFT_DIR --app-url URL --database PATH
           [--tool NAME --args JSON] [--route PATH]

Creator mode runs this with a timeout, so a broken or hanging draft can't take
the app down. Queries go through the running app (``client.query``).
"""
import argparse
import asyncio
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess

from . import client, plugins


class FakeRequest:
    """Enough of a Starlette request for simple GET route handlers."""

    def __init__(self, path: str):
        path, _, query = path.partition('?')
        self.url = path
        self.query_params = dict(p.partition('=')[::2] for p in query.split('&') if p)
        self.path_params = {}

    async def json(self):
        return {}


def check_javascript(path: Path) -> str | None:
    node = shutil.which('node')
    if not node or not path.exists():
        return None
    result = subprocess.run([node, '--check', str(path)], capture_output=True, text=True, timeout=30)
    return result.stderr.strip() or None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--app-url', required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--tool')
    parser.add_argument('--args', default='{}')
    parser.add_argument('--route')
    options = parser.parse_args()

    report = {'ok': False}
    output = io.StringIO()

    def query(sql):
        return client.query(sql, url=options.app_url, database=options.database)

    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        plugin = plugins.load_plugin(options.directory, query)
        report['plugin'] = plugin.public()
        report['load_error'] = plugin.error
        report['client_js_error'] = check_javascript(options.directory / 'client.js')
        if plugin.error is None and options.tool:
            tool = next((t for t in plugin.context.tools if t.name == options.tool or t.name.endswith('_' + options.tool)), None)
            if tool is None:
                report['tool_result'] = {'text': f'no tool named {options.tool}', 'is_error': True}
            else:
                result = asyncio.run(plugins.call_tool(tool, json.loads(options.args)))
                if 'table' in result:
                    result['table'] = {**result['table'], 'rows': result['table']['rows'][:20]}
                report['tool_result'] = result
        if plugin.error is None and options.route:
            handler = plugin.context.routes.get(('GET', options.route.partition('?')[0]))
            if handler is None:
                report['route_result'] = f'no GET route {options.route}'
            else:
                value = handler(FakeRequest(options.route))
                if asyncio.iscoroutine(value):
                    value = asyncio.run(value)
                report['route_result'] = value if isinstance(value, (dict, list, str, int, float)) else repr(value)
    report['ok'] = (report['load_error'] is None and not report['client_js_error']
                    and not (report.get('tool_result') or {}).get('is_error'))
    report['output'] = output.getvalue()[-4000:]
    print(json.dumps(report, default=str))


if __name__ == '__main__':
    main()
