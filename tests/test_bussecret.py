"""The per-install bus secret (bussecret.py, PROTOCOL.md §1.4, WIRING.md §19).

Made once, 0600, compared in constant time, never logged; required for every POST but the Brain UI's
API and for a body's input and approvals; a body without it keeps its performances and plain phases;
every first-party client presents it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from strawberry_crab import bussecret, cli, doctor, paths, strawberryd
from strawberry_crab.client import DaemonClient
from strawberry_crab.daemon import Daemon
from strawberry_crab.doorways import beat_watch
from strawberry_crab.events import CannedReactor
from strawberry_crab.server import create_app
from tests.bus import BUS_SECRET, V1_HELLO, connect, trusted
from tests.test_approvals import CARD
from tests.test_confirm import REMOVE, REMOVE_CALL, daemon_over
from tests.test_runs import HELLO_V2, PHASES, JAZZ, say, steps, thinker_daemon, until, v2_sink
from tests.test_thinker import plain_config

ROOT = Path(__file__).resolve().parents[1]
WRONG = "w" * 43


# ----------------------------------------------------------------------------- the file


def test_the_daemon_makes_one_secret_readable_by_the_user_alone(tmp_path):
    where = tmp_path / "state" / "bus-secret"
    made = bussecret.ensure(where)
    assert bussecret.VALID.match(made) and len(made) == 43
    assert bussecret.read(where) == made and bussecret.ensure(where) == made      # kept from then on
    if os.name == "posix":
        assert where.stat().st_mode & 0o777 == 0o600
    assert not [p for p in where.parent.iterdir() if p.name != "bus-secret"]      # no temporary file left
    assert bussecret.ensure(tmp_path / "other") != made                            # random per install


@pytest.mark.linux_only
def test_a_loose_file_is_made_0600_again_and_a_broken_one_replaced(tmp_path, caplog):
    where = tmp_path / "bus-secret"
    made = bussecret.ensure(where)
    where.chmod(0o644)
    with caplog.at_level(logging.WARNING):
        assert bussecret.ensure(where) == made
    assert where.stat().st_mode & 0o777 == 0o600 and "mode 644" in caplog.text
    where.write_text("not a secret\n")
    replaced = bussecret.ensure(where)
    assert replaced != made and bussecret.read(where) == replaced and where.stat().st_mode & 0o777 == 0o600


def test_two_daemons_starting_at_once_agree(tmp_path, monkeypatch):
    where = tmp_path / "bus-secret"
    first = "a" * 43
    real_link = os.link

    def raced(source, target):   # another daemon wrote its secret a moment before this one links
        Path(target).write_text(first + "\n")
        return real_link(source, target)

    monkeypatch.setattr(os, "link", raced)
    assert bussecret.ensure(where) == first


def test_no_file_is_no_secret_and_it_lives_in_the_state_dir(tmp_path):
    assert bussecret.read(tmp_path / "nothing") is None
    assert bussecret.path() == paths.state_dir() / "bus-secret" == paths.bus_secret_file()
    assert bussecret.read() == BUS_SECRET            # conftest's, where the daemon keeps it
    assert bussecret.headers({"A": "b"}) == {"A": "b", bussecret.HEADER: BUS_SECRET}


def test_it_is_compared_in_constant_time(monkeypatch):
    seen = []
    real = bussecret.hmac.compare_digest
    monkeypatch.setattr(bussecret.hmac, "compare_digest", lambda a, b: seen.append((a, b)) or real(a, b))
    assert bussecret.matches(BUS_SECRET, BUS_SECRET) and seen
    assert not bussecret.matches(BUS_SECRET, WRONG) and not bussecret.matches(BUS_SECRET, BUS_SECRET[:-1])
    assert not bussecret.matches(BUS_SECRET, None) and not bussecret.matches(BUS_SECRET, 12)
    assert not bussecret.matches(BUS_SECRET, "") and not bussecret.matches(None, BUS_SECRET)
    assert not bussecret.matches("", "")


def test_the_daemon_without_a_writable_state_dir_lets_nothing_in(monkeypatch, caplog):
    daemon = Daemon(reactor=CannedReactor(), config=plain_config())

    def fail(where=None):
        raise OSError("read-only file system")

    monkeypatch.setattr(bussecret, "ensure", fail)
    with caplog.at_level(logging.ERROR):
        assert daemon.bus_secret() is None
    assert "could not be made" in caplog.text
    assert not bussecret.matches(daemon.bus_secret(), BUS_SECRET)


# ----------------------------------------------------------------------------- HTTP


def quiet_daemon() -> Daemon:
    config = plain_config()
    config.gate.enabled = config.tools.enabled = config.thinker.enabled = config.actions.mpris = False
    config.voice.enabled = False
    return Daemon(reactor=CannedReactor(), config=config)


POSTS = [("/event", {"source": "git", "title": "repo", "body": "x"}), ("/perform", {"state": "idle"}),
         ("/tempo", {"silent": True}), ("/command", {"command": "mute", "value": True}), ("/listen", None),
         ("/probe", {}), ("/ui-token", {})]


@pytest.mark.parametrize("path, body", POSTS)
async def test_every_post_needs_the_secret(aiohttp_client, path, body):
    daemon = quiet_daemon()
    bare = await aiohttp_client(create_app(daemon), headers={})
    for headers, reason in (({}, "no_secret"), ({bussecret.HEADER: WRONG}, "bad_secret"),
                            ({bussecret.HEADER: ""}, "no_secret")):
        response = await bare.post(path, json=body, headers=headers) if body is not None else \
            await bare.post(path, headers=headers)
        assert response.status == 403
        assert (await response.json())["reason"] == reason
    assert daemon.performed == 0 and daemon.ledger.to_list() == []
    right = await bare.post(path, json=body, headers={bussecret.HEADER: BUS_SECRET}) if body is not None else \
        await bare.post(path, headers={bussecret.HEADER: BUS_SECRET})
    assert right.status != 403


async def test_reads_need_no_secret_and_an_unknown_post_is_refused_all_the_same(aiohttp_client):
    bare = await aiohttp_client(create_app(quiet_daemon()), headers={})
    assert (await bare.get("/health")).status == 200 and (await bare.get("/config")).status == 200
    assert (await bare.post("/nowhere", json={})).status == 403


async def test_the_brain_ui_api_runs_on_its_session_not_the_secret(aiohttp_client):
    from tests.test_brainui import sign_in

    daemon = quiet_daemon()
    client = await aiohttp_client(create_app(daemon))
    page = await sign_in(client)                 # /ui-token with the secret, then the cookie
    assert (await client.post("/ui-token", json={}, headers={bussecret.HEADER: ""})).status == 403
    response = await page.post("/ui/api/cancel", {"run_id": "r-1"}, **{bussecret.HEADER: ""})
    assert response.status == 409               # not running: the page's own answer, not a 403


async def test_the_secret_is_never_logged(aiohttp_client, caplog):
    caplog.set_level(logging.DEBUG)
    daemon = quiet_daemon()
    client = await aiohttp_client(create_app(daemon))
    await client.post("/event", json={"source": "git", "title": "repo", "body": "x"})
    await client.post("/perform", json={"state": "idle"}, headers={bussecret.HEADER: WRONG})
    ws = await connect(client, HELLO_V2)
    other = await client.ws_connect("/ws")
    await other.send_str(json.dumps(HELLO_V2 | {"secret": WRONG}))
    await other.send_str("{" + json.dumps(trusted())[1:-2])      # a hello cut short: not JSON
    await other.send_json({"type": "made-up", "secret": BUS_SECRET})
    await asyncio.sleep(0.2)
    for socket_ in (ws, other):
        await socket_.close()
    await daemon.close()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert BUS_SECRET not in text and WRONG not in text
    assert "refused: a wrong bus secret" in text and "without the right bus secret" in text


# ----------------------------------------------------------------------------- the websocket


async def receive_all(ws, seconds: float = 0.4) -> list[dict]:
    out = []
    while True:
        try:
            out.append(json.loads((await ws.receive(timeout=seconds)).data))
        except asyncio.TimeoutError:
            return out


async def test_a_body_without_the_secret_gets_phases_but_no_input_and_no_approvals(aiohttp_client):
    spotify, qwen, daemon, _ = daemon_over([REMOVE_CALL])
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    card = await connect(client, CARD | {"capabilities": {"phases": PHASES, "approvals": True,
                                                          "sends": {"approval": True, "heard": True}}})
    stranger = await client.ws_connect("/ws")
    await stranger.send_str(json.dumps({k: v for k, v in CARD.items() if k != "secret"}
                                       | {"capabilities": {"phases": PHASES, "approvals": True,
                                                           "sends": {"approval": True, "heard": True, "cancel": True}}}))
    welcome = json.loads((await stranger.receive(timeout=1)).data)
    assert welcome["type"] == "welcome" and welcome["trusted"] is False
    assert welcome["accepted"] == {"phases": sorted(PHASES), "cancel": False, "approvals": False, "approval": False}
    assert json.loads((await stranger.receive(timeout=1)).data) == {"type": "input.refused", "ref": "hello",
                                                                     "reason": "no_secret"}
    await receive_all(card)
    await say(client, REMOVE)
    await until(lambda: daemon.approvals.open is not None)
    got = await receive_all(stranger)
    kinds = [m.get("type") for m in got if "type" in m]
    assert "routing" in kinds and "thinking" in kinds and "speaking" in kinds    # the run's phases
    assert not [k for k in kinds if k.startswith("approval")]                    # never the card or its line
    assert any(m.get("state") == "talking" for m in got)                         # performances, as ever
    assert any(m.get("type") == "approval.request" for m in await receive_all(card))
    pending = daemon.approvals.open.approval_id
    for message in ({"type": "approval.answer", "approval_id": pending, "answer": "yes", "hold": True},
                    {"type": "heard", "text": "yes"}, {"type": "poked", "zone": "belly", "level": 1},
                    {"type": "run.cancel", "run_id": daemon.approvals.open.run_id}):
        await stranger.send_json(message)
        refused = json.loads((await stranger.receive(timeout=1)).data)
        assert refused["type"] == "input.refused" and refused["reason"] == "no_secret"
    assert daemon.approvals.open is not None and not spotify.removed and daemon.pokes.said == 0
    await card.send_json({"type": "approval.answer", "approval_id": pending, "answer": "no"})
    await until(lambda: daemon.approvals.open is None and daemon.runs.busy() is None)
    for ws in (card, stranger):
        await ws.close()
    await daemon.close()


async def test_a_wrong_secret_is_refused_as_such(aiohttp_client):
    client, daemon, spotify, qwen, sink, _ = await thinker_daemon(aiohttp_client, ["[happy] Hi."])
    ws = await client.ws_connect("/ws")
    await ws.send_str(json.dumps(HELLO_V2 | {"secret": WRONG}))
    welcome = json.loads((await ws.receive(timeout=1)).data)
    assert welcome["trusted"] is False and welcome["accepted"]["cancel"] is False
    assert json.loads((await ws.receive(timeout=1)).data)["reason"] == "bad_secret"
    await ws.send_json({"type": "heard", "text": JAZZ})
    assert json.loads((await ws.receive(timeout=1)).data) == {"type": "input.refused", "ref": "heard",
                                                               "reason": "bad_secret"}
    assert daemon.ledger.to_list() == []
    health = await (await client.get("/health")).json()
    assert {"protocol": 2, "trusted": False} in [{k: b[k] for k in ("protocol", "trusted")} for b in health["bodies"]]
    await ws.close()
    await daemon.close()


async def test_a_visual_body_that_asks_for_nothing_gated_is_not_scolded(aiohttp_client):
    client, daemon, spotify, qwen, sink, _ = await thinker_daemon(aiohttp_client, ["[happy] Hi."])
    orbs = await client.ws_connect("/ws")
    await orbs.send_json({"type": "hello", "client": "orbs", "version": "dev", "protocol": 2,
                          "body": {"id": "orbs"}, "capabilities": {"phases": PHASES}})
    assert json.loads((await orbs.receive(timeout=1)).data)["type"] == "welcome"
    await say(client, JAZZ)
    got = await receive_all(orbs)
    assert "run.completed" in [m.get("type") for m in got] and "input.refused" not in [m.get("type") for m in got]
    await orbs.close()
    await daemon.close()


async def test_the_v1_bytes_stay_the_same_and_a_v1_body_without_the_secret_cannot_type(aiohttp_client, caplog):
    """test_runs' golden frames, recorded before runs existed, for a v1 body that has no secret and tries to
    type and poke: the same bytes, nothing more (a v1 body gets no typed message), and the input is dropped."""
    caplog.set_level(logging.INFO)
    config = plain_config()
    config.gate.enabled = config.tools.enabled = config.thinker.enabled = config.actions.mpris = False
    daemon = Daemon(reactor=CannedReactor(), config=config)
    client = await aiohttp_client(create_app(daemon))
    ws = await client.ws_connect("/ws")
    other = await client.ws_connect("/ws")
    await other.send_str(json.dumps(HELLO_V2))
    await ws.send_str(json.dumps({"type": "hello", "client": "old-body", "version": "dev"}))
    await ws.send_str(json.dumps({"type": "heard", "text": "say something"}))
    await ws.send_str(json.dumps({"type": "poked", "zone": "belly", "level": 1}))
    await client.post("/perform", json={"state": "talking", "text": "Hello there.", "emotion": "happy"})
    await client.post("/event", json={"source": "voice", "title": "how are you"})
    await client.post("/event", json={"source": "notification", "app": "Chat", "title": "Alex", "body": "lunch?"})
    await client.post("/event", json={"source": "git", "title": "strawberry", "body": "Add runs"})
    await client.post("/command", json={"command": "mute", "value": True})
    await client.post("/perform", json={"state": "dancing"})
    await ws.send_str(json.dumps({"type": "ping"}))
    await ws.send_str(json.dumps({"type": "run.cancel", "run_id": "r-1"}))
    await ws.send_str(json.dumps({"type": "ping"}))
    frames = []
    while True:
        try:
            message = await ws.receive(timeout=0.5)
        except asyncio.TimeoutError:
            break
        frames.append(message.data)
    assert frames == [
        '{"state": "talking", "emotion": "happy", "text": "Hello there."}',
        '{"state": "talking", "emotion": "neutral", "text": "You said: how are you", "reaction": "nod"}',
        '{"state": "talking", "emotion": "neutral", "text": "Chat: Alex", "reaction": "nod"}',
        '{"state": "talking", "emotion": "neutral", "anim": "notify_perk", "text": "Commit in strawberry: Add runs"}',
        '{"command": "mute", "value": true}',
        '{"state": "dancing", "emotion": "neutral"}',
        '{"type": "pong"}',
        '{"type": "pong"}',
    ]
    assert [t["said"] for t in daemon.ledger.to_list()] == ["how are you"]     # the typed line never ran
    assert "heard from a body without the bus secret; refused" in caplog.text
    assert "poked from a body without the bus secret; refused" in caplog.text
    for socket_ in (ws, other):
        await socket_.close()


async def test_a_v1_body_with_the_secret_may_type(aiohttp_client):
    daemon = quiet_daemon()
    client = await aiohttp_client(create_app(daemon))
    ws = await connect(client, V1_HELLO)
    await ws.send_json({"type": "heard", "text": "how are you"})
    reply = await ws.receive_json(timeout=5)
    assert reply["state"] == "talking" and daemon.ledger.to_list()[-1]["said"] == "how are you"
    await ws.close()


async def test_a_sink_without_the_secret_gets_no_approval_events(aiohttp_client):
    spotify, qwen, daemon, _ = daemon_over([REMOVE_CALL])
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    blind = v2_sink(daemon, {k: v for k, v in HELLO_V2.items() if k != "secret"}
                    | {"capabilities": {"phases": PHASES, "approvals": True}})
    seeing = v2_sink(daemon, HELLO_V2 | {"capabilities": {"phases": PHASES, "approvals": True}})
    await say(client, REMOVE)
    await until(lambda: daemon.approvals.open is not None)
    await asyncio.sleep(0.1)
    assert "approval.request" in [m["type"] for m in steps(seeing)]
    assert "approval.request" not in [m["type"] for m in steps(blind)] and steps(blind)
    daemon.approvals.resolve(daemon.approvals.open, "no", "voice")
    await until(lambda: daemon.runs.busy() is None)
    await daemon.close()


# ----------------------------------------------------------------------------- every first-party client


class Captured:
    def __init__(self) -> None:
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        raise urllib.error.URLError("not sent in a test")

    def headers(self) -> list[dict]:
        return [{k.lower(): v for k, v in r.header_items()} for r in self.requests]


@pytest.fixture
def captured(monkeypatch):
    sent = Captured()
    monkeypatch.setattr(urllib.request, "urlopen", sent)
    return sent


def test_the_doorways_and_the_tray_present_it(captured):
    DaemonClient("http://127.0.0.1:1").post_sync("/event", {"source": "git"})
    beat_watch.Watcher("http://127.0.0.1:1", "", 2.0, capture=object()).post({"silent": True})
    assert [h.get(bussecret.HEADER.lower()) for h in captured.headers()] == [BUS_SECRET, BUS_SECRET]


def test_the_cli_presents_it(captured, monkeypatch):
    here = cli.Here(port=1)
    cli.http(here, "POST", "/ui-token", {})
    cli.http(here, "GET", "/health")
    with pytest.raises(urllib.error.URLError):
        cli.post_event("http://127.0.0.1:1", {"source": "git"})
    secrets_sent = [h.get(bussecret.HEADER.lower()) for h in captured.headers()]
    assert secrets_sent == [BUS_SECRET, None, BUS_SECRET]       # a GET needs none


def test_the_hotkeys_bare_listen_presents_it(monkeypatch):
    sent = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def sendall(self, data):
            sent.append(data)

        def recv(self, size):
            return b"HTTP/1.1 200 OK\r\n"

    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: Conn())
    assert cli.listen_fast(1) == 0
    assert f"{bussecret.HEADER}: {BUS_SECRET}\r\n".encode() in sent[0]


def test_doctor_and_strawberryd_talk_present_it(captured, monkeypatch):
    doctor.Probes().http("http://127.0.0.1:1/probe", body={})
    doctor.Probes().http("http://127.0.0.1:1/health")
    headers = captured.headers()
    assert headers[0][bussecret.HEADER.lower()] == BUS_SECRET and bussecret.HEADER.lower() not in headers[1]
    answers = iter(["hello"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers, ""))
    monkeypatch.setattr(urllib.request, "urlopen", Health(captured))
    strawberryd.talk(plain_config())
    posted = [r for r in captured.requests if r.get_method() == "POST"]
    assert posted and {k.lower(): v for k, v in posted[0].header_items()}[bussecret.HEADER.lower()] == BUS_SECRET


class Health:
    """GET /health answers; a POST is captured and refused."""

    def __init__(self, captured: Captured) -> None:
        self.captured = captured

    def __call__(self, request, timeout=None):
        if isinstance(request, str) or request.get_method() == "GET":
            return Reply({"gate": {"ready": False}, "thinker": {"model": None}, "actions": {}})
        return self.captured(request, timeout)


class Reply:
    def __init__(self, data: dict) -> None:
        self.data = json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.data


async def test_the_windows_hook_child_reads_the_file_itself(aiohttp_client, tmp_path):
    """The child Python that posts a git event on Windows (cli.POST_CHILD) gets the file's path on its
    command line, never the secret, and presents it: a real POST to a real daemon."""
    from aiohttp import web

    daemon = quiet_daemon()
    runner = web.AppRunner(create_app(daemon))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    argv = [sys.executable, "-c", cli.POST_CHILD, f"http://127.0.0.1:{port}",
            json.dumps({"source": "git", "app": "post-commit", "title": "repo", "body": "Fix"}),
            str(bussecret.path())]
    assert BUS_SECRET not in " ".join(argv)
    try:
        child = await asyncio.create_subprocess_exec(*argv)
        assert await asyncio.wait_for(child.wait(), 20) == 0
        assert daemon.performed == 1
    finally:
        await runner.cleanup()


def test_the_widget_reads_the_file_and_presents_it_in_its_hello():
    hello = (ROOT / "widget" / "ws_client.gd").read_text()
    widget_paths = (ROOT / "widget" / "paths.gd").read_text()
    assert 'greeting["secret"] = secret' in hello and "Paths.bus_secret()" in hello
    assert 'state_dir().path_join("bus-secret")' in widget_paths
    assert '_xdg("XDG_STATE_HOME", ".local/state")' in widget_paths
    assert 'data_dir().path_join("state")' in widget_paths                     # Windows: as paths.state_dir
    validate = (ROOT / "widget" / "validate_widget.gd").read_text()
    assert "X-Strawberry-Secret" in validate and "trusted" in validate


def test_the_check_scripts_keep_the_secret_out_of_the_users_state_dir():
    phase1 = (ROOT / "scripts" / "check_phase1.sh").read_text()
    assert phase1.index('export "${STATE_ENV[@]}"') < phase1.index('"$BIN/strawberryd" --port')
    for name in ("check_reconnect.sh", "check_tray.sh"):
        assert 'export XDG_STATE_HOME="$(mktemp -d)"' in (ROOT / "scripts" / name).read_text()
