"""Model catalogue: the models of every connected service (see connections.py), cached for an hour."""
import asyncio
from dataclasses import asdict, dataclass
import time

import anthropic

DEFAULT_MODEL = 'claude:claude-opus-5-5'
CACHE_SECONDS = 3600


@dataclass(frozen=True)
class ModelInfo:
    ref: str            # '<provider>:<model id>', unique across providers
    provider: str       # 'claude' (Anthropic API), 'subscription' (Claude plan) or a preset name (presets.py)
    id: str
    name: str
    vision: bool        # accepts image input, so it can inspect plots
    tools: bool         # supports tool calling; required to query the database
    available: bool     # credentials for the provider are configured
    context: int | None = None
    price_in: float | None = None   # USD per million input tokens
    price_out: float | None = None
    efforts: tuple = ()             # reasoning levels the model takes, lowest first ('off'/'on' for Ollama)
    default_effort: str | None = None  # the level nembot uses unless the user picks another


EFFORT_ORDER = ['none', 'off', 'minimal', 'low', 'medium', 'high', 'on', 'xhigh', 'max']


def efforts(levels) -> tuple:
    """Known reasoning levels in order, lowest first."""
    levels = {str(level) for level in levels or ()}
    return tuple(level for level in EFFORT_ORDER if level in levels)


def default_effort(levels: tuple) -> str | None:
    """nembot's default: medium, as for Claude, else the nearest level (high before low), else on."""
    return next((level for level in ('medium', 'high', 'low', 'on') if level in levels), levels[0] if levels else None)


async def claude_models(client: anthropic.AsyncAnthropic) -> list[ModelInfo]:
    """Claude models from the Anthropic models API. Every one accepts images and tools."""
    models = []
    async for m in client.models.list(limit=100):
        capability = ((m.to_dict().get('capabilities') or {}).get('effort') or {})
        levels = efforts(level for level, v in capability.items() if isinstance(v, dict) and v.get('supported'))
        models.append(ModelInfo(f'claude:{m.id}', 'claude', m.id, m.display_name, True, True, True,
                                getattr(m, 'max_input_tokens', None), efforts=levels, default_effort=default_effort(levels)))
    return models


class Catalog:
    """``sources()`` returns {label: async () -> list[ModelInfo]} for the services connected right now.

    A source that fails is reported in ``errors`` (e.g. "Claude models unavailable: …") and skipped.
    ``preferred`` lists refs to default to, in order, when they're available.
    """

    def __init__(self, sources, preferred=()):
        self._sources = sources
        self.preferred = list(preferred)
        self._models: dict[str, ModelInfo] = {}
        self._fetched = 0.0
        self._lock = asyncio.Lock()
        self.errors: list[str] = []

    def invalidate(self) -> None:
        self._fetched = 0.0

    async def models(self) -> dict[str, ModelInfo]:
        async with self._lock:
            if not self._fetched or time.monotonic() - self._fetched > CACHE_SECONDS:
                sources = self._sources()
                results = await asyncio.gather(*(self._fetch(label, fetch) for label, fetch in sources.items()))
                self.errors = [error for _, error in results if error]
                self._models = {m.ref: m for models, _ in results for m in models}
                self._fetched = time.monotonic()
            return self._models

    @staticmethod
    async def _fetch(label: str, fetch) -> tuple[list[ModelInfo], str | None]:
        try:
            return await asyncio.wait_for(fetch(), 60), None
        except Exception as exc:  # e.g. logged out of the `ant` profile, a key revoked, a local server stopped
            return [], f'{label} models unavailable: {exc}'

    async def get(self, ref: str) -> ModelInfo | None:
        models = await self.models()
        return models.get(ref)

    def usable(self) -> list[ModelInfo]:
        return [m for m in self._models.values() if m.tools and m.available]

    def default(self) -> str:
        """The first preferred model that's available, else the first usable one, else DEFAULT_MODEL."""
        usable = {m.ref for m in self.usable()}
        for ref in self.preferred:
            if ref in usable:
                return ref
        return next(iter(m.ref for m in self.usable()), DEFAULT_MODEL)


def public(model: ModelInfo) -> dict:
    return asdict(model)
