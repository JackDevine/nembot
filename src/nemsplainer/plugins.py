"""User plugins: extra chat tools, HTTP routes and UI, loaded from a directory.

A plugin is a folder in the plugins directory (default ``plugins/``):

- ``plugin.toml`` (required): ``name``, ``title``, ``description``, ``version``.
- ``plugin.py`` (optional): ``def setup(nem): ...`` registers tools, routes and
  a short prompt note through the :class:`Context` passed as ``nem``.
- ``client.js`` (optional): browser code using the ``nemsplainer`` API object
  (tabs, chat event renderers, result actions); see ``static/plugins.js``.
- ``style.css`` (optional): extra styles.

Plugins run in the app's process with its permissions. Creator mode writes
drafts to ``<plugins>/.drafts/<name>``, tests them in a subprocess
(``plugin_test.py``) and installs them only after the user approves.
Inspired by DeepSeek Harness's plugin architecture and Creator mode.
"""
import importlib.util
import inspect
import itertools
import json
import logging
from pathlib import Path
import re
import shutil
import threading
import time
import tomllib
import traceback
from dataclasses import dataclass, field
from typing import Callable

API_VERSION = 1
NAME = re.compile(r'[a-z][a-z0-9_-]{0,39}')
TOOL_NAME = re.compile(r'[a-z][a-z0-9_]{0,39}')
ALLOWED_FILES = re.compile(r'[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*\.(toml|py|js|css|md|json|txt|sql|csv)')
MAX_FILE_BYTES = 200_000
_modules = itertools.count(1)
log = logging.getLogger(__name__)


@dataclass
class Tool:
    plugin: str
    name: str            # full tool name seen by the model: <plugin>_<tool>
    description: str
    input_schema: dict
    function: Callable


@dataclass
class Context:
    """What ``setup(nem)`` receives. ``query`` runs one SELECT and returns a polars DataFrame."""
    name: str
    directory: Path
    query: Callable
    tools: list[Tool] = field(default_factory=list)
    routes: dict[tuple[str, str], Callable] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    log: logging.Logger = None

    def __post_init__(self):
        self.log = logging.getLogger(f'nemsplainer.plugin.{self.name}')

    @property
    def data_dir(self) -> Path:
        """A per-plugin folder for files the plugin wants to keep."""
        path = self.directory / 'data'
        path.mkdir(exist_ok=True)
        return path

    def tool(self, description: str, input_schema: dict | None = None, name: str | None = None):
        """Decorator registering a chat tool. The function gets the tool input dict and returns
        a str, or a dict with 'text' plus optional 'table' {'columns', 'rows'} and 'events'."""
        def register(function):
            tool = name or function.__name__
            if not TOOL_NAME.fullmatch(tool):
                raise ValueError(f'tool name {tool!r} must match {TOOL_NAME.pattern}')
            full = f"{self.name.replace('-', '_')}_{tool}"
            schema = input_schema or {'type': 'object', 'properties': {}}
            self.tools.append(Tool(self.name, full, description, schema, function))
            return function
        return register

    def route(self, method: str, path: str):
        """Decorator for an HTTP handler served at /plugins/<name><path>. The handler gets
        (request) and returns a dict/list (JSON), a str (HTML) or a Starlette Response."""
        def register(function):
            if not path.startswith('/'):
                raise ValueError('route paths start with /')
            self.routes[method.upper(), path] = function
            return function
        return register

    def prompt(self, text: str):
        """A short note added to the analyst's system prompt (keep it to a sentence or two)."""
        self.notes.append(text.strip())


@dataclass
class Plugin:
    name: str
    directory: Path
    manifest: dict
    enabled: bool = True
    context: Context | None = None
    error: str | None = None
    loaded_at: float | None = None

    @property
    def status(self) -> str:
        return 'disabled' if not self.enabled else 'error' if self.error else 'loaded'

    def public(self) -> dict:
        ctx = self.context
        return {
            'name': self.name, 'title': self.manifest.get('title', self.name),
            'description': self.manifest.get('description', ''), 'version': self.manifest.get('version', ''),
            'status': self.status, 'error': self.error,
            'tools': [{'name': t.name, 'description': t.description} for t in ctx.tools] if ctx else [],
            'routes': [f'{m} /plugins/{self.name}{p}' for m, p in ctx.routes] if ctx else [],
            'notes': ctx.notes if ctx else [],
            'client': (self.directory / 'client.js').exists(), 'style': (self.directory / 'style.css').exists(),
            'files': sorted(str(p.relative_to(self.directory)) for p in self.directory.rglob('*')
                            if p.is_file() and 'data' not in p.relative_to(self.directory).parts[:1]),
        }


def read_manifest(directory: Path) -> dict:
    path = directory / 'plugin.toml'
    if not path.exists():
        raise ValueError('missing plugin.toml')
    manifest = tomllib.loads(path.read_text())
    if manifest.get('name') != directory.name:
        raise ValueError(f'plugin.toml name {manifest.get("name")!r} must equal the folder name {directory.name!r}')
    return manifest


def load_plugin(directory: Path, query: Callable) -> Plugin:
    """Import one plugin into a fresh module and run its setup; errors are captured, not raised."""
    plugin = Plugin(directory.name, directory, {})
    try:
        if not NAME.fullmatch(directory.name):
            raise ValueError(f'plugin folder names must match {NAME.pattern}')
        plugin.manifest = read_manifest(directory)
        context = Context(directory.name, directory, query)
        source = directory / 'plugin.py'
        if source.exists():
            module_name = f'nemsplainer_plugin_{directory.name.replace("-", "_")}_{next(_modules)}'
            spec = importlib.util.spec_from_file_location(module_name, source)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            setup = getattr(module, 'setup', None)
            if not callable(setup):
                raise ValueError('plugin.py must define setup(nem)')
            setup(context)
        plugin.context = context
        plugin.loaded_at = time.time()
    except Exception:
        plugin.error = traceback.format_exc(limit=8)
    return plugin


class PluginManager:
    def __init__(self, directory: Path, query: Callable):
        self.directory = directory
        self.query = query
        self.plugins: dict[str, Plugin] = {}
        self.lock = threading.Lock()

    @property
    def drafts(self) -> Path:
        return self.directory / '.drafts'

    def _state_path(self) -> Path:
        return self.directory / '.state.json'

    def disabled(self) -> set[str]:
        try:
            return set(json.loads(self._state_path().read_text()).get('disabled', []))
        except (OSError, ValueError):
            return set()

    def set_enabled(self, name: str, enabled: bool):
        disabled = self.disabled()
        disabled.discard(name) if enabled else disabled.add(name)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._state_path().write_text(json.dumps({'disabled': sorted(disabled)}, indent=1))
        self.reload()

    def reload(self) -> dict[str, Plugin]:
        """(Re)load every plugin folder. A broken plugin is reported, never fatal."""
        plugins = {}
        disabled = self.disabled()
        if self.directory.exists():
            for folder in sorted(p for p in self.directory.iterdir() if p.is_dir() and not p.name.startswith(('.', '_'))):
                if folder.name in disabled:
                    plugins[folder.name] = Plugin(folder.name, folder, _safe_manifest(folder), enabled=False)
                else:
                    plugins[folder.name] = load_plugin(folder, self.query)
                    if plugins[folder.name].error:
                        log.warning('plugin %s failed to load:\n%s', folder.name, plugins[folder.name].error)
        with self.lock:
            self.plugins = plugins
        return plugins

    def active(self) -> list[Plugin]:
        return [p for p in self.plugins.values() if p.status == 'loaded']

    def tools(self) -> list[Tool]:
        return [t for p in self.active() for t in p.context.tools]

    def tool_definitions(self) -> list[dict]:
        return [{'name': t.name, 'description': f'[plugin {t.plugin}] {t.description}', 'input_schema': t.input_schema}
                for t in self.tools()]

    def prompt_notes(self) -> str:
        notes = [f'- {p.name}: {n}' for p in self.active() for n in p.context.notes]
        return ('\n\nInstalled plugins add these notes:\n' + '\n'.join(notes)) if notes else ''

    def find_tool(self, name: str) -> Tool | None:
        return next((t for t in self.tools() if t.name == name), None)

    def route(self, plugin: str, method: str, path: str) -> Callable | None:
        found = self.plugins.get(plugin)
        if not found or found.status != 'loaded':
            return None
        return found.context.routes.get((method.upper(), path))

    def install(self, name: str) -> Plugin:
        """Copy a draft into place (backing up any previous version) and reload."""
        draft = self.drafts / name
        read_manifest(draft)
        target = self.directory / name
        if target.exists():
            backup = self.directory / '.backup' / f'{name}-{time.strftime("%Y%m%d-%H%M%S")}'
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(target, backup, ignore=shutil.ignore_patterns('data'))
            for item in target.iterdir():  # keep the plugin's data folder
                if item.name != 'data':
                    shutil.rmtree(item) if item.is_dir() else item.unlink()
        shutil.copytree(draft, target, dirs_exist_ok=True)
        self.reload()
        return self.plugins[name]

    def start_draft(self, name: str) -> Path:
        """Return the draft folder, seeding it from the installed plugin when there is one."""
        if not NAME.fullmatch(name):
            raise ValueError(f'plugin names must match {NAME.pattern}')
        draft = self.drafts / name
        installed = self.directory / name
        if not draft.exists():
            if installed.exists():
                shutil.copytree(installed, draft, ignore=shutil.ignore_patterns('data', '__pycache__'))
            else:
                draft.mkdir(parents=True)
        return draft


def _safe_manifest(folder: Path) -> dict:
    try:
        return read_manifest(folder)
    except Exception:
        return {}


def safe_path(root: Path, relative: str) -> Path:
    """Resolve a model-supplied relative path inside ``root``, or raise."""
    if not ALLOWED_FILES.fullmatch(relative or ''):
        raise ValueError(f'path {relative!r} is not allowed (use a relative path with an allowed extension: '
                         'toml, py, js, css, md, json, txt, sql, csv)')
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('path escapes the plugin folder')
    return path


async def call_tool(tool: Tool, args: dict) -> dict:
    """Run a plugin tool; returns {'text', 'table'?, 'events'?, 'is_error'}."""
    import asyncio
    try:
        if inspect.iscoroutinefunction(tool.function):
            result = await tool.function(args)
        else:
            result = await asyncio.to_thread(tool.function, args)
    except Exception:
        return {'text': f'Plugin tool {tool.name} failed:\n{traceback.format_exc(limit=5)}', 'is_error': True}
    if isinstance(result, str):
        return {'text': result, 'is_error': False}
    if isinstance(result, dict) and isinstance(result.get('text'), str):
        return {'is_error': False, **result}
    return {'text': f'Plugin tool {tool.name} returned {type(result).__name__}; expected str or dict with "text".',
            'is_error': True}


def table_from_frame(frame, limit: int = 200) -> dict:
    """Helper for plugins: a polars DataFrame -> {'columns', 'rows'} for a tool result table."""
    rows = frame.head(limit).rows()
    return {'columns': frame.columns, 'rows': [list(r) for r in rows], 'total': frame.height}
