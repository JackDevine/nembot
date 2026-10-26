"""OpenRouter: the OpenAI-compatible adapter with the OpenRouter preset (kept for existing imports)."""
from . import credentials
from .openai_compat import (OpenAICompatProvider, WEB_SEARCH, _call_id, _merge_reasoning,  # noqa: F401
                            functions, render, usage_event)
from .presets import PRESETS


def OpenRouterProvider() -> OpenAICompatProvider:
    preset = PRESETS['openrouter']
    return OpenAICompatProvider(preset, lambda: (credentials.api_key('openrouter', preset.env)[0], None))
