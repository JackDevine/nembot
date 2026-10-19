"""Localhost web chat over the MMS database, backed by Claude or OpenRouter models."""
import argparse
from decimal import Decimal
import asyncio
import contextlib
import json
import logging
from pathlib import Path

import anthropic
import duckdb
import openai
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
import uvicorn

from . import client, downloads, explorer, live, plugins, reports, snippets
from .creator import Creator
from .notebooks import NotebookServers
from .ingest import SUPPORTED
from .chat import Conversation, run_turn
from .claude import ClaudeProvider
from .models import DEFAULT_MODEL, Catalog, public
from .openrouter import OpenRouterProvider

STATIC = Path(__file__).parent / 'static'


def _json_default(value):
    """JSON for plugin route results: numbers stay numbers, dates become ISO strings."""
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)


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


def create_app(database: str, cache_dir: str = 'data/cache', url: str = 'http://127.0.0.1:8000',
               plugins_dir: str = 'plugins') -> Starlette:
    # One read-write connection for the whole app: downloads write through a
    # cursor while chat/explorer reads use their own cursors (DuckDB MVCC).
    db = duckdb.connect(database)
    jobs = downloads.Downloads(db, Path(database).absolute(), Path(cache_dir).absolute())
    snippets.ensure(db)
    report_dir = Path(database).absolute().parent / 'reports'
    notebook_servers = NotebookServers(report_dir, url)

    def plugin_query(sql: str):
        import io
        import polars
        cursor = db.cursor()
        try:
            return polars.read_parquet(io.BytesIO(client.parquet_bytes(cursor, sql)))
        finally:
            cursor.close()
    plugin_manager = plugins.PluginManager(Path(plugins_dir).absolute(), plugin_query)
    plugin_manager.reload()
    creator = Creator(plugin_manager, db, url, str(Path(database).absolute()))

    async def exporter(fmt, title, content, results):
        return await asyncio.to_thread(reports.export, db, report_dir, fmt, title, content, results,
                                       url=url, database=str(Path(database).absolute()))
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
        mode = 'creator' if body.get('mode') == 'creator' else 'analyst'
        conversation = conversations.setdefault(conversation_id, Conversation(mode=mode))
        if conversation.mode != mode:
            return JSONResponse({'error': 'start a new chat to switch modes'}, status_code=409)
        if conversation.lock.locked():
            return JSONResponse({'error': 'a reply is already in progress'}, status_code=409)

        async def events():
            async with conversation.lock:
                yield f"data: {json.dumps({'type': 'model', 'model': public(model)})}\n\n"
                try:
                    async for event in run_turn(providers, db, conversation, text, model,
                                                web_search=bool(body.get('web_search')), jobs=jobs,
                                                exporter=exporter, plugin_manager=plugin_manager,
                                                creator=creator):
                        yield f'data: {json.dumps(event)}\n\n'
                except Exception as exc:
                    logging.getLogger(__name__).exception('chat turn failed')
                    yield f"data: {json.dumps({'type': 'error', 'message': _api_error(exc)})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"

        return StreamingResponse(events(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache'})

    async def chat_decision(request: Request):
        """The user's answer to a request_download popup."""
        body = await request.json()
        conversation = conversations.get(str(body.get('conversation', '')))
        future = conversation and conversation.pending.get(str(body.get('request', '')))
        if not future or future.done():
            return JSONResponse({'error': 'no pending download request'}, status_code=404)
        future.set_result({'approve': bool(body.get('approve')), 'start': body.get('start'), 'end': body.get('end'),
                           'recent': bool(body.get('recent', True))})
        return JSONResponse({'ok': True})

    async def snippet_list(request: Request):
        if request.method == 'GET':
            return JSONResponse({'snippets': await asyncio.to_thread(snippets.list_all, db)})
        body = await request.json()
        sql = str(body.get('sql') or '').strip()
        plot_spec = body.get('plot_spec') if isinstance(body.get('plot_spec'), dict) else None
        if not sql:
            return JSONResponse({'error': 'sql is required'}, status_code=400)
        question = str(body.get('question') or '')[:2000] or None
        name = snippets.fallback_name(sql, plot_spec)
        model = await catalog.get(str(body.get('model') or DEFAULT_MODEL))
        if model and model.available:
            try:  # the current chat model suggests a name; the user can edit it
                name = await asyncio.wait_for(
                    providers[model.provider].complete(model, snippets.name_prompt(sql, plot_spec, question)), 60) or name
            except Exception:
                logging.getLogger(__name__).warning('snippet naming failed', exc_info=True)
        snippet = await asyncio.to_thread(snippets.create, db, name=name, sql=sql, plot_spec=plot_spec,
                                          question=question, model=model.ref if model else None,
                                          conversation=str(body.get('conversation') or '') or None)
        return JSONResponse(snippet)

    async def snippet_item(request: Request):
        snippet_id = int(request.path_params['id'])
        if request.method == 'DELETE':
            await asyncio.to_thread(snippets.delete, db, snippet_id)
            return JSONResponse({'ok': True})
        if request.method == 'PATCH':
            body = await request.json()
            snippet = await asyncio.to_thread(snippets.rename, db, snippet_id, str(body.get('name', '')))
        else:
            snippet = await asyncio.to_thread(snippets.get, db, snippet_id)
        return JSONResponse(snippet) if snippet else JSONResponse({'error': 'unknown snippet'}, status_code=404)

    async def snippet_run(request: Request):
        snippet = await asyncio.to_thread(snippets.get, db, int(request.path_params['id']))
        if not snippet:
            return JSONResponse({'error': 'unknown snippet'}, status_code=404)
        return JSONResponse({'snippet': snippet, **await asyncio.to_thread(snippets.run, db, snippet)})

    async def query(request: Request):
        """SELECT-only query endpoint for generated notebooks (Parquet response)."""
        if request.headers.get('content-type', '').split(';')[0] != 'application/json':
            return JSONResponse({'error': 'expected application/json'}, status_code=415)
        body = await request.json()

        def run():
            cursor = db.cursor()
            try:
                return client.parquet_bytes(cursor, str(body.get('sql', '')))
            finally:
                cursor.close()
        try:
            data = await asyncio.to_thread(run)
        except (ValueError, duckdb.Error) as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)
        return Response(data, media_type='application/vnd.apache.parquet')

    def report_path(name: str) -> Path | None:
        path = (report_dir / name).resolve()
        return path if path.parent == report_dir.resolve() and path.is_file() else None

    async def report_list(request: Request):
        return JSONResponse({'reports': await asyncio.to_thread(reports.listing, report_dir)})

    async def report_file(request: Request):
        path = report_path(request.path_params['name'])
        if path is None:
            return JSONResponse({'error': 'no such report'}, status_code=404)
        inline = path.suffix in ('.html', '.pdf')
        return FileResponse(path, filename=None if inline else path.name,
                            content_disposition_type='inline' if inline else 'attachment')

    async def notebook_open(request: Request):
        path = report_path(request.path_params['name'])
        if path is None or path.suffix not in ('.ipynb', '.py'):
            return JSONResponse({'error': 'no such notebook'}, status_code=404)
        kind = 'jupyter' if path.suffix == '.ipynb' else 'marimo'
        try:
            target = await asyncio.to_thread(notebook_servers.url, kind, path.name)
        except RuntimeError as exc:
            return JSONResponse({'error': str(exc)}, status_code=500)
        return RedirectResponse(target)

    async def plugin_list(request: Request):
        return JSONResponse({'plugins': [p.public() for p in plugin_manager.plugins.values()],
                             'directory': str(plugin_manager.directory)})

    async def plugin_reload(request: Request):
        await asyncio.to_thread(plugin_manager.reload)
        return await plugin_list(request)

    async def plugin_enable(request: Request):
        body = await request.json()
        name = request.path_params['name']
        if name not in plugin_manager.plugins:
            return JSONResponse({'error': 'unknown plugin'}, status_code=404)
        await asyncio.to_thread(plugin_manager.set_enabled, name, bool(body.get('enabled')))
        return await plugin_list(request)

    async def plugin_asset(request: Request):
        name, asset = request.path_params['name'], request.path_params['asset']
        plugin = plugin_manager.plugins.get(name)
        allowed = asset in ('client.js', 'style.css')
        path = plugin.directory / asset if allowed and plugin and plugin.status == 'loaded' else None
        if path is None or not path.exists():
            return JSONResponse({'error': 'not found'}, status_code=404)
        if asset == 'style.css':
            return FileResponse(path, media_type='text/css')
        # Scope client.js to its own API object, so it can't see other plugins' state.
        source = path.read_text()
        wrapped = f'nemsplainer.definePlugin({json.dumps(name)}, function (nemsplainer) {{\n{source}\n}});\n'
        return Response(wrapped, media_type='text/javascript')

    async def plugin_route(request: Request):
        name, rest = request.path_params['name'], '/' + request.path_params['path']
        handler = plugin_manager.route(name, request.method, rest)
        if handler is None:
            return JSONResponse({'error': f'no {request.method} route {rest} in plugin {name}'}, status_code=404)
        try:
            value = handler(request)
            if asyncio.iscoroutine(value):
                value = await value
        except Exception as exc:
            logging.getLogger(__name__).exception('plugin %s route %s failed', name, rest)
            return JSONResponse({'error': f'{type(exc).__name__}: {exc}'}, status_code=500)
        if isinstance(value, Response):
            return value
        if isinstance(value, str):
            return Response(value, media_type='text/html')
        return JSONResponse(json.loads(json.dumps(value, default=_json_default)))

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
                           values={k: query[k] for k in explorer.FILTER_COLUMNS if query.get(k)},
                           sort=query.get('sort'), descending=query.get('order') == 'desc')
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
        if body.get('live'):
            if name not in live.FEEDS:
                return JSONResponse({'error': f'Recent data is not supported for {name} yet'}, status_code=400)
            try:
                job = await asyncio.to_thread(downloads.submit_live, jobs, db, name, str(body.get('reason') or 'explorer'))
            except RuntimeError as exc:
                return JSONResponse({'error': str(exc)}, status_code=502)
            if job is None:
                return JSONResponse({'error': f'{name} already has the latest published data'}, status_code=409)
            return JSONResponse(job.public())
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

    @contextlib.asynccontextmanager
    async def lifespan(app):
        yield
        notebook_servers.stop()  # also registered with atexit

    return Starlette(lifespan=lifespan, routes=[
        Route('/', index),
        Route('/api/models', models),
        Route('/api/chat', chat, methods=['POST']),
        Route('/api/chat/decision', chat_decision, methods=['POST']),
        Route('/api/query', query, methods=['POST']),
        Route('/api/plugins', plugin_list),
        Route('/api/plugins/reload', plugin_reload, methods=['POST']),
        Route('/api/plugins/{name}/enable', plugin_enable, methods=['POST']),
        Route('/plugins/{name}/_assets/{asset}', plugin_asset),
        Route('/plugins/{name}/{path:path}', plugin_route, methods=['GET', 'POST', 'PUT', 'DELETE']),
        Route('/api/reports', report_list),
        Route('/reports/{name}', report_file),
        Route('/notebooks/open/{name}', notebook_open),
        Route('/api/snippets', snippet_list, methods=['GET', 'POST']),
        Route('/api/snippets/{id:int}', snippet_item, methods=['GET', 'PATCH', 'DELETE']),
        Route('/api/snippets/{id:int}/run', snippet_run),
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
    parser.add_argument('--plugins-dir', default='plugins', metavar='PATH')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    Path(args.database).parent.mkdir(parents=True, exist_ok=True)  # an empty database is fine: use the explorer
    logging.basicConfig(level=logging.INFO, format='%(levelname)s:     %(name)s: %(message)s')
    for noisy in ('httpx', 'httpx2', 'anthropic', 'openai'):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        app = create_app(args.database, args.cache_dir, f'http://{args.host}:{args.port}', args.plugins_dir)
    except duckdb.IOException as exc:
        parser.error(f'cannot open {args.database} for writing ({exc}). Is another nemsplainer process using it?')
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == '__main__':
    main()
