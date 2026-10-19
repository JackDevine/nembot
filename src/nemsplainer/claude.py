"""Claude via the Anthropic API: renders neutral history and streams one response."""
import logging
from typing import AsyncIterator

import anthropic

from .chat import TOOLS, VISION_PLOT_NOTE, earlier_reply_label, system_prompt
from .models import ModelInfo

log = logging.getLogger(__name__)

# Per-model request features. Models not listed get a plain request (no
# thinking/effort/fallback parameters), which every Claude model accepts.
_FALLBACKS = {'claude-opus-5-5', 'claude-opus-5', 'claude-fable-5-1', 'claude-sonnet-5-5'}
_THINKING_UPDATES = {'claude-opus-5-5', 'claude-fable-5-1', 'claude-fable-5', 'claude-sonnet-5-5'}
_ADAPTIVE = _THINKING_UPDATES | {'claude-opus-5', 'claude-opus-4-8', 'claude-opus-4-7', 'claude-opus-4-6',
                                 'claude-sonnet-5', 'claude-sonnet-4-6', 'claude-haiku-5-5'}


def request_options(model_id: str) -> dict:
    options, betas = {}, []
    if model_id in _ADAPTIVE:
        options['output_config'] = {'effort': 'medium'}
        if model_id in _THINKING_UPDATES:
            # Progress notes between tool calls arrive as thinking blocks.
            options['thinking'] = {'type': 'adaptive', 'display': 'updates'}
            betas.append('thinking-display-updates-2026-08-18')
        else:
            options['thinking'] = {'type': 'adaptive'}
    if model_id in _FALLBACKS:
        options['fallbacks'] = 'default'
        betas.append('server-side-fallback-2026-07-01')
    if betas:
        options['betas'] = betas
    return options


def render(history: list, model: ModelInfo) -> list[dict]:
    """Neutral history -> Anthropic messages. Deterministic, so turns replay byte-identically."""
    messages = []
    for entry in history:
        if entry['role'] == 'user':
            messages.append({'role': 'user', 'content': entry['text']})
        elif entry['role'] == 'assistant':
            label = earlier_reply_label(entry, model)
            raw = entry['raw'].get('claude')
            if raw is not None and not label:
                content = raw  # same model: replay exactly, thinking blocks included
            else:
                text = '\n\n'.join(part for part in (label, entry['text']) if part)
                content = ([{'type': 'text', 'text': text}] if text else []) + [
                    {'type': 'tool_use', 'id': c['id'], 'name': c['name'], 'input': c['input'] if isinstance(c['input'], dict) else {}}
                    for c in entry['tool_calls']
                ]
            messages.append({'role': 'assistant', 'content': content or [{'type': 'text', 'text': '(no reply)'}]})
        else:
            blocks = []
            for r in entry['results']:
                content = [{'type': 'text', 'text': r['text']}]
                if r['image']:
                    content += [{'type': 'text', 'text': VISION_PLOT_NOTE},
                                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': r['image']}}]
                block = {'type': 'tool_result', 'tool_use_id': r['id'], 'content': content}
                if r['is_error']:
                    block['is_error'] = True
                blocks.append(block)
            messages.append({'role': 'user', 'content': blocks})
    return messages


def _echoable(content: list) -> list:
    """Drop pre-fallback thinking/tool_use blocks, which must not be echoed back."""
    boundary = max((i for i, b in enumerate(content) if b.type == 'fallback'), default=-1)
    dropped = {'thinking', 'redacted_thinking', 'tool_use'}
    return [b for i, b in enumerate(content) if i > boundary or b.type not in dropped]


class ClaudeProvider:
    def __init__(self, client: anthropic.AsyncAnthropic):
        self.client = client

    async def respond(self, model: ModelInfo, history: list) -> AsyncIterator[dict]:
        tools = [{**tool, 'eager_input_streaming': True} for tool in TOOLS]
        json_retries = 0
        while True:
            try:
                async with self.client.beta.messages.stream(
                    model=model.id,
                    max_tokens=64000,
                    system=system_prompt(vision=True),
                    tools=tools,
                    messages=render(history, model),
                    cache_control={'type': 'ephemeral'},
                    **request_options(model.id),
                ) as stream:
                    async for event in stream:
                        if event.type == 'content_block_start':
                            kind = event.content_block.type
                            if kind == 'tool_use':
                                yield {'type': 'tool', 'name': event.content_block.name}
                            elif kind in ('text', 'thinking'):
                                yield {'type': 'block', 'kind': kind}
                        elif event.type == 'content_block_delta':
                            if event.delta.type == 'text_delta':
                                yield {'type': 'text', 'delta': event.delta.text}
                            elif event.delta.type == 'thinking_delta':
                                yield {'type': 'progress', 'delta': event.delta.thinking}
                    response = await stream.get_final_message()
                break
            except ValueError:
                # Tool input JSON the SDK could not parse at all; re-issue the request.
                json_retries += 1
                if json_retries > 2:
                    raise
                yield {'type': 'notice', 'message': 'Retrying after malformed tool input…'}

        usage = response.usage
        log.info('claude %s: in=%s cache_read=%s cache_write=%s out=%s stop=%s', response.model, usage.input_tokens,
                 usage.cache_read_input_tokens, usage.cache_creation_input_tokens, usage.output_tokens, response.stop_reason)
        if response.stop_reason == 'refusal':
            yield {'type': 'error', 'message': 'The model declined this request.'}
            return
        content = _echoable(response.content)
        calls = [{'id': b.id, 'name': b.name, 'input': b.input} for b in content if b.type == 'tool_use']
        if calls and response.stop_reason == 'max_tokens':
            raise RuntimeError('Tool input was truncated by max_tokens.')
        yield {
            'role': 'assistant', 'model': model.ref, 'model_name': model.name,
            'text': '\n\n'.join(b.text for b in content if b.type == 'text'),
            'tool_calls': calls,
            'raw': {'claude': [b.to_dict() for b in content]},
            'stop': 'pause' if response.stop_reason == 'pause_turn' else 'tool_use' if calls else 'end',
        }
