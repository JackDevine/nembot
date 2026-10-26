"""Onboarding: test a connection before saving it, and sign in to OpenRouter.

A connection is tested in two steps: list the service's models (this checks the
key and address without spending anything), then send one tiny request that must
call a tool, since nembot can't query the database without tool calling. The key
is saved when the first step passes; the second only warns, as another model of
the same service may still work.
"""
import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

import anthropic
import httpx2 as httpx
import openai

from . import credentials
from .connections import ANTHROPIC, ant_profile, anthropic_client
from .models import ModelInfo, claude_models
from .presets import PRESETS, list_models

PING = {'name': 'ping', 'description': 'Reply to a connection test. Always call this when asked to.',
        'input_schema': {'type': 'object', 'properties': {'word': {'type': 'string'}}, 'required': ['word']}}
PING_PROMPT = 'This is a connection test. Call the ping tool with word "nembot". Do not reply with text.'
CLAUDE_TEST_MODELS = ['claude-haiku-5-5', 'claude-haiku-4-5-20251001', 'claude-sonnet-5-5']
OPENROUTER_AUTH = 'https://openrouter.ai/auth'
OPENROUTER_KEYS = 'https://openrouter.ai/api/v1/auth/keys'


def explain(exc: Exception, where: str) -> str:
    """A plain-language reason a connection failed."""
    status = getattr(exc, 'status_code', None) or getattr(getattr(exc, 'response', None), 'status_code', None)
    if status in (401, 403):
        return f'The key was rejected ({status}). Check that it was copied in full and is still active.'
    if status == 402:
        return 'The account has no credit left (402).'
    if status == 429:
        return 'Rate limited (429). Wait a moment and try again.'
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, openai.APIConnectionError,
                        anthropic.APIConnectionError)):
        return f'Could not reach {where}. Is it running, and is the address right?'
    if isinstance(exc, anthropic.CredentialsError):
        return 'No Anthropic credentials found. Paste an API key, or run `ant auth login`.'
    message = getattr(exc, 'message', None) or str(exc) or type(exc).__name__
    return f'{message} ({status})' if status else message


def pick_test_model(models: list[ModelInfo], preferred: list[str]) -> ModelInfo | None:
    """The preferred model if listed, else the cheapest priced tool model, else the first tool model."""
    tools = [m for m in models if m.tools]
    by_id = {m.id: m for m in tools}
    for model_id in preferred:
        if model_id in by_id:
            return by_id[model_id]
    priced = [m for m in tools if m.price_in and m.price_out and ':free' not in m.id]
    if priced:
        return min(priced, key=lambda m: m.price_in + m.price_out)
    return tools[0] if tools else None


async def _tool_test_openai(preset, key: str | None, base_url: str | None, model_id: str) -> tuple[bool, str]:
    client = openai.AsyncOpenAI(base_url=base_url or preset.base_url, api_key=key or 'local',
                                default_headers=preset.headers, timeout=120)
    response = await client.chat.completions.create(
        model=model_id, messages=[{'role': 'user', 'content': PING_PROMPT}], max_tokens=300,
        tools=[{'type': 'function', 'function': {'name': PING['name'], 'description': PING['description'],
                                                 'parameters': PING['input_schema']}}])
    calls = response.choices[0].message.tool_calls if response.choices else None
    if calls and calls[0].function.name == 'ping':
        return True, 'It called a tool correctly.'
    return False, 'It answered but did not call the tool, so it may not be able to query the database.'


async def _tool_test_ollama(base_url: str, model_id: str) -> tuple[bool, str]:
    root = base_url.rstrip('/').removesuffix('/v1')
    body = {'model': model_id, 'stream': False, 'messages': [{'role': 'user', 'content': PING_PROMPT}],
            'tools': [{'type': 'function', 'function': {'name': PING['name'], 'description': PING['description'],
                                                        'parameters': PING['input_schema']}}]}
    async with httpx.AsyncClient(timeout=300) as client:
        response = await client.post(f'{root}/api/chat', json=body)
        response.raise_for_status()
        calls = response.json().get('message', {}).get('tool_calls') or []
    if calls and calls[0].get('function', {}).get('name') == 'ping':
        return True, 'It called a tool correctly.'
    return False, 'It answered but did not call the tool, so it may not be able to query the database.'


async def _tool_test_claude(client: anthropic.AsyncAnthropic, model_id: str) -> tuple[bool, str]:
    response = await client.messages.create(model=model_id, max_tokens=300, tools=[PING],
                                            messages=[{'role': 'user', 'content': PING_PROMPT}])
    if any(b.type == 'tool_use' and b.name == 'ping' for b in response.content):
        return True, 'It called a tool correctly.'
    return False, 'It answered but did not call the tool.'


async def check(name: str, key: str | None = None, base_url: str | None = None,
                model: str | None = None) -> dict:
    """Test a connection with the given key/address (or the stored ones). Never saves anything."""
    steps = []
    result = {'name': name, 'ok': False, 'listed': False, 'steps': steps}
    if name == 'claude':
        key = key or credentials.api_key('claude', ANTHROPIC['env'])[0]
        if not key and not ant_profile():
            steps.append({'name': 'Key', 'ok': False, 'detail': 'Paste an API key, or run `ant auth login`.'})
            return result
        client = anthropic_client(key)
        where, preferred = 'the Anthropic API', CLAUDE_TEST_MODELS
        lister = lambda: claude_models(client)
    else:
        preset = PRESETS[name]
        if preset.needs_key:
            key = key or credentials.api_key(name, preset.env)[0]
            if not key:
                steps.append({'name': 'Key', 'ok': False, 'detail': 'Paste a key first.'})
                return result
        base_url = (base_url or credentials.provider(name).get('base_url') or preset.base_url).rstrip('/')
        where, preferred = base_url, [preset.test_model] if preset.test_model else []
        lister = lambda: list_models(preset, key, base_url)
    try:
        models = await lister()
    except Exception as exc:
        steps.append({'name': 'Models', 'ok': False, 'detail': explain(exc, where)})
        return result
    tool_models = [m for m in models if m.tools]
    steps.append({'name': 'Models', 'ok': bool(models),
                  'detail': f'{len(models)} models, {len(tool_models)} of them can call tools.' if models
                  else 'Connected, but no chat models were found.'})
    result['listed'] = bool(models)
    if not tool_models:
        if models:
            steps.append({'name': 'Tool calling', 'ok': False,
                          'detail': 'None of these models can call tools, which nembot needs to query data.'})
        return result
    chosen = next((m for m in tool_models if m.id == model), None) if model else None
    chosen = chosen or pick_test_model(models, preferred)
    result['test_model'] = chosen.id
    started = time.monotonic()
    try:
        if name == 'claude':
            ok, detail = await _tool_test_claude(client, chosen.id)
        elif name == 'ollama':
            ok, detail = await _tool_test_ollama(base_url, chosen.id)
        else:
            ok, detail = await _tool_test_openai(preset, key, base_url, chosen.id)
    except Exception as exc:
        ok, detail = False, explain(exc, where)
    steps.append({'name': 'Tool calling', 'ok': ok,
                  'detail': f'Tested {chosen.name} ({time.monotonic() - started:.1f} s): {detail}'})
    result['ok'] = ok
    return result


def refusal(client_host: str, origin: str | None, own_origins: set[str], method: str,
            content_type: str) -> tuple[int, str] | None:
    """Why a settings request is refused, or None. Only this computer may change settings, and a
    page from another site must not be able to post here from the user's browser: a cross-site
    JSON post needs a CORS preflight, which this app never approves, and the Origin must be ours."""
    if client_host not in ('127.0.0.1', '::1', 'localhost') or (origin and origin not in own_origins):
        return 403, 'Settings can only be changed from this computer.'
    if method == 'POST' and not content_type.startswith('application/json'):
        return 415, 'expected JSON'
    return None


# ---- Sign in with OpenRouter (OAuth PKCE) ----
_pending: dict[str, tuple[str, float]] = {}  # state -> (code_verifier, started)


def openrouter_start(callback_url: str) -> str:
    """The OpenRouter page to send the user to; it redirects back to ``callback_url`` with a code."""
    now = time.monotonic()
    for state, (_, started) in list(_pending.items()):
        if now - started > 900:
            del _pending[state]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    state = secrets.token_urlsafe(16)
    _pending[state] = (verifier, now)
    return OPENROUTER_AUTH + '?' + urlencode({'callback_url': callback_url, 'code_challenge': challenge,
                                              'code_challenge_method': 'S256', 'state': state,
                                              'key_label': 'nembot'})


async def openrouter_finish(state: str, code: str) -> str:
    """Exchange the callback's code for an API key."""
    pending = _pending.pop(state, None)
    if pending is None:
        raise ValueError('This sign-in link has expired or was already used. Start again from Settings.')
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(OPENROUTER_KEYS, json={'code': code, 'code_verifier': pending[0],
                                                            'code_challenge_method': 'S256'})
        response.raise_for_status()
        key = response.json().get('key')
    if not key:
        raise ValueError('OpenRouter did not return a key.')
    return key
