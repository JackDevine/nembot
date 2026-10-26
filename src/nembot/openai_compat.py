"""OpenAI-compatible chat APIs (OpenRouter, OpenAI, Gemini, DeepSeek, Ollama, …; see presets.py).

Renders neutral history into chat-completions messages and streams one response.
OpenRouter extras (its web search tool, reported cost, reasoning_details replay)
apply only to the OpenRouter preset.
"""
import json
import logging
import re
import time
from typing import AsyncIterator
import uuid

import openai

from .chat import NO_VISION_PLOT_NOTE, TOOLS, VISION_PLOT_NOTE, earlier_reply_label, system_prompt
from .models import ModelInfo
from .presets import Preset

log = logging.getLogger(__name__)

def functions(tools: list[dict]) -> list[dict]:
    return [{'type': 'function', 'function': {'name': t['name'], 'description': t['description'],
                                              'parameters': t['input_schema']}} for t in tools]


def _call_id(value: str | None) -> str:
    # Shared history must also satisfy Anthropic's tool id pattern.
    cleaned = re.sub(r'[^A-Za-z0-9_-]', '_', value or '')
    return cleaned or f'call_{uuid.uuid4().hex[:24]}'


WEB_SEARCH = {'type': 'openrouter:web_search', 'parameters': {'max_results': 5}}


def render(history: list, model: ModelInfo, web_search: bool = False, system: str | None = None) -> list[dict]:
    """Neutral history -> OpenAI chat messages for ``model``. Deterministic per model."""
    messages = [{'role': 'system', 'content': system or system_prompt(model.vision, web_search)}]
    for entry in history:
        if entry['role'] == 'user':
            text = entry['text'] + (f"\n\n{entry['context']}" if entry.get('context') else '')
            messages.append({'role': 'user', 'content': text})
        elif entry['role'] == 'assistant':
            text = '\n\n'.join(part for part in (earlier_reply_label(entry, model), entry['text']) if part)
            message = {'role': 'assistant', 'content': text or None}
            if entry['tool_calls']:
                message['tool_calls'] = [{
                    'id': c['id'], 'type': 'function',
                    'function': {'name': c['name'], 'arguments': c['input'] if isinstance(c['input'], str) else json.dumps(c['input'])},
                } for c in entry['tool_calls']]
            reasoning = entry['raw'].get(model.provider, {}).get('reasoning_details')
            if reasoning and not earlier_reply_label(entry, model):
                message['reasoning_details'] = reasoning  # passed back unmodified, same model only
            if message['content'] is None and 'tool_calls' not in message:
                message['content'] = '(no reply)'
            messages.append(message)
        else:
            images = []
            for r in entry['results']:
                text = r['text']
                if r['image'] and not model.vision:
                    text += f'\n\n{NO_VISION_PLOT_NOTE}'
                elif r['image']:
                    images.append(r)
                messages.append({'role': 'tool', 'tool_call_id': r['id'], 'content': text})
            # Tool messages are text-only, so plots follow as a user message.
            if images:
                content = []
                for r in images:
                    content += [{'type': 'text', 'text': f"Plot from tool call {r['id']}. {VISION_PLOT_NOTE}"},
                                {'type': 'image_url', 'image_url': {'url': f"data:image/png;base64,{r['image']}"}}]
                messages.append({'role': 'user', 'content': content})
    return messages


def _merge_reasoning(merged: list[dict], pieces) -> None:
    """Concatenate streamed reasoning_details fragments that share an index."""
    for piece in pieces or []:
        piece = piece if isinstance(piece, dict) else piece.model_dump()
        index = piece.get('index')
        target = next((m for m in merged if index is not None and m.get('index') == index), None)
        if target is None:
            merged.append(dict(piece))
            continue
        for key, value in piece.items():
            if key in ('text', 'summary', 'data') and isinstance(value, str) and isinstance(target.get(key), str):
                target[key] += value
            elif value is not None:
                target[key] = value


def usage_event(model: ModelInfo, usage, started: float | None, finished: float, free: bool = False) -> dict:
    """Tokens, cost and generation time of one request, for the chat's stats line.

    OpenRouter reports the request's cost; if it doesn't, estimate it from the catalogue's prices.
    Models on this computer (``free``) cost nothing; other services without prices are unpriced.
    """
    details = getattr(usage, 'prompt_tokens_details', None)
    cached = (getattr(details, 'cached_tokens', 0) or 0) if details else 0
    prompt, output = usage.prompt_tokens or 0, usage.completion_tokens or 0
    server = _extra(usage, 'server_tool_use')
    searches = server.get('web_search_requests', 0) if isinstance(server, dict) else 0
    cost = _extra(usage, 'cost')
    if free:
        cost = 0
    elif cost is None and model.price_in is not None and model.price_out is not None:
        cost = (prompt * model.price_in + output * model.price_out) / 1_000_000
    return {'type': 'usage', 'model': model.id, 'input': prompt - cached, 'cache_write': 0, 'cache_read': cached,
            'output': output, 'searches': searches or 0, 'cost': float(cost) if cost is not None else None,
            'seconds': round(finished - started, 3) if started else None}


def _extra(obj, name):
    value = getattr(obj, name, None)
    if value is None and getattr(obj, 'model_extra', None):
        value = obj.model_extra.get(name)
    return value


class OpenAICompatProvider:
    """One preset's provider. ``connection()`` returns its current (key, base_url), so keys saved
    during onboarding take effect without a restart."""

    def __init__(self, preset: Preset, connection):
        self.preset = preset
        self.web_search = preset.web_search  # run_turn turns web search off for services without it
        self._connection = connection
        self._client, self._made_with = None, None

    @property
    def client(self) -> openai.AsyncOpenAI:
        key, base_url = self._connection()
        if self.preset.needs_key and not key:
            raise RuntimeError(f'{self.preset.label} is not connected. Add it on the Settings page.')
        if self._client is None or self._made_with != (key, base_url):
            self._client = openai.AsyncOpenAI(base_url=base_url or self.preset.base_url,
                                              api_key=key or 'local', default_headers=self.preset.headers)
            self._made_with = (key, base_url)
        return self._client

    async def complete(self, model: ModelInfo, prompt: str) -> str:
        """A short one-off answer (e.g. a snippet name)."""
        response = await self.client.chat.completions.create(
            model=model.id, messages=[{'role': 'user', 'content': prompt}], max_tokens=1500)
        return (response.choices[0].message.content or '') if response.choices else ''

    async def respond(self, model: ModelInfo, history: list, web_search: bool = False, *,
                      system: str | None = None, tools: list[dict] | None = None,
                      effort: str | None = None) -> AsyncIterator[dict]:
        tools = functions(TOOLS if tools is None else tools)
        web_search = web_search and self.preset.web_search
        stream = await self.client.chat.completions.create(
            model=model.id,
            messages=render(history, model, web_search, system),
            tools=tools + [WEB_SEARCH] if web_search else tools,
            stream=True,
            stream_options={'include_usage': True},
            # OpenRouter's catalogue lists each model's levels; without one a model uses its own default
            # (Kimi K3.1's is max).
            **({'extra_body': {'reasoning': {'effort': effort}}} if effort and self.preset.name == 'openrouter' else {}),
        )
        text, calls, reasoning, sources = [], {}, [], {}
        finish, usage, in_text = None, None, False
        started = None
        async for chunk in stream:
            error = _extra(chunk, 'error')
            if error:
                message = error.get('message') if isinstance(error, dict) else str(error)
                raise RuntimeError(f'{self.preset.label} error: {message}')
            if chunk.usage:
                usage = chunk.usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if started is None and (delta.content or delta.tool_calls or _extra(delta, 'reasoning') or _extra(delta, 'reasoning_details')):
                started = time.monotonic()  # first generated token
            _merge_reasoning(reasoning, _extra(delta, 'reasoning_details'))
            thought = _extra(delta, 'reasoning') or _extra(delta, 'reasoning_content')  # OpenRouter/Ollama, DeepSeek
            if isinstance(thought, str) and thought:
                yield {'type': 'progress', 'delta': thought}
            for annotation in _extra(delta, 'annotations') or []:
                citation = annotation.get('url_citation') if isinstance(annotation, dict) else None
                if citation and citation.get('url') and citation['url'] not in sources:
                    sources[citation['url']] = {'url': citation['url'], 'title': citation.get('title') or citation['url']}
            if delta.content:
                if not in_text:
                    yield {'type': 'block', 'kind': 'text'}
                    in_text = True
                text.append(delta.content)
                yield {'type': 'text', 'delta': delta.content}
            for part in delta.tool_calls or []:
                in_text = False
                call = calls.setdefault(part.index, {'id': None, 'name': '', 'arguments': ''})
                if part.id:
                    call['id'] = part.id
                if part.function and part.function.name:
                    if not call['name']:
                        yield {'type': 'tool', 'name': part.function.name}
                    call['name'] += part.function.name
                if part.function and part.function.arguments:
                    call['arguments'] += part.function.arguments
            if choice.finish_reason:
                finish = choice.finish_reason

        finished = time.monotonic()
        if usage:
            log.info('%s %s: in=%s out=%s cost=%s searches=%s finish=%s', self.preset.name, model.id,
                     usage.prompt_tokens, usage.completion_tokens, _extra(usage, 'cost'),
                     _extra(usage, 'server_tool_use'), finish)
            yield usage_event(model, usage, started, finished, free=self.preset.local)
        if sources:
            yield {'type': 'sources', 'kind': 'cited', 'sources': list(sources.values())}
        if finish == 'content_filter':
            yield {'type': 'error', 'message': 'The model declined this request.'}
            return
        tool_calls = []
        for call in (calls[i] for i in sorted(calls)):
            try:
                args = json.loads(call['arguments'] or '{}')
            except json.JSONDecodeError:
                args = call['arguments']  # kept raw; execute_tool reports INVALID_JSON
            tool_calls.append({'id': _call_id(call['id']), 'name': call['name'], 'input': args})
        if tool_calls and finish == 'length':
            raise RuntimeError('Tool input was truncated by the output token limit.')
        yield {
            'role': 'assistant', 'model': model.ref, 'model_name': model.name, 'text': ''.join(text), 'tool_calls': tool_calls,
            'raw': {self.preset.name: {'reasoning_details': reasoning}} if reasoning else {},
            'stop': 'tool_use' if tool_calls else 'end',
        }
