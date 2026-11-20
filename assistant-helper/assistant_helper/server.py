"""HTTP + SSE surface (see assistant/HELPER-PROTOCOL.md)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from urllib.parse import unquote

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .assistant import Assistant

log = logging.getLogger(__name__)

HEARTBEAT_S = 10.0
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Max-Age": "600",
    # Chromium private-network preflights (page origin ua-content:// -> 127.0.0.1)
    "Access-Control-Allow-Private-Network": "true",
}


class CORSMiddleware:
    """CORS * on every response; OPTIONS preflight answered with 204 for any path/header."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["method"] == "OPTIONS":
            req_headers = dict(scope.get("headers") or [])
            allow_headers = req_headers.get(b"access-control-request-headers", b"*").decode() or "*"
            headers = [(k.lower().encode(), v.encode()) for k, v in CORS_HEADERS.items()]
            headers.append((b"access-control-allow-headers", allow_headers.encode()))
            await send({"type": "http.response.start", "status": 204, "headers": headers})
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_with_cors(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"access-control-allow-origin", b"*"))
                headers.append((b"access-control-allow-headers", b"*"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cors)


async def _json_body(request: Request):
    raw = await request.body()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON body: {e}")


def _bad(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=status)


def create_app(assistant: Assistant, load_models: bool = True, capture: bool = True, debug: bool = False) -> Starlette:
    async def health(request: Request):
        return JSONResponse(assistant.health())

    async def events(request: Request):
        q = assistant.events.subscribe()

        async def stream():
            try:
                yield f"data: {json.dumps({'type': 'status', **assistant.status()})}\n\n"
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_S)
                    except asyncio.TimeoutError:
                        ev = {"type": "status", **assistant.status()}
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            finally:
                assistant.events.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                          "Connection": "keep-alive"})

    async def catalog(request: Request):
        try:
            body = await _json_body(request)
        except ValueError as e:
            return _bad(str(e))
        actions = body.get("actions")
        if not isinstance(actions, list):
            return _bad("expected { actions: [ActionSpec] }")
        assistant.set_catalog(actions)
        log.info("catalog: %d actions", len(assistant.catalog.actions))
        return JSONResponse({"ok": True})

    async def context(request: Request):
        try:
            body = await _json_body(request)
        except ValueError as e:
            return _bad(str(e))
        if not isinstance(body, dict):
            return _bad("expected a SessionSnapshot object")
        assistant.set_snapshot(body)
        return JSONResponse({"ok": True})

    async def listen(request: Request):
        try:
            body = await _json_body(request)
        except ValueError as e:
            return _bad(str(e))
        if assistant.capture is None:
            return JSONResponse({"ok": False, "opened": [], "errors": ["audio capture disabled in this helper"]})
        inputs = body.get("inputs") or []
        enabled = bool(body.get("enabled", True))
        opened, errors = await asyncio.get_running_loop().run_in_executor(
            None, assistant.capture.configure, inputs, enabled)
        return JSONResponse({"ok": not errors, "opened": opened, "errors": errors})

    async def prompt(request: Request):
        try:
            body = await _json_body(request)
        except ValueError as e:
            return _bad(str(e))
        text = str(body.get("text") or "").strip()
        if not text:
            return _bad("text is required")
        pid = assistant.submit_prompt(text)
        return JSONResponse({"ok": True, "planIds": [pid]})

    async def feedback(request: Request):
        try:
            body = await _json_body(request)
        except ValueError as e:
            return _bad(str(e))
        if body.get("outcome") not in ("go", "dismissed", "failed"):
            return _bad("outcome must be go | dismissed | failed")
        assistant.record_feedback(body)
        return JSONResponse({"ok": True})

    async def profile(request: Request):
        return JSONResponse(assistant.profile.snapshot())

    async def put_recipe(request: Request):
        try:
            body = await _json_body(request)
        except ValueError as e:
            return _bad(str(e))
        name = str(body.get("name") or "").strip()
        if not name or not isinstance(body.get("steps"), list):
            return _bad("expected { name, aliases?, steps: [Step], description? }")
        aliases = body.get("aliases") or []
        if not isinstance(aliases, list):
            return _bad("aliases must be an array of strings")
        assistant.profile.put_recipe(name, body["steps"], [str(a) for a in aliases], body.get("description"))
        return JSONResponse({"ok": True})

    async def delete_recipe(request: Request):
        name = unquote(request.path_params["name"])
        found = assistant.profile.delete_recipe(name)
        return JSONResponse({"ok": found, **({} if found else {"error": f"no recipe named {name!r}"})},
                            status_code=200 if found else 404)

    async def debug_utterance(request: Request):
        body = await _json_body(request)
        uid = assistant.inject_utterance(str(body["text"]), str(body.get("input") or "debug"))
        return JSONResponse({"ok": True, "utteranceId": uid})

    async def debug_feed_file(request: Request):
        body = await _json_body(request)
        secs = await asyncio.get_running_loop().run_in_executor(
            None, assistant.feed_file, str(body["path"]), str(body.get("input") or "file"), bool(body.get("realtime")))
        return JSONResponse({"ok": True, "seconds": secs})

    async def debug_reset(request: Request):
        assistant.reset_conversation()
        return JSONResponse({"ok": True})

    debug_routes = [
        Route("/debug/reset", debug_reset, methods=["POST"]),
        Route("/debug/utterance", debug_utterance, methods=["POST"]),
        Route("/debug/feed-file", debug_feed_file, methods=["POST"]),
    ] if debug else []

    @contextlib.asynccontextmanager
    async def lifespan(app):
        await assistant.start(load_models=load_models)
        if capture:
            assistant.start_capture()
        yield
        await assistant.stop()

    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/events", events, methods=["GET"]),
            Route("/catalog", catalog, methods=["POST"]),
            Route("/context", context, methods=["POST"]),
            Route("/listen", listen, methods=["POST"]),
            Route("/prompt", prompt, methods=["POST"]),
            Route("/feedback", feedback, methods=["POST"]),
            Route("/profile", profile, methods=["GET"]),
            Route("/profile/recipe", put_recipe, methods=["POST"]),
            Route("/profile/recipe/{name:path}", delete_recipe, methods=["DELETE"]),
            *debug_routes,
        ],
        lifespan=lifespan,
    )
    app.add_middleware(CORSMiddleware)
    return app
