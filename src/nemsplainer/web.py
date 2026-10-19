"""Localhost web chat over the MMS database, backed by the Anthropic API."""
import argparse
import json
from pathlib import Path

import anthropic
import duckdb
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route
import uvicorn

from .chat import Conversation, run_turn

STATIC = Path(__file__).parent / 'static'


def _api_error(exc: Exception) -> str:
    match exc:
        case anthropic.AuthenticationError():
            return 'Anthropic authentication failed. Run `ant auth login` or set ANTHROPIC_API_KEY.'
        case anthropic.RateLimitError():
            return 'Rate limited by the Anthropic API; wait a moment and retry.'
        case anthropic.APIStatusError():
            return f'Anthropic API error {exc.status_code}: {exc.message}'
        case anthropic.APIConnectionError():
            return 'Could not reach the Anthropic API.'
    return f'{type(exc).__name__}: {exc}'


def create_app(database: str) -> Starlette:
    db = duckdb.connect(database, read_only=True)
    client = anthropic.AsyncAnthropic()
    conversations: dict[str, Conversation] = {}

    async def index(request: Request):
        return FileResponse(STATIC / 'index.html')

    async def chat(request: Request):
        body = await request.json()
        text = str(body.get('message', '')).strip()
        conversation_id = str(body.get('conversation', ''))
        if not text or not conversation_id:
            return JSONResponse({'error': 'message and conversation are required'}, status_code=400)
        conversation = conversations.setdefault(conversation_id, Conversation())
        if conversation.lock.locked():
            return JSONResponse({'error': 'a reply is already in progress'}, status_code=409)

        async def events():
            async with conversation.lock:
                try:
                    async for event in run_turn(client, db, conversation, text):
                        yield f'data: {json.dumps(event)}\n\n'
                except Exception as exc:
                    yield f"data: {json.dumps({'type': 'error', 'message': _api_error(exc)})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"

        return StreamingResponse(events(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache'})

    return Starlette(routes=[Route('/', index), Route('/api/chat', chat, methods=['POST'])])


def main() -> None:
    parser = argparse.ArgumentParser(description='Serve the NEM analyst chat on localhost.')
    parser.add_argument('--database', default='data/nem.duckdb', metavar='PATH')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    if not Path(args.database).exists():
        parser.error(f'{args.database} not found; run `nemsplainer` to load data first')
    uvicorn.run(create_app(args.database), host=args.host, port=args.port)


if __name__ == '__main__':
    main()
