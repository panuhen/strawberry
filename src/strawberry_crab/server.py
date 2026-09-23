"""HTTP intake + websocket server on one port (WIRING.md §2).

  POST /event    {source, app, title, body, urgency}  <- what the hooks hit
  POST /perform  a raw contract blob                  <- curl / tests / future TTS-less callers
  POST /tempo    a beat estimate                      <- doorways/beat_watch.py (§4c)
  POST /command  {command, value}                     <- the tray, to the widgets (§14);
                                                         reload_notifications is the daemon's own
  POST /listen   the hotkey: listen once (again = stop early)   (§7)
  POST /probe    time each model slot on fixed sentences         <- strawberry doctor --talk
  GET  /health
  GET  /ws       the Godot widget connects here and stays connected
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
from typing import Any

from aiohttp import WSMsgType, web
from aiohttp.web_log import AccessLogger

from . import __version__, firstrun, paths
from .client import listen_for_stop_request, plain_signal_handler
from .config import Config, ConfigError
from .contract import ContractError, Performance, anim_for
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


# The access log, minus the polling: the tray asks GET /health every 2 s and beat_watch posts
# /tempo every interval_s, and a journal line for each buries everything else. A poll that fails
# (4xx/5xx) is still logged. The line is aiohttp's default (request line, status, size, agent):
# it has no request body in it, and nothing here adds one.
QUIET_ROUTES = frozenset({("GET", "/health"), ("POST", "/tempo")})


class QuietAccessLogger(AccessLogger):
    def log(self, request: web.BaseRequest, response: web.StreamResponse, time: float) -> None:
        if (request.method, request.path) in QUIET_ROUTES and response.status < 400:
            return
        super().log(request, response, time)


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
            "wake": daemon.wake.stats() if daemon.wake else {"watching": False, "resumes": 0, "reason": "off in config"},
            "tools": daemon.toolbox.stats(),
            "actions": daemon.actor.stats(),
            "thinker": daemon.thinker.stats(),
            "ledger": daemon.ledger.to_list(),
            "state": daemon.current_state(),
            "rest_state": daemon.rest_state,
            "tempo": daemon.fresh_tempo(),
            # seconds since beat_watch last posted (None: never), for `strawberry doctor`
            "tempo_age_s": None if daemon.tempo is None else round(time.monotonic() - daemon.tempo_at, 1),
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


# Optional flags: a watcher from before they existed leaves them out, and the widget treats a
# missing flag as the old behaviour.
TEMPO_FLAGS = ("steady",)


def parse_tempo(data: Any) -> dict[str, Any]:
    """Either {"silent": true} or every TEMPO_FIELDS number within range, plus the optional
    boolean TEMPO_FLAGS; nothing else."""
    if not isinstance(data, dict):
        raise ContractError("tempo must be an object")
    if data.get("silent") is True:
        return {"silent": True}
    unknown = set(data) - set(TEMPO_FIELDS) - set(TEMPO_FLAGS)
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
    for key in TEMPO_FLAGS:
        if key in data:
            if not isinstance(data[key], bool):
                raise ContractError(f"tempo.{key} must be true or false")
            out[key] = data[key]
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

# What the daemon does itself instead of sending on (§14): the tray has changed the config file.
DAEMON_COMMANDS = {
    "reload_notifications": None,   # re-read [notifications] (body mode, body_apps, filters)
}


def parse_command(data: Any) -> dict[str, Any]:
    """{"command": "mute", "value": true} -> the message the widgets receive (WIRING.md §14)."""
    if not isinstance(data, dict):
        raise ContractError("command must be an object")
    name = data.get("command")
    known = COMMANDS | DAEMON_COMMANDS
    if name not in known:
        raise ContractError(f"unknown command {name!r}; one of {sorted(known)}")
    unknown = set(data) - {"command", "value"}
    if unknown:
        raise ContractError(f"unknown fields: {sorted(unknown)}")
    expected = known[name]
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
    if message["command"] == "reload_notifications":
        try:
            body = daemon.reload_notifications()
        except ConfigError as exc:
            log.warning("command: [notifications] not reloaded (%s); keeping the settings she has", exc)
            return _error(f"config not reloaded: {exc}", 409)
        log.info("command: [notifications] reloaded, bodies %s", body)
        return web.json_response({"sent": 0, "command": message, "body": body})
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
        if await _check_version(daemon, ws, data.get("version")) != "major":
            _first_run_notice(daemon)
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


def _first_run_notice(daemon: Daemon) -> None:
    """The privacy note in her bubble, once per user: the marker is written before it is sent,
    so two widgets saying hello together do not both get it (firstrun.py)."""
    if not firstrun.pending():
        return
    try:
        firstrun.mark_shown()
    except OSError as exc:
        log.warning("could not write %s (%s); the privacy note will show again next start",
                    paths.privacy_notice_marker(), exc)
    note = Performance(state="talking", anim=anim_for("happy"), text=firstrun.bubble_text(daemon.config),
                       emotion="happy", reaction="wave")
    daemon.background(daemon.perform(note), "first-run privacy note")


async def _check_version(daemon: Daemon, ws: web.WebSocketResponse, version: Any) -> str:
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
    return verdict


def run(config: Config) -> None:
    daemon = Daemon(config=config)
    app = create_app(daemon)
    log.info("strawberryd starting on http://%s:%d; config %s",
             config.daemon.host, config.daemon.port, config.path or "defaults")
    if firstrun.pending():
        log.info("%s", firstrun.log_text(config))
    # The loop is handled the way aiohttp's run_app handles it (asyncio.run would also wait on
    # the default executor before returning); only the signal handling differs, in serve().
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(serve(app, config.daemon.host, config.daemon.port))
    finally:
        try:
            _cancel_all(loop)
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            asyncio.set_event_loop(None)
            loop.close()
    if daemon.listener.load_running:
        # Stopped while whisper downloads: huggingface_hub's own download threads would hold the
        # interpreter's exit until the model is complete, minutes for `medium`. The partial file
        # is resumed on the next start.
        log.info("whisper is still loading; exiting without waiting for it")
        logging.shutdown()
        os._exit(0)


# Short shutdown: widgets are closed explicitly in on_shutdown, nothing else is long-lived.
SHUTDOWN_TIMEOUT_S = 2.0


async def serve(app: web.Application, host: str, port: int) -> None:
    """Serve `app` until SIGTERM or SIGINT, then shut down all the way: on_shutdown, on_cleanup.

    Not web.run_app: its signal handler raises GracefulExit out of the loop, and a second SIGTERM
    read while aiohttp is still closing the listening socket, before any on_shutdown hook of ours
    runs, raises it again in the middle of the cleanup. That is the normal stop under the tray:
    systemd signals the whole cgroup and the tray terminates the daemon a millisecond later. The
    cleanup was cancelled half way and the brain, gate and thinker sessions went to the garbage
    collector ("Unclosed client session"). Here a signal only sets an event, so any number of
    them is one stop (WIRING.md §2).
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def on_signal(sig: signal.Signals) -> None:
        name = signal.Signals(sig).name
        if stop.is_set():
            log.info("%s during shutdown ignored; already stopping", name)
        else:
            log.info("%s: shutting down", name)
        stop.set()

    def on_stop_request() -> None:
        if stop.is_set():
            log.info("stop request during shutdown ignored; already stopping")
        else:
            log.info("stop requested: shutting down")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, on_signal, sig)
        except NotImplementedError:
            plain_signal_handler(loop, sig, on_signal, sig)     # Windows: Ctrl+C
        except (RuntimeError, ValueError):
            pass     # not the main thread (tests)
    if hasattr(signal, "SIGBREAK"):
        plain_signal_handler(loop, signal.SIGBREAK, on_signal, signal.SIGBREAK)   # Windows: Ctrl+Break
    if sys.platform == "win32":
        listen_for_stop_request(loop, on_stop_request)       # Windows: `strawberry stop`, the tray (winproc.py)
    # Left in place until the loop closes: a signal during cleanup is one more no-op, not a kill.

    started = time.monotonic()
    runner = web.AppRunner(app, handle_signals=False, shutdown_timeout=SHUTDOWN_TIMEOUT_S,
                           access_log_class=QuietAccessLogger)
    setup = asyncio.ensure_future(runner.setup())      # on_startup: the models load here
    stopped = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({setup, stopped}, return_when=asyncio.FIRST_COMPLETED)
        if not setup.done():
            # Stopped while the models load: _start_daemon closes what it had opened. aiohttp
            # runs no on_cleanup for an app that never started, so there is nothing else to do.
            setup.cancel()
            await asyncio.gather(setup, return_exceptions=True)
            log.info("stopped during startup")
            return
        setup.result()                                  # a startup error ends the daemon here
        try:
            site = web.TCPSite(runner, host, port)
            await site.start()
            # Only now does the port answer: the parts load first (whisper's in the background).
            log.info("strawberryd listening on http://%s:%d (ws at /ws), %.1fs after the start",
                     host, port, time.monotonic() - started)
            await stopped
        finally:
            await runner.cleanup()
            log.info("shut down; sessions closed")
    finally:
        stopped.cancel()


def _cancel_all(loop: asyncio.AbstractEventLoop) -> None:
    tasks = [task for task in asyncio.all_tasks(loop) if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
