"""The Brain UI (WIRING.md §17): a local page, served by the daemon, that shows what the router
decides and what the learning loop learns, and lets the user act on it. It calls learning.py;
nothing here decides anything.

    POST /ui-token            a one-time login token. Not a browser route: it refuses an Origin
                              (server.local_only) and answers a loopback client only
    GET  /ui/login?token=     swaps the token for a session cookie, then redirects to /ui
    GET  /ui                  the page; /ui/app.js, /ui/style.css, /ui/icon.svg beside it
    GET  /ui/api/learning | routes | data | settings | system
    GET  /ui/api/events       server-sent events: `route` (a sentence routed), `learning` (a file
                              of the loop changed), a comment line as a keep-alive
    POST /ui/api/accept | reject | rollback | use | review | train | forget

Everything else on the port refuses a request with an Origin header, so no web page can reach
it. The routes under /ui are the one place a browser is let in, and only this way:

- the request comes from this machine, and its Host is 127.0.0.1:<port> or localhost:<port>
  (a DNS-rebinding page has its own name in Host);
- the token is 256 bits, single-use and good for TOKEN_S; it is swapped for a session cookie
  (another 256 bits, HttpOnly, SameSite=Strict, Path=/ui) kept in memory only, so a restart ends
  every session, as SESSION_IDLE_S without a request or SESSION_MAX_S in all does;
- an API call needs the session and the session's CSRF token in the X-Strawberry-CSRF header,
  which a page of another origin cannot send without a CORS preflight nobody answers. A POST also
  needs Origin to be exactly this daemon's own origin. Browsers send no Origin on a same-origin
  GET, so a GET needs Origin either absent or exact, and Sec-Fetch-Site, when sent, same-origin;
- every response carries a strict CSP (no inline script, no framing), nosniff, no-referrer, and
  the API's no-store.

Any local process could already talk to the daemon's other routes: the token keeps browsers out,
not other users of this machine (WIRING.md §2). Sentences reach the page only while outcome
logging is on, only through the session's API, and never a log line: the access log leaves out
the query (the token) and the API's successful GETs (server.QuietAccessLogger).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import html
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

from . import privacy, routefeed
from .learning import LearningError, check_version

log = logging.getLogger("strawberryd.ui")

UI_DIR = Path(__file__).parent / "ui"
ASSETS = {"app.js": "text/javascript", "style.css": "text/css", "icon.svg": "image/svg+xml"}
COOKIE = "strawberry_ui"
CSRF_HEADER = "X-Strawberry-CSRF"
TOKEN_S = 60.0
SESSION_IDLE_S = 12 * 3600.0
SESSION_MAX_S = 24 * 3600.0
MAX_TOKENS = 8
MAX_SESSIONS = 16
MAX_STREAMS = 8
STREAM_TICK_S = 2.0
KEEPALIVE_S = 15.0
LOOPBACK = ("127.0.0.1", "::1")
MAX_EXAMPLES = 500
MAX_OUTCOMES = 60

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "font-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}

# A sign-in page with nothing in it: shown for a missing, used or expired token, and to a browser
# without a session. No script, no data.
PLAIN = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Strawberry: Brain</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/ui/style.css">
<link rel="icon" href="/ui/icon.svg" type="image/svg+xml">
</head><body class="plain"><main class="panel"><h1>Brain</h1><p>{message}</p>
<p>Run <code>strawberry ui</code> in a terminal: it opens this page with a fresh link.</p></main></body></html>
"""


@dataclass
class Session:
    id: str
    csrf: str
    created: float = field(default_factory=time.monotonic)
    seen: float = field(default_factory=time.monotonic)


class Sessions:
    """The one-time tokens and the sessions, in memory only."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.tokens: dict[str, float] = {}       # token -> expiry
        self.sessions: list[Session] = []

    def issue(self) -> str:
        now = self.clock()
        self.tokens = {t: exp for t, exp in self.tokens.items() if exp > now}
        while len(self.tokens) >= MAX_TOKENS:
            self.tokens.pop(next(iter(self.tokens)))     # the oldest goes
        token = secrets.token_urlsafe(32)
        self.tokens[token] = now + TOKEN_S
        return token

    def redeem(self, token: str) -> Session | None:
        """A session for a token issued here, unused and unexpired; the token is gone either way."""
        if not token or len(token) > 128:
            return None
        found = None
        for candidate in list(self.tokens):
            if hmac.compare_digest(candidate.encode(), token.encode()):
                found = candidate
        if found is None:
            return None
        expiry = self.tokens.pop(found)
        if expiry <= self.clock():
            return None
        self.prune()
        while len(self.sessions) >= MAX_SESSIONS:
            self.sessions.pop(0)
        session = Session(secrets.token_urlsafe(32), secrets.token_urlsafe(32), self.clock(), self.clock())
        self.sessions.append(session)
        return session

    def find(self, cookie: str | None) -> Session | None:
        if not cookie or len(cookie) > 128:
            return None
        self.prune()
        found = None
        for session in self.sessions:
            if hmac.compare_digest(session.id.encode(), cookie.encode()):
                found = session
        if found is not None:
            found.seen = self.clock()
        return found

    def alive(self, session: Session) -> bool:
        self.prune()
        return session in self.sessions

    def prune(self) -> None:
        now = self.clock()
        self.sessions = [s for s in self.sessions
                         if now - s.seen < SESSION_IDLE_S and now - s.created < SESSION_MAX_S]

    def end(self, session: Session) -> None:
        if session in self.sessions:
            self.sessions.remove(session)


class BrainUI:
    def __init__(self, daemon) -> None:
        self.daemon = daemon
        self.sessions = Sessions()
        self.closing = False
        self.wake: set[asyncio.Event] = set()
        self.streams = 0
        self._sizes: dict[str, Any] | None = None

    @property
    def learning(self):
        return self.daemon.trainer.learning

    @property
    def logging_on(self) -> bool:
        return bool(self.daemon.config.learning.log_outcomes)

    async def close(self, _app: web.Application | None = None) -> None:
        self.closing = True
        for event in list(self.wake):
            event.set()


UI = web.AppKey("brainui", BrainUI)


def setup(app: web.Application, daemon) -> BrainUI:
    ui = BrainUI(daemon)
    app[UI] = ui
    app.on_shutdown.append(ui.close)
    app.add_routes([
        web.post("/ui-token", token),
        web.get("/ui", page),
        web.get("/ui/", page),
        web.get("/ui/login", login),
        web.get(r"/ui/{name:app\.js|style\.css|icon\.svg}", asset),
        web.get("/ui/api/learning", api_learning),
        web.get("/ui/api/routes", api_routes),
        web.get("/ui/api/data", api_data),
        web.get("/ui/api/settings", api_settings),
        web.get("/ui/api/system", api_system),
        web.get("/ui/api/events", api_events),
        web.post("/ui/api/accept", api_accept),
        web.post("/ui/api/reject", api_reject),
        web.post("/ui/api/rollback", api_rollback),
        web.post("/ui/api/use", api_use),
        web.post("/ui/api/review", api_review),
        web.post("/ui/api/train", api_train),
        web.post("/ui/api/forget", api_forget),
    ])
    return ui


# ----------------------------------------------------------------------------- the checks


def own_port(request: web.Request) -> int | None:
    sock = request.transport.get_extra_info("sockname") if request.transport is not None else None
    return sock[1] if sock else None


def own_hosts(request: web.Request) -> set[str]:
    port = own_port(request)
    return {f"127.0.0.1:{port}", f"localhost:{port}"} if port else set()


def secure(response: web.StreamResponse, api: bool) -> web.StreamResponse:
    for name, value in HEADERS.items():
        response.headers[name] = value
    if api:
        response.headers["Cache-Control"] = "no-store"
    return response


def refuse(status: int, message: str, api: bool) -> web.StreamResponse:
    if api:
        response: web.Response = web.json_response({"error": message}, status=status)
    else:
        response = web.Response(text=PLAIN.format(message=html.escape(message)), content_type="text/html", status=status)
        response.headers["Cache-Control"] = "no-store"
    return secure(response, api)


def checked(kind: str):
    """A /ui route: `login`, `asset` (the page's script, style and icon: no data, no session needed, so
    the sign-in page has its style), `page`, `get` or `post` (the API). Sets
    `browser_ok` for server.local_only, which refuses an Origin everywhere else."""
    api = kind in ("get", "post")

    def wrap(handler: Callable[..., Awaitable[web.StreamResponse]]):
        async def guarded(request: web.Request) -> web.StreamResponse:
            ui = request.app[UI]
            if request.remote not in LOOPBACK:
                return refuse(403, "This page answers this machine only.", api)
            host = request.headers.get("Host", "").lower()
            if host not in own_hosts(request):
                return refuse(403, "Wrong host.", api)
            origin = request.headers.get("Origin")
            if origin is not None and origin != f"http://{host}":
                return refuse(403, "Wrong origin.", api)
            if kind == "post" and origin is None:
                return refuse(403, "Wrong origin.", api)
            if kind == "get" and request.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin":
                return refuse(403, "Wrong origin.", api)
            session = None
            if kind not in ("login", "asset"):
                session = ui.sessions.find(request.cookies.get(COOKIE))
                if session is None:
                    return refuse(401, "Not signed in, or the session has ended.", api)
            if api:
                sent = request.headers.get(CSRF_HEADER, "")
                if not hmac.compare_digest(sent.encode(), session.csrf.encode()):
                    return refuse(403, "Missing or wrong CSRF header.", api)
            try:
                response = await handler(request, ui, session)
            except web.HTTPException as exc:
                return refuse(exc.status, exc.text or exc.reason, api)
            if not response.prepared:
                secure(response, api)
            return response

        guarded.browser_ok = True    # type: ignore[attr-defined]
        guarded.__name__ = handler.__name__
        guarded.__doc__ = handler.__doc__
        return guarded

    return wrap


# ----------------------------------------------------------------------------- the token and the page


async def token(request: web.Request) -> web.Response:
    """`strawberry ui`: a one-time login token (server.local_only refuses this to a browser)."""
    if request.remote not in LOOPBACK:
        return web.json_response({"error": "the UI token is for this machine only"}, status=403)
    ui = request.app[UI]
    issued = ui.sessions.issue()
    log.info("ui: a one-time login token issued (good for %.0fs)", TOKEN_S)
    return web.json_response({"token": issued, "expires_s": TOKEN_S, "path": f"/ui/login?token={issued}"},
                             headers={"Cache-Control": "no-store"})


@checked("login")
async def login(request: web.Request, ui: BrainUI, _session: None) -> web.StreamResponse:
    session = ui.sessions.redeem(request.query.get("token", ""))
    if session is None:
        return refuse(403, "This link has been used or has expired.", api=False)
    log.info("ui: signed in (%d session(s))", len(ui.sessions.sessions))
    redirect = web.Response(status=303, headers={"Location": "/ui", "Cache-Control": "no-store"})
    redirect.set_cookie(COOKIE, session.id, path="/ui", httponly=True, samesite="Strict")
    return redirect


@checked("page")
async def page(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    # The session's CSRF token, for the page's own API calls. URL-safe base64, nothing to escape.
    html = html.replace("{{csrf}}", session.csrf)
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


@checked("asset")
async def asset(request: web.Request, ui: BrainUI, _session: None) -> web.Response:
    name = request.match_info["name"]
    if name not in ASSETS:
        raise web.HTTPNotFound()
    body = (UI_DIR / name).read_bytes()
    return web.Response(body=body, content_type=ASSETS[name], headers={"Cache-Control": "no-cache"})


# ----------------------------------------------------------------------------- reading


def candidate_detail(loop, version: str | None) -> dict[str, Any] | None:
    """The candidate waiting against the head it was compared with: the held-out counts, the wrong
    reflexes, the per-field scores and how many sentences are new to it. Counts only."""
    m = loop.manifest(version) if version else None
    if m is None:
        return None
    held = m.get("heldout") or {}

    def score(s: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(s, dict):
            return None
        return {k: s.get(k) for k in ("fields", "strict", "fields_fi", "sensitive", "reflexes", "reflexes_wrong",
                                      "auroc", "per_field")}

    parent = loop.manifest(m.get("parent")) or {}
    learned = set(m.get("learned") or {})
    return {"version": m.get("version"), "created": m.get("created"), "parent": m.get("parent"),
            "compared_with": m.get("compared_with"), "trigger": m.get("trigger"), "passed": m.get("passed"),
            "why": m.get("why") or None, "status": m.get("status"), "counts": m.get("counts"),
            "left_out": m.get("left_out"), "learned": len(learned),
            "new_to_it": len(learned - set(parent.get("learned") or {})),
            "current": score(held.get("current")), "candidate": score(held.get("candidate"))}


def recent_outcomes(path: Path, with_text: bool) -> dict[str, Any]:
    """The outcome log's last records and its counts per signal; the sentence only while logging is on."""
    from .outcomes import read

    if not path.exists():
        return {"records": 0, "signals": {}, "recent": []}
    records = read(path)
    signals: dict[str, int] = {}
    for r in records:
        signals[str(r.get("outcome"))] = signals.get(str(r.get("outcome")), 0) + 1
    rows = []
    for r in reversed(records[-MAX_OUTCOMES:]):
        route = r.get("route") if isinstance(r.get("route"), dict) else {}
        row = {"id": r.get("id"), "ts": r.get("ts"), "source": r.get("source"), "outcome": r.get("outcome"),
               "path": r.get("path"), "reflex": r.get("reflex"), "ok": r.get("ok"), "teacher": r.get("teacher"),
               "follows": (r.get("follows") or {}).get("as") if isinstance(r.get("follows"), dict) else None,
               "after_s": r.get("after_s"),
               "kind": route.get("kind"), "topic": route.get("topic"), "decision": route.get("decision"),
               "tool": route.get("tool"), "confidence": route.get("confidence"),
               "tool_confidence": route.get("tool_confidence")}
        if with_text and isinstance(r.get("text"), str):
            if privacy.pattern(r["text"]):
                row["private"] = True       # the outcome log keeps no private sentence; a file edited by hand might
            else:
                row["text"] = r["text"]
        rows.append(row)
    return {"records": len(records), "signals": signals, "recent": rows}


def learning_view(ui: BrainUI) -> dict[str, Any]:
    loop, with_text = ui.learning, ui.logging_on
    status = loop.status()
    report = loop.report(7.0)
    report["line"] = loop.report_line(report)
    examples = sorted(loop.examples(), key=lambda e: e.last, reverse=True)
    rows = []
    for e in examples[:MAX_EXAMPLES]:
        row = e.summary(text=with_text)
        row["usable"] = e.usable
        rows.append(row)
    return {"logging": with_text, "status": status, "candidate": candidate_detail(loop, (status["candidate"] or {}).get("version")),
            "versions": loop.versions(), "history": (loop.state().get("history") or [])[-40:], "report": report,
            "examples": rows, "examples_total": len(examples),
            "outcomes": recent_outcomes(loop.outcomes_path, with_text)}


def data_view(ui: BrainUI) -> dict[str, Any]:
    from . import gateeval, gatehead

    loop = ui.learning
    if ui._sizes is None:
        base, dataset = gatehead.read_dataset(gatehead.TRAIN_SET)
        phrases, notifications = gateeval.load_set(gatehead.HELDOUT_SET)
        ui._sizes = {"train": len(base), "dataset": dataset, "heldout_sentences": len(phrases),
                     "heldout_notifications": len(notifications)}
    state = loop.state()
    last = state.get("last_train") or {}
    left: dict[str, int] = {}
    for why in (last.get("left_out") or {}).values():
        left[str(why)] = left.get(str(why), 0) + 1
    chart = []
    for m in loop.manifests():
        held = m.get("heldout") or {}
        chart.append({"version": m.get("version"), "at": m.get("created_at"), "status": m.get("status"),
                      "heldout": _pct(((held.get("candidate") or {}).get("fields"))),
                      "strict": _pct(((held.get("candidate") or {}).get("strict"))),
                      "compared": _pct(((held.get("current") or {}).get("fields"))),
                      "compared_with": m.get("compared_with")})
    return {"sets": ui._sizes, "store": loop.store.stats(), "last_train": {
                "at": last.get("at"), "result": last.get("result"), "counts": last.get("counts"), "left_out": left},
            "shipped": loop.heldout_of("shipped"), "in_use": loop.current_version(), "chart": chart}


def _pct(pair: Any) -> float | None:
    from .learning import pct

    return pct(pair) if isinstance(pair, list) and len(pair) == 2 else None


SECRET = re.compile(r"token|secret|password|passwd|api_?key|auth", re.IGNORECASE)


def redacted(value: Any, key: str = "") -> Any:
    """The settings for the page: env values and anything named like a secret shown as set, not as is."""
    if isinstance(value, dict):
        return {k: ("(set)" if (key == "env" or SECRET.search(str(k))) and v not in ("", None, {}, [])
                    and not isinstance(v, (bool, int, float)) else redacted(v, str(k)))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redacted(v, key) for v in value]
    return value


def system_view(ui: BrainUI) -> dict[str, Any]:
    """What runs: the models, where the gate embeds, the head in use. From the same parts /health
    reads, minus everything that can hold a sentence (the ledger, the last route, the last
    transcript, the thinker's last call)."""
    from . import __version__

    d = ui.daemon
    gate = d.gate.stats()
    gate.pop("last_route", None)
    voice = d.listener.stats()
    voice.pop("last_transcript", None)
    thinker = {k: v for k, v in d.thinker.stats().items() if k != "last"}
    trainer = d.trainer.health()
    return {"version": __version__, "uptime_s": round(d.uptime, 1), "widgets": d.hub.count,
            "widget_versions": d.hub.versions, "state": d.current_state(), "gate": gate, "brain": d.brain_stats(),
            "thinker": thinker, "speech": d.speaker.stats(), "voice": voice, "tools": d.toolbox.stats(),
            "learning": d.outcomes.stats() | trainer, "config_path": str(d.config.path) if d.config.path else None}


async def json_of(work: Callable[[], Any]) -> web.Response:
    """The learning loop's file work off the event loop; its refusals ("no candidate is waiting",
    "a training run is already going") as a 409 with the reason."""
    try:
        return web.json_response(await asyncio.to_thread(work))
    except LearningError as exc:
        return web.json_response({"error": str(exc)}, status=409)


@checked("get")
async def api_learning(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    def work() -> dict[str, Any]:
        return learning_view(ui) | {"trainer": ui.daemon.trainer.health().get("trainer")}

    return await json_of(work)


@checked("get")
async def api_routes(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    return web.json_response({"logging": ui.logging_on, "routes": ui.daemon.feed.recent(ui.logging_on)})


@checked("get")
async def api_data(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    return await json_of(lambda: data_view(ui))


@checked("get")
async def api_settings(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    config = ui.daemon.config
    return web.json_response({"path": str(config.path) if config.path else None, "config": redacted(config.to_dict())})


@checked("get")
async def api_system(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    return web.json_response(system_view(ui))


@checked("get")
async def api_events(request: web.Request, ui: BrainUI, session: Session) -> web.StreamResponse:
    """Server-sent events, read-only: each routed sentence as it is done, and a nudge when one of
    the learning loop's files changes (the page then asks for the learning view again)."""
    if ui.streams >= MAX_STREAMS:
        return refuse(429, "Too many open streams.", api=True)
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "X-Accel-Buffering": "no"})
    secure(response, api=True)
    await response.prepare(request)
    feed = ui.daemon.feed
    queue = feed.subscribe()
    wake = asyncio.Event()
    ui.wake.add(wake)
    ui.streams += 1
    signature = ui.learning.signature()
    quiet_since = time.monotonic()
    try:
        await response.write(b"retry: 3000\n\n")
        while not ui.closing and ui.sessions.alive(session):
            getter = asyncio.ensure_future(queue.get())
            waker = asyncio.ensure_future(wake.wait())
            done, _ = await asyncio.wait({getter, waker}, timeout=STREAM_TICK_S, return_when=asyncio.FIRST_COMPLETED)
            waker.cancel()
            if getter in done:
                entry = routefeed.view(getter.result(), ui.logging_on)
                await response.write(_event("route", entry))
                quiet_since = time.monotonic()
            else:
                getter.cancel()
            now = ui.learning.signature()
            if now != signature:
                signature = now
                await response.write(_event("learning", {}))
                quiet_since = time.monotonic()
            if time.monotonic() - quiet_since >= KEEPALIVE_S:
                await response.write(b": keep-alive\n\n")
                quiet_since = time.monotonic()
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        feed.unsubscribe(queue)
        ui.wake.discard(wake)
        ui.streams -= 1
    with contextlib.suppress(ConnectionResetError, RuntimeError):
        await response.write_eof()
    return response


def _event(name: str, data: Any) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


# ----------------------------------------------------------------------------- acting


async def _payload(request: web.Request) -> dict[str, Any]:
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text="Content-Type must be application/json")
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text="body must be JSON") from None
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="body must be an object")
    return data


def _version(data: dict[str, Any], required: bool = False) -> str | None:
    version = data.get("version")
    if version is None and not required:
        return None
    try:
        return check_version(version)
    except LearningError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from None


async def _then_follow(ui: BrainUI, result: dict[str, Any]) -> dict[str, Any]:
    """After a switch: the running gate takes the head `current` names now, not at the next tick."""
    with contextlib.suppress(Exception):
        await ui.daemon.trainer.follow_pointer()
    return result


@checked("post")
async def api_accept(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    version = _version(await _payload(request))
    response = await json_of(lambda: {"switched": ui.learning.accept(version)})
    if response.status == 200:
        await _then_follow(ui, {})
        log.info("ui: candidate accepted")
    return response


@checked("post")
async def api_reject(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    version = _version(await _payload(request))
    response = await json_of(lambda: {"rejected": ui.learning.reject(version)})
    if response.status == 200:
        log.info("ui: candidate rejected")
    return response


@checked("post")
async def api_rollback(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    await _payload(request)
    response = await json_of(lambda: {"switched": ui.learning.rollback()})
    if response.status == 200:
        await _then_follow(ui, {})
        log.info("ui: rolled back")
    return response


@checked("post")
async def api_use(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    version = _version(await _payload(request), required=True)
    response = await json_of(lambda: {"switched": ui.learning.use(version)})
    if response.status == 200:
        await _then_follow(ui, {})
        log.info("ui: head %s put in use", version)
    return response


@checked("post")
async def api_review(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    data = await _payload(request)
    key, verdict = data.get("key"), data.get("verdict")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key):
        raise web.HTTPBadRequest(text="key must be an example's key")
    if verdict not in ("approve", "reject"):
        raise web.HTTPBadRequest(text="verdict must be approve or reject")
    response = await json_of(lambda: {"done": ui.learning.review(key, verdict)})
    if response.status == 200 and not json.loads(response.text or "{}").get("done"):
        return web.json_response({"error": "no such example"}, status=404)
    if response.status == 200:
        log.info("ui: an example %s", "approved" if verdict == "approve" else "rejected")
    return response


@checked("post")
async def api_train(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    await _payload(request)
    started, why = ui.daemon.trainer.train_now()
    if not started:
        return web.json_response({"error": why}, status=409)
    log.info("ui: a training run started by hand")
    return web.json_response({"started": True})


@checked("post")
async def api_forget(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    data = await _payload(request)
    everything = data.get("everything", False)
    if not isinstance(everything, bool) or data.get("confirm") != "forget":
        raise web.HTTPBadRequest(text='forget needs {"confirm": "forget"} and a true or false "everything"')
    response = await json_of(lambda: ui.learning.forget(everything=everything))
    if response.status == 200:
        await _then_follow(ui, {})
        log.info("ui: learning forgotten%s", " with the heads" if everything else "")
    return response
