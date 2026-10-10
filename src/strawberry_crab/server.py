"""HTTP intake + websocket server on one port (WIRING.md §2).

  POST /event    {source, app, title, body, urgency}  <- what the hooks hit
  POST /perform  a raw contract blob                  <- curl / tests / future TTS-less callers
  POST /tempo    a beat estimate                      <- doorways/beat_watch.py (§4c)
  POST /command  {command, value}                     <- the tray, to the widgets (§14);
                                                         reload_notifications is the daemon's own
  POST /listen   the hotkey: listen once (again = stop early)   (§7)
  POST /probe    time each model slot on fixed sentences         <- strawberry doctor --talk
  GET  /health
  GET  /config   the effective settings
  GET  /ws       the Godot widget connects here and stays connected
  POST /ui-token a one-time login token for the Brain UI      <- strawberry ui
  /ui, /ui/...   the Brain UI (brainui.py)

Every route refuses a request with a browser's Origin header (local_only), except the Brain UI's
under /ui, which make their own, stricter checks (brainui.checked). Every POST but the Brain UI's
API needs the bus secret in the X-Strawberry-Secret header, and so does GET /config; GET /health
without it says only that she is up (`liveness`). A body's hello must present it for input, approvals
and what she says (bussecret.py); without it a body gets the shape of a performance and of the run
phases, never their words (hub.shape).
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

from . import __version__, bodylink, brainui, bussecret, firstrun, paths, pokes
from .client import listen_for_stop_request, plain_signal_handler
from .config import Config, ConfigError
from .contract import ContractError, Performance, anim_for
from .daemon import Daemon
from .events import Event
from .logtext import sentence

log = logging.getLogger("strawberryd.http")

DAEMON = web.AppKey("daemon", Daemon)
REFUSALS = web.AppKey("secret_refusals", dict)   # (path, reason) -> how many, for the journal (_secret_refusal)

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
        if request.path == "/ui" or request.path.startswith("/ui/"):
            # The Brain UI: the page polls and streams, and /ui/login carries the one-time token in
            # its query. A successful API read is not logged; nothing is logged with its query.
            if request.method == "GET" and request.path.startswith("/ui/api/") and response.status < 400:
                return
            self.logger.info('%s "%s %s" %s', request.remote, request.method, request.path, response.status)
            return
        super().log(request, response, time)


def create_app(daemon: Daemon) -> web.Application:
    app = web.Application(middlewares=[local_only])
    app[DAEMON] = daemon
    app[REFUSALS] = {}
    brainui.setup(app, daemon)
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


@web.middleware
async def local_only(request: web.Request, handler) -> web.StreamResponse:
    """Local tools only, on every route but the Brain UI's. Browsers always send Origin; Godot,
    curl, and the doorways never do.

    Without this a web page could POST performances at her, open /ws and read what she is about
    to say, which will include notification text, or read /health, which holds the ledger: the
    user's recent sentences and her replies (WIRING.md §2).
    """
    browser_ok = getattr(request.match_info.handler, "browser_ok", False)
    if "Origin" in request.headers and not browser_ok:
        # The Brain UI's routes (brainui.checked) let a browser in on their own terms: this host,
        # this origin, a session and its CSRF header. Nothing else does.
        raise web.HTTPForbidden(text=json.dumps({"error": "browser origins are not accepted"}), content_type="application/json")
    if request.method not in READ_METHODS and not browser_ok:
        # Everything that is not a read changes what she does or says: it needs the bus secret
        # (bussecret.py), which only a process that can read the user's own files has.
        refusal = _secret_refusal(request)
        if refusal is not None:
            raise web.HTTPForbidden(text=json.dumps(refusal), content_type="application/json")
    return await handler(request)


READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SECRET_ERRORS = {
    "no_secret": f"this needs the bus secret (the {bussecret.HEADER} header, from the file the daemon made)",
    "bad_secret": "the bus secret is wrong (the daemon made a new one? read the file again)",
}


def _secret_refusal(request: web.Request, quiet: bool = False) -> dict[str, str] | None:
    """None when the request carries the bus secret; else the 403's body. Logged once per route and reason
    and then every hundredth time (an old doorway posts every two seconds), never with the value. `quiet`:
    not logged (a liveness poll of /health without the secret is normal: the check scripts' curl)."""
    given = request.headers.get(bussecret.HEADER, "")
    if bussecret.matches(request.app[DAEMON].bus_secret(), given):
        return None
    reason = "bad_secret" if given else "no_secret"
    if quiet:
        return {"error": SECRET_ERRORS[reason], "reason": reason}
    counts = request.app[REFUSALS]
    seen = counts.get((request.path, reason), 0)
    counts[(request.path, reason)] = seen + 1
    if seen % 100 == 0:
        log.warning("%s %s refused: %s (%d so far)", request.method, request.path,
                    "a wrong bus secret" if given else "no bus secret", seen + 1)
    return {"error": SECRET_ERRORS[reason], "reason": reason}


async def _body(request: web.Request) -> Any:
    # Requiring the JSON content type also forces a CORS preflight, which nobody answers.
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(
            text=json.dumps({"error": "Content-Type must be application/json"}), content_type="application/json"
        )
    try:
        return await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text=json.dumps({"error": "body must be JSON"}), content_type="application/json")


# /health without the bus secret: enough to tell that she is up and which version (the check scripts' curl,
# `strawberry status` against a daemon whose secret it cannot read). Everything else in /health can say what
# the user said or did (the ledger, thinker.last, actions.last, the gate's last route), so it needs the
# secret. `withheld` names why the rest is missing (no_secret or bad_secret), so `doctor` can say so.
def liveness(daemon: Daemon) -> dict[str, Any]:
    return {"ok": True, "version": __version__, "uptime_s": round(daemon.uptime, 1), "widgets": daemon.hub.count,
            "state": daemon.current_state()}


async def health(request: web.Request) -> web.Response:
    daemon = request.app[DAEMON]
    refusal = _secret_refusal(request, quiet=not request.headers.get(bussecret.HEADER))
    if refusal is not None:
        return web.json_response(liveness(daemon) | {"withheld": refusal["reason"]})
    return web.json_response(
        {
            "ok": True,
            "widgets": daemon.hub.count,
            "widget_versions": daemon.hub.versions,
            "bodies": daemon.hub.bodies(),
            # what the user points at and holds on a body, and what touches did (bodylink.py): ids and counts
            "input": daemon.body_stats(),
            "runs": daemon.runs.stats(),
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
            "confirm": daemon.confirm_stats(),
            "approvals": daemon.approvals.stats(),
            "thinker": daemon.thinker.stats(),
            "learning": daemon.outcomes.stats() | daemon.trainer.health(),
            "ledger": daemon.ledger.to_list(notices=True),
            # her inbox (inbox.py): counts only, never an app, a sender or a text
            "messages": daemon.inbox.stats() if daemon.inbox is not None else {"enabled": False},
            "state": daemon.current_state(),
            "rest_state": daemon.rest_state,
            "tempo": daemon.fresh_tempo(),
            # seconds since beat_watch last posted (None: never), for `strawberry doctor`
            "tempo_age_s": None if daemon.tempo is None else round(time.monotonic() - daemon.tempo_at, 1),
        }
    )


async def config(request: web.Request) -> web.Response:
    """The effective settings: defaults merged with the file and env (WIRING.md §15). They name the MCP
    servers' commands, their environment and headers (which can hold a token), paths in the user's home, her
    persona and the notification filters, so only a client with the bus secret gets them: 403 otherwise,
    as for a POST."""
    refusal = _secret_refusal(request)
    if refusal is not None:
        raise web.HTTPForbidden(text=json.dumps(refusal), content_type="application/json")
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

# Optional groups (PROTOCOL §4): each comes whole or not at all, so a body never gets half a bar.
# A body that predates them ignores them (the widget's dance_style.gd reads only what it knows).
TEMPO_BAR = {"beats_per_bar": (2, 12), "beat_index": (0, 11), "next_downbeat": (0.0, 1e11),
             "downbeat_confidence": (0.0, 1.0)}
TEMPO_SECTION = {"section": ("steady", "build", "drop", "break"), "section_confidence": (0.0, 1.0),
                 "section_since": (0.0, 1e11)}
TEMPO_INTS = ("beats_per_bar", "beat_index")


def _tempo_group(data: dict[str, Any], group: dict[str, tuple], name: str) -> dict[str, Any]:
    present = [key for key in group if key in data]
    if not present:
        return {}
    if len(present) != len(group):
        raise ContractError(f"tempo {name} fields come together: {sorted(group)}")
    out: dict[str, Any] = {}
    for key, allowed in group.items():
        value = data[key]
        if isinstance(allowed[0], str):
            if value not in allowed:
                raise ContractError(f"tempo.{key} must be one of {list(allowed)}")
        elif key in TEMPO_INTS:
            if isinstance(value, bool) or not isinstance(value, int) or not (allowed[0] <= value <= allowed[1]):
                raise ContractError(f"tempo.{key} must be a whole number from {allowed[0]} to {allowed[1]}")
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not (allowed[0] <= value <= allowed[1]):
            raise ContractError(f"tempo.{key} must be a number from {allowed[0]} to {allowed[1]}")
        out[key] = value if key in TEMPO_INTS or isinstance(value, str) else float(value)
    return out


def parse_tempo(data: Any) -> dict[str, Any]:
    """Either {"silent": true} or every TEMPO_FIELDS number within range, plus the optional
    boolean TEMPO_FLAGS and the optional TEMPO_BAR and TEMPO_SECTION groups; nothing else."""
    if not isinstance(data, dict):
        raise ContractError("tempo must be an object")
    if data.get("silent") is True:
        return {"silent": True}
    unknown = set(data) - set(TEMPO_FIELDS) - set(TEMPO_FLAGS) - set(TEMPO_BAR) - set(TEMPO_SECTION)
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
    bar = _tempo_group(data, TEMPO_BAR, "bar")
    if bar:
        if bar["beat_index"] >= bar["beats_per_bar"]:
            raise ContractError("tempo.beat_index must be less than beats_per_bar")
        # The next downbeat is the beat at next_beat or one of the bar's beats after it (1 ms of rounding).
        ahead = bar["next_downbeat"] - out["next_beat"]
        if not (-0.002 <= ahead <= bar["beats_per_bar"] * out["period_s"] + 0.002):
            raise ContractError("tempo.next_downbeat must be within a bar after next_beat")
    return out | bar | _tempo_group(data, TEMPO_SECTION, "section")


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
    result = request.app[DAEMON].listen()
    status = 503 if "error" in result else 200
    return web.json_response(result, status=status)


LOOPBACK = ("127.0.0.1", "::1")


async def probe(request: web.Request) -> web.Response:
    """`strawberry doctor --talk`: the daemon's own scripted lines through each slot, timed.

    Takes no text (the sentences are Daemon.PROBE_LINES), performs nothing, and answers only a
    client on this machine even when [daemon] host is not loopback.
    """
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
        gone = daemon.hub.body(ws)
        daemon.hub.discard(ws)
        daemon.body_gone(gone)
        log.info("widget disconnected (%d open)", daemon.hub.count)
    return ws


async def _on_widget_message(daemon: Daemon, ws: web.WebSocketResponse, raw: str) -> None:
    # The widget introduces itself, pings for liveness, relays typed sentences ("heard") and, with
    # its "Talk when poked" setting on, pokes ("poked"); a v2 body may also stop the run going on
    # ("run.cancel"). Anything else is logged, not acted on.
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.debug("widget sent non-JSON (%d chars)", len(raw))   # not the text: it may be a hello, secret and all
        return
    kind = data.get("type") if isinstance(data, dict) else None
    if kind == "hello":
        log.info("widget hello: %s", {k: v for k, v in data.items() if k not in ("type", "capabilities", bussecret.FIELD)})
        body = daemon.hub.hello(ws, data, daemon.bus_secret())
        if not body.trusted:
            log.info("widget hello without %s bus secret: the shape of performances and phases only, no words, "
                     "input or approvals",
                     "the right" if body.secret == "wrong" else "the")
        if await _check_version(daemon, ws, data.get("version")) != "major":
            if body.protocol >= 2:
                await ws.send_str(json.dumps(welcome(daemon, body), ensure_ascii=False))
                if not body.trusted and (body.declared or body.secret == "wrong"):
                    # It asked for input or approvals, or sent a secret that is not this install's: say why
                    # it got none (welcome's `accepted` says what it got).
                    await _refuse(ws, "hello", _secret_reason(body))
                # A body that (re)connects while she waits for a yes gets the open approval now, so its
                # card shows (PROTOCOL §13b): the request as it went out, with the time left.
                pending = daemon.approvals.open
                if pending is not None and pending.request is not None and body.approvals:
                    await ws.send_str(json.dumps(pending.request, ensure_ascii=False))
            _first_run_notice(daemon, ws, body)
    elif kind == "ping":
        sent_t = data.get("t")
        if isinstance(sent_t, (int, float)) and not isinstance(sent_t, bool):
            # v2 clock sample (PROTOCOL §12.1): the body's time back, and ours.
            await ws.send_str(json.dumps({"type": "pong", "t": sent_t, "brain_t": round(time.monotonic(), 6)}))
        else:
            await ws.send_str('{"type": "pong"}')
    elif kind == "run.cancel":
        await _cancel_from_body(daemon, ws, data)
    elif kind == "approval.answer":
        await _answer_from_body(daemon, ws, data)
    elif kind == "heard":
        # Typed into the widget's box: the same funnel as a spoken sentence (§8b). In the background,
        # so the socket keeps answering pings while Qwen thinks (the widget drops a silent socket).
        text = str(data.get("text", "")).strip()
        if text and await _untrusted_input(daemon, ws, kind):
            return
        if text:
            log.info("widget typed: %s", sentence(text))
            daemon.background(daemon.handle_event(Event.from_dict({"source": "voice", "title": text})),
                              f"typed {sentence(text)}")
    elif kind == "poked":
        if not await _untrusted_input(daemon, ws, kind):
            _poked(daemon, data)
    elif kind in ("touch", "target"):
        await _body_input(daemon, ws, kind, data)
    else:
        log.debug("widget message of type %r", str(kind)[:32])


def _secret_reason(body) -> str:
    return "bad_secret" if body.secret == "wrong" else "no_secret"


async def _refuse(ws: web.WebSocketResponse, ref: str, reason: str) -> None:
    await ws.send_str(json.dumps({"type": "input.refused", "ref": ref[:32], "reason": reason}))


async def _untrusted_input(daemon: Daemon, ws: web.WebSocketResponse, kind: str) -> bool:
    """True (and the input is dropped) when this socket's hello did not present the bus secret. A v2 body is
    told (`input.refused`, `no_secret` or `bad_secret`); a v1 body only gets the v1 traffic, so it is not,
    and the journal says it instead. Never the text that came with it."""
    body = daemon.hub.body(ws)
    if body.trusted:
        return False
    log.info("%s from a body without the bus secret; refused", kind)
    if body.protocol >= 2:
        await _refuse(ws, kind, _secret_reason(body))
    return True


def _poked(daemon: Daemon, data: dict[str, Any]) -> None:
    """`poked {zone, level}`: the widget was poked with "Talk when poked" on. She may answer with a
    short line of her own (pokes.py), voiced like any other, unless she is busy (a run going on, or
    a state other than her resting one) or has said one lately. The widget did its reaction already."""
    poke = pokes.parse(data)
    if poke is None:
        log.debug("poked: not a poke this daemon knows: %s", data)
        return
    zone, level = poke
    busy = daemon.runs.busy() is not None or daemon.current_state() not in ("idle", "dancing")
    if not daemon.pokes.allowed(busy):
        log.info("poked (%s, level %d): no line (%s)", zone, level, "busy" if busy else "said one lately")
        return
    text, emotion = daemon.pokes.line(zone, level)
    daemon.background(daemon.perform(Performance(state="talking", text=text, emotion=emotion)), "poked")


async def _body_input(daemon: Daemon, ws: web.WebSocketResponse, kind: str, data: dict[str, Any]) -> None:
    """`touch` or `target` (PROTOCOL Part 1c, bodylink.py). Ignored from a v1 body (its bytes never change).
    From a v2 body, in this order: over its rate (touch 20, target 10 a second) dropped and counted, with no
    reply; then refused (`input.refused`, ref the type) without the bus secret, without the capability in its
    hello, or when the message does not check out (unknown_field, bad_value, unknown_entity, not_declared).
    What passes goes to the daemon (Daemon.body_touch, body_target)."""
    body = daemon.hub.body(ws)
    if body.protocol < 2:
        log.debug("%s from a v1 body; ignored", kind)
        return
    if not body.rates[kind].allow():
        body.inputs["dropped"] += 1
        if body.inputs["dropped"] in (1, 10) or body.inputs["dropped"] % 1000 == 0:
            log.info("%s from body %s over its rate; dropped (%d so far)", kind, body.id or "?", body.inputs["dropped"])
        return
    reason = ""
    if not body.trusted:
        reason = _secret_reason(body)
    elif not (body.touch_kinds if kind == "touch" else body.target):
        reason = "not_declared"
    if not reason:
        if kind == "touch":
            parsed, reason = bodylink.parse_touch(data, body.entity_map(), body.touch_kinds)
        else:
            parsed, reason = bodylink.parse_target(data, body.entity_map())
    if reason:
        body.inputs["refused"] += 1
        log.info("%s from body %s refused: %s", kind, body.id or "?", reason)
        await _refuse(ws, kind, reason)
        return
    body.inputs[kind] += 1
    if kind == "touch":
        await daemon.body_touch(body, parsed)
    else:
        await daemon.body_target(body, parsed)


def welcome(daemon: Daemon, body) -> dict[str, Any]:
    """The answer to a v2 hello (PROTOCOL §10): what the brain will send this body."""
    message: dict[str, Any] = {"type": "welcome", "protocol": body.protocol, "brain": __version__,
                               "t": round(time.monotonic(), 6), "rest_state": daemon.rest_state,
                               "accepted": {"phases": sorted(body.phases), "cancel": body.cancel}
                               | body.accepted_approvals() | body.accepted_input(), "trusted": body.trusted}
    if body.id:
        message["body_id"] = body.id
    return message


async def _cancel_from_body(daemon: Daemon, ws: web.WebSocketResponse, data: dict[str, Any]) -> None:
    """`run.cancel {run_id}` (the widget's ✕): only from a v2 body that declared it can, and only for
    the run going on now. It stops that run and nothing else: no line, no new run."""
    body = daemon.hub.body(ws)
    run_id = data.get("run_id")
    if body.protocol >= 2 and not body.trusted:
        await _untrusted_input(daemon, ws, "run.cancel")
        return
    if body.protocol < 2 or not body.cancel:
        log.info("run.cancel from a body that did not declare it; ignored")
        return
    current = daemon.runs.busy()
    if not isinstance(run_id, str) or current is None or current.run_id != run_id:
        log.info("run.cancel for a run that is not the one going on; ignored")
        await ws.send_str(json.dumps({"type": "input.refused", "ref": run_id[:32] if isinstance(run_id, str) else "",
                                      "reason": "not_current"}))
        return
    daemon.runs.cancel(run_id, "stopped")


async def _answer_from_body(daemon: Daemon, ws: web.WebSocketResponse, data: dict[str, Any]) -> None:
    """`approval.answer {approval_id, answer, hold}` (her card's Yes or No): a yes or no to the open
    approval, and nothing else. Ignored from a v1 body (its bytes never change); refused to a v2 body
    that did not declare `sends.approval`, for an id that is not the open one, for one already answered
    (the first answer wins) and for a yes to a hold tier without `hold: true` (approvals.py)."""
    body = daemon.hub.body(ws)
    approval_id = data.get("approval_id")
    ref = approval_id[:32] if isinstance(approval_id, str) else ""
    if body.protocol < 2:
        log.info("approval.answer from a v1 body; ignored")
        return
    if not body.trusted:
        log.info("approval.answer from a body without the bus secret; refused")
        await _refuse(ws, ref, _secret_reason(body))
        return
    reason = "not_declared" if not body.answers else daemon.approvals.answer(
        approval_id, data.get("answer"), "body", hold=data.get("hold") is True)
    if reason is not None:
        if reason == "not_declared":
            log.info("approval.answer from a body that did not declare it; refused")
        await ws.send_str(json.dumps({"type": "input.refused", "ref": ref, "reason": reason}))


def _first_run_notice(daemon: Daemon, ws: web.WebSocketResponse, body) -> None:
    """The privacy note in her bubble, once per user, to a body that shows text: every v1 body, and a
    v2 body whose hello says `speech.bubble` (PROTOCOL §9b). A body that shows no text (the orbs) never
    gets it and never uses it up. It goes to that body alone, and is marked shown once it reached it;
    while one is on its way, a second hello does not start another (firstrun.py). A body without the bus
    secret would get it without its text (hub.shape), so it neither gets it nor uses it up."""
    if not body.shows_text or not body.trusted or daemon.privacy_note or not firstrun.pending():
        return
    daemon.privacy_note = "sending"
    daemon.background(_say_first_run_notice(daemon, ws), "first-run privacy note")


async def _say_first_run_notice(daemon: Daemon, ws: web.WebSocketResponse) -> None:
    note = Performance(state="talking", anim=anim_for("happy"), text=firstrun.bubble_text(daemon.config),
                       emotion="happy", reaction="wave")
    sent = 0
    try:
        sent = await daemon.perform(note, to=(ws,))
    finally:
        daemon.privacy_note = "shown" if sent else ""
    if not sent:
        log.info("the privacy note did not reach the body that said hello; the next one that shows text gets it")
        return
    try:
        firstrun.mark_shown()
    except OSError as exc:
        log.warning("could not write %s (%s); the privacy note will show again next start",
                    paths.privacy_notice_marker(), exc)


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
