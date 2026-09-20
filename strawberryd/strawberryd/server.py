"""HTTP intake + websocket server on one port (WIRING.md §2).

  POST /event    {source, app, title, body, urgency}  <- what the hooks hit
  POST /perform  a raw contract blob                  <- curl / tests / future TTS-less callers
  GET  /health
  GET  /ws       the Godot widget connects here and stays connected
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import WSMsgType, web

from .config import Config
from .contract import ContractError, Performance
from .daemon import Daemon
from .events import Event

log = logging.getLogger("strawberryd.http")

DAEMON = web.AppKey("daemon", Daemon)


def create_app(daemon: Daemon) -> web.Application:
    app = web.Application()
    app[DAEMON] = daemon
    app.on_startup.append(_start_daemon)
    app.on_shutdown.append(_close_widgets)
    app.on_cleanup.append(_close_daemon)
    app.add_routes(
        [
            web.get("/health", health),
            web.get("/config", config),
            web.post("/perform", perform),
            web.post("/event", event),
            web.get("/ws", websocket),
        ]
    )
    return app


async def _start_daemon(app: web.Application) -> None:
    await app[DAEMON].start()


async def _close_daemon(app: web.Application) -> None:
    await app[DAEMON].close()


async def _close_widgets(app: web.Application) -> None:
    closed = await app[DAEMON].hub.close_all()
    if closed:
        log.info("closed %d widget socket(s) for shutdown", closed)


def _error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _reject_browsers(request: web.Request) -> None:
    """Local tools only. Browsers always send Origin; Godot, curl, and the doorways never do.

    Without this a web page could POST performances at her, or open /ws and read what
    she is about to say, which will include notification text (WIRING.md §2).
    """
    if "Origin" in request.headers:
        raise web.HTTPForbidden(text=json.dumps({"error": "browser origins are not accepted"}), content_type="application/json")


async def _body(request: web.Request) -> Any:
    _reject_browsers(request)
    # Requiring the JSON content type also forces a CORS preflight, which nobody answers.
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(
            text=json.dumps({"error": "Content-Type must be application/json"}), content_type="application/json"
        )
    try:
        return await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text=json.dumps({"error": "body must be JSON"}), content_type="application/json")


async def health(request: web.Request) -> web.Response:
    daemon = request.app[DAEMON]
    return web.json_response(
        {
            "ok": True,
            "widgets": daemon.hub.count,
            "performed": daemon.performed,
            "uptime_s": round(daemon.uptime, 1),
            "brain": daemon.brain_stats(),
            "speech": daemon.speaker.stats(),
            "rest_state": daemon.rest_state,
        }
    )


async def config(request: web.Request) -> web.Response:
    """The effective settings: defaults merged with the file and env (WIRING.md §15)."""
    _reject_browsers(request)
    return web.json_response(request.app[DAEMON].config.to_dict())


async def perform(request: web.Request) -> web.Response:
    daemon = request.app[DAEMON]
    try:
        performance = Performance.from_dict(await _body(request))
    except ContractError as exc:
        return _error(str(exc))
    sent = await daemon.perform(performance)
    return web.json_response({"sent": sent, "performance": performance.to_dict()})


async def event(request: web.Request) -> web.Response:
    daemon = request.app[DAEMON]
    try:
        incoming = Event.from_dict(await _body(request))
    except ContractError as exc:
        return _error(str(exc))
    performance, sent = await daemon.handle_event(incoming)
    return web.json_response({"sent": sent, "performance": performance.to_dict()})


async def websocket(request: web.Request) -> web.WebSocketResponse:
    _reject_browsers(request)
    daemon = request.app[DAEMON]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    daemon.hub.add(ws)
    log.info("widget connected (%d open)", daemon.hub.count)
    if daemon.rest_state != "idle":
        # Catch the newcomer up: a fresh widget assumes idle, but the music may already be on.
        await ws.send_str(json.dumps({"state": daemon.rest_state}))
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await _on_widget_message(ws, msg.data)
            elif msg.type == WSMsgType.ERROR:
                log.warning("widget socket error: %s", ws.exception())
    finally:
        daemon.hub.discard(ws)
        log.info("widget disconnected (%d open)", daemon.hub.count)
    return ws


async def _on_widget_message(ws: web.WebSocketResponse, raw: str) -> None:
    # The widget introduces itself and pings for liveness; anything else is logged, not acted on.
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.debug("widget sent non-JSON: %r", raw[:200])
        return
    kind = data.get("type") if isinstance(data, dict) else None
    if kind == "hello":
        log.info("widget hello: %s", {k: v for k, v in data.items() if k != "type"})
    elif kind == "ping":
        await ws.send_str('{"type": "pong"}')
    else:
        log.debug("widget message: %s", data)


def run(config: Config) -> None:
    daemon = Daemon(config=config)
    app = create_app(daemon)
    log.info("strawberryd listening on http://%s:%d (ws at /ws); config %s",
             config.daemon.host, config.daemon.port, config.path or "defaults")
    # Short shutdown: widgets are closed explicitly in on_shutdown, nothing else is long-lived.
    web.run_app(app, host=config.daemon.host, port=config.daemon.port, print=None, shutdown_timeout=2.0)
