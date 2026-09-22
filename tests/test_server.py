import asyncio

import pytest

from strawberry_crab.contract import ONE_SHOTS
from strawberry_crab.daemon import Daemon
from strawberry_crab.server import create_app


@pytest.fixture
def daemon():
    # Plumbing tests run against the canned reactor; the model path has its own tests.
    from strawberry_crab.config import Config
    from strawberry_crab.events import CannedReactor

    config = Config()
    config.brain.enabled = False
    config.speech.enabled = False
    # No whisper, Ollama or MCP servers in plumbing tests; each has its own test file.
    config.voice.enabled = False
    config.gate.enabled = False
    config.tools.enabled = False
    config.thinker.enabled = False
    return Daemon(reactor=CannedReactor(), config=config)


@pytest.fixture
async def client(aiohttp_client, daemon):
    return await aiohttp_client(create_app(daemon))


async def test_health_reports_no_widgets_at_start(client):
    response = await client.get("/health")
    assert response.status == 200
    body = await response.json()
    assert body["ok"] is True
    assert body["widgets"] == 0
    assert body["performed"] == 0
    assert body["brain"] == {"model": None, "canned": True}


async def test_config_endpoint_shows_effective_settings(client):
    response = await client.get("/config")
    assert response.status == 200
    body = await response.json()
    assert body["brain"]["reaction_model"] == "gemma3:1b"
    assert body["daemon"]["port"] == 8770
    assert (await client.get("/config", headers={"Origin": "https://evil.example"})).status == 403


async def test_perform_rejects_bad_blob_with_reason(client):
    response = await client.post("/perform", json={"state": "zoomies"})
    assert response.status == 400
    assert "state must be one of" in (await response.json())["error"]


async def test_perform_rejects_non_json(client):
    response = await client.post("/perform", data=b"not json", headers={"Content-Type": "application/json"})
    assert response.status == 400
    assert (await response.json())["error"] == "body must be JSON"


async def test_perform_requires_json_content_type(client):
    response = await client.post("/perform", data=b'{"state":"idle"}', headers={"Content-Type": "text/plain"})
    assert response.status == 415


async def test_browser_origins_are_refused_on_http(client):
    response = await client.post("/perform", json={"state": "idle"}, headers={"Origin": "https://evil.example"})
    assert response.status == 403


async def test_browser_origins_are_refused_on_websocket(client):
    response = await client.get("/ws", headers={"Origin": "https://evil.example", "Connection": "Upgrade", "Upgrade": "websocket"})
    assert response.status == 403
    assert (await (await client.get("/health")).json())["widgets"] == 0


async def test_perform_without_widget_is_accepted_but_reaches_nobody(client):
    response = await client.post("/perform", json={"state": "thinking"})
    assert response.status == 200
    assert (await response.json())["sent"] == 0


async def test_perform_reaches_connected_widget(client):
    ws = await client.ws_connect("/ws")
    blob = {"state": "talking", "anim": "alert_snap", "text": "Did someone say my name?", "emotion": "alert"}
    response = await client.post("/perform", json=blob)
    assert (await response.json())["sent"] == 1
    assert await ws.receive_json(timeout=2) == blob
    await ws.close()


async def test_widget_connecting_during_music_is_told_to_dance(client):
    await client.post("/perform", json={"state": "dancing"})            # music started, no widget yet
    await client.post("/perform", json={"state": "talking", "text": "Now playing: something"})
    assert (await (await client.get("/health")).json())["rest_state"] == "dancing"
    ws = await client.ws_connect("/ws")
    assert await ws.receive_json(timeout=2) == {"state": "dancing"}     # first thing it hears
    await client.post("/perform", json={"state": "idle"})               # paused
    assert (await ws.receive_json(timeout=2))["state"] == "idle"
    await ws.close()
    late = await client.ws_connect("/ws")                                # idle needs no catch-up
    await client.post("/perform", json={"state": "thinking"})
    assert (await late.receive_json(timeout=2))["state"] == "thinking"
    await late.close()


TEMPO = {"bpm": 128.0, "period_s": 0.469, "confidence": 0.8, "next_beat": 1_800_000_000.5,
         "evenness": 0.7, "low_ratio": 0.45, "density": 3.2, "loudness_db": -18.0}


async def test_tempo_is_forwarded_validated_and_remembered(client):
    ws = await client.ws_connect("/ws")
    assert (await (await client.get("/health")).json())["tempo_age_s"] is None     # beat_watch never posted
    response = await client.post("/tempo", json=TEMPO)
    assert response.status == 200 and (await response.json())["sent"] == 1
    assert await ws.receive_json(timeout=2) == {"tempo": TEMPO}
    health = await (await client.get("/health")).json()
    assert health["tempo"] == TEMPO and 0 <= health["tempo_age_s"] < 5
    late = await client.ws_connect("/ws")                       # a newcomer hears the fresh beat
    assert await late.receive_json(timeout=2) == {"tempo": TEMPO}
    assert (await client.post("/tempo", json={"silent": True})).status == 200
    assert await ws.receive_json(timeout=2) == {"tempo": {"silent": True}}
    for bad in ({"bpm": 128.0}, {**TEMPO, "bpm": 900}, {**TEMPO, "extra": 1}, {**TEMPO, "confidence": "high"}, [1]):
        assert (await client.post("/tempo", json=bad)).status == 400, bad
    await ws.close()
    await late.close()


async def test_health_counts_connected_widget(client):
    ws = await client.ws_connect("/ws")
    await ws.send_json({"type": "hello", "client": "test"})
    assert (await (await client.get("/health")).json())["widgets"] == 1
    await ws.close()


async def test_event_produces_canned_reaction_and_forwards_it(client):
    ws = await client.ws_connect("/ws")
    response = await client.post("/event", json={"source": "git", "title": "strawberry", "body": "Add websocket"})
    assert response.status == 200
    body = await response.json()
    assert body["sent"] == 1
    performance = body["performance"]
    assert performance["state"] == "talking"
    assert performance["anim"] in ONE_SHOTS
    assert "strawberry" in performance["text"] and "Add websocket" in performance["text"]
    assert await ws.receive_json(timeout=2) == performance
    await ws.close()


async def test_critical_notification_reacts_as_alert(client):
    response = await client.post(
        "/event", json={"source": "notification", "app": "Battery", "title": "5% left", "urgency": "CRITICAL"}
    )
    performance = (await response.json())["performance"]
    assert performance["emotion"] == "alert"
    assert performance["anim"] == "alert_snap"
    assert performance["reaction"] == "shiver"


async def test_message_notification_waves_hops_and_carries_the_icon(client, tmp_path):
    icon = tmp_path / "whatsapp.png"
    icon.write_bytes(b"\x89PNG")
    response = await client.post(
        "/event",
        json={"source": "notification", "app": "WhatsApp", "title": "James", "body": "hi", "category": "im.received", "icon": str(icon)},
    )
    performance = (await response.json())["performance"]
    assert performance["reaction"] == "wave"
    assert performance["hop"] is True
    assert performance["icon"] == str(icon)
    assert "anim" not in performance


async def test_pre_push_event_is_a_happy_push_line(client):
    response = await client.post(
        "/event", json={"source": "git", "app": "pre-push", "title": "strawberry", "body": "pushing 2 commits on main to origin"}
    )
    performance = (await response.json())["performance"]
    assert performance["emotion"] == "happy"
    assert performance["text"] == "strawberry: pushing 2 commits on main to origin"


async def test_media_event_is_a_happy_now_playing_line(client):
    response = await client.post("/event", json={"source": "media", "app": "Spotify", "title": "Daft Punk — Around the World"})
    performance = (await response.json())["performance"]
    assert performance["state"] == "talking"
    assert performance["emotion"] == "happy"
    assert performance["reaction"] == "nod" and "anim" not in performance  # she is dancing; a hop would break it
    assert performance["text"] == "Now playing: Daft Punk — Around the World"


async def test_event_requires_source(client):
    response = await client.post("/event", json={"title": "x"})
    assert response.status == 400
    assert "source is required" in (await response.json())["error"]


async def test_widget_ping_gets_a_pong(client):
    ws = await client.ws_connect("/ws")
    await ws.send_json({"type": "ping"})
    assert await ws.receive_json(timeout=2) == {"type": "pong"}
    await ws.close()


async def test_widget_typed_line_takes_the_voice_path(client, daemon):
    ws = await client.ws_connect("/ws")
    await ws.send_json({"type": "heard", "text": "  "})          # blank: ignored
    await ws.send_json({"type": "heard", "text": "hello there"})
    reply = await ws.receive_json(timeout=5)
    assert reply["state"] == "talking" and reply.get("text")
    assert daemon.ledger.to_list()[-1]["said"] == "hello there"   # same funnel as speech: it is in the ledger
    await ws.send_json({"type": "ping"})
    assert await ws.receive_json(timeout=2) == {"type": "pong"}  # nothing else came out of the blank line
    await ws.close()


async def test_shutdown_closes_widgets_with_going_away(aiohttp_client, daemon):
    from aiohttp import WSCloseCode, WSMsgType

    client = await aiohttp_client(create_app(daemon))
    ws = await client.ws_connect("/ws")
    await client.server.app.shutdown()
    msg = await ws.receive(timeout=2)
    assert msg.type == WSMsgType.CLOSE
    assert msg.data == WSCloseCode.GOING_AWAY
    assert daemon.hub.count == 0


async def test_hub_forgets_a_closed_widget(client):
    ws = await client.ws_connect("/ws")
    await ws.close()
    for _ in range(20):
        if (await (await client.get("/health")).json())["widgets"] == 0:
            break
        await asyncio.sleep(0.05)
    assert (await (await client.get("/health")).json())["widgets"] == 0
    assert (await (await client.post("/perform", json={"state": "idle"})).json())["sent"] == 0


async def test_command_reaches_the_widget(client):
    """The tray's way in: POST /command, out to every widget socket (WIRING.md §14)."""
    ws = await client.ws_connect("/ws")
    response = await client.post("/command", json={"command": "hide"})
    assert response.status == 200
    assert (await response.json()) == {"sent": 1, "command": {"command": "hide"}}
    assert await ws.receive_json(timeout=2) == {"command": "hide"}

    await client.post("/command", json={"command": "quiet", "value": 3600})
    assert await ws.receive_json(timeout=2) == {"command": "quiet", "value": 3600}
    await client.post("/command", json={"command": "mute", "value": True})
    assert await ws.receive_json(timeout=2) == {"command": "mute", "value": True}
    await ws.close()


async def test_command_refuses_what_a_widget_would_not_understand(client):
    for bad, reason in (
        ({"command": "explode"}, "unknown command"),
        ({"command": "mute", "value": "yes"}, "needs a bool"),
        ({"command": "quiet", "value": -1}, "value >= 0"),
        ({"command": "quiet", "value": True}, "needs a number"),
        ({"command": "volume", "value": 2}, "between 0 and 1"),
        ({"command": "sleep_after", "value": -5}, "value >= 0"),
        ({"command": "skin"}, "needs a str"),
        ({"command": "hide", "colour": "blue"}, "unknown fields"),
    ):
        response = await client.post("/command", json=bad)
        assert response.status == 400, bad
        assert reason in (await response.json())["error"], bad
    assert (await client.post("/command", json={"command": "quit"},
                              headers={"Origin": "https://evil.example"})).status == 403


async def test_reload_notifications_rereads_the_section_and_goes_to_no_widget(client, daemon):
    """The tray's "Message bodies" rows: the daemon re-reads [notifications] itself (§14)."""
    from strawberry_crab.config import default_path

    ws = await client.ws_connect("/ws")
    path = default_path()                              # the throwaway XDG one (conftest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[notifications]\nbody = "glance"\nbody_apps = { Signal = "off" }\n')
    response = await client.post("/command", json={"command": "reload_notifications"})
    assert response.status == 200
    assert await response.json() == {"sent": 0, "command": {"command": "reload_notifications"}, "body": "glance"}
    assert daemon.config.notifications.body == "glance"
    assert daemon.config.notifications.mode_for("Signal") == "off"
    assert daemon.config.brain.enabled is False       # only [notifications]; the rest waits for a restart

    path.write_text('[notifications]\nbody = "loud"\n')
    response = await client.post("/command", json={"command": "reload_notifications"})
    assert response.status == 409 and "not reloaded" in (await response.json())["error"]
    assert daemon.config.notifications.body == "glance"      # a bad file changes nothing
    with pytest.raises(asyncio.TimeoutError):
        await ws.receive_json(timeout=0.2)                  # the widget never heard of it
    await ws.close()


async def test_health_carries_the_state_the_tray_shows(client):
    assert (await (await client.get("/health")).json())["state"] == "idle"
    await client.post("/perform", json={"state": "thinking"})
    assert (await (await client.get("/health")).json())["state"] == "thinking"
    await client.post("/perform", json={"state": "dancing"})
    body = await (await client.get("/health")).json()
    assert body["state"] == "dancing" and body["rest_state"] == "dancing"


async def test_a_stale_transient_falls_back_to_her_resting_state(daemon):
    from strawberry_crab.contract import Performance

    await daemon.perform(Performance(state="dancing"))
    await daemon.perform(Performance(state="talking"))
    assert daemon.current_state() == "talking"
    daemon.state_at -= daemon.TRANSIENT_S + 1      # the widget went back to dancing on its own
    assert daemon.current_state() == "dancing"


# --- the version handshake (WIRING.md §1) --------------------------------------------

def test_version_verdicts():
    from strawberry_crab.server import version_verdict

    assert version_verdict("dev", "0.1.0") == "dev"
    assert version_verdict("0.1.0", "0.1.0") == "same"
    assert version_verdict("0.1", "0.1.0") == "same"
    assert version_verdict("0.1.3", "0.1.0") == "minor"
    assert version_verdict("0.4.0", "0.1.0") == "minor"
    assert version_verdict("1.0.0", "0.1.0") == "major"
    assert version_verdict(None, "0.1.0") == "unknown"
    assert version_verdict("banana", "0.1.0") == "unknown"


async def test_a_dev_widget_is_served_and_health_shows_its_version(client):
    ws = await client.ws_connect("/ws")
    await ws.send_json({"type": "hello", "client": "strawberry-widget", "version": "dev"})
    await asyncio.sleep(0.05)
    body = await (await client.get("/health")).json()
    assert body["widgets"] == 1 and body["widget_versions"] == ["dev"]
    from strawberry_crab import __version__
    assert body["version"] == __version__
    await ws.close()


async def test_a_minor_mismatch_is_logged_and_served(client, caplog):
    from strawberry_crab import __version__
    major, minor, *_ = __version__.split(".")
    other = f"{major}.{int(minor) + 1}.0"
    ws = await client.ws_connect("/ws")
    await ws.send_json({"type": "hello", "client": "strawberry-widget", "version": other})
    await asyncio.sleep(0.05)
    assert not ws.closed
    assert "differ in minor/patch version" in caplog.text
    assert (await (await client.get("/health")).json())["widget_versions"] == [other]
    await ws.close()


async def test_a_major_mismatch_is_refused(client, caplog):
    from strawberry_crab import __version__
    from strawberry_crab.server import CLOSE_VERSION_REFUSED

    other = f"{int(__version__.split('.')[0]) + 1}.0.0"
    ws = await client.ws_connect("/ws")
    await ws.send_json({"type": "hello", "client": "strawberry-widget", "version": other})
    message = await ws.receive(timeout=2)
    assert ws.closed and ws.close_code == CLOSE_VERSION_REFUSED
    assert f"widget {other}, daemon {__version__}" in str(message.extra)
    assert "refusing widget" in caplog.text
    body = await (await client.get("/health")).json()
    assert body["widgets"] == 0 and body["widget_versions"] == []


def test_the_close_code_matches_the_widget():
    from pathlib import Path
    from strawberry_crab.server import CLOSE_VERSION_REFUSED

    ws_client = (Path(__file__).parents[1] / "widget" / "ws_client.gd").read_text()
    assert f"const CLOSE_VERSION_REFUSED := {CLOSE_VERSION_REFUSED}" in ws_client


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_two_sigterms_at_once_are_one_stop_and_cleanup_finishes(daemon, monkeypatch, caplog):
    # systemd signals the tray's whole cgroup and the tray terminates the daemon a millisecond
    # later, so both SIGTERMs are queued before shutdown starts. Both must end in one full cleanup.
    import logging
    import signal

    from strawberry_crab import server

    handlers = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, fn, *args: handlers.__setitem__(sig, (fn, args)))
    started, closed = asyncio.Event(), []
    real_start, real_close = daemon.start, daemon.close

    async def start():
        await real_start()
        started.set()

    async def close():
        await asyncio.sleep(0.05)        # a cleanup that takes a while, like closing sessions
        await real_close()
        closed.append(True)

    monkeypatch.setattr(daemon, "start", start)
    monkeypatch.setattr(daemon, "close", close)
    task = asyncio.ensure_future(server.serve(create_app(daemon), "127.0.0.1", _free_port()))
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.sleep(0.05)            # the site is listening
    assert set(handlers) == {signal.SIGINT, signal.SIGTERM}
    fn, args = handlers[signal.SIGTERM]
    with caplog.at_level(logging.INFO, logger="strawberryd.http"):
        fn(*args)
        fn(*args)                        # the tray's terminate(), right behind systemd's
        await asyncio.wait_for(task, 5)
        fn(*args)                        # and one more after the end: still nothing raised
    assert closed == [True]
    assert "SIGTERM: shutting down" in caplog.text
    assert "SIGTERM during shutdown ignored" in caplog.text


async def test_a_stop_while_the_models_load_ends_serve_and_closes(daemon, monkeypatch):
    import signal

    from strawberry_crab import server

    handlers = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, fn, *args: handlers.__setitem__(sig, (fn, args)))
    closed = []

    async def slow_start():
        await asyncio.sleep(10)

    async def close():
        closed.append(True)

    monkeypatch.setattr(daemon, "start", slow_start)
    monkeypatch.setattr(daemon, "close", close)
    task = asyncio.ensure_future(server.serve(create_app(daemon), "127.0.0.1", _free_port()))
    await asyncio.sleep(0.05)
    fn, args = handlers[signal.SIGTERM]
    fn(*args)
    await asyncio.wait_for(task, 5)
    assert closed == [True]              # by _start_daemon; no on_cleanup for an app never started


# Run as the tray's daemon child, stopped the way `systemctl --user stop strawberry-tray` stops
# it: systemd signals the whole cgroup and the tray terminates the daemon a moment later, so the
# second SIGTERM lands while aiohttp is closing the listening socket, before any on_shutdown hook.
# Every ClientSession is counted, so the verdict does not hang on the garbage collector.
_DOUBLE_SIGTERM_CHILD = """
import asyncio, os, runpy, signal, sys, time
import aiohttp
from aiohttp import web_runner
from strawberry_crab.daemon import Daemon

sessions = []
real_init = aiohttp.ClientSession.__init__

def init(self, *args, **kwargs):
    real_init(self, *args, **kwargs)
    sessions.append(self)

aiohttp.ClientSession.__init__ = init

real_start = Daemon.start

async def start(self):
    await real_start(self)
    asyncio.get_running_loop().call_later(0.2, os.kill, os.getpid(), signal.SIGTERM)

Daemon.start = start

real_stop = web_runner.BaseSite.stop

async def stop(self):
    os.kill(os.getpid(), signal.SIGTERM)      # the tray's terminate()
    time.sleep(0.02)                          # delivered before the loop looks again
    await asyncio.sleep(0)                    # the loop reads it from its wakeup pipe
    await asyncio.sleep(0)                    # and dispatches it
    await real_stop(self)

web_runner.BaseSite.stop = stop
sys.argv = ["strawberryd", *sys.argv[1:]]
try:
    runpy.run_module("strawberry_crab.strawberryd", run_name="__main__", alter_sys=True)
finally:
    print(f"sessions: {len(sessions)} opened, {sum(not s.closed for s in sessions)} left open", flush=True)
"""


def test_the_tray_daemon_child_closes_its_sessions_on_a_double_sigterm(tmp_path):
    # The real process, started the way the tray starts it (child_specs), with the brain, the
    # gate and the thinker on: three aiohttp sessions. Ollama is a closed port, so nothing loads,
    # but every session is opened. Before the fix the second SIGTERM raised aiohttp's GracefulExit
    # in the middle of the cleanup and the sessions were left to the garbage collector.
    import subprocess

    from strawberry_crab.tray import child_specs

    port, ollama = _free_port(), _free_port()
    config = tmp_path / "config.toml"
    config.write_text(f"""
[brain]
enabled = true
ollama_url = "http://127.0.0.1:{ollama}"
timeout_s = 0.2
[gate]
enabled = true
[thinker]
enabled = true
[speech]
enabled = false
[voice]
enabled = false
[tools]
enabled = false
[actions]
mpris = false
""")
    argv = child_specs(port, config, widget=False)[0].argv
    assert argv[1:3] == ["-m", "strawberry_crab.strawberryd"]
    argv = [argv[0], "-c", _DOUBLE_SIGTERM_CHILD, *argv[3:]]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    output = result.stdout + result.stderr
    assert "sessions: 3 opened, 0 left open" in output, output
    assert "Unclosed" not in output, output
    assert "SIGTERM during shutdown ignored" in output, output
    assert "shut down; sessions closed" in output, output
    assert result.returncode == 0, output


async def test_stopped_during_startup_still_closes_the_daemon(daemon, monkeypatch):
    from strawberry_crab import server

    closed = []

    async def slow_start():
        await asyncio.sleep(10)

    async def close():
        closed.append(True)

    monkeypatch.setattr(daemon, "start", slow_start)
    monkeypatch.setattr(daemon, "close", close)
    app = create_app(daemon)
    task = asyncio.ensure_future(server._start_daemon(app))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]


async def test_close_closes_every_part_even_when_one_fails(daemon):
    closed = []

    class Part:
        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail

        async def close(self):
            closed.append(self.name)
            if self.fail:
                raise RuntimeError("boom")

    daemon.reactor = Part("reactor", fail=True)
    daemon.gate = Part("gate")
    daemon.thinker = Part("thinker")
    await daemon.close()
    assert closed == ["reactor", "gate", "thinker"]


async def test_probe_reports_what_is_off_and_performs_nothing(client, daemon):
    ws = await client.ws_connect("/ws")
    response = await client.post("/probe", json={})
    assert response.status == 200
    body = await response.json()
    assert set(body["skipped"]) == {"gate", "voice", "brain", "tts", "whisper"}
    assert [row["text"] for row in body["lines"]] == list(Daemon.PROBE_LINES)
    assert daemon.performed == 0 and daemon.ledger.to_list() == []
    await ws.close()


async def test_probe_times_every_slot_and_offers_the_brain_no_tools(aiohttp_client, daemon, tmp_path, caplog):
    import logging
    import wave
    from types import SimpleNamespace

    from strawberry_crab.contract import Performance

    caplog.set_level(logging.INFO)
    wav = tmp_path / "line.wav"
    with wave.open(str(wav), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(22050)
        out.writeframes(b"\x00\x01" * 22050)
    seen = {}

    async def route(text):
        return SimpleNamespace(topic="other")

    async def react(event, context=""):
        return Performance(state="talking", text="hi")

    async def run(text, context="", careful=False, topic="", tools=True):
        seen["tools"] = tools
        return SimpleNamespace(ok=True)

    async def say(text):
        return str(wav)

    def transcribe(audio, hotwords=""):
        seen["samples"] = len(audio)
        return "hello"

    daemon.gate = SimpleNamespace(ready=True, route=route)
    daemon.reactor = SimpleNamespace(session=object(), react=react)
    daemon.config.brain.enabled = True
    daemon.thinker = SimpleNamespace(enabled=True, run=run)
    daemon.speaker = SimpleNamespace(ready=True, say=say, stats=dict)
    daemon.listener = SimpleNamespace(ready=True, transcriber=transcribe, disabled_reason=None)
    app = create_app(daemon)
    app.on_startup.clear()          # the stand-ins have nothing to start or close
    app.on_cleanup.clear()
    client = await aiohttp_client(app)
    body = await (await client.post("/probe", json={})).json()
    assert body["skipped"] == {}
    for row in body["lines"]:
        for slot in ("gate", "voice", "brain", "tts", "whisper"):
            assert row[slot]["ok"] and row[slot]["ms"] >= 0
    assert seen["tools"] is False
    assert 15900 <= seen["samples"] <= 16100                  # 22.05 kHz resampled to whisper's 16 kHz
    assert "probe:" in caplog.text and "Hello there" not in caplog.text   # times are logged, text is not


async def test_probe_is_refused_to_browsers(client):
    response = await client.post("/probe", json={}, headers={"Origin": "http://evil.example"})
    assert response.status == 403
