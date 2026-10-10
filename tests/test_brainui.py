"""The Brain UI (brainui.py, WIRING.md §17): the one-time token, the session, the checks that keep
every other web page out (Host, Origin, CSRF), the headers, the sentences only while outcome
logging is on, the page's script putting data in as text, and the actions reaching learning.py."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from strawberry_crab import brainui, bussecret, cli, paths
from strawberry_crab.config import Config, LearningConfig
from strawberry_crab.daemon import Daemon
from strawberry_crab.events import CannedReactor
from strawberry_crab.learning import Learning, LearningError
from strawberry_crab.routefeed import RouteFeed
from strawberry_crab.server import QuietAccessLogger, create_app
from tests.bus import BUS_SECRET
from tests.test_learning import CANARY, teach, world, write_outcomes  # noqa: F401 (world: a fixture)
from tests.test_outcomes import route
from tests.test_thinker import ScriptedGate, bare, plain_config, voice_daemon

UI_DIR = Path(brainui.__file__).parent / "ui"
XSS = '<img src=x onerror=alert(1)>'


def quiet_config(log_outcomes: bool = False) -> Config:
    config = Config()
    config.brain.enabled = config.speech.enabled = config.voice.enabled = config.gate.enabled = False
    config.tools.enabled = config.thinker.enabled = config.actions.mpris = False
    config.learning = LearningConfig(log_outcomes=log_outcomes)
    return config


@pytest.fixture
async def client(aiohttp_client):
    return await aiohttp_client(create_app(Daemon(reactor=CannedReactor(), config=quiet_config())))


def ui_of(client) -> brainui.BrainUI:
    return client.server.app[brainui.UI]


def origin(client) -> str:
    return f"http://127.0.0.1:{client.port}"


class Signed:
    """A signed-in page: its cookie and CSRF token, sent by hand (the test client's own jar is cleared)."""

    def __init__(self, client, cookie: str, csrf: str) -> None:
        self.client, self.cookie, self.csrf = client, cookie, csrf

    def headers(self, csrf: bool = True, origin: str | None = None, **more: str) -> dict[str, str]:
        out = {"Cookie": f"{brainui.COOKIE}={self.cookie}"} | more
        if csrf:
            out[brainui.CSRF_HEADER] = self.csrf
        if origin is not None:
            out["Origin"] = origin
        return out

    async def get(self, path: str, **kw):
        return await self.client.get(path, headers=self.headers(**kw))

    async def post(self, path: str, body: dict | None = None, **kw):
        kw.setdefault("origin", origin(self.client))
        return await self.client.post(path, json=body or {}, headers=self.headers(**kw))


async def new_token(client) -> str:
    response = await client.post("/ui-token", json={})
    assert response.status == 200
    return (await response.json())["token"]


async def sign_in(client) -> Signed:
    token = await new_token(client)
    response = await client.get(f"/ui/login?token={token}", allow_redirects=False)
    assert response.status == 303 and response.headers["Location"] == "/ui"
    cookie = response.cookies[brainui.COOKIE].value
    client.session.cookie_jar.clear()
    page = await client.get("/ui", headers={"Cookie": f"{brainui.COOKIE}={cookie}"})
    assert page.status == 200
    csrf = re.search(r'<meta name="csrf" content="([^"]+)">', await page.text()).group(1)
    return Signed(client, cookie, csrf)


# ----------------------------------------------------------------------------- the token and the session


async def test_a_token_signs_in_once_and_sets_a_strict_http_only_cookie(client):
    token = await new_token(client)
    assert len(token) >= 43                       # 32 random bytes
    first = await client.get(f"/ui/login?token={token}", allow_redirects=False)
    assert first.status == 303 and first.headers["Location"] == "/ui"
    cookie = first.headers["Set-Cookie"]
    assert f"{brainui.COOKIE}=" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert "Path=/ui" in cookie and token not in cookie
    client.session.cookie_jar.clear()
    again = await client.get(f"/ui/login?token={token}", allow_redirects=False)
    assert again.status == 403 and "Set-Cookie" not in again.headers
    assert (await client.get("/ui/login?token=", allow_redirects=False)).status == 403
    assert (await client.get("/ui/login?token=" + "x" * 43, allow_redirects=False)).status == 403


async def test_a_token_expires_after_a_minute_and_a_session_after_twelve_idle_hours(client):
    now = [1000.0]
    ui_of(client).sessions.clock = lambda: now[0]
    token = await new_token(client)
    now[0] += brainui.TOKEN_S + 1
    assert (await client.get(f"/ui/login?token={token}", allow_redirects=False)).status == 403
    signed = await sign_in(client)
    now[0] += brainui.SESSION_IDLE_S - 60
    assert (await signed.get("/ui/api/routes")).status == 200      # a request is activity
    now[0] += brainui.SESSION_IDLE_S - 60
    assert (await signed.get("/ui/api/routes")).status == 200
    now[0] += brainui.SESSION_IDLE_S + 1
    assert (await signed.get("/ui/api/routes")).status == 401
    signed = await sign_in(client)
    for _ in range(int(brainui.SESSION_MAX_S // (brainui.SESSION_IDLE_S / 2)) + 1):
        now[0] += brainui.SESSION_IDLE_S / 2
        await signed.get("/ui/api/routes")
    assert (await signed.get("/ui/api/routes")).status == 401     # a day at most, however busy


async def test_sessions_live_in_memory_and_end_with_the_daemon(aiohttp_client):
    first = await aiohttp_client(create_app(Daemon(reactor=CannedReactor(), config=quiet_config())))
    signed = await sign_in(first)
    second = await aiohttp_client(create_app(Daemon(reactor=CannedReactor(), config=quiet_config())))
    response = await second.get("/ui/api/routes", headers=signed.headers())
    assert response.status == 401


async def test_no_session_is_a_401_page_or_json(client):
    page = await client.get("/ui")
    assert page.status == 401 and "strawberry ui" in await page.text()
    api = await client.get("/ui/api/learning", headers={brainui.CSRF_HEADER: "x"})
    assert api.status == 401 and "error" in await api.json()
    stale = await client.get("/ui/api/learning", headers={"Cookie": f"{brainui.COOKIE}=not-a-session"})
    assert stale.status == 401
    assert (await client.get("/ui/style.css")).status == 200     # no data in it: the sign-in page needs it


async def test_the_token_route_refuses_browsers_and_other_machines(client, monkeypatch):
    assert (await client.post("/ui-token", json={}, headers={"Origin": origin(client)})).status == 403
    assert (await client.post("/ui-token", json={}, headers={"Origin": "https://evil.example"})).status == 403
    monkeypatch.setattr(brainui, "LOOPBACK", ("192.0.2.1",))
    assert (await client.post("/ui-token", json={})).status == 403


async def test_the_ui_answers_this_machine_only(client, monkeypatch):
    signed = await sign_in(client)
    monkeypatch.setattr(brainui, "LOOPBACK", ("192.0.2.1",))
    assert (await signed.get("/ui")).status == 403
    assert (await signed.get("/ui/api/routes")).status == 403


# ----------------------------------------------------------------------------- Host, Origin, CSRF


async def test_a_wrong_host_is_refused_everywhere_under_ui(client):
    signed = await sign_in(client)
    port = client.port
    for host in (f"evil.example:{port}", "127.0.0.1:1", f"127.0.0.1.nip.io:{port}", "", f"localhost:{port}x"):
        assert (await signed.get("/ui", Host=host)).status == 403, host
        assert (await signed.get("/ui/style.css", Host=host)).status == 403, host
        assert (await signed.get("/ui/api/learning", Host=host)).status == 403, host
        assert (await signed.post("/ui/api/rollback", Host=host)).status == 403, host
    token = await new_token(client)
    assert (await client.get(f"/ui/login?token={token}", headers={"Host": f"evil.example:{port}"},
                             allow_redirects=False)).status == 403
    assert (await signed.get("/ui/api/routes", Host=f"localhost:{port}")).status == 200


async def test_a_wrong_origin_is_refused_and_a_post_needs_its_own(client):
    signed = await sign_in(client)
    for bad in ("https://evil.example", f"http://127.0.0.1:{client.port + 1}", "null", f"http://localhost:{client.port}"):
        assert (await signed.get("/ui/api/learning", origin=bad)).status == 403, bad
        assert (await signed.post("/ui/api/rollback", origin=bad)).status == 403, bad
        assert (await signed.get("/ui", origin=bad)).status == 403, bad
    # A browser sends no Origin on a same-origin GET; a POST always carries one.
    assert (await signed.get("/ui/api/routes")).status == 200
    assert (await signed.get("/ui/api/routes", origin=origin(client))).status == 200
    response = await client.post("/ui/api/rollback", json={}, headers=signed.headers())
    assert response.status == 403
    cross = await signed.get("/ui/api/routes", **{"Sec-Fetch-Site": "same-site"})
    assert cross.status == 403


async def test_api_calls_need_the_sessions_csrf_header(client):
    signed = await sign_in(client)
    assert (await signed.post("/ui/api/rollback", csrf=False)).status == 403
    wrong = await client.post("/ui/api/rollback", json={}, headers=signed.headers(csrf=False) | {
        "Origin": origin(client), brainui.CSRF_HEADER: signed.csrf[:-2] + "xx"})
    assert wrong.status == 403
    assert (await signed.get("/ui/api/learning", csrf=False)).status == 403
    assert (await signed.get("/ui/api/events", csrf=False)).status == 403
    other = await sign_in(client)
    mixed = await client.get("/ui/api/routes", headers=signed.headers(csrf=False) | {brainui.CSRF_HEADER: other.csrf})
    assert mixed.status == 403


async def test_a_post_needs_json(client):
    signed = await sign_in(client)
    response = await client.post("/ui/api/review", data="key=a&verdict=reject", headers=signed.headers(origin=origin(client)) | {
        "Content-Type": "application/x-www-form-urlencoded"})
    assert response.status == 415


async def test_every_other_route_still_refuses_any_origin_even_the_uis_own(client):
    signed = await sign_in(client)
    own = origin(client)
    for method, path in (("GET", "/health"), ("GET", "/config"), ("POST", "/event"), ("POST", "/perform"),
                         ("POST", "/command"), ("POST", "/listen"), ("POST", "/tempo"), ("POST", "/probe"),
                         ("POST", "/ui-token"), ("GET", "/nowhere"), ("GET", "/uix"), ("GET", "/ui-token")):
        for value in (own, "https://evil.example"):
            response = await client.request(method, path, json={} if method == "POST" else None,
                                            headers=signed.headers(origin=value))
            assert response.status == 403, (method, path, value)
    ws = await client.get("/ws", headers={"Origin": own, "Connection": "Upgrade", "Upgrade": "websocket"})
    assert ws.status == 403
    dotted = await client.get("/ui/../health", headers={"Origin": own})
    assert dotted.status in (403, 404)


# ----------------------------------------------------------------------------- headers and the page


async def test_every_ui_response_has_the_strict_headers(client):
    signed = await sign_in(client)
    for response, api in ((await signed.get("/ui"), False), (await signed.get("/ui/app.js"), False),
                          (await signed.get("/ui/api/learning"), True), (await client.get("/ui"), False),
                          (await signed.post("/ui/api/rollback"), True)):
        csp = response.headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        if api:
            assert response.headers["Cache-Control"] == "no-store"
    assert (await signed.get("/ui")).headers["Cache-Control"] == "no-store"   # it holds the CSRF token
    js = await signed.get("/ui/app.js")
    assert js.content_type == "text/javascript"


def test_the_page_puts_data_in_as_text_never_as_markup():
    script = (UI_DIR / "app.js").read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                 "setTimeout(\"", "createContextualFragment", "DOMParser"):
        assert sink not in script, sink
    assert "textContent" in script
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    assert re.findall(r"<script[^>]*>", html) == ['<script src="/ui/app.js" defer>']
    assert not re.search(r"\son[a-z]+=", html) and "style=" not in html
    for name in brainui.ASSETS:
        text = (UI_DIR / name).read_text(encoding="utf-8")
        assert "http://" not in text.replace("http://www.w3.org/2000/svg", "") and "https://" not in text, name


# ----------------------------------------------------------------------------- sentences only while logging is on


async def learning_daemon(aiohttp_client, log_outcomes: bool):
    """A daemon whose learning loop has a labelled example and an outcome record, both with the canary."""
    config = plain_config()
    config.learning = LearningConfig(log_outcomes=log_outcomes)
    toolbox, _qwen, thinker = bare([f"[neutral] Fine, {CANARY}."] * 4)
    text = f"hold everything {CANARY}"
    gate = ScriptedGate({text: route(text, kind="request", topic="music", tool="pause")})
    daemon, _ = voice_daemon(config, toolbox, thinker, gate=gate)
    write_outcomes(teach([f"hold everything {CANARY}", f"freeze it {CANARY} {XSS}"]))
    Learning(config).extract()
    client = await aiohttp_client(create_app(daemon))
    await client.post("/event", json={"source": "voice", "title": text})
    return client, daemon


@pytest.mark.parametrize("log_outcomes", [False, True])
async def test_sentences_reach_the_page_only_while_outcome_logging_is_on(aiohttp_client, log_outcomes):
    client, daemon = await learning_daemon(aiohttp_client, log_outcomes)
    signed = await sign_in(client)
    bodies = {}
    for name in ("learning", "routes", "data", "settings", "system"):
        response = await signed.get(f"/ui/api/{name}")
        assert response.status == 200, name
        bodies[name] = await response.text()
    learning = json.loads(bodies["learning"])
    assert learning["logging"] is log_outcomes and learning["examples"] and learning["outcomes"]["recent"]
    routes = json.loads(bodies["routes"])["routes"]
    assert routes and routes[-1]["route"]["tool"] == "pause"
    if log_outcomes:
        assert CANARY in bodies["learning"] and CANARY in bodies["routes"]
        assert any(e.get("text", "").endswith(XSS) for e in learning["examples"])
    else:
        for name, body in bodies.items():
            assert CANARY not in body, name
        assert all("text" not in e for e in learning["examples"])
    for name in ("data", "settings", "system"):
        assert CANARY not in bodies[name], name
    await daemon.close()


async def test_the_live_feed_keeps_no_private_sentence_and_nothing_with_logging_off():
    on = RouteFeed(LearningConfig(log_outcomes=True))
    for text, reading in (("skip this", route("skip this", tool="skip")),
                          ("my code is 482913", route("my code is 482913")),
                          ("tell me a secret", route("tell me a secret", sensitive=0.9)),
                          ("the tv is on", route("the tv is on", kind="other")),
                          ("no reading", route("no reading", sensitive=None))):
        live = on.start("typed")
        on.routed(live, text, reading)
        on.acted(live, "reflex", True, reflex="mpris.skip")
        on.done(live)
    assert [e.get("text") for e in on.recent(with_text=True)] == ["skip this", None, None, None, None]
    assert all("text" not in e for e in on.recent(with_text=False))
    off = RouteFeed(LearningConfig(log_outcomes=False))
    live = off.start("voice")
    off.routed(live, "skip this", route("skip this", tool="skip"))
    off.done(live)
    assert "text" not in off.recent(with_text=True)[0] and off.recent(True)[0]["path"] == "answer"


async def test_no_sentence_or_token_reaches_a_log_line(caplog):
    config = quiet_config(log_outcomes=True)
    toolbox, _qwen, thinker = bare(["[neutral] Fine."] * 4)    # her own lines are logged; the user's are not
    text = f"hold everything {CANARY}"
    daemon, _ = voice_daemon(config, toolbox, thinker, gate=ScriptedGate({text: route(text, tool="pause")}))
    write_outcomes(teach([f"freeze it {CANARY}"]))
    runner = web.AppRunner(create_app(daemon), access_log_class=QuietAccessLogger)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    base = f"http://127.0.0.1:{port}"
    caplog.set_level(logging.DEBUG)
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True),
                                         headers={bussecret.HEADER: BUS_SECRET}) as session:
            await session.post(base + "/event", json={"source": "voice", "title": text})
            token = (await (await session.post(base + "/ui-token", json={})).json())["token"]
            login = await session.get(f"{base}/ui/login?token={token}", allow_redirects=False)
            cookie = login.cookies[brainui.COOKIE].value
            headers = {"Cookie": f"{brainui.COOKIE}={cookie}"}
            page = await (await session.get(base + "/ui", headers=headers)).text()
            csrf = re.search(r'content="([^"]+)">', page.split('name="csrf"')[1]).group(1)
            headers[brainui.CSRF_HEADER] = csrf
            for name in ("learning", "routes", "data", "system"):
                body = await (await session.get(f"{base}/ui/api/{name}", headers=headers)).text()
                assert name != "learning" or CANARY in body
            await session.get(f"{base}/ui/login?token={token}", allow_redirects=False)   # used: a 403, logged
    finally:
        await runner.cleanup()
    lines = [r.getMessage() for r in caplog.records]
    assert not [line for line in lines if CANARY in line or token in line or cookie in line or csrf in line]
    access = [line for line in lines if '"GET /ui' in line]
    assert any('"GET /ui/login" 303' in line for line in access) and any('"GET /ui/login" 403' in line for line in access)
    assert not any("/ui/api/" in line for line in access)      # the page's reads are polling


# ----------------------------------------------------------------------------- the actions call learning.py


class FakeLearning:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def accept(self, version=None):
        self.calls.append(("accept", version))
        return {"from": "shipped", "to": version or "v2"}

    def reject(self, version=None):
        self.calls.append(("reject", version))
        return version or "v2"

    def rollback(self):
        self.calls.append(("rollback",))
        return {"from": "v2", "to": "shipped"}

    def use(self, version):
        self.calls.append(("use", version))
        return {"from": "v2", "to": version}

    def review(self, key, verdict):
        self.calls.append(("review", key, verdict))
        return key != "missing"

    def forget(self, everything=False):
        self.calls.append(("forget", everything))
        return {"examples": 3, "heads": 1 if everything else 0, "caches": 0}

    def signature(self):
        return ()


async def test_accept_reject_rollback_use_review_and_forget_call_the_learning_api(client):
    fake = FakeLearning()
    daemon = client.server.app[brainui.UI].daemon
    daemon.trainer.learning = fake
    followed = []

    async def follow():
        followed.append(True)
        return True

    daemon.trainer.follow_pointer = follow
    signed = await sign_in(client)
    assert (await (await signed.post("/ui/api/accept", {"version": "v2"})).json())["switched"]["to"] == "v2"
    assert (await (await signed.post("/ui/api/reject", {})).json())["rejected"] == "v2"
    assert (await signed.post("/ui/api/rollback")).status == 200
    assert (await signed.post("/ui/api/use", {"version": "shipped"})).status == 200
    assert (await signed.post("/ui/api/review", {"key": "abc123", "verdict": "reject"})).status == 200
    assert (await signed.post("/ui/api/review", {"key": "missing", "verdict": "approve"})).status == 404
    assert (await signed.post("/ui/api/forget", {"confirm": "forget", "everything": True})).status == 200
    assert fake.calls == [("accept", "v2"), ("reject", None), ("rollback",), ("use", "shipped"),
                          ("review", "abc123", "reject"), ("review", "missing", "approve"), ("forget", True)]
    assert len(followed) == 4          # accept, rollback, use and forget move `current`: the gate follows at once
    # Bad input never reaches it.
    for path, body in (("/ui/api/use", {"version": "../../etc/passwd"}), ("/ui/api/use", {}),
                       ("/ui/api/review", {"key": "a b", "verdict": "approve"}),
                       ("/ui/api/review", {"key": "abc", "verdict": "delete"}),
                       ("/ui/api/forget", {"everything": True}), ("/ui/api/forget", {"confirm": "forget", "everything": "yes"})):
        assert (await signed.post(path, body)).status == 400, (path, body)
    assert len(fake.calls) == 7


async def test_a_refusal_from_the_loop_is_a_409_with_its_reason(client):
    signed = await sign_in(client)
    response = await signed.post("/ui/api/accept")
    assert response.status == 409 and (await response.json())["error"] == "no candidate is waiting"
    rollback = await signed.post("/ui/api/rollback")
    assert rollback.status == 409 and "nothing to roll back" in (await rollback.json())["error"]


async def test_train_now_starts_the_daemons_own_run_once(client, monkeypatch):
    daemon = client.server.app[brainui.UI].daemon
    signed = await sign_in(client)
    response = await signed.post("/ui/api/train")
    assert response.status == 409 and (await response.json())["error"] == "the gate is not ready"
    started = []

    async def run(trigger="idle"):
        started.append(trigger)
        await asyncio.sleep(0.2)

    monkeypatch.setattr(daemon.trainer, "run", run)
    monkeypatch.setattr(daemon.gate, "ready", True, raising=False)
    monkeypatch.setattr(daemon.gate, "embedder", object(), raising=False)
    assert (await signed.post("/ui/api/train")).status == 200
    busy = await signed.post("/ui/api/train")
    assert busy.status == 409 and "already" in (await busy.json())["error"]
    await asyncio.sleep(0.25)
    assert started == ["by hand"]


async def test_the_real_loop_through_the_page_accepts_and_rolls_back(aiohttp_client, world):
    """learning.py's own switch, end to end through the API: accept moves `current`, rollback moves it back."""
    from tests.test_learning import config as learning_config, train

    cfg = learning_config()
    write_outcomes(teach(["hold everything", "freeze the music"]))
    loop = Learning(cfg)
    result = await train(loop)
    assert result["status"] == "candidate"
    daemon = Daemon(reactor=CannedReactor(), config=quiet_config())
    daemon.trainer.learning = loop
    client = await aiohttp_client(create_app(daemon))
    signed = await sign_in(client)
    view = await (await signed.get("/ui/api/learning")).json()
    assert view["candidate"]["version"] == result["version"] and view["candidate"]["current"]["fields"]
    accepted = await signed.post("/ui/api/accept", {"version": result["version"]})
    assert accepted.status == 200 and loop.current_version() == result["version"]
    data = await (await signed.get("/ui/api/data")).json()
    assert data["in_use"] == result["version"] and data["chart"][0]["heldout"] is not None
    assert (await signed.post("/ui/api/rollback")).status == 200 and loop.current_version() == "shipped"


# ----------------------------------------------------------------------------- the live stream


async def read_event(response, name: str, timeout: float = 5.0) -> dict:
    async def find() -> dict:
        event = None
        while True:
            line = (await response.content.readline()).decode().rstrip("\n")
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and event == name:
                return json.loads(line[6:])

    return await asyncio.wait_for(find(), timeout)


async def test_the_event_stream_sends_each_route_and_checks_like_the_api(client):
    signed = await sign_in(client)
    assert (await signed.get("/ui/api/events", origin="https://evil.example")).status == 403
    stream = await signed.get("/ui/api/events")
    assert stream.status == 200 and stream.headers["Content-Type"].startswith("text/event-stream")
    assert stream.headers["Cache-Control"] == "no-store"
    await client.post("/event", json={"source": "voice", "title": f"hello {CANARY}"})
    entry = await read_event(stream, "route")
    assert entry["path"] in ("unrouted", "chat") and "text" not in entry
    stream.close()


# ----------------------------------------------------------------------------- `strawberry ui`


def test_strawberry_ui_prints_the_link_when_no_browser_opens(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "daemon_up", lambda here: True)

    def http(here, method, path, body=None, timeout=2.0):
        calls.append((method, path, body))
        return json.dumps({"token": "t0k", "path": "/ui/login?token=t0k"}).encode()

    monkeypatch.setattr(cli, "http", http)
    here = cli.Here(port=8799)
    assert cli.cmd_ui(here, open_browser=False) == 0
    assert calls == [("POST", "/ui-token", {})]
    assert "http://127.0.0.1:8799/ui/login?token=t0k" in capsys.readouterr().out
    monkeypatch.setattr(cli, "launch_browser", lambda url: False)
    assert cli.cmd_ui(here) == 0 and "token=t0k" in capsys.readouterr().out
    opened = []
    monkeypatch.setattr(cli, "launch_browser", lambda url: opened.append(url) or True)
    assert cli.cmd_ui(here) == 0 and opened == ["http://127.0.0.1:8799/ui/login?token=t0k"]
    assert "token" not in capsys.readouterr().out


def test_strawberry_ui_needs_the_daemon_and_a_display(monkeypatch):
    monkeypatch.setattr(cli, "daemon_up", lambda here: False)
    with pytest.raises(cli.CliError, match="strawberryd is down"):
        cli.cmd_ui(cli.Here(port=8799))
    if not paths.windows():
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setattr(cli.sys, "platform", "linux")
        assert cli.launch_browser("http://127.0.0.1:1/ui/login?token=x") is False


def test_the_settings_page_shows_secrets_as_set():
    config = {"tools": {"servers": {"spotify": {"command": "x", "env": {"CLIENT_SECRET": "s3cr3t", "PORT": "1"}}}},
              "thinker": {"api_key": "k", "keep_alive": "30m", "max_tokens": 5}}
    out = brainui.redacted(config)
    assert out["tools"]["servers"]["spotify"]["env"] == {"CLIENT_SECRET": "(set)", "PORT": "(set)"}
    assert out["thinker"] == {"api_key": "(set)", "keep_alive": "30m", "max_tokens": 5}
    assert "s3cr3t" not in json.dumps(out)
