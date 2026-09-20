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

from .contract import ContractError, Performance
from .daemon import Daemon
from .events import Event

log = logging.getLogger("strawberryd.http")

DAEMON = web.AppKey("daemon", Daemon)


def create_app(daemon: Daemon) -> web.Application:
    app = web.Application()
    app[DAEMON] = daemon
    app.add_routes(
        [
            web.get("/health", health),
            web.post("/perform", perform),
            web.post("/event", event),
            web.get("/ws", websocket),
        ]
    )
    return app


def _error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def _body(request: web.Request) -> Any:
    try:
        return await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text=json.dumps({"error": "body must be JSON"}), content_type="application/json")


async def health(request: web.Request) -> web.Response:
    daemon = request.app[DAEMON]
    return web.json_response(
        {"ok": True, "widgets": daemon.hub.count, "performed": daemon.performed, "uptime_s": round(daemon.uptime, 1)}
    )


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
    daemon = request.app[DAEMON]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    daemon.hub.add(ws)
    log.info("widget connected (%d open)", daemon.hub.count)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                _on_widget_message(msg.data)
            elif msg.type == WSMsgType.ERROR:
                log.warning("widget socket error: %s", ws.exception())
    finally:
        daemon.hub.discard(ws)
        log.info("widget disconnected (%d open)", daemon.hub.count)
    return ws


def _on_widget_message(raw: str) -> None:
    # The widget only talks back to introduce itself; anything else is logged, not acted on.
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.debug("widget sent non-JSON: %r", raw[:200])
        return
    if isinstance(data, dict) and data.get("type") == "hello":
        log.info("widget hello: %s", {k: v for k, v in data.items() if k != "type"})
    else:
        log.debug("widget message: %s", data)


def run(host: str, port: int) -> None:
    daemon = Daemon()
    app = create_app(daemon)
    log.info("strawberryd listening on http://%s:%d (ws at /ws)", host, port)
    web.run_app(app, host=host, port=port, print=None)
