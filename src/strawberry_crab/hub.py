"""The daemon's end of the widget websocket (WIRING.md §1, PROTOCOL.md).

Normally exactly one Godot widget is connected. A set rather than a single slot keeps
a reconnecting widget, or a second dev instance, from fighting over the connection.

Each socket is a `Body`: protocol 1 until its hello says 2 (PROTOCOL §10). A v1 body gets exactly
the v1 traffic, byte for byte; a v2 body also gets the run events it accepted in `welcome`
(`send_phase`: the phase families, and the approval events when it said `approvals`), and the
performance that answers a run carries that run's id.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from aiohttp import WSCloseCode, web

log = logging.getLogger("strawberryd.hub")

PROTOCOL = 2                     # the highest protocol the brain speaks
# The phase families of PROTOCOL §11b this brain sends. `subagent` is not produced yet, so a body that
# asks for it is not told it gets it. Approvals are their own capability (`approvals`, §13b).
PHASES = ("listening", "routing", "thinking", "tool", "token_rate", "speaking", "run")
MAX_ID = 64


@dataclass(eq=False)
class Body:
    """What one socket said about itself in its hello (nothing yet: v1)."""

    protocol: int = 1
    id: str = ""
    name: str = ""
    client: str = ""
    version: str = ""
    phases: frozenset[str] = frozenset()     # accepted: the families it asked for that we send
    cancel: bool = False                     # accepted: it may send run.cancel
    approvals: bool = False                  # accepted: it shows approval.request / approval.resolved
    answers: bool = False                    # accepted: it may send approval.answer (needs `approvals`)
    asked: frozenset[str] = frozenset()      # which of approvals / answers its hello mentioned (welcome)
    connected: float = field(default_factory=time.monotonic)

    def wants(self, kind: str) -> bool:
        if self.protocol < 2:
            return False
        return self.approvals if family(kind) == "approval" else family(kind) in self.phases

    def accepted_approvals(self) -> dict[str, bool]:
        """`approvals` and `approval` for welcome's `accepted` and /health, when its hello mentioned them
        (a body that did not ask gets the stage 1 shape, unchanged)."""
        out: dict[str, bool] = {}
        if "approvals" in self.asked or "approval" in self.asked:
            out["approvals"] = self.approvals
        if "approval" in self.asked:
            out["approval"] = self.answers
        return out


def family(kind: str) -> str:
    """"tool.started" -> "tool", "run.completed" -> "run", "thinking" -> "thinking"."""
    return kind.split(".", 1)[0]


def _text(value: Any, limit: int = MAX_ID) -> str:
    return "".join(ch for ch in str(value) if ch.isprintable())[:limit] if isinstance(value, str) else ""


def body_from_hello(data: dict[str, Any]) -> Body:
    """A Body from a hello; a hello without `protocol` (or below 2) is v1, whatever else it says."""
    try:
        protocol = int(data.get("protocol", 1))
    except (TypeError, ValueError):
        protocol = 1
    body = Body(protocol=min(max(protocol, 1), PROTOCOL), client=_text(data.get("client")),
                version=_text(data.get("version")))
    if body.protocol < 2:
        return body
    about = data.get("body") if isinstance(data.get("body"), dict) else {}
    body.id, body.name = _text(about.get("id")), _text(about.get("name"))
    capabilities = data.get("capabilities") if isinstance(data.get("capabilities"), dict) else {}
    asked = capabilities.get("phases") if isinstance(capabilities.get("phases"), list) else []
    body.phases = frozenset(p for p in asked if isinstance(p, str) and p in PHASES)
    sends = capabilities.get("sends") if isinstance(capabilities.get("sends"), dict) else {}
    body.cancel = sends.get("cancel") is True
    # Approvals (PROTOCOL §13b): shown with `approvals: true`, answered with `sends.approval` (true, or
    # Part 2's list of ways, e.g. ["click"]). Answering needs showing: the id only comes in a request.
    body.approvals = capabilities.get("approvals") is True
    answer = sends.get("approval")
    body.answers = body.approvals and (answer is True or (isinstance(answer, list) and bool(answer)))
    body.asked = frozenset(k for k, v in (("approvals", capabilities.get("approvals")), ("approval", answer))
                           if v is not None)
    return body


class WidgetHub:
    def __init__(self) -> None:
        self._sockets: set[web.WebSocketResponse] = set()
        self._versions: dict[web.WebSocketResponse, str] = {}   # from each widget's hello
        self._bodies: dict[web.WebSocketResponse, Body] = {}
        self.pump_task: asyncio.Task | None = None

    def set_version(self, ws: web.WebSocketResponse, version: str) -> None:
        self._versions[ws] = version

    @property
    def versions(self) -> list[str]:
        """The versions the open widgets reported in their hello (`/health`, WIRING.md §1)."""
        return sorted(v for ws, v in self._versions.items() if ws in self._sockets and not ws.closed)

    @property
    def count(self) -> int:
        return sum(1 for ws in self._sockets if not ws.closed)

    def add(self, ws: web.WebSocketResponse) -> None:
        self._sockets.add(ws)
        self._bodies.setdefault(ws, Body())

    def discard(self, ws: web.WebSocketResponse) -> None:
        self._sockets.discard(ws)
        self._versions.pop(ws, None)
        self._bodies.pop(ws, None)

    def body(self, ws: web.WebSocketResponse) -> Body:
        return self._bodies.get(ws) or Body()

    def hello(self, ws: web.WebSocketResponse, data: dict[str, Any]) -> Body:
        """Record what the hello says about this socket; a v1 hello leaves it a v1 body."""
        body = body_from_hello(data)
        self._bodies[ws] = body
        return body

    def bodies(self) -> list[dict[str, Any]]:
        """For /health: who is connected and what each accepted."""
        out = []
        for ws in self._sockets:
            if ws.closed:
                continue
            body = self.body(ws)
            row: dict[str, Any] = {"protocol": body.protocol}
            if body.protocol >= 2:
                row |= {"id": body.id, "phases": sorted(body.phases), "cancel": body.cancel} | body.accepted_approvals()
            out.append(row)
        return out

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

    async def send(self, payload: dict[str, Any], run_id: str = "", source: str = "") -> int:
        """Push one JSON object to every open widget. Returns how many received it. `run_id`: the run
        this performance answers, and `source` what started it (voice, typed, notification), added for
        v2 bodies only (a v1 body gets exactly `payload`)."""
        text = json.dumps(payload, ensure_ascii=False)
        tags = {"run_id": run_id} | ({"source": source} if source else {})
        tagged = json.dumps(payload | tags, ensure_ascii=False) if run_id else text
        sent = 0
        for ws in list(self._sockets):
            if ws.closed:
                self._sockets.discard(ws)
                continue
            try:
                await ws.send_str(tagged if self.body(ws).protocol >= 2 else text)
                sent += 1
            except (ConnectionResetError, RuntimeError) as exc:
                log.warning("dropping widget socket: %s", exc)
                self._sockets.discard(ws)
        return sent

    async def send_phase(self, message: dict[str, Any]) -> int:
        """One run event (runs.emit) to every v2 body that accepted its family; v1 bodies never."""
        kind = str(message.get("type", ""))
        text = None
        sent = 0
        for ws in list(self._sockets):
            if ws.closed or not self.body(ws).wants(kind):
                continue
            text = text or json.dumps(message, ensure_ascii=False)
            try:
                await ws.send_str(text)
                sent += 1
            except (ConnectionResetError, RuntimeError) as exc:
                log.warning("dropping widget socket: %s", exc)
                self._sockets.discard(ws)
        return sent

    async def pump(self, queue: asyncio.Queue) -> None:
        """Forward the run book's events (runs.RunBook.subscribe) to the bodies, in order."""
        while True:
            message = await queue.get()
            try:
                await self.send_phase(message)
            except Exception as exc:   # noqa: BLE001 - one bad send must not end the stream
                log.warning("hub: a run event was not sent (%s)", type(exc).__name__)
            finally:
                queue.task_done()   # Daemon.perform waits for these (queue.join) before a performance
