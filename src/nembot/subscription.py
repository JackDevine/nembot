"""Claude through the user's Claude plan (Pro/Max/Team), via the Claude Agent SDK.

Anthropic allows a Claude subscription to be used through the Agent SDK, which
runs the Claude Code CLI, but not by calling the API with its login. So this
provider hands nembot's tools to the SDK as in-process MCP tools and keeps
nembot's own turn loop (``chat.run_turn``) in charge:

- ``respond`` starts one SDK session per user turn and streams it until the model
  calls nembot tools. It yields those calls as a normal assistant turn and returns,
  leaving the session waiting inside the tool handlers.
- ``run_turn`` runs the tools as for any provider and calls ``respond`` again; the
  results in history are handed to the waiting handlers and the session continues.
- ``close_turn`` ends a session that ``run_turn`` abandoned (stop, error, round limit).

Claude Code's own tools are off (only WebSearch/WebFetch, with web search on), it
loads no user settings, and sessions aren't saved, so these chats don't appear in
Claude Code's history. Each session starts fresh: earlier turns of the chat are
sent as a transcript, since the SDK can't replay other providers' turns.
"""
import asyncio
from dataclasses import dataclass, field
import json
import logging
import tempfile
import time
from typing import AsyncIterator

import claude_agent_sdk as sdk

from .chat import TOOLS, VISION_PLOT_NOTE, earlier_reply_label, system_prompt, text_from_blocks
from .models import ModelInfo

log = logging.getLogger(__name__)

SERVER = 'nembot'
PREFIX = f'mcp__{SERVER}__'
WEB_TOOLS = ['WebSearch', 'WebFetch']
_UI_NAMES = {'WebSearch': 'web_search', 'WebFetch': 'web_fetch'}  # as the Claude API provider shows them
MAX_RESULT_CHARS = 200_000  # keep tool results inline (Claude Code otherwise saves big ones to a file)
TOOL_TIMEOUT_MS = str(24 * 3600 * 1000)  # request_download waits for the user's approval and the download


class PlanError(RuntimeError):
    """A problem with the Claude plan session, worded for the chat."""


def _options(model_id: str, system: str, tools: list, web_search: bool, workdir: str, **extra) -> sdk.ClaudeAgentOptions:
    return sdk.ClaudeAgentOptions(
        model=model_id,
        system_prompt=system,
        tools=WEB_TOOLS if web_search else [],
        allowed_tools=[PREFIX + t.name for t in tools] + (WEB_TOOLS if web_search else []),
        mcp_servers={SERVER: sdk.create_sdk_mcp_server(SERVER, tools=tools)} if tools else {},
        strict_mcp_config=True,
        setting_sources=[],
        cwd=workdir,
        include_partial_messages=True,
        extra_args={'no-session-persistence': None},
        env={'MCP_TOOL_TIMEOUT': TOOL_TIMEOUT_MS},
        **extra,
    )


async def list_models() -> tuple[list[ModelInfo], str | None]:
    """The models the signed-in Claude plan offers, from Claude Code itself."""
    with tempfile.TemporaryDirectory() as workdir:
        async with sdk.ClaudeSDKClient(sdk.ClaudeAgentOptions(tools=[], setting_sources=[], cwd=workdir)) as client:
            info = await client.get_server_info() or {}
    account = info.get('account') or {}
    plan = account.get('subscriptionType')
    if not plan:
        return [], 'Claude plan models unavailable: Claude Code is not signed in to a Claude subscription.'
    models, seen = [], set()
    for m in info.get('models') or []:
        model_id = m.get('resolvedModel') or ''
        if m.get('value') == 'default' or not model_id.startswith('claude-') or model_id in seen:
            continue
        seen.add(model_id)
        name = m.get('displayName') or model_id
        models.append(ModelInfo(f'subscription:{model_id}', 'subscription', model_id,
                                name if name.startswith('Claude') else f'Claude {name}', True, True, True))
    return models, None


def render_prompt(history: list, model: ModelInfo) -> str:
    """The user's latest message, preceded by the chat so far as a transcript."""
    *earlier, latest = history
    message = latest['text'] + (f"\n\n{latest['context']}" if latest.get('context') else '')
    if not earlier:
        return message
    lines = []
    for entry in earlier:
        if entry['role'] == 'user':
            lines.append(f"[User]\n{entry['text']}")
        elif entry['role'] == 'assistant':
            label = earlier_reply_label(entry, model) or '[Your earlier reply]'
            lines.append('\n'.join(part for part in (label, entry['text']) if part))
            for call in entry['tool_calls']:
                lines.append(f"[Tool call {call['name']}: {json.dumps(call['input'], ensure_ascii=False)}]")
        else:
            for r in entry['results']:
                note = ' (error)' if r['is_error'] else ''
                image = '\n(The plot image is not repeated here.)' if r['image'] else ''
                lines.append(f"[Tool result {r['name']}{note}]\n{r['text']}{image}")
    return ('<earlier_conversation>\nThe conversation so far, for context. Tool results here are from '
            'earlier turns; call the tools again if you need fresh data.\n\n' + '\n\n'.join(lines)
            + f'\n</earlier_conversation>\n\n{message}')


def tool_result(result: dict) -> dict:
    content = [{'type': 'text', 'text': result['text']}]
    if result['image']:
        content += [{'type': 'text', 'text': VISION_PLOT_NOTE},
                    {'type': 'image', 'data': result['image'], 'mimeType': 'image/png'}]
    return {'content': content, 'is_error': bool(result['is_error'])}


@dataclass
class Session:
    """One user turn's SDK session, alive while nembot runs the tools it asked for."""
    client: object
    messages: AsyncIterator
    workdir: tempfile.TemporaryDirectory
    waiting: list = field(default_factory=list)   # (name, input, future): handlers awaiting a result
    ready: list = field(default_factory=list)     # (name, input, result): results no handler has taken yet
    asked: list = field(default_factory=list)     # the tool calls of the turn handed to run_turn

    def deliver(self, name: str, args: dict, result: dict) -> None:
        for i, (wname, wargs, future) in enumerate(self.waiting):
            if wname == name and wargs == args:
                del self.waiting[i]
                if not future.done():
                    future.set_result(result)
                return
        self.ready.append((name, args, result))

    async def receive(self, name: str, args: dict) -> dict:
        for i, (rname, rargs, result) in enumerate(self.ready):
            if rname == name and rargs == args:
                del self.ready[i]
                return result
        future = asyncio.get_running_loop().create_future()
        self.waiting.append((name, args, future))
        return await future

    async def close(self) -> None:
        for _, _, future in self.waiting:
            if not future.done():
                future.set_result({'content': [{'type': 'text', 'text': 'The turn ended.'}], 'is_error': True})
        self.waiting.clear()
        try:
            await self.client.disconnect()
        except Exception:
            log.debug('closing the plan session failed', exc_info=True)
        self.workdir.cleanup()


def _usage_event(start: dict, delta: dict, model: str, started: float | None, finished: float, searches: int) -> dict:
    usage = {**(start or {}), **{k: v for k, v in (delta or {}).items() if v is not None}}
    return {'type': 'usage', 'model': model, 'input': usage.get('input_tokens') or 0,
            'cache_write': usage.get('cache_creation_input_tokens') or 0,
            'cache_read': usage.get('cache_read_input_tokens') or 0, 'output': usage.get('output_tokens') or 0,
            'searches': searches, 'cost': 0, 'plan': True,  # covered by the Claude plan, not billed per token
            'seconds': round(finished - started, 3) if started else None}


class SubscriptionProvider:
    def __init__(self, client_factory=sdk.ClaudeSDKClient):
        self._client = client_factory
        self._sessions: dict[int, Session] = {}  # by id() of the conversation's history list

    async def complete(self, model: ModelInfo, prompt: str) -> str:
        """A short one-off answer (e.g. a chat title), at low effort."""
        with tempfile.TemporaryDirectory() as workdir:
            options = _options(model.id, 'Answer briefly.', [], False, workdir, effort='low', max_turns=1)
            parts = []
            async with self._client(options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    if isinstance(message, sdk.AssistantMessage) and not message.parent_tool_use_id:
                        parts += [b.text for b in message.content if isinstance(b, sdk.TextBlock)]
            return ''.join(parts)

    async def close_turn(self, history: list) -> None:
        session = self._sessions.pop(id(history), None)
        if session is not None:
            await session.close()

    async def _start(self, model: ModelInfo, history: list, web_search: bool, system: str | None,
                     tools: list[dict]) -> Session:
        holder = {}

        def handler(name):
            async def run(args):
                return await holder['session'].receive(name, args)
            return run

        mcp_tools = [sdk.SdkMcpTool(t['name'], t['description'], t['input_schema'], handler(t['name']),
                                    sdk.ToolAnnotations(maxResultSizeChars=MAX_RESULT_CHARS)) for t in tools]
        workdir = tempfile.TemporaryDirectory(prefix='nembot-plan-')
        options = _options(model.id, system or system_prompt(vision=True, web_search=web_search), mcp_tools,
                           web_search, workdir.name, effort='medium',
                           thinking={'type': 'adaptive', 'display': 'summarized'})
        client = self._client(options)
        try:
            await client.connect()
            await client.query(render_prompt(history, model))
        except BaseException:
            workdir.cleanup()
            raise
        session = holder['session'] = Session(client, client.receive_messages().__aiter__(), workdir)
        return session

    async def respond(self, model: ModelInfo, history: list, web_search: bool = False, *,
                      system: str | None = None, tools: list[dict] | None = None) -> AsyncIterator[dict]:
        key = id(history)
        session = self._sessions.get(key)
        last = history[-1]
        if session is not None and last['role'] == 'tool' and session.asked:
            asked = {c['id']: c for c in session.asked}
            for r in last['results']:
                call = asked.get(r['id'])
                if call is not None:
                    session.deliver(call['name'], call['input'], tool_result(r))
            session.asked = []
        else:
            if session is not None:  # a leftover from an abandoned turn
                await self.close_turn(history)
            session = self._sessions[key] = await self._start(model, history, web_search, system,
                                                              TOOLS if tools is None else tools)
        handed_off = False
        try:
            async for item in self._stream(session, model):
                if item.get('role') == 'assistant' and item['tool_calls']:
                    session.asked = item['tool_calls']
                    handed_off = True
                yield item
        finally:
            if not handed_off:
                await self.close_turn(history)

    async def _stream(self, session: Session, model: ModelInfo) -> AsyncIterator[dict]:
        """Stream the session until it calls nembot tools or finishes; yield events, then the turn."""
        blocks: list[tuple[str, str]] = []  # (kind, text) of this turn, across web-search rounds
        calls: list[dict] = []
        previous, started, start_usage, delta_usage, searches = None, None, {}, {}, 0
        async for message in session.messages:
            if isinstance(message, sdk.StreamEvent):
                if message.parent_tool_use_id:
                    continue
                event = message.event
                kind = event.get('type')
                if kind == 'message_start':
                    previous, started, delta_usage, searches = None, None, {}, 0
                    start_usage = (event.get('message') or {}).get('usage') or {}
                elif kind == 'content_block_start':
                    started = started or time.monotonic()
                    block = event.get('content_block') or {}
                    btype = block.get('type')
                    if btype in ('tool_use', 'server_tool_use'):
                        name = block.get('name', '')
                        yield {'type': 'tool', 'name': _UI_NAMES.get(name, name.removeprefix(PREFIX))}
                    elif btype in ('text', 'thinking') and btype != previous:
                        yield {'type': 'block', 'kind': btype}
                    previous = btype
                elif kind == 'content_block_delta':
                    delta = event.get('delta') or {}
                    if delta.get('type') == 'text_delta':
                        yield {'type': 'text', 'delta': delta.get('text', '')}
                    elif delta.get('type') == 'thinking_delta':
                        yield {'type': 'progress', 'delta': delta.get('thinking', '')}
                elif kind == 'message_delta':
                    delta_usage = event.get('usage') or {}
                elif kind == 'message_stop':
                    yield _usage_event(start_usage, delta_usage, model.id, started, time.monotonic(), searches)
                    blocks.append(('end', ''))  # text from separate responses becomes separate paragraphs
                    if calls:  # the SDK is now waiting in our tool handlers
                        yield self._turn(model, blocks, calls, 'tool_use')
                        return
            elif isinstance(message, sdk.AssistantMessage):
                if message.parent_tool_use_id:
                    continue
                if message.error:
                    raise PlanError(_ERRORS.get(message.error, f'Claude plan error: {message.error}'))
                for block in message.content:
                    if isinstance(block, sdk.TextBlock):
                        blocks.append(('text', block.text))
                    elif isinstance(block, sdk.ToolUseBlock) and block.name.startswith(PREFIX):
                        calls.append({'id': block.id, 'name': block.name.removeprefix(PREFIX), 'input': block.input})
                    elif isinstance(block, sdk.ToolUseBlock) and block.name == 'WebSearch':
                        searches += 1
            elif isinstance(message, sdk.RateLimitEvent):
                info = message.rate_limit_info
                if info.status == 'rejected':
                    when = time.strftime('%H:%M', time.localtime(info.resets_at)) if info.resets_at else 'later'
                    raise PlanError(f'Your Claude plan’s usage limit has been reached; it resets at {when}.')
            elif isinstance(message, sdk.ResultMessage):
                if message.is_error and message.subtype != 'success':
                    raise PlanError('; '.join(message.errors or []) or f'Claude plan session failed ({message.subtype}).')
                yield self._turn(model, blocks, [], 'end')
                return
        raise PlanError('The Claude Code session ended unexpectedly.')

    @staticmethod
    def _turn(model: ModelInfo, blocks: list, calls: list, stop: str) -> dict:
        return {'role': 'assistant', 'model': model.ref, 'model_name': model.name,
                'text': text_from_blocks(blocks), 'tool_calls': calls, 'raw': {}, 'stop': stop}


_ERRORS = {
    'authentication_failed': 'Claude Code is not signed in. Run `claude` and /login with your Claude account.',
    'billing_error': 'Your Claude plan could not be used (billing). Check your subscription.',
    'rate_limit': 'Your Claude plan’s usage limit has been reached; try again later.',
    'server_error': 'Claude had a server error; try again.',
}
