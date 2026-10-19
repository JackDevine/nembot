"""Localhost web chat over the MMS database, backed by Claude or OpenRouter models."""
import argparse
import json
import logging
from pathlib import Path

import anthropic
import duckdb
import openai
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route
import uvicorn

from .chat import Conversation, run_turn
from .claude import ClaudeProvider
from .models import DEFAULT_MODEL, Catalog, public
from .openrouter import OpenRouterProvider

STATIC = Path(__file__).parent / 'static'


def _api_error(exc: Exception) -> str:
    match exc:
        case anthropic.AuthenticationError():
            return 'Anthropic authentication failed. Run `ant auth login` or set ANTHROPIC_API_KEY.'
        case openai.AuthenticationError():
            return 'OpenRouter authentication failed. Check OPENROUTER_API_KEY.'
        case anthropic.RateLimitError() | openai.RateLimitError():
            return 'Rate limited by the model provider; wait a moment and retry.'
        case anthropic.APIStatusError() | openai.APIStatusError():
            return f'Model provider error {exc.status_code}: {exc.message}'
        case anthropic.APIConnectionError() | openai.APIConnectionError():
            return 'Could not reach the model provider.'
    return f'{type(exc).__name__}: {exc}'


def create_app(database: str) -> Starlette:
    db = duckdb.connect(database, read_only=True)
    claude = anthropic.AsyncAnthropic()
    catalog = Catalog(claude)
    providers = {'claude': ClaudeProvider(claude), 'openrouter': OpenRouterProvider()}
    conversations: dict[str, Conversation] = {}

    async def index(request: Request):
        return FileResponse(STATIC / 'index.html')

    async def models(request: Request):
        listed = await catalog.models()
        return JSONResponse({'default': DEFAULT_MODEL, 'errors': catalog.errors,
                             'models': [public(m) for m in listed.values()]})

    async def chat(request: Request):
        body = await request.json()
        text = str(body.get('message', '')).strip()
        conversation_id = str(body.get('conversation', ''))
        if not text or not conversation_id:
            return JSONResponse({'error': 'message and conversation are required'}, status_code=400)
        model = await catalog.get(str(body.get('model') or DEFAULT_MODEL))
        if model is None:
            return JSONResponse({'error': 'unknown model'}, status_code=400)
        if not model.tools or not model.available:
            return JSONResponse({'error': f'{model.name} cannot be used for this chat'}, status_code=400)
        conversation = conversations.setdefault(conversation_id, Conversation())
        if conversation.lock.locked():
            return JSONResponse({'error': 'a reply is already in progress'}, status_code=409)

        async def events():
            async with conversation.lock:
                yield f"data: {json.dumps({'type': 'model', 'model': public(model)})}\n\n"
                try:
                    async for event in run_turn(providers, db, conversation, text, model):
                        yield f'data: {json.dumps(event)}\n\n'
                except Exception as exc:
                    logging.getLogger(__name__).exception('chat turn failed')
                    yield f"data: {json.dumps({'type': 'error', 'message': _api_error(exc)})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"

        return StreamingResponse(events(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache'})

    return Starlette(routes=[
        Route('/', index),
        Route('/api/models', models),
        Route('/api/chat', chat, methods=['POST']),
    ])


def main() -> None:
    parser = argparse.ArgumentParser(description='Serve the NEM analyst chat on localhost.')
    parser.add_argument('--database', default='data/nem.duckdb', metavar='PATH')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    if not Path(args.database).exists():
        parser.error(f'{args.database} not found; run `nemsplainer` to load data first')
    logging.basicConfig(level=logging.INFO, format='%(levelname)s:     %(name)s: %(message)s')
    for noisy in ('httpx', 'httpx2', 'anthropic', 'openai'):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    uvicorn.run(create_app(args.database), host=args.host, port=args.port)


if __name__ == '__main__':
    main()
