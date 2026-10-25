"""Model catalogue: Claude models from Anthropic plus the OpenRouter catalogue."""
import asyncio
from dataclasses import asdict, dataclass
import os
import time

import anthropic
import httpx2 as httpx

DEFAULT_MODEL = 'claude:claude-opus-5-5'
OPENROUTER_MODELS = 'https://openrouter.ai/api/v1/models'
CACHE_SECONDS = 3600


@dataclass(frozen=True)
class ModelInfo:
    ref: str            # '<provider>:<model id>', unique across providers
    provider: str       # 'claude' (direct Anthropic API) or 'openrouter'
    id: str
    name: str
    vision: bool        # accepts image input, so it can inspect plots
    tools: bool         # supports tool calling; required to query the database
    available: bool     # credentials for the provider are configured
    context: int | None = None
    price_in: float | None = None   # USD per million input tokens
    price_out: float | None = None


class Catalog:
    def __init__(self, claude: anthropic.AsyncAnthropic):
        self._claude = claude
        self._models: dict[str, ModelInfo] = {}
        self._fetched = 0.0
        self._lock = asyncio.Lock()
        self.errors: list[str] = []

    async def models(self) -> dict[str, ModelInfo]:
        async with self._lock:
            if not self._models or time.monotonic() - self._fetched > CACHE_SECONDS:
                self.errors = []
                claude, openrouter = await asyncio.gather(self._claude_models(), self._openrouter_models())
                models = {m.ref: m for m in claude + openrouter}
                if models:
                    self._models, self._fetched = models, time.monotonic()
            return self._models

    async def get(self, ref: str) -> ModelInfo | None:
        models = await self.models()
        if ref in models:
            return models[ref]
        if ref == DEFAULT_MODEL:  # usable even if the models API could not be reached
            return ModelInfo(ref, 'claude', ref.split(':', 1)[1], 'Claude Opus 5.5', True, True, True)
        return None

    async def _claude_models(self) -> list[ModelInfo]:
        try:
            listed = [m async for m in self._claude.models.list(limit=100)]
        except (anthropic.APIError, anthropic.CredentialsError) as exc:  # e.g. logged out of the `ant` profile
            self.errors.append(f'Claude models unavailable: {exc}')
            return []
        # Every Claude model served by the models API accepts images and tools.
        return [ModelInfo(f'claude:{m.id}', 'claude', m.id, m.display_name, True, True, True,
                          getattr(m, 'max_input_tokens', None)) for m in listed]

    async def _openrouter_models(self) -> list[ModelInfo]:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(OPENROUTER_MODELS)
                response.raise_for_status()
                data = response.json()['data']
        except Exception as exc:
            self.errors.append(f'OpenRouter models unavailable: {exc}')
            return []
        available = bool(os.environ.get('OPENROUTER_API_KEY'))
        models = []
        for m in data:
            inputs = m.get('architecture', {}).get('input_modalities') or []
            if 'text' not in (m.get('architecture', {}).get('output_modalities') or ['text']):
                continue  # image/audio generators can't drive a chat
            pricing = m.get('pricing') or {}
            models.append(ModelInfo(
                f"openrouter:{m['id']}", 'openrouter', m['id'], m.get('name') or m['id'],
                'image' in inputs, 'tools' in (m.get('supported_parameters') or []), available,
                m.get('context_length'), _per_million(pricing.get('prompt')), _per_million(pricing.get('completion')),
            ))
        return models


def _per_million(value) -> float | None:
    try:
        return round(float(value) * 1_000_000, 4)
    except (TypeError, ValueError):
        return None


def public(model: ModelInfo) -> dict:
    return asdict(model)
