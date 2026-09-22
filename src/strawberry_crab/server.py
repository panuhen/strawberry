"""HTTP intake + websocket server on one port (WIRING.md §2).

  POST /event    {source, app, title, body, urgency}  <- what the hooks hit
  POST /perform  a raw contract blob                  <- curl / tests / future TTS-less callers
  POST /tempo    a beat estimate                      <- doorways/beat_watch.py (§4c)
  POST /command  {command, value}                     <- the tray, to the widgets (§14)
  POST /listen   the hotkey: listen once (again = stop early)   (§7)
  POST /probe    time each model slot on fixed sentences         <- strawberry doctor --talk
  GET  /health
  GET  /ws       the Godot widget connects here and stays connected
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import signal
from typing import Any

from aiohttp import WSMsgType, web

from . import __version__
from .config import Config
from .contract import ContractError, Performance
from .daemon import Daemon
from .events import Event

log = logging.getLogger("strawberryd.http")

DAEMON = web.AppKey("daemon", Daemon)

# The widget says its version in the hello (WIRING.md §1). A source run says "dev" and is always
# welcome; a released binary on another minor or patch is logged and served; another major is
# refused: the socket is closed with this code and a reason the widget shows in its bubble.
DEV_VERSION = "dev"
CLOSE_VERSION_REFUSED = 4001   # = widget/ws_client.gd CLOSE_VERSION_REFUSED
_VERSION = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?")


def version_verdict(widget: str | None, daemon: str = __version__) -> str:
    """How a widget's version sits with ours: "same", "dev", "minor" (another minor or patch:
    warn), "major" (refuse) or "unknown" (missing or unreadable: warn, serve)."""
    if widget == DEV_VERSION:
        return "dev"
    ours, theirs = _VERSION.match(daemon), _VERSION.match(widget or "")
    if ours is None or theirs is None:
        return "unknown"
    if ours.group(1) != theirs.group(1):
        return "major"
    return "same" if ours.groups("0") == theirs.groups("0") else "minor"


def create_app(daemon: Daemon) -> web.Application:
    app = web.Application()
    app[DAEMON] = daemon
    app.on_startup.append(_start_daemon)
    app.on_shutdown.append(_hold_signals)
    app.on_shutdown.append(_close_widgets)
    app.on_cleanup.append(_close_daemon)
    app.add_routes(
        [
            web.get("/health", health),
            web.get("/config", config),
            web.post("/perform", perform),
            web.post("/event", event),
            web.post("/tempo", tempo),
            web.post("/command", command),
            web.post("/listen", listen),
            web.post("/probe", probe),
            web.get("/ws", websocket),
        ]
    )
    return app


async def _start_daemon(app: web.Application) -> None:
    try:
        await app[DAEMON].start()
    except BaseException:
        # Stopped while the models load (SIGTERM cancels startup): aiohttp runs no cleanup for an
        # app that never started, so the sessions opened so far are closed here.
        await app[DAEMON].close()
        raise


async def _close_daemon(app: web.Application) -> None:
    await app[DAEMON].close()


async def _hold_signals(app: web.Application) -> None:
    """One SIGTERM is enough: ignore the rest while shutting down.

    Stopping the tray unit signals its whole cgroup, and the tray terminates its children as
    well, so the daemon gets SIGTERM twice. aiohttp's handler would raise GracefulExit again in
    the middle of cleanup, cancel it, and leave the Ollama sessions to the garbage collector
    ("Unclosed client session" in the journal). runner.cleanup() removes these handlers at its end.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _already_stopping, sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass     # not the main thread (tests), or no signals on this platform


def _already_stopping(sig: signal.Signals) -> None:
    log.info("%s during shutdown ignored; already stopping", signal.Signals(sig).name)


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
            "widget_versions": daemon.hub.versions,
            "version": __version__,
            "performed": daemon.performed,
            "uptime_s": round(daemon.uptime, 1),
            "brain": daemon.brain_stats(),
            "speech": daemon.speaker.stats(),
            "voice": daemon.listener.stats() | {"hotwords": len(daemon.vocabulary)},
            "gate": daemon.gate.stats(),
            "tools": daemon.toolbox.stats(),
            "actions": daemon.actor.stats(),
            "thinker": daemon.thinker.stats(),
            "ledger": daemon.ledger.to_list(),
            "state": daemon.current_state(),
            "rest_state": daemon.rest_state,
            "tempo": daemon.fresh_tempo(),
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


TEMPO_FIELDS = {
    "bpm": (30.0, 300.0),
    "period_s": (0.2, 2.0),
    "confidence": (0.0, 1.0),
    "next_beat": (0.0, 1e11),
    "evenness": (0.0, 1.0),
    "low_ratio": (0.0, 1.0),
    "density": (0.0, 60.0),
    "loudness_db": (-120.0, 10.0),
}


def parse_tempo(data: Any) -> dict[str, Any]:
    """Either {"silent": true} or every TEMPO_FIELDS number within range; nothing else."""
    if not isinstance(data, dict):
        raise ContractError("tempo must be an object")
    if data.get("silent") is True:
        return {"silent": True}
    unknown = set(data) - set(TEMPO_FIELDS)
    if unknown:
        raise ContractError(f"unknown tempo fields: {sorted(unknown)}")
    out: dict[str, Any] = {}
    for key, (low, high) in TEMPO_FIELDS.items():
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ContractError(f"tempo.{key} must be a number")
        if not (low <= value <= high):
            raise ContractError(f"tempo.{key} out of range")
        out[key] = float(value)
    return out


# What a widget will act on (widget.gd run_command). Anything else is refused here rather
# than sent on, so a typo in a script does not end up as a push_warning nobody reads.
COMMANDS = {
    "quit": None,            # no value
    "show": None,
    "hide": None,
    "chat": None,
    "sleep_now": None,
    "reset_position": None,
    "skin": str,
    "mute": bool,
    "on_top": bool,
    "hat": bool,
    "quiet": (int, float),   # seconds from now; 0 clears
    "volume": (int, float),  # 0…1
    "sleep_after": (int, float),   # minutes of quiet before she dozes off; 0 = never
}


def parse_command(data: Any) -> dict[str, Any]:
    """{"command": "mute", "value": true} -> the message the widgets receive (WIRING.md §14)."""
    if not isinstance(data, dict):
        raise ContractError("command must be an object")
    name = data.get("command")
    if name not in COMMANDS:
        raise ContractError(f"unknown command {name!r}; one of {sorted(COMMANDS)}")
    unknown = set(data) - {"command", "value"}
    if unknown:
        raise ContractError(f"unknown fields: {sorted(unknown)}")
    expected = COMMANDS[name]
    message: dict[str, Any] = {"command": name}
    if expected is None:
        return message
    value = data.get("value")
    if isinstance(value, bool) != (expected is bool) or not isinstance(value, expected):
        raise ContractError(f"{name} needs a {getattr(expected, '__name__', 'number')} value")
    if name in ("quiet", "sleep_after") and value < 0:
        raise ContractError(f"{name} needs a value >= 0")
    if name == "volume" and not (0.0 <= value <= 1.0):
        raise ContractError("volume must be between 0 and 1")
    message["value"] = value
    return message


async def command(request: web.Request) -> web.Response:
    """The tray's way to the widget: one command, broadcast to every open widget socket."""
    daemon = request.app[DAEMON]
    try:
        message = parse_command(await _body(request))
    except ContractError as exc:
        return _error(str(exc))
    sent = await daemon.hub.send(message)
    log.info("command -> %d widget(s): %s", sent, message)
    return web.json_response({"sent": sent, "command": message})


async def listen(request: web.Request) -> web.Response:
    _reject_browsers(request)
    result = request.app[DAEMON].listen()
    status = 503 if "error" in result else 200
    return web.json_response(result, status=status)


LOOPBACK = ("127.0.0.1", "::1")


async def probe(request: web.Request) -> web.Response:
    """`strawberry doctor --talk`: the daemon's own scripted lines through each slot, timed.

    Takes no text (the sentences are Daemon.PROBE_LINES), performs nothing, and answers only a
    client on this machine even when [daemon] host is not loopback.
    """
    _reject_browsers(request)
    if request.remote not in LOOPBACK:
        return _error("the probe is local only", 403)
    result = await request.app[DAEMON].probe()
    log.info("probe: %s", {slot: [row[slot].get("ms") for row in result["lines"] if slot in row]
                           for slot in ("gate", "voice", "brain", "tts", "whisper")})
    return web.json_response(result)


async def tempo(request: web.Request) -> web.Response:
    daemon = request.app[DAEMON]
    try:
        estimate = parse_tempo(await _body(request))
    except ContractError as exc:
        return _error(str(exc))
    sent = await daemon.set_tempo(estimate)
    return web.json_response({"sent": sent})


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
    fresh = daemon.fresh_tempo()
    if fresh is not None:
        await ws.send_str(json.dumps({"tempo": fresh}))
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await _on_widget_message(daemon, ws, msg.data)
            elif msg.type == WSMsgType.ERROR:
                log.warning("widget socket error: %s", ws.exception())
    finally:
        daemon.hub.discard(ws)
        log.info("widget disconnected (%d open)", daemon.hub.count)
    return ws


async def _on_widget_message(daemon: Daemon, ws: web.WebSocketResponse, raw: str) -> None:
    # The widget introduces itself, pings for liveness, and relays typed sentences ("heard");
    # anything else is logged, not acted on.
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.debug("widget sent non-JSON: %r", raw[:200])
        return
    kind = data.get("type") if isinstance(data, dict) else None
    if kind == "hello":
        log.info("widget hello: %s", {k: v for k, v in data.items() if k != "type"})
        await _check_version(daemon, ws, data.get("version"))
    elif kind == "ping":
        await ws.send_str('{"type": "pong"}')
    elif kind == "heard":
        # Typed into the widget's box: the same funnel as a spoken sentence (§8b). In the background,
        # so the socket keeps answering pings while Qwen thinks (the widget drops a silent socket).
        text = str(data.get("text", "")).strip()
        if text:
            log.info("widget typed: %r", text)
            daemon.background(daemon.handle_event(Event.from_dict({"source": "voice", "title": text})),
                              f"typed {text[:40]!r}")
    else:
        log.debug("widget message: %s", data)


async def _check_version(daemon: Daemon, ws: web.WebSocketResponse, version: Any) -> None:
    version = str(version) if version is not None else None
    verdict = version_verdict(version)
    daemon.hub.set_version(ws, version or "unknown")
    if verdict == "major":
        reason = f"widget {version}, daemon {__version__}"
        log.error("refusing widget %s: its major version differs from strawberryd %s; closing its "
                  "socket (install the matching widget: strawberry widget --fetch)", version, __version__)
        await ws.close(code=CLOSE_VERSION_REFUSED, message=reason.encode())
    elif verdict == "minor":
        log.warning("widget %s and strawberryd %s differ in minor/patch version; serving it "
                    "(`strawberry widget --fetch` gets the matching one)", version, __version__)
    elif verdict == "unknown":
        log.warning("widget did not say a readable version (%r); serving it", version)


def run(config: Config) -> None:
    daemon = Daemon(config=config)
    app = create_app(daemon)
    log.info("strawberryd listening on http://%s:%d (ws at /ws); config %s",
             config.daemon.host, config.daemon.port, config.path or "defaults")
    # Short shutdown: widgets are closed explicitly in on_shutdown, nothing else is long-lived.
    web.run_app(app, host=config.daemon.host, port=config.daemon.port, print=None, shutdown_timeout=2.0)
