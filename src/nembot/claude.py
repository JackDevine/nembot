"""Claude via the Anthropic API: renders neutral history and streams one response."""
import logging
import time
from typing import AsyncIterator

import anthropic

from .chat import TOOLS, VISION_PLOT_NOTE, earlier_reply_label, system_prompt, text_from_blocks
from .models import ModelInfo
from .pricing import claude_cost

log = logging.getLogger(__name__)

# Per-model request features. Models not listed get a plain request (no
# thinking/effort/fallback parameters), which every Claude model accepts.
_FALLBACKS = {'claude-opus-5-5', 'claude-opus-5', 'claude-fable-5-1', 'claude-sonnet-5-5'}
_THINKING_UPDATES = {'claude-opus-5-5', 'claude-fable-5-1', 'claude-fable-5', 'claude-sonnet-5-5'}
_ADAPTIVE = _THINKING_UPDATES | {'claude-opus-5', 'claude-opus-4-8', 'claude-opus-4-7', 'claude-opus-4-6',
                                 'claude-sonnet-5', 'claude-sonnet-4-6', 'claude-haiku-5-5'}


# Web tools with dynamic filtering; other models get the basic versions.
_DYNAMIC_WEB = {'claude-opus-5-5', 'claude-opus-5', 'claude-fable-5', 'claude-fable-5-1', 'claude-opus-4-8',
                'claude-opus-4-7', 'claude-opus-4-6', 'claude-sonnet-5-5', 'claude-sonnet-5', 'claude-sonnet-4-6'}


def web_tools(model_id: str) -> list[dict]:
    if model_id in _DYNAMIC_WEB:
        return [{'type': 'web_search_20260209', 'name': 'web_search'},
                {'type': 'web_fetch_20260209', 'name': 'web_fetch'}]
    return [{'type': 'web_search_20250305', 'name': 'web_search'},
            {'type': 'web_fetch_20250910', 'name': 'web_fetch'}]


def sources(content: list) -> tuple[str, list[dict]]:
    """Web sources for the UI: cited ones if any, else every page searched or fetched.

    With dynamic filtering Claude often reads results through code and cites
    nothing, so the consulted pages are the useful fallback.
    """
    cited, consulted = {}, {}
    for block in content:
        if block.type == 'text':
            for citation in getattr(block, 'citations', None) or []:
                url = getattr(citation, 'url', None)
                if url:
                    cited.setdefault(url, {'url': url, 'title': getattr(citation, 'title', None) or url})
        elif block.type == 'web_search_tool_result' and isinstance(block.content, list):
            for result in block.content:
                url = getattr(result, 'url', None)
                if url:
                    consulted.setdefault(url, {'url': url, 'title': getattr(result, 'title', None) or url})
        elif block.type == 'web_fetch_tool_result':
            url = getattr(block.content, 'url', None)
            if url:
                consulted.setdefault(url, {'url': url, 'title': url})
    return ('cited', list(cited.values())) if cited else ('consulted', list(consulted.values()))


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
            if entry.get('context'):
                messages.append({'role': 'user', 'content': [{'type': 'text', 'text': entry['text']},
                                                             {'type': 'text', 'text': entry['context']}]})
            else:
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


def usage_event(response, started: float | None, finished: float) -> dict:
    """Tokens, cost and generation time of one request, for the chat's stats line."""
    usage = response.usage
    server = getattr(usage, 'server_tool_use', None)
    tokens = {'input': usage.input_tokens or 0, 'cache_write': usage.cache_creation_input_tokens or 0,
              'cache_read': usage.cache_read_input_tokens or 0, 'output': usage.output_tokens or 0,
              'searches': (getattr(server, 'web_search_requests', 0) or 0) if server else 0}
    # Priced by the model that served the request (a fallback may have taken over).
    return {'type': 'usage', 'model': response.model, **tokens, 'cost': claude_cost(response.model, **tokens),
            'seconds': round(finished - started, 3) if started else None}


class ClaudeProvider:
    def __init__(self, client: anthropic.AsyncAnthropic):
        self.client = client

    async def complete(self, model: ModelInfo, prompt: str) -> str:
        """A short one-off answer (e.g. a snippet name), at low effort."""
        options = {'output_config': {'effort': 'low'}} if model.id in _ADAPTIVE else {}
        response = await self.client.messages.create(
            model=model.id, max_tokens=4000, messages=[{'role': 'user', 'content': prompt}], **options)
        if response.stop_reason == 'refusal':
            return ''
        return ''.join(b.text for b in response.content if b.type == 'text')

    async def respond(self, model: ModelInfo, history: list, web_search: bool = False, *,
                      system: str | None = None, tools: list[dict] | None = None) -> AsyncIterator[dict]:
        tools = [{**tool, 'eager_input_streaming': True} for tool in (TOOLS if tools is None else tools)]
        if web_search:
            tools += web_tools(model.id)
        json_retries = 0
        while True:
            try:
                async with self.client.beta.messages.stream(
                    model=model.id,
                    max_tokens=64000,
                    system=system or system_prompt(vision=True, web_search=web_search),
                    tools=tools,
                    messages=render(history, model),
                    cache_control={'type': 'ephemeral'},
                    **request_options(model.id),
                ) as stream:
                    previous, started = None, None
                    async for event in stream:
                        if event.type == 'content_block_start' and started is None:
                            started = time.monotonic()  # generation begins (thinking included)
                        if event.type == 'content_block_start':
                            kind = event.content_block.type
                            if kind in ('tool_use', 'server_tool_use'):
                                yield {'type': 'tool', 'name': event.content_block.name}
                            elif kind in ('text', 'thinking') and kind != previous:
                                # Citations split text into many blocks; keep them in one segment.
                                yield {'type': 'block', 'kind': kind}
                            previous = kind
                        elif event.type == 'content_block_delta':
                            if event.delta.type == 'text_delta':
                                yield {'type': 'text', 'delta': event.delta.text}
                            elif event.delta.type == 'thinking_delta':
                                yield {'type': 'progress', 'delta': event.delta.thinking}
                    finished = time.monotonic()
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
        yield usage_event(response, started, finished)
        if response.stop_reason == 'refusal':
            yield {'type': 'error', 'message': 'The model declined this request.'}
            return
        content = _echoable(response.content)
        calls = [{'id': b.id, 'name': b.name, 'input': b.input} for b in content if b.type == 'tool_use']
        if calls and response.stop_reason == 'max_tokens':
            raise RuntimeError('Tool input was truncated by max_tokens.')
        kind, found = sources(content)
        if found:
            yield {'type': 'sources', 'kind': kind, 'sources': found}
        yield {
            'role': 'assistant', 'model': model.ref, 'model_name': model.name,
            'text': text_from_blocks([(b.type, getattr(b, 'text', '')) for b in content]),
            'tool_calls': calls,
            'raw': {'claude': [b.to_dict() for b in content]},
            'stop': 'pause' if response.stop_reason == 'pause_turn' else 'tool_use' if calls else 'end',
        }
