"""Ollama through its own /api/chat endpoint.

Ollama's OpenAI-compatible endpoint ignores the context size and runs models at
the server default (often 2,048 tokens), silently cutting nembot's ~9k-token
prompt so the model never sees its tools. The native endpoint takes
``options.num_ctx``, so this adapter asks for ``NUM_CTX`` tokens (or the
model's maximum if smaller; ``num_ctx`` in the [providers.ollama] config
overrides it). Messages are rendered by openai_compat.render and converted.
"""
import json
import logging
import time
from typing import AsyncIterator

import httpx2 as httpx

from . import credentials
from .chat import TOOLS
from .models import ModelInfo
from .openai_compat import _call_id, functions, render
from .presets import PRESETS

log = logging.getLogger(__name__)

NUM_CTX = 16384


def native_messages(messages: list[dict]) -> list[dict]:
    """OpenAI chat messages -> Ollama's: arguments as objects, tool names on results, images as base64 lists."""
    names, out = {}, []
    for message in messages:
        role = message['role']
        if role == 'assistant':
            converted = {'role': 'assistant', 'content': message.get('content') or ''}
            calls = []
            for call in message.get('tool_calls') or []:
                names[call['id']] = call['function']['name']
                arguments = call['function']['arguments']
                try:
                    arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
                except json.JSONDecodeError:
                    arguments = {}
                calls.append({'function': {'name': call['function']['name'], 'arguments': arguments}})
            if calls:
                converted['tool_calls'] = calls
            out.append(converted)
        elif role == 'tool':
            out.append({'role': 'tool', 'content': message['content'], 'tool_name': names.get(message['tool_call_id'], '')})
        elif isinstance(message.get('content'), list):  # text and plot images
            parts = message['content']
            out.append({'role': role, 'content': '\n\n'.join(p['text'] for p in parts if p['type'] == 'text'),
                        'images': [p['image_url']['url'].split(',', 1)[1] for p in parts if p['type'] == 'image_url']})
        else:
            out.append({'role': role, 'content': message['content']})
    return out


class OllamaProvider:
    web_search = False

    def __init__(self):
        self.preset = PRESETS['ollama']

    def _root(self) -> str:
        base = credentials.provider('ollama').get('base_url') or self.preset.base_url
        return base.rstrip('/').removesuffix('/v1')

    @staticmethod
    def num_ctx(model: ModelInfo) -> int:
        """NUM_CTX (or the configured num_ctx), capped at the model's maximum context."""
        wanted = int(credentials.provider('ollama').get('num_ctx') or NUM_CTX)
        return min(wanted, model.context) if model.context else wanted

    def _chat(self, model: ModelInfo, messages: list[dict], tools: list | None, stream: bool):
        client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10))
        body = {'model': model.id, 'messages': native_messages(messages), 'stream': stream,
                'options': {'num_ctx': self.num_ctx(model)}}
        if tools:
            body['tools'] = tools
        return client, body

    async def complete(self, model: ModelInfo, prompt: str) -> str:
        client, body = self._chat(model, [{'role': 'user', 'content': prompt}], None, False)
        async with client:
            response = await client.post(f'{self._root()}/api/chat', json=body)
            response.raise_for_status()
            return response.json().get('message', {}).get('content', '')

    async def respond(self, model: ModelInfo, history: list, web_search: bool = False, *,
                      system: str | None = None, tools: list[dict] | None = None) -> AsyncIterator[dict]:
        client, body = self._chat(model, render(history, model, False, system),
                                        functions(TOOLS if tools is None else tools), True)
        text, calls, in_text, started, final = [], [], False, None, {}
        async with client, client.stream('POST', f'{self._root()}/api/chat', json=body) as response:
            if response.status_code >= 400:
                detail = (await response.aread()).decode(errors='replace')
                raise RuntimeError(f'Ollama error {response.status_code}: {detail[:300]}')
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if chunk.get('error'):
                    raise RuntimeError(f"Ollama error: {chunk['error']}")
                message = chunk.get('message') or {}
                if started is None and (message.get('content') or message.get('thinking') or message.get('tool_calls')):
                    started = time.monotonic()
                if message.get('thinking'):
                    yield {'type': 'progress', 'delta': message['thinking']}
                if message.get('content'):
                    if not in_text:
                        yield {'type': 'block', 'kind': 'text'}
                        in_text = True
                    text.append(message['content'])
                    yield {'type': 'text', 'delta': message['content']}
                for call in message.get('tool_calls') or []:
                    function = call.get('function') or {}
                    yield {'type': 'tool', 'name': function.get('name', '')}
                    in_text = False
                    calls.append({'id': _call_id(call.get('id')), 'name': function.get('name', ''),
                                  'input': function.get('arguments') or {}})
                if chunk.get('done'):
                    final = chunk
        finished = time.monotonic()
        prompt_tokens, output = final.get('prompt_eval_count') or 0, final.get('eval_count') or 0
        log.info('ollama %s: in=%s out=%s num_ctx=%s', model.id, prompt_tokens, output, body['options']['num_ctx'])
        yield {'type': 'usage', 'model': model.id, 'input': prompt_tokens, 'cache_write': 0, 'cache_read': 0,
               'output': output, 'searches': 0, 'cost': 0, 'seconds': round(finished - started, 3) if started else None}
        if prompt_tokens >= body['options']['num_ctx'] - 8:
            yield {'type': 'notice', 'message': f"The conversation filled this model's {body['options']['num_ctx']:,}-token "
                                                'context, so Ollama dropped its start. Start a new chat, or raise num_ctx '
                                                'in the [providers.ollama] config if your computer has the memory.'}
        yield {'role': 'assistant', 'model': model.ref, 'model_name': model.name, 'text': ''.join(text),
               'tool_calls': calls, 'raw': {}, 'stop': 'tool_use' if calls else 'end'}
