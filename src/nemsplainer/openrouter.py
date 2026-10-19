"""OpenRouter via its OpenAI-compatible API: renders neutral history and streams one response."""
import json
import logging
import os
import re
from typing import AsyncIterator
import uuid

import openai

from .chat import NO_VISION_PLOT_NOTE, TOOLS, VISION_PLOT_NOTE, earlier_reply_label, system_prompt
from .models import ModelInfo

log = logging.getLogger(__name__)

BASE_URL = 'https://openrouter.ai/api/v1'
FUNCTIONS = [{'type': 'function', 'function': {'name': t['name'], 'description': t['description'],
                                               'parameters': t['input_schema']}} for t in TOOLS]


def _call_id(value: str | None) -> str:
    # Shared history must also satisfy Anthropic's tool id pattern.
    cleaned = re.sub(r'[^A-Za-z0-9_-]', '_', value or '')
    return cleaned or f'call_{uuid.uuid4().hex[:24]}'


def render(history: list, model: ModelInfo) -> list[dict]:
    """Neutral history -> OpenAI chat messages for ``model``. Deterministic per model."""
    messages = [{'role': 'system', 'content': system_prompt(model.vision)}]
    for entry in history:
        if entry['role'] == 'user':
            messages.append({'role': 'user', 'content': entry['text']})
        elif entry['role'] == 'assistant':
            text = '\n\n'.join(part for part in (earlier_reply_label(entry, model), entry['text']) if part)
            message = {'role': 'assistant', 'content': text or None}
            if entry['tool_calls']:
                message['tool_calls'] = [{
                    'id': c['id'], 'type': 'function',
                    'function': {'name': c['name'], 'arguments': c['input'] if isinstance(c['input'], str) else json.dumps(c['input'])},
                } for c in entry['tool_calls']]
            reasoning = entry['raw'].get('openrouter', {}).get('reasoning_details')
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


def _extra(obj, name):
    value = getattr(obj, name, None)
    if value is None and getattr(obj, 'model_extra', None):
        value = obj.model_extra.get(name)
    return value


class OpenRouterProvider:
    def __init__(self):
        self._client = None

    @property
    def client(self) -> openai.AsyncOpenAI:
        if self._client is None:
            key = os.environ.get('OPENROUTER_API_KEY')
            if not key:
                raise RuntimeError('OPENROUTER_API_KEY is not set.')
            self._client = openai.AsyncOpenAI(base_url=BASE_URL, api_key=key,
                                              default_headers={'X-Title': 'nemsplainer'})
        return self._client

    async def respond(self, model: ModelInfo, history: list) -> AsyncIterator[dict]:
        stream = await self.client.chat.completions.create(
            model=model.id,
            messages=render(history, model),
            tools=FUNCTIONS,
            stream=True,
            stream_options={'include_usage': True},
        )
        text, calls, reasoning = [], {}, []
        finish, usage, in_text = None, None, False
        async for chunk in stream:
            error = _extra(chunk, 'error')
            if error:
                message = error.get('message') if isinstance(error, dict) else str(error)
                raise RuntimeError(f'OpenRouter error: {message}')
            if chunk.usage:
                usage = chunk.usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            _merge_reasoning(reasoning, _extra(delta, 'reasoning_details'))
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

        if usage:
            log.info('openrouter %s: in=%s out=%s cost=%s finish=%s', model.id, usage.prompt_tokens,
                     usage.completion_tokens, _extra(usage, 'cost'), finish)
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
            'raw': {'openrouter': {'reasoning_details': reasoning}} if reasoning else {},
            'stop': 'tool_use' if tool_calls else 'end',
        }
