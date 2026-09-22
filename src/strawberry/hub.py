"""The daemon's end of the widget websocket (WIRING.md §1).

Normally exactly one Godot widget is connected. A set rather than a single slot keeps
a reconnecting widget, or a second dev instance, from fighting over the connection.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import WSCloseCode, web

log = logging.getLogger("strawberryd.hub")


class WidgetHub:
    def __init__(self) -> None:
        self._sockets: set[web.WebSocketResponse] = set()

    @property
    def count(self) -> int:
        return sum(1 for ws in self._sockets if not ws.closed)

    def add(self, ws: web.WebSocketResponse) -> None:
        self._sockets.add(ws)

    def discard(self, ws: web.WebSocketResponse) -> None:
        self._sockets.discard(ws)

    async def close_all(self, reason: str = "strawberryd shutting down") -> int:
        """Tell every widget we are going away so it reconnects at once instead of
        waiting for TCP to notice (WIRING.md §1)."""
        closed = 0
        for ws in list(self._sockets):
            self._sockets.discard(ws)
            if ws.closed:
                continue
            try:
                await ws.close(code=WSCloseCode.GOING_AWAY, message=reason.encode())
                closed += 1
            except (ConnectionResetError, RuntimeError):
                pass
        return closed

    async def send(self, payload: dict[str, Any]) -> int:
        """Push one JSON object to every open widget. Returns how many received it."""
        text = json.dumps(payload, ensure_ascii=False)
        sent = 0
        for ws in list(self._sockets):
            if ws.closed:
                self._sockets.discard(ws)
                continue
            try:
                await ws.send_str(text)
                sent += 1
            except (ConnectionResetError, RuntimeError) as exc:
                log.warning("dropping widget socket: %s", exc)
                self._sockets.discard(ws)
        return sent
