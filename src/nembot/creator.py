"""Creator mode: a separate chat mode for building nembot plugins.

The analyst prompt never mentions how to change the app; everything about the
plugin API lives here. Creator mode can inspect the running app, write files
into a draft folder, test the draft in a subprocess and, with the user's
approval, install it. Modelled on DeepSeek Harness's Creator mode.
"""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
from typing import AsyncIterator

from . import catalog, explorer, plugins
from .plugins import API_VERSION, MAX_FILE_BYTES, PluginManager, safe_path

TEST_TIMEOUT = 90

PROMPT = f"""You are nembot's plugin creator. nembot is a local web app for exploring
Australia's NEM (National Electricity Market) data from AEMO: a chat analyst with DuckDB tools,
an Explorer of MMS tables, Snippets, Reports, and plugins. You help the user build plugins that
add features without changing the core app. You can't edit core files; only plugin drafts.

## Workflow
1. Understand the request; ask a short question only if it is genuinely ambiguous.
2. Call `inspect_runtime` to see installed plugins, core tools, downloaded tables and extension points.
   Use `query_mms` / `describe_table` to check the data you will rely on.
3. Write the plugin into its draft folder with `write_plugin_file` (one call per file, full contents).
   Editing an installed plugin? `list_plugin_files` seeds the draft from it; read files, then rewrite them.
4. Call `test_plugin` (optionally with a tool name + args, or a GET route) and fix every error it reports.
5. Call `install_plugin`. The user reviews the files and approves; the app reloads plugins.
6. Tell the user what was added and where to find it (a tab, a chat tool, a button).
Keep plugins small and focused. Prefer reading data with SQL over adding dependencies (only the
Python standard library, polars, duckdb and altair are available).

## Plugin layout (folder name = plugin name: lowercase letters, digits, - or _)
plugins/<name>/
  plugin.toml   required: name = "<name>", title = "...", description = "...", version = "0.1.0"
  plugin.py     optional server code
  client.js     optional browser code
  style.css     optional styles (prefix classes with the plugin name)

## plugin.py (plugin API version {API_VERSION})
def setup(nem):
    # nem.name, nem.data_dir (a Path for the plugin's own files), nem.log (logging.Logger)
    # nem.query(sql) -> polars.DataFrame  (one read-only SELECT against the NEM DuckDB database)

    @nem.tool("Describe what it does and when to use it.", {{
        "type": "object",
        "properties": {{"region": {{"type": "string", "description": "e.g. SA1"}}}},
        "required": ["region"]}})
    def spike_count(args):            # becomes the chat tool '<name>_spike_count'
        frame = nem.query(f"SELECT count(*) AS n FROM DISPATCHPRICE WHERE REGIONID = '{{args['region']}}' AND RRP > 300")
        return {{"text": f"{{frame['n'][0]}} intervals above $300/MWh",           # what the model reads
                "table": {{"columns": frame.columns, "rows": [list(r) for r in frame.rows()]}},  # optional table shown to the user
                "events": [{{"kind": "badge", "data": {{"n": int(frame['n'][0])}}}}]}}   # optional events for client.js
        # or simply: return "plain text result"

    @nem.route("GET", "/summary")     # served at /plugins/<name>/summary
    def summary(request):             # request.query_params is a dict; may be async
        return {{"ok": True}}           # dict/list -> JSON, str -> HTML, or a starlette Response

    nem.prompt("One sentence telling the analyst when to use this plugin's tools.")
Validate tool inputs yourself (they come from a model). Escape or whitelist values you put in SQL
(e.g. check a region is one of NSW1, QLD1, SA1, TAS1, VIC1). Tools may be async.

## client.js (runs in the page; `nembot` is this plugin's API object)
nembot.addTab({{ id: "spikes", title: "Spikes", render(container) {{
  container.append(nembot.el("h2", "", "Price spikes"));
  nembot.getJSON("/summary").then(data => {{ /* path is relative to /plugins/<name> */ }});
}} }});
nembot.onChatEvent("badge", (event, place) => place(nembot.el("div", "", `n = ${{event.data.n}}`)));
nembot.addResultAction(result => {{  // result: {{type: "table"|"plot", sql, columns?, rows?, result_id?}}
  const b = nembot.el("button", "small", "My action"); b.onclick = () => alert(result.sql); return b;
}});
Helpers: nembot.el(tag, className, text), .getJSON(path), .fetch(path, options), .renderTable(
{{columns, rows, sql?}}), .renderPlot({{spec, sql?}}) (a Vega-Lite spec WITH data), .renderMarkdown(text),
.askChat(text) (prefill the analyst chat), .name. Use textContent, never innerHTML with data.
Tabs appear in the top navigation at #plugin/<name>/<id>.

## Rules
- Only write inside the draft folder via the tools; never ask the user to edit core files.
- Installed plugins run with the app's permissions on the user's machine: no network access, no
  shell commands, no writes outside nem.data_dir, unless the user explicitly asked for it.
- Data model: AEMO MMS tables (see inspect_runtime for what's downloaded). Times are market time
  (UTC+10), 5-minute interval-ending SETTLEMENTDATE for dispatch tables. Regions NSW1 QLD1 SA1 TAS1 VIC1.
"""

TOOLS = [
    {'name': 'inspect_runtime', 'description': 'Describe the running app: installed plugins (status, errors, '
     'tools, routes), core chat tools, downloaded tables, drafts and the plugin API version.',
     'input_schema': {'type': 'object', 'properties': {}}},
    {'name': 'list_plugin_files', 'description': "List a plugin draft's files (creating the draft, seeded from "
     'the installed plugin of that name if any).',
     'input_schema': {'type': 'object', 'properties': {'plugin': {'type': 'string'}}, 'required': ['plugin']}},
    {'name': 'read_plugin_file', 'description': 'Read a file from a plugin draft.',
     'input_schema': {'type': 'object', 'properties': {'plugin': {'type': 'string'}, 'path': {'type': 'string'}},
                      'required': ['plugin', 'path']}},
    {'name': 'write_plugin_file', 'description': 'Create or replace a file in a plugin draft (full contents).',
     'input_schema': {'type': 'object', 'properties': {'plugin': {'type': 'string'}, 'path': {'type': 'string'},
                                                       'content': {'type': 'string'}},
                      'required': ['plugin', 'path', 'content']}},
    {'name': 'delete_plugin_file', 'description': 'Delete a file from a plugin draft.',
     'input_schema': {'type': 'object', 'properties': {'plugin': {'type': 'string'}, 'path': {'type': 'string'}},
                      'required': ['plugin', 'path']}},
    {'name': 'test_plugin', 'description': 'Load a plugin draft in an isolated process and report load errors, '
     'registrations and client.js syntax; optionally call one of its tools with args, or GET one of its routes.',
     'input_schema': {'type': 'object', 'properties': {
         'plugin': {'type': 'string'}, 'tool': {'type': 'string', 'description': 'Tool name (short or full).'},
         'args': {'type': 'object', 'description': 'Tool input.'},
         'route': {'type': 'string', 'description': 'GET route path, e.g. /summary?x=1'}}, 'required': ['plugin']}},
    {'name': 'install_plugin', 'description': 'Ask the user to approve installing a tested draft; on approval it '
     'replaces any installed version (backed up) and reloads plugins. Returns the load status.',
     'input_schema': {'type': 'object', 'properties': {'plugin': {'type': 'string'},
                                                       'summary': {'type': 'string', 'description': 'One or two '
                                                                   'sentences for the user: what it adds.'}},
                      'required': ['plugin', 'summary']}},
]
TOOL_NAMES = {t['name'] for t in TOOLS}


class Creator:
    def __init__(self, manager: PluginManager, db, app_url: str, database: str):
        self.manager, self.db, self.app_url, self.database = manager, db, app_url, database

    def inspect(self, core_tools: list[str]) -> dict:
        overview = explorer.overview(self.db)
        downloaded = [{'table': t['name'], 'rows': t['rows']} for p in overview['packages'] for t in p['tables']
                      if t['downloaded']]
        drafts = sorted(p.name for p in self.manager.drafts.iterdir() if p.is_dir()) if self.manager.drafts.exists() else []
        return {
            'plugin_api_version': API_VERSION, 'plugins_dir': str(self.manager.directory),
            'installed_plugins': [p.public() for p in self.manager.plugins.values()], 'drafts': drafts,
            'core_chat_tools': core_tools, 'downloaded_tables': downloaded,
            'data_model': f"AEMO Electricity Data Model {catalog.source()['version']}; describe_table documents any table",
            'client_extension_points': ['addTab', 'onChatEvent', 'addResultAction'],
        }

    def files(self, name: str) -> list[dict]:
        draft = self.manager.start_draft(name)
        return [{'path': str(p.relative_to(draft)), 'bytes': p.stat().st_size} for p in sorted(draft.rglob('*'))
                if p.is_file() and '__pycache__' not in p.parts]

    def test(self, name: str, tool: str | None, args: dict | None, route: str | None) -> dict:
        draft = self.manager.start_draft(name)
        command = [sys.executable, '-I', '-m', 'nembot.plugin_test', str(draft), '--app-url', self.app_url,
                   '--database', self.database]
        if tool:
            command += ['--tool', tool, '--args', json.dumps(args or {})]
        if route:
            command += ['--route', route]
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=TEST_TIMEOUT, cwd=draft)
        except subprocess.TimeoutExpired:
            return {'ok': False, 'error': f'the test did not finish within {TEST_TIMEOUT} s (an infinite loop?)'}
        try:
            return json.loads(done.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {'ok': False, 'error': (done.stderr or done.stdout)[-3000:]}

    async def run(self, call: dict, conversation, core_tools: list[str]) -> AsyncIterator[dict]:
        """Execute one creator tool; yields UI events and finally {'type': 'result', 'text', 'is_error'}."""
        name, args = call['name'], call['input'] if isinstance(call['input'], dict) else {}

        def result(value, is_error=False):
            text = value if isinstance(value, str) else json.dumps(value, indent=1, default=str)
            return {'type': 'result', 'text': text[:20000], 'is_error': is_error}

        try:
            plugin = str(args.get('plugin', '')).strip().lower()
            if name == 'inspect_runtime':
                yield result(await asyncio.to_thread(self.inspect, core_tools))
            elif name == 'list_plugin_files':
                yield result(self.files(plugin))
            elif name == 'read_plugin_file':
                path = safe_path(self.manager.start_draft(plugin), args['path'])
                yield result(path.read_text() if path.exists() else f'{args["path"]} does not exist in the draft')
            elif name == 'write_plugin_file':
                content = str(args.get('content', ''))
                if len(content.encode()) > MAX_FILE_BYTES:
                    raise ValueError(f'files are limited to {MAX_FILE_BYTES} bytes')
                path = safe_path(self.manager.start_draft(plugin), args['path'])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
                yield {'type': 'plugin_file', 'plugin': plugin, 'path': args['path'], 'content': content}
                yield result(f'Wrote {args["path"]} ({len(content)} characters) in the {plugin} draft.')
            elif name == 'delete_plugin_file':
                path = safe_path(self.manager.start_draft(plugin), args['path'])
                path.unlink(missing_ok=True)
                yield result(f'Deleted {args["path"]}.')
            elif name == 'test_plugin':
                report = await asyncio.to_thread(self.test, plugin, args.get('tool'), args.get('args'), args.get('route'))
                yield {'type': 'plugin_test', 'plugin': plugin, 'report': report}
                yield result(report, is_error=not report.get('ok'))
            elif name == 'install_plugin':
                async for event in self._install(plugin, str(args.get('summary', '')), call['id'], conversation):
                    yield event
            else:
                yield result(f'Unknown creator tool {name}', True)
        except (ValueError, OSError, KeyError) as exc:
            yield result(f'{type(exc).__name__}: {exc}', True)

    async def _install(self, plugin: str, summary: str, request_id: str, conversation) -> AsyncIterator[dict]:
        files = self.files(plugin)
        if not any(f['path'] == 'plugin.toml' for f in files):
            yield {'type': 'result', 'text': 'The draft has no plugin.toml yet.', 'is_error': True}
            return
        report = await asyncio.to_thread(self.test, plugin, None, None, None)
        future = asyncio.get_running_loop().create_future()
        conversation.pending[request_id] = future
        replacing = (self.manager.directory / plugin).exists()
        yield {'type': 'plugin_install_request', 'id': request_id, 'plugin': plugin, 'summary': summary,
               'files': files, 'replacing': replacing, 'test': report}
        try:
            decision = await asyncio.wait_for(future, 1800)
        except TimeoutError:
            decision = {'approve': False}
        finally:
            conversation.pending.pop(request_id, None)
        if not decision.get('approve'):
            yield {'type': 'plugin_install_result', 'id': request_id, 'plugin': plugin, 'status': 'declined'}
            yield {'type': 'result', 'text': 'The user declined the installation. The draft is kept.', 'is_error': False}
            return
        installed = await asyncio.to_thread(self.manager.install, plugin)
        public = installed.public()
        yield {'type': 'plugin_install_result', 'id': request_id, 'plugin': plugin, 'status': public['status'],
               'error': public['error']}
        yield {'type': 'result', 'text': json.dumps({'installed': plugin, **public}, default=str),
               'is_error': public['status'] != 'loaded'}
