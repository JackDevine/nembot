"""Localhost web chat over the MMS database, backed by Claude or OpenRouter models."""
import argparse
from decimal import Decimal
import asyncio
import contextlib
import faulthandler
import json
import logging
from pathlib import Path
import signal
from urllib.parse import quote, urlsplit

import anthropic
import claude_agent_sdk
import duckdb
import openai
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
import uvicorn

from . import chats, client, credentials, docs, downloads, explorer, live, plugins, reports, setup, snippets, subscription
from .creator import Creator
from .notebooks import NotebookServers
from . import notebook_context
from .notebook_files import NotebookFiles
from .ingest import SUPPORTED
from .chat import MAX_COMPARE, Conversation, model_event, keep_branch, rerun_chart, run_compare, run_single, run_turn, stop_turn
from .connections import Connections
from .presets import PRESETS
from .models import public

STATIC = Path(__file__).parent / 'static'


class RevalidatingStaticFiles(StaticFiles):
    """Static files the browser rechecks on every load (a cheap 304 when unchanged), so edits show on refresh."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers['Cache-Control'] = 'no-cache'
        return response


log = logging.getLogger(__name__)

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
        case subscription.PlanError():
            return str(exc)
        case claude_agent_sdk.CLINotFoundError():
            return 'Claude Code was not found, so the Claude plan models cannot run.'
        case claude_agent_sdk.ClaudeSDKError():
            return f'Claude Code error: {exc}'
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
    docs.ensure(db)
    chats.ensure(db)
    report_dir = Path(database).absolute().parent / 'reports'
    notebook_servers = NotebookServers(
        report_dir, url, str(Path(database).absolute()),
        prepare=lambda kind: notebook_context.write_marimo_project(report_dir, db) if kind == 'marimo' else None)
    notebook_files = NotebookFiles(report_dir, url, str(Path(database).absolute()), servers=notebook_servers)

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
    async def documents(title, content, results, conversation_id=None):
        seed, missing, errors = await asyncio.to_thread(docs.seed_from_markdown, db, content, results)
        doc = await asyncio.to_thread(docs.create, db, title, seed=seed, conversation=conversation_id)
        return {'id': doc['id'], 'title': doc['title'], 'missing': missing, 'errors': errors}

    connections = Connections(session_dir=Path(database).absolute().parent / 'claude-sessions')
    catalog, providers = connections.catalog, connections.providers
    conversations: dict[str, Conversation] = {}

    async def index(request: Request):
        return FileResponse(STATIC / 'index.html', headers={'Cache-Control': 'no-cache'})

    async def models(request: Request):
        listed = await catalog.models()
        return JSONResponse({'default': catalog.default(), 'errors': catalog.errors,
                             'providers': connections.labels(),
                             'models': [public(m) for m in listed.values()]})

    # ---- Setup (onboarding and Settings): only for requests from this computer ----
    def setup_guard(request: Request) -> Response | None:
        """Refuse other machines, and pages from other sites (which could post here from your browser)."""
        refused = setup.refusal(request.client.host if request.client else '', request.headers.get('origin'),
                                {f'{request.url.scheme}://{request.url.netloc}', url.rstrip('/')},
                                request.method, request.headers.get('content-type', ''))
        return JSONResponse({'error': refused[1]}, status_code=refused[0]) if refused else None

    async def setup_status(request: Request):
        if refused := setup_guard(request):
            return refused
        await catalog.models()
        return JSONResponse({'cards': await connections.status(), 'usable': len(catalog.usable()),
                             'errors': catalog.errors, 'config': str(credentials.path()),
                             'keyring': bool(credentials.setting('keyring', False)),
                             'confirm_code': bool(credentials.setting('confirm_code', True)),
                             'keyring_available': credentials.keyring_available(),
                             'has_data': await asyncio.to_thread(setup_has_data)})

    def setup_has_data() -> bool:
        cursor = db.cursor()
        try:
            return bool(cursor.execute("SELECT count(*) FROM _nembot.files").fetchone()[0])
        except duckdb.Error:
            return False
        finally:
            cursor.close()

    async def setup_connect(request: Request):
        """Test a connection and save it if the service answered (the key itself is never sent back)."""
        if refused := setup_guard(request):
            return refused
        body = await request.json()
        name = str(body.get('name', ''))
        if name not in ('claude', *PRESETS):
            return JSONResponse({'error': 'unknown service'}, status_code=400)
        key = str(body.get('key') or '').strip() or None
        base_url = str(body.get('base_url') or '').strip().rstrip('/') or None
        result = await setup.check(name, key=key, base_url=base_url, model=body.get('model'))
        if result['listed']:
            preset = PRESETS.get(name)
            fields = {}
            if preset and preset.local:
                fields = {'enabled': True, 'base_url': base_url if base_url and base_url != preset.base_url else None}
            await asyncio.to_thread(credentials.save_provider, name, api_key=key, **fields)
            connections.refresh()
            result['saved'] = True
        return JSONResponse(result)

    async def setup_forget(request: Request):
        if refused := setup_guard(request):
            return refused
        name = str((await request.json()).get('name', ''))
        await asyncio.to_thread(credentials.forget_provider, name)
        connections.refresh()
        return JSONResponse({'ok': True})

    async def setup_settings(request: Request):
        if refused := setup_guard(request):
            return refused
        body = await request.json()
        if 'confirm_code' in body:
            await asyncio.to_thread(credentials.set_setting, 'confirm_code', bool(body['confirm_code']))
        if 'keyring' in body:
            try:
                await asyncio.to_thread(credentials.use_keyring, bool(body['keyring']))
            except Exception as exc:
                return JSONResponse({'error': str(exc)}, status_code=400)
        connections.refresh()
        return JSONResponse({'ok': True})

    def openrouter_callback_url() -> str:
        # OpenRouter accepts localhost callbacks on any port.
        return f'http://localhost:{urlsplit(url).port or 80}/setup/openrouter/callback'

    async def setup_openrouter_start(request: Request):
        if refused := setup_guard(request):
            return refused
        return JSONResponse({'url': setup.openrouter_start(openrouter_callback_url())})

    async def setup_openrouter_callback(request: Request):
        host = request.client.host if request.client else ''
        if host not in ('127.0.0.1', '::1'):
            return JSONResponse({'error': 'Settings can only be changed from this computer.'}, status_code=403)
        params = request.query_params
        back = url.rstrip('/') + '/#setup'
        try:
            key = await setup.openrouter_finish(params.get('state', ''), params.get('code', ''))
            result = await setup.check('openrouter', key=key)
            if not result['listed']:
                raise ValueError(result['steps'][-1]['detail'] if result['steps'] else 'OpenRouter did not answer.')
            await asyncio.to_thread(credentials.save_provider, 'openrouter', api_key=key)
            connections.refresh()
            return RedirectResponse(back + '/connected/openrouter')
        except Exception as exc:
            log.warning('OpenRouter sign-in failed: %s', exc)
            return RedirectResponse(back + '/error/' + quote(str(exc)[:300]))

    async def chat(request: Request):
        body = await request.json()
        text = str(body.get('message', '')).strip()
        conversation_id = str(body.get('conversation', ''))
        if not text or not conversation_id:
            return JSONResponse({'error': 'message and conversation are required'}, status_code=400)
        # Several models answer side by side (a compare turn); the first is the chat's model.
        refs = body.get('models') or [body.get('model') or catalog.default()]
        if not isinstance(refs, list) or len(set(map(str, refs))) != len(refs) or not 1 <= len(refs) <= MAX_COMPARE:
            return JSONResponse({'error': f'choose 1 to {MAX_COMPARE} different models'}, status_code=400)
        chosen = []
        for ref in refs:
            model = await catalog.get(str(ref))
            if model is None:
                return JSONResponse({'error': f'unknown model {ref}'}, status_code=400)
            if not model.tools or not model.available:
                return JSONResponse({'error': f'{model.name} cannot be used for this chat'}, status_code=400)
            chosen.append(model)
        model = chosen[0]
        mode = 'creator' if body.get('mode') == 'creator' else 'analyst'
        if len(chosen) > 1 and mode == 'creator':  # the models would overwrite each other's plugin drafts
            return JSONResponse({'error': 'Creator mode works with one model at a time'}, status_code=400)
        web_search = bool(body.get('web_search'))
        efforts = {str(k): str(v) for k, v in body.get('efforts').items()} if isinstance(body.get('efforts'), dict) else {}
        saved = None
        if conversation_id not in conversations:  # a saved chat reopened after a restart
            saved = await asyncio.to_thread(chats.get, db, conversation_id)
            if saved:
                conversations[conversation_id] = Conversation(history=saved['history'], results=saved['results'],
                                                              mode=saved['mode'])
        conversation = conversations.setdefault(conversation_id, Conversation(mode=mode))
        if conversation.mode != mode:
            return JSONResponse({'error': 'start a new chat to switch modes'}, status_code=409)
        if conversation.lock.locked():
            return JSONResponse({'error': 'a reply is already in progress'}, status_code=409)
        if saved is None and conversation.history:
            saved = await asyncio.to_thread(chats.get, db, conversation_id)
        transcript = saved['transcript'] if saved else []
        # A new chat is named by its model from the first message, while the reply streams.
        naming = None
        if not saved or not saved['title']:
            naming = asyncio.create_task(asyncio.wait_for(
                providers[model.provider].complete(model, chats.title_prompt(text)), 60))

        async def events():
            shown = []
            turn = {'text': text, 'events': shown}

            def store():
                kept = next((e['branch'] for e in shown if e['type'] == 'kept'), None)
                chats.save(db, conversation_id, mode=conversation.mode, web_search=web_search,
                           model=chosen[kept].ref if kept is not None else model.ref,
                           history=conversation.history, results=conversation.results,
                           transcript=[*transcript, {**turn, 'events': chats.compact(shown)}])

            async with conversation.lock:
                conversation.branches = None  # an earlier compare turn's choice is final now
                options = dict(web_search=web_search, jobs=jobs, exporter=exporter, plugin_manager=plugin_manager,
                               creator=creator, documents=lambda *a: documents(*a, conversation_id),
                               interactive=True, efforts=efforts, notebook_files=notebook_files)
                if len(chosen) > 1:
                    turn_events = run_compare(providers, db, conversation, text, chosen, describe_error=_api_error,
                                              **options)
                else:
                    event = {'type': 'model', 'model': model_event(model, efforts)}
                    shown.append(event)
                    yield f'data: {json.dumps(event)}\n\n'
                    turn_events = run_single(providers, db, conversation, text, model, describe_error=_api_error,
                                             **options)
                try:
                    async for event in turn_events:
                        shown.append(event)
                        yield f'data: {json.dumps(event)}\n\n'
                except Exception as exc:
                    logging.getLogger(__name__).exception('chat turn failed')
                    event = {'type': 'error', 'message': _api_error(exc)}
                    shown.append(event)
                    yield f'data: {json.dumps(event)}\n\n'
                except BaseException:  # the browser went away mid-reply: keep what was shown
                    store()
                    if naming:
                        naming.cancel()
                    raise
                await asyncio.to_thread(store)
                if naming:
                    try:
                        title = await naming
                    except Exception:
                        logging.getLogger(__name__).warning('chat naming failed', exc_info=True)
                        title = None
                    title = await asyncio.to_thread(chats.set_title, db, conversation_id,
                                                    title or chats.fallback_title(text), by_user=False)
                    if title:
                        yield f"data: {json.dumps({'type': 'chat_title', 'id': conversation_id, 'title': title})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"

        return StreamingResponse(events(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache'})

    async def chart(request: Request):
        """Re-run a saved chat's chart from its SQL and spec (saved chats keep no chart data)."""
        body = await request.json()
        data = await asyncio.to_thread(rerun_chart, db, str(body.get('sql') or ''), body.get('plot_spec'))
        return JSONResponse(data, status_code=400 if 'error' in data else 200)

    async def chat_stop(request: Request):
        """Stop a chat's reply: every model's, or one compared model's (``branch``)."""
        body = await request.json()
        conversation = conversations.get(str(body.get('conversation', '')))
        branch = body.get('branch')
        if conversation is None or not (branch is None or isinstance(branch, int)):
            return JSONResponse({'error': 'no such chat'}, status_code=404)
        return JSONResponse({'stopped': stop_turn(conversation, branch)})

    async def chat_keep(request: Request):
        """Continue a chat from another model's answer to its latest compare turn."""
        body = await request.json()
        conversation_id = str(body.get('conversation', ''))
        conversation = conversations.get(conversation_id)
        if conversation is None or conversation.lock.locked() or not isinstance(body.get('branch'), int):
            return JSONResponse({'error': 'that chat is busy or no longer open'}, status_code=409)
        branch = body['branch']
        if not keep_branch(conversation, branch):
            return JSONResponse({'error': 'You can only switch answers on the latest turn, before the app restarts.'},
                                status_code=409)
        saved = await asyncio.to_thread(chats.get, db, conversation_id)
        if saved:
            transcript = saved['transcript']
            transcript[-1]['events'] = [{**e, 'branch': branch} if e.get('type') == 'kept' else e
                                        for e in transcript[-1]['events']]
            await asyncio.to_thread(chats.save, db, conversation_id, mode=conversation.mode,
                                    web_search=saved['web_search'], model=conversation.branches[branch]['model'],
                                    history=conversation.history, results=conversation.results, transcript=transcript)
        return JSONResponse({'ok': True, 'branch': branch})

    async def chat_answer(request: Request):
        """The user's reply to a card waiting in the chat: questionnaire answers (or a skip), or
        approval to run agent-written code ({approve, remember})."""
        body = await request.json()
        conversation = conversations.get(str(body.get('conversation', '')))
        future = conversation and conversation.pending.get(str(body.get('request', '')))
        if not future or future.done():
            return JSONResponse({'error': 'this is no longer waiting for an answer'}, status_code=404)
        future.set_result({'skipped': bool(body.get('skipped')), 'answers': body.get('answers'),
                           'approve': bool(body.get('approve')), 'remember': bool(body.get('remember'))})
        return JSONResponse({'ok': True})

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
        model = await catalog.get(str(body.get('model') or catalog.default()))
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

    async def chat_list(request: Request):
        return JSONResponse({'chats': await asyncio.to_thread(chats.list_all, db)})

    async def chat_item(request: Request):
        chat_id = request.path_params['id']
        if request.method == 'DELETE':
            conversation = conversations.get(chat_id)
            if conversation and conversation.lock.locked():
                return JSONResponse({'error': 'wait for the reply to finish'}, status_code=409)
            conversations.pop(chat_id, None)
            if saved := await asyncio.to_thread(chats.get, db, chat_id):  # its Claude plan sessions go too
                await asyncio.to_thread(providers['subscription'].forget_chat, saved['history'])
            await asyncio.to_thread(chats.delete, db, chat_id)
            return JSONResponse({'ok': True})
        if request.method == 'PATCH':
            body = await request.json()
            title = await asyncio.to_thread(chats.set_title, db, chat_id, body.get('title'), by_user=True)
            return JSONResponse({'id': chat_id, 'title': title}) if title else JSONResponse(
                {'error': 'unknown chat or empty title'}, status_code=400)
        chat = await asyncio.to_thread(chats.get, db, chat_id)
        if not chat:
            return JSONResponse({'error': 'unknown chat'}, status_code=404)
        return JSONResponse({k: chat[k] for k in ('id', 'title', 'mode', 'web_search', 'model', 'transcript')})

    async def doc_list(request: Request):
        if request.method == 'GET':
            return JSONResponse({'docs': await asyncio.to_thread(docs.list_all, db)})
        body = await request.json()
        return JSONResponse(await asyncio.to_thread(docs.create, db, body.get('title')))

    async def doc_item(request: Request):
        doc_id = int(request.path_params['id'])
        if request.method == 'DELETE':
            await asyncio.to_thread(docs.delete, db, doc_id)
            return JSONResponse({'ok': True})
        if request.method == 'PUT':
            body = await request.json()
            try:
                saved = await asyncio.to_thread(docs.save, db, doc_id, title=body.get('title'),
                                                content=body.get('content'), threads=body.get('threads'))
            except ValueError as exc:
                return JSONResponse({'error': str(exc)}, status_code=400)
        else:
            saved = await asyncio.to_thread(docs.get, db, doc_id)
        return JSONResponse(saved) if saved else JSONResponse({'error': 'unknown document'}, status_code=404)

    async def doc_image_upload(request: Request):
        data = await request.body()
        mime = request.headers.get('content-type', '').split(';')[0].strip().lower()
        try:
            key = await asyncio.to_thread(docs.add_image, db, int(request.path_params['id']), mime, data)
        except LookupError as exc:
            return JSONResponse({'error': str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)
        return JSONResponse({'url': f'/api/docs/images/{key}'})

    async def doc_image(request: Request):
        image = await asyncio.to_thread(docs.get_image, db, request.path_params['key'])
        if image is None:
            return JSONResponse({'error': 'unknown image'}, status_code=404)
        return Response(image[1], media_type=image[0], headers={'Cache-Control': 'private, max-age=31536000, immutable',
                                                                'X-Content-Type-Options': 'nosniff'})

    async def doc_block(request: Request):
        """A table/chart snapshot for a document: new blocks from chat results, and Refresh."""
        body = await request.json()
        sql = str(body.get('sql') or '').strip()
        if not sql:
            return JSONResponse({'error': 'sql is required'}, status_code=400)
        attrs = await asyncio.to_thread(docs.snapshot, db, sql, body.get('plot_spec') or None, str(body.get('title') or ''))
        if isinstance(attrs, str):
            return JSONResponse({'error': attrs}, status_code=400)
        return JSONResponse(attrs)

    async def doc_ask(request: Request):
        """nembot tagged on a highlighted passage: stream its thread reply and suggestions."""
        body = await request.json()
        request_text = str(body.get('request', '')).strip()
        blocks = [b for b in body.get('blocks') or [] if isinstance(b, dict) and isinstance(b.get('id'), str)
                  and isinstance(b.get('text'), str)]
        if not request_text:
            return JSONResponse({'error': 'request is required'}, status_code=400)
        model = await catalog.get(str(body.get('model') or catalog.default()))
        if model is None or not model.tools or not model.available:
            return JSONResponse({'error': 'choose a model that can use tools'}, status_code=400)
        messages = [m for m in body.get('messages') or [] if isinstance(m, dict)]
        text = docs.ask_prompt(docs.clean_title(body.get('title')), blocks, str(body.get('quote', '')),
                               body.get('quote_block') if isinstance(body.get('quote_block'), str) else None,
                               messages, request_text)
        conversation = Conversation(mode='doc')
        session = docs.DocSession(db, blocks)

        async def events():
            yield f"data: {json.dumps({'type': 'model', 'model': public(model)})}\n\n"
            try:
                async for event in run_turn(providers, db, conversation, text, model,
                                            web_search=bool(body.get('web_search')), doc_session=session):
                    if event['type'] not in ('table', 'plot'):  # the thread shows text; data goes in via suggestions
                        yield f'data: {json.dumps(event)}\n\n'
            except Exception as exc:
                logging.getLogger(__name__).exception('document ask failed')
                yield f"data: {json.dumps({'type': 'error', 'message': _api_error(exc)})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        return StreamingResponse(events(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache'})

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

    async def explorer_values(request: Request):
        query = request.query_params
        try:
            result = await asyncio.to_thread(
                explorer.top_values, db, request.path_params['name'].upper(), str(query.get('column', '')),
                filters=json.loads(query.get('filters') or '[]'), search=query.get('q') or None,
                start=explorer.parse_date(query.get('start')), end=explorer.parse_date(query.get('end')))
        except (ValueError, duckdb.Error) as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)
        return JSONResponse(result) if result else JSONResponse({'error': 'unknown table'}, status_code=404)

    async def explorer_summaries(request: Request):
        query = request.query_params
        try:
            result = await asyncio.to_thread(
                explorer.summaries, db, request.path_params['name'].upper(),
                filters=json.loads(query.get('filters') or '[]'), search=query.get('q') or None,
                start=explorer.parse_date(query.get('start')), end=explorer.parse_date(query.get('end')))
        except (ValueError, duckdb.Error) as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)
        return JSONResponse(result) if result else JSONResponse({'error': 'unknown table'}, status_code=404)

    async def about(request: Request):
        from .attribution import COPYRIGHT_URL, CREDIT
        return JSONResponse({'credit': CREDIT, 'copyright_url': COPYRIGHT_URL,
                             'data_model': explorer.catalog.source()})

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
        wrapped = f'nembot.definePlugin({json.dumps(name)}, function (nembot) {{\n{source}\n}});\n'
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

    async def explorer_sql(request: Request):
        """The explorer's SQL editor: one read-only SELECT, first rows only."""
        body = await request.json()
        try:
            return JSONResponse(await asyncio.to_thread(explorer.run_sql, db, str(body.get('sql') or '')))
        except (ValueError, duckdb.Error) as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)

    async def explorer_check(request: Request):
        """Live checking for the SQL editor: DuckDB parses and binds the query without running it."""
        body = await request.json()
        return JSONResponse({'problems': await asyncio.to_thread(explorer.check_sql, db, str(body.get('sql') or ''))})

    async def explorer_functions(request: Request):
        return JSONResponse({'functions': await asyncio.to_thread(explorer.functions, db)},
                            headers={'Cache-Control': 'max-age=3600'})

    async def explorer_schema(request: Request):
        return JSONResponse(await asyncio.to_thread(explorer.schema, db))

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
                           sort=query.get('sort'), descending=query.get('order') == 'desc',
                           filters=json.loads(query.get('filters') or '[]'), search=query.get('q') or None)
        except ValueError as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)
        try:
            result = await asyncio.to_thread(explorer.rows, db, request.path_params['name'].upper(), **options)
        except (ValueError, duckdb.Error) as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)
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
        # Fold the write-ahead log into the database file, so the next start doesn't depend on
        # replaying it (DuckDB 1.5 can fail to replay some logs, e.g. ALTER TABLE on a table
        # whose rows are still only in the log).
        try:
            db.execute('CHECKPOINT')
        except Exception:
            logging.getLogger(__name__).warning('checkpoint on shutdown failed', exc_info=True)

    return Starlette(lifespan=lifespan, routes=[
        Route('/', index),
        Route('/api/models', models),
        Route('/api/setup', setup_status),
        Route('/api/setup/connect', setup_connect, methods=['POST']),
        Route('/api/setup/forget', setup_forget, methods=['POST']),
        Route('/api/setup/settings', setup_settings, methods=['POST']),
        Route('/api/setup/openrouter/start', setup_openrouter_start, methods=['POST']),
        Route('/setup/openrouter/callback', setup_openrouter_callback),
        Route('/api/chat', chat, methods=['POST']),
        Route('/api/chat/decision', chat_decision, methods=['POST']),
        Route('/api/chat/answer', chat_answer, methods=['POST']),
        Route('/api/chat/keep', chat_keep, methods=['POST']),
        Route('/api/chat/stop', chat_stop, methods=['POST']),
        Route('/api/chart', chart, methods=['POST']),
        Route('/api/query', query, methods=['POST']),
        Route('/api/about', about),
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
        Route('/api/chats', chat_list),
        Route('/api/chats/{id}', chat_item, methods=['GET', 'PATCH', 'DELETE']),
        Route('/api/docs', doc_list, methods=['GET', 'POST']),
        Route('/api/docs/block', doc_block, methods=['POST']),
        Route('/api/docs/images/{key}', doc_image),
        Route('/api/docs/{id:int}', doc_item, methods=['GET', 'PUT', 'DELETE']),
        Route('/api/docs/{id:int}/images', doc_image_upload, methods=['POST']),
        Route('/api/docs/{id:int}/ask', doc_ask, methods=['POST']),
        Route('/api/explorer', explorer_overview),
        Route('/api/explorer/tables/{name}', explorer_table),
        Route('/api/explorer/tables/{name}/rows', explorer_rows),
        Route('/api/explorer/tables/{name}/values', explorer_values),
        Route('/api/explorer/tables/{name}/summaries', explorer_summaries),
        Route('/api/explorer/freshness', freshness),
        Route('/api/explorer/sql', explorer_sql, methods=['POST']),
        Route('/api/explorer/schema', explorer_schema),
        Route('/api/explorer/check', explorer_check, methods=['POST']),
        Route('/api/explorer/functions', explorer_functions),
        Route('/api/downloads', download_jobs, methods=['GET', 'POST']),
        Route('/api/downloads/plan/{name}', download_plan),
        Route('/api/downloads/{id:int}', download_job, methods=['POST', 'DELETE']),
        Mount('/static', RevalidatingStaticFiles(directory=STATIC), name='static'),
    ])


def main() -> None:
    # `kill -USR1 <pid>` prints every thread's stack to stderr without stopping the server, to diagnose
    # a stuck download or request (attaching a debugger to a running process is often not allowed).
    faulthandler.register(signal.SIGUSR1, all_threads=True)
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
        parser.error(f'cannot open {args.database} for writing ({exc}). Is another nembot process using it?')
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == '__main__':
    main()
