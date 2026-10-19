"""Localhost web chat over the MMS database, backed by Claude or OpenRouter models."""
import argparse
import asyncio
import json
import logging
from pathlib import Path

import anthropic
import duckdb
import openai
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
import uvicorn

from . import downloads, explorer
from .ingest import SUPPORTED
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


def create_app(database: str, cache_dir: str = 'data/cache') -> Starlette:
    # One read-write connection for the whole app: downloads write through a
    # cursor while chat/explorer reads use their own cursors (DuckDB MVCC).
    db = duckdb.connect(database)
    jobs = downloads.Downloads(db, Path(database).absolute(), Path(cache_dir).absolute())
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
                    async for event in run_turn(providers, db, conversation, text, model,
                                                web_search=bool(body.get('web_search'))):
                        yield f'data: {json.dumps(event)}\n\n'
                except Exception as exc:
                    logging.getLogger(__name__).exception('chat turn failed')
                    yield f"data: {json.dumps({'type': 'error', 'message': _api_error(exc)})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"

        return StreamingResponse(events(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache'})

    async def explorer_overview(request: Request):
        return JSONResponse(await asyncio.to_thread(explorer.overview, db))

    async def explorer_table(request: Request):
        result = await asyncio.to_thread(explorer.table, db, request.path_params['name'].upper())
        return JSONResponse(result) if result else JSONResponse({'error': 'unknown table'}, status_code=404)

    async def explorer_rows(request: Request):
        query = request.query_params
        try:
            options = dict(offset=int(query.get('offset', 0)), limit=int(query.get('limit', 25)),
                           start=explorer.parse_date(query.get('start')), end=explorer.parse_date(query.get('end')),
                           values={k: query[k] for k in explorer.FILTER_COLUMNS if query.get(k)})
        except ValueError as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)
        result = await asyncio.to_thread(explorer.rows, db, request.path_params['name'].upper(), **options)
        return JSONResponse(result) if result else JSONResponse({'error': 'unknown table'}, status_code=404)

    async def freshness(request: Request):
        overview = await asyncio.to_thread(explorer.overview, db)
        names = [t['name'] for p in overview['packages'] for t in p['tables'] if t['downloaded']]

        def check():
            result = {}
            for name in names:
                try:
                    loaded = downloads.loaded_months(db, name)
                    latest = downloads.latest_published(name)
                except Exception as exc:  # offline, or NEMWEB unavailable
                    return {'error': str(exc), 'tables': result}
                result[name] = {'loaded': loaded[-1] if loaded else None, 'latest': latest,
                                'stale': bool(loaded) and loaded[-1] < latest}
            return {'tables': result}
        return JSONResponse(await asyncio.to_thread(check))

    async def download_plan(request: Request):
        name = request.path_params['name'].upper()
        if name not in SUPPORTED:
            return JSONResponse({'error': f'{name} is not published in the monthly archive'}, status_code=404)
        try:
            return JSONResponse(await asyncio.to_thread(downloads.plan, db, name))
        except RuntimeError as exc:
            return JSONResponse({'error': str(exc)}, status_code=502)

    async def download_jobs(request: Request):
        if request.method == 'GET':
            return JSONResponse({'jobs': jobs.list()})
        body = await request.json()
        name = str(body.get('table', '')).upper()
        if name not in SUPPORTED:
            return JSONResponse({'error': f'{name} is not published in the monthly archive'}, status_code=400)
        try:
            start, end = downloads.month_range(str(body.get('start', '')), str(body.get('end', '')))
        except ValueError as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)
        job = jobs.submit(name, start, end, str(body.get('reason') or 'explorer'))
        return JSONResponse(job.public())

    async def download_job(request: Request):
        job_id = int(request.path_params['id'])
        if request.method == 'DELETE':
            jobs.dismiss(job_id)
            return JSONResponse({'ok': True})
        job = jobs.cancel(job_id)
        return JSONResponse(job.public()) if job else JSONResponse({'error': 'unknown job'}, status_code=404)

    return Starlette(routes=[
        Route('/', index),
        Route('/api/models', models),
        Route('/api/chat', chat, methods=['POST']),
        Route('/api/explorer', explorer_overview),
        Route('/api/explorer/tables/{name}', explorer_table),
        Route('/api/explorer/tables/{name}/rows', explorer_rows),
        Route('/api/explorer/freshness', freshness),
        Route('/api/downloads', download_jobs, methods=['GET', 'POST']),
        Route('/api/downloads/plan/{name}', download_plan),
        Route('/api/downloads/{id:int}', download_job, methods=['POST', 'DELETE']),
        Mount('/static', StaticFiles(directory=STATIC), name='static'),
    ])


def main() -> None:
    parser = argparse.ArgumentParser(description='Serve the NEM analyst chat on localhost.')
    parser.add_argument('--database', default='data/nem.duckdb', metavar='PATH')
    parser.add_argument('--cache-dir', default='data/cache', metavar='PATH')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    Path(args.database).parent.mkdir(parents=True, exist_ok=True)  # an empty database is fine: use the explorer
    logging.basicConfig(level=logging.INFO, format='%(levelname)s:     %(name)s: %(message)s')
    for noisy in ('httpx', 'httpx2', 'anthropic', 'openai'):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        app = create_app(args.database, args.cache_dir)
    except duckdb.IOException as exc:
        parser.error(f'cannot open {args.database} for writing ({exc}). Is another nemsplainer process using it?')
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == '__main__':
    main()
