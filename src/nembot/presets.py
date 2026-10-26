"""AI services nembot can connect to through the OpenAI-compatible adapter (openai_compat.py).

Each preset says where the service lives, where to get a key, how to list its
models and what they can do. Only OpenRouter and Ollama are tested by us; the
others are marked ``tested=False`` and shown as "community tested". Model
capabilities come from the service where it reports them (OpenRouter's catalogue,
Ollama's /api/show); otherwise from the model name, and the onboarding test
(a real tool call) is what confirms a model works.
"""
import asyncio
from dataclasses import dataclass, field
import re

import httpx2 as httpx

from .models import ModelInfo


@dataclass(frozen=True)
class Preset:
    name: str            # provider id in model refs: '<name>:<model id>'
    label: str           # shown in the UI
    base_url: str        # OpenAI-compatible API root (…/v1)
    env: str | None      # environment variable holding the key
    key_url: str | None  # where to create a key (None: no key needed)
    tested: bool         # tested by the nembot developers
    local: bool = False  # runs on this computer; the address can be changed
    web_search: bool = False  # has a server-side web search tool we support
    test_model: str | None = None  # preferred model for the onboarding tool-call test
    note: str = ''
    headers: dict = field(default_factory=dict)

    @property
    def needs_key(self) -> bool:
        return self.key_url is not None


PRESETS = {p.name: p for p in [
    Preset('openrouter', 'OpenRouter', 'https://openrouter.ai/api/v1', 'OPENROUTER_API_KEY',
           'https://openrouter.ai/settings/keys', tested=True, web_search=True,
           test_model='openai/gpt-4o-mini', headers={'X-Title': 'nembot'},
           note='One account for hundreds of models from many providers, paid per use.'),
    Preset('openai', 'OpenAI', 'https://api.openai.com/v1', 'OPENAI_API_KEY',
           'https://platform.openai.com/api-keys', tested=False, test_model='gpt-4o-mini',
           note='GPT models with an OpenAI API key (separate from a ChatGPT subscription).'),
    Preset('gemini', 'Google Gemini', 'https://generativelanguage.googleapis.com/v1beta/openai', 'GEMINI_API_KEY',
           'https://aistudio.google.com/apikey', tested=False, test_model='gemini-2.5-flash',
           note='Gemini models with a Google AI Studio key.'),
    Preset('deepseek', 'DeepSeek', 'https://api.deepseek.com/v1', 'DEEPSEEK_API_KEY',
           'https://platform.deepseek.com/api_keys', tested=False, test_model='deepseek-chat',
           note='DeepSeek models with a DeepSeek platform key.'),
    Preset('ollama', 'Ollama', 'http://127.0.0.1:11434/v1', None, None, tested=True, local=True,
           note='Models running on this computer with Ollama. Free; quality depends on the model and your hardware.'),
    Preset('lmstudio', 'LM Studio', 'http://127.0.0.1:1234/v1', None, None, tested=False, local=True,
           note='Models running on this computer with LM Studio.'),
]}

# Model ids that aren't chat models (embeddings, speech, images, …).
_NOT_CHAT = re.compile(r'embed|tts|whisper|dall-e|imagen|image|audio|realtime|transcribe|moderation|search|'
                       r'veo|lyria|aqa|davinci|babbage|computer-use|codex-mini', re.I)


def _vision(preset: str, model_id: str) -> bool:
    """Image input, guessed from the name where the service doesn't say."""
    mid = model_id.lower()
    if preset == 'openai':
        return mid.startswith(('gpt-4o', 'gpt-4.1', 'gpt-5', 'o3', 'o4', 'chatgpt-4o'))
    if preset == 'gemini':
        return 'gemini' in mid or 'gemma-3' in mid
    if preset == 'lmstudio':
        return any(tag in mid for tag in ('vl', 'vision', 'gemma-3', 'llava', 'pixtral'))
    return False


def _per_million(value) -> float | None:
    try:
        return round(float(value) * 1_000_000, 4)
    except (TypeError, ValueError):
        return None


async def list_models(preset: Preset, key: str | None, base_url: str | None = None,
                      available: bool = True) -> list[ModelInfo]:
    """The preset's chat models. Raises on network or authentication errors."""
    base = (base_url or preset.base_url).rstrip('/')
    headers = {'Authorization': f'Bearer {key}'} if key else {}
    async with httpx.AsyncClient(timeout=20) as client:
        if preset.name == 'ollama':
            return await _ollama_models(client, base, available)
        if preset.name == 'openrouter' and key:
            # OpenRouter lists models without a key, so check the key itself.
            (await client.get(f'{base}/key', headers=headers)).raise_for_status()
        response = await client.get(f'{base}/models', headers=headers)
        response.raise_for_status()
        data = response.json().get('data') or []
    if preset.name == 'openrouter':
        return _openrouter_models(data, available)
    models = []
    for m in data:
        model_id = str(m.get('id', '')).removeprefix('models/')  # Gemini lists "models/<id>"
        if not model_id or _NOT_CHAT.search(model_id):
            continue
        models.append(ModelInfo(f'{preset.name}:{model_id}', preset.name, model_id,
                                m.get('display_name') or m.get('name') or model_id,
                                _vision(preset.name, model_id), True, available))
    return sorted(models, key=lambda m: m.name.lower())


def _openrouter_models(data: list, available: bool) -> list[ModelInfo]:
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


async def _ollama_models(client, base: str, available: bool) -> list[ModelInfo]:
    """Ollama reports each model's capabilities (e.g. ['completion', 'tools', 'vision'])."""
    root = base.removesuffix('/v1')
    response = await client.get(f'{root}/api/tags')
    response.raise_for_status()
    names = [m['name'] for m in response.json().get('models') or []]

    async def show(name):
        try:
            r = await client.post(f'{root}/api/show', json={'model': name})
            r.raise_for_status()
            info = r.json()
        except Exception:
            return name, [], None
        context = next((v for k, v in (info.get('model_info') or {}).items() if k.endswith('.context_length')), None)
        return name, info.get('capabilities') or [], context

    shown = await asyncio.gather(*(show(name) for name in names))
    return [ModelInfo(f'ollama:{name}', 'ollama', name, name, 'vision' in caps, 'tools' in caps, available, context)
            for name, caps, context in shown if 'embedding' not in caps]


async def reachable(preset: Preset, base_url: str | None = None) -> bool:
    """Is a local server answering at its address?"""
    base = (base_url or preset.base_url).rstrip('/')
    url = f"{base.removesuffix('/v1')}/api/version" if preset.name == 'ollama' else f'{base}/models'
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            return (await client.get(url)).status_code == 200
    except Exception:
        return False
