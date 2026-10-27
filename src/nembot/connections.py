"""The AI services nembot is connected to: their providers, model catalogue and setup status.

Credentials come from credentials.py (environment variables first). When the
onboarding page saves or removes a connection, ``refresh()`` rebuilds the
Anthropic client and drops the cached model list, so no restart is needed.

The Claude plan provider (subscription.py) is opt-in: it's only offered with
``claude_plan = true`` in the config, because Anthropic doesn't allow third-party
apps to offer claude.ai login without approval.
"""
import asyncio
import os
from pathlib import Path

import anthropic

from . import credentials, subscription
from .claude import ClaudeProvider
from .models import DEFAULT_MODEL, Catalog, claude_models
from .ollama import OllamaProvider
from .openai_compat import OpenAICompatProvider
from .presets import PRESETS, list_models, reachable

ANTHROPIC = {'name': 'claude', 'label': 'Anthropic API', 'env': 'ANTHROPIC_API_KEY',
             'key_url': 'https://platform.claude.com/settings/keys',
             'note': 'Claude models with an Anthropic API key, billed per use.'}


def ant_profile() -> str | None:
    """The `ant auth login` profile the Anthropic SDK would use, if it has stored credentials."""
    root = Path(os.environ.get('ANTHROPIC_CONFIG_DIR') or Path.home() / '.config' / 'anthropic')
    name = os.environ.get('ANTHROPIC_PROFILE')
    if not name:
        try:
            name = (root / 'active_config').read_text().strip() or 'default'
        except OSError:
            name = 'default'
    return name if (root / 'credentials' / f'{name}.json').exists() else None


def anthropic_client(key: str | None) -> anthropic.AsyncAnthropic:
    """A client for ``key``, else the `ant` profile. The SDK reads the profile when the client is made and
    raises if it's missing or broken; then a placeholder is returned (the API counts as not connected)."""
    try:
        return anthropic.AsyncAnthropic(api_key=key) if key else anthropic.AsyncAnthropic()
    except Exception:
        return anthropic.AsyncAnthropic(api_key='not-configured')


class Connections:
    def __init__(self, session_dir=None):
        self.providers = {'claude': ClaudeProvider(anthropic_client(None)),
                          'subscription': subscription.SubscriptionProvider(session_dir=session_dir)}
        for name, preset in PRESETS.items():
            self.providers[name] = OpenAICompatProvider(preset, lambda name=name: self.connection(name))
        self.providers['ollama'] = OllamaProvider()  # its own API, which takes a context size
        self.catalog = Catalog(self._sources, preferred=[DEFAULT_MODEL, 'subscription:claude-opus-5-5'] + [
            f'{p.name}:{p.test_model}' for p in PRESETS.values() if p.test_model])
        self.refresh()

    # ---- credentials ----
    def connection(self, name: str) -> tuple[str | None, str | None]:
        """(key, base_url) for a preset."""
        preset = PRESETS[name]
        key = credentials.api_key(name, preset.env)[0] if preset.needs_key else None
        return key, credentials.provider(name).get('base_url')

    def anthropic_key(self) -> tuple[str | None, str | None]:
        return credentials.api_key('claude', ANTHROPIC['env'])

    def claude_connected(self) -> str | None:
        """How the Anthropic API is connected ('environment', 'file', 'keychain', 'ant profile'), or None."""
        return self.anthropic_key()[1] or ('ant profile' if ant_profile() else None)

    def connected(self, name: str) -> bool:
        if name == 'claude':
            return bool(self.claude_connected())
        if name == 'subscription':
            return self.plan_enabled()
        preset = PRESETS[name]
        if preset.needs_key:
            return bool(self.connection(name)[0])
        return bool(credentials.provider(name).get('enabled'))  # a local server the user added

    @staticmethod
    def plan_enabled() -> bool:
        return bool(credentials.setting('claude_plan', False))

    def refresh(self) -> None:
        key = self.anthropic_key()[0]
        self.providers['claude'].client = anthropic_client(key)
        self.catalog.invalidate()

    # ---- models ----
    def _sources(self) -> dict:
        sources = {}
        if self.plan_enabled():
            async def plan():
                models, error = await subscription.list_models()
                if error:
                    raise RuntimeError(error.removeprefix('Claude plan models unavailable: '))
                return models
            sources['Claude plan'] = plan
        if self.claude_connected():
            client = self.providers['claude'].client
            sources['Claude'] = lambda: claude_models(client)
        for name, preset in PRESETS.items():
            if self.connected(name):
                key, base_url = self.connection(name)
                sources[preset.label] = lambda preset=preset, key=key, base_url=base_url: list_models(preset, key, base_url)
        return sources

    @staticmethod
    def labels() -> list[dict]:
        """Picker groups in order: provider name, short label and group title."""
        return ([{'name': 'subscription', 'label': 'Claude plan', 'title': 'Claude (your Claude plan, through Claude Code)'},
                 {'name': 'claude', 'label': 'Claude', 'title': 'Claude (Anthropic API)'}]
                + [{'name': p.name, 'label': p.label, 'title': p.label + (' (on this computer)' if p.local else '')}
                   for p in PRESETS.values()])

    # ---- setup page ----
    async def status(self) -> list[dict]:
        """One card per service for the onboarding page; keys are never included."""
        local = {name: p for name, p in PRESETS.items() if p.local}
        found = dict(zip(local, await asyncio.gather(
            *(reachable(p, credentials.provider(name).get('base_url')) for name, p in local.items()))))
        cards = []
        if self.plan_enabled():
            cards.append({'name': 'subscription', 'label': 'Claude plan', 'connected': True, 'source': 'setting',
                          'needs_key': False, 'local': False, 'tested': True, 'web_search': True,
                          'note': 'Claude through your Claude plan, using this computer’s Claude Code login '
                                  '(turned on with claude_plan = true in the config).'})
        source = self.claude_connected()
        cards.append({**ANTHROPIC, 'connected': bool(source), 'source': source, 'needs_key': True, 'local': False,
                      'tested': True, 'web_search': True})
        for name, preset in PRESETS.items():
            key_source = credentials.api_key(name, preset.env)[1] if preset.needs_key else None
            cards.append({'name': name, 'label': preset.label, 'connected': self.connected(name),
                          'source': key_source or ('setup' if self.connected(name) else None),
                          'env': preset.env, 'key_url': preset.key_url, 'needs_key': preset.needs_key,
                          'local': preset.local, 'detected': found.get(name),
                          'base_url': self.connection(name)[1] or preset.base_url,
                          'tested': preset.tested, 'web_search': preset.web_search, 'note': preset.note})
        return cards
