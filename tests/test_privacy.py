"""Notification bodies (WIRING.md §4): the three modes, the sensitive filter, fail-closed, and no
message text in any log line."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from aiohttp import web

from strawberry_crab import privacy
from strawberry_crab.brain import OllamaReactor
from strawberry_crab.config import Config, GateConfig, NotificationsConfig
from strawberry_crab.contract import Performance
from strawberry_crab.daemon import Daemon
from strawberry_crab.doorways import notify_watch, toast_watch
from strawberry_crab.events import CannedReactor, Event
from strawberry_crab.server import create_app
from strawberry_crab.systemone import Gate
from tests.test_notify_watch import bus_notify
from tests.test_toast_watch import FakeListener, FakeNotification
from tests.test_systemone import FakeEmbedder


# ----------------------------------------------------------------------------- patterns


@pytest.mark.parametrize("text", [
    "Your verification code is 482913",
    "482913 is your Acme login code. Don't share it.",
    "G-582014 is your Google verification code.",
    "Your Slack confirmation code: 123-456",
    "Use 7731 as your one-time PIN",
    "2FA code 90210",
    "Your security code is 4410 and it expires in 10 minutes",
    "  884120  ",                                            # the body is only a code
    "WA-7731",
    "Do not share this code with anyone.",
    "Never give your PIN to anybody",
    "Click here to reset your password: https://example.com/r/abc",
    "Your magic link is ready",
    "https://app.example.com/login?token=eyJhbGciOi",
    "Here is your sign-in link",
])
def test_patterns_catch_codes_and_sign_in_links(text):
    assert privacy.pattern(text) is not None


@pytest.mark.parametrize("text", [
    "meeting at 1400",
    "PR #4821 merged",
    "3 tests failed",
    "Build 2024.10.1234 is out",
    "Invoice total 1,250.00 EUR",
    "Standup at 09:30, room 4",
    "your order 5521 has shipped",                            # a number, but no code word near it
    "can you review my code when you get a chance?",         # the word, but no number
    "Room 1234 at 14:00, bring the access badge",
    "",
])
def test_patterns_leave_ordinary_numbers_alone(text):
    assert privacy.pattern(text) is None


def test_a_code_needs_its_word_nearby():
    far = "code review notes " + "blah " * 30 + "see you in room 1234"
    assert privacy.pattern(far) is None
    assert privacy.pattern("the code is 1234") == "one-time code"


def test_leaks_catches_links_numbers_and_quotes():
    body = "can you look at PR 4821 before 3? https://x.example.com/p/4821 staging password is bluefin"
    assert privacy.leaks("Alex wants a review before the afternoon's out.", body) is None
    assert privacy.leaks("PR 4821 needs you!", body) == "a number from the message"
    assert privacy.leaks("See https://x.example.com now", body) == "a link or an address"
    assert privacy.leaks("Apparently staging password is bluefin, shh.", body) == "four words from the message"
    assert privacy.leaks("Write to alex@example.com", body) == "a link or an address"
    assert privacy.leaks("Three things to do.", body) is None
    assert privacy.leaks("Alex asks about lunch at 1.", "lunch?", strict=True) == "a number"


# ----------------------------------------------------------------------------- the gate question


async def test_is_sensitive_on_the_gate_yes_and_no():
    gate = Gate(GateConfig(query_prefix="", document_prefix=""), embedder=FakeEmbedder())
    await gate.start()
    yes = await gate.sensitive("New sign-in to your account from Firefox on Linux")
    no = await gate.sensitive("are we still on for lunch on Friday?")
    assert yes is not None and no is not None
    assert yes[0] > privacy.SENSITIVE_P > no[0]
    assert gate.sensitive_calls == 2 and gate.calls == 0   # not counted as a routed sentence
    verdict = await privacy.check(gate, "Acme", "New sign-in to your account from Firefox on Linux")
    assert verdict.sensitive and verdict.reason.startswith("gate ")
    verdict = await privacy.check(gate, "Alex", "are we still on for lunch on Friday?")
    assert not verdict.sensitive and verdict.reason.startswith("clear ")


async def test_a_gate_that_is_down_counts_as_sensitive():
    down = Gate(GateConfig(), embedder=FakeEmbedder(fail=True))
    await down.start()
    assert (await privacy.check(down, "Alex", "lunch?")).reason == "gate unavailable"
    off = Gate(GateConfig(enabled=False), ollama_url="http://127.0.0.1:1")
    await off.start()
    assert (await privacy.check(off, "Alex", "lunch?")).sensitive


async def test_a_gate_that_fails_mid_run_counts_as_sensitive():
    embedder = FakeEmbedder()
    gate = Gate(GateConfig(query_prefix="", document_prefix=""), embedder=embedder)
    await gate.start()
    embedder.fail = True
    verdict = await privacy.check(gate, "Alex", "lunch?")
    assert verdict.sensitive and verdict.reason == "gate unavailable"


async def test_patterns_run_before_the_gate_and_on_the_title():
    embedder = FakeEmbedder()
    gate = Gate(GateConfig(query_prefix="", document_prefix=""), embedder=embedder)
    await gate.start()
    calls = embedder.calls
    assert (await privacy.check(gate, "Acme", "Your code is 482913")).reason == "pattern: one-time code"
    assert (await privacy.check(gate, "Your code is 482913", "")).sensitive     # the title too
    assert embedder.calls == calls                                              # no embedding needed


# ----------------------------------------------------------------------------- the daemon


class ScriptedGate:
    """IS_SENSITIVE answers by script: a p(yes), or None for a gate that cannot answer."""

    def __init__(self, p: float | None = 0.02) -> None:
        self.p = p
        self.asked: list[str] = []

    async def start(self) -> None: ...
    async def close(self) -> None: ...

    async def route(self, text):
        return None

    async def sensitive(self, text):
        self.asked.append(text)
        return None if self.p is None else (self.p, 12.0)

    def stats(self):
        return {"ready": self.p is not None}


class Brain:
    """A reactor that records what it was shown, in order, with a gist of its own."""

    def __init__(self, line="Somebody wants a word.", gist="Alex asks about lunch at noon.") -> None:
        self.line = line
        self.gist_line = gist
        self.seen: list[tuple[str, Event]] = []

    async def react(self, event, context=""):
        self.seen.append(("react", event))
        return Performance(state="talking", text=self.line, emotion="happy")

    async def gist(self, event):
        self.seen.append(("gist", event))
        return self.gist_line


def make_daemon(brain=None, gate=None, **notifications) -> Daemon:
    config = Config()
    config.brain.enabled = config.speech.enabled = config.voice.enabled = False
    config.tools.enabled = config.thinker.enabled = False
    config.actions.mpris = False
    config.notifications = NotificationsConfig(**notifications)
    return Daemon(reactor=brain or Brain(), config=config, gate=gate or ScriptedGate())  # type: ignore[arg-type]


LUNCH = Event(source="notification", app="Slack", title="Alex", body="lunch at 12? the usual place", category="im.received")


async def test_off_drops_a_body_that_arrives_anyway():
    brain, gate = Brain(), ScriptedGate()
    daemon = make_daemon(brain, gate)                       # default: off
    performance, _ = await daemon.handle_event(LUNCH)
    assert [kind for kind, _ in brain.seen] == ["react"]
    assert brain.seen[0][1].body == "" and brain.seen[0][1].title == "Alex"
    assert gate.asked == []                                  # nothing to check: the body is gone
    assert performance.text == "Somebody wants a word."


async def test_react_shows_gemma_the_body_after_the_checks():
    brain, gate = Brain(), ScriptedGate(0.03)
    daemon = make_daemon(brain, gate, body="react")
    performance, _ = await daemon.handle_event(LUNCH)
    assert gate.asked == ["Alex: lunch at 12? the usual place"]
    assert [kind for kind, _ in brain.seen] == ["react"] and brain.seen[0][1].body == LUNCH.body
    assert performance.text == "Somebody wants a word." and performance.reaction == "wave"


async def test_react_line_that_quotes_the_message_becomes_the_canned_line():
    brain = Brain(line="Lunch at 12? The usual place!")
    daemon = make_daemon(brain, body="react")
    performance, _ = await daemon.handle_event(LUNCH)
    assert performance.text == "Slack: Alex"


async def test_glance_is_two_calls_gist_first_then_her_quip():
    brain = Brain(line="Food, finally. Go on then, reply.")
    daemon = make_daemon(brain, body="glance")
    performance, _ = await daemon.handle_event(LUNCH)
    assert [kind for kind, _ in brain.seen] == ["gist", "react"]
    assert brain.seen[0][1].body == LUNCH.body
    quip_event = brain.seen[1][1]
    assert quip_event.said == "Alex asks about lunch at noon." and quip_event.body == ""   # the quip never sees it
    assert performance.text.startswith("Alex asks about lunch at noon. ")
    assert performance.text.endswith("Food, finally. Go on then, reply.")


async def test_glance_quip_is_capped_and_a_gist_with_numbers_is_refused():
    brain = Brain(line="one two three four five six seven eight nine ten", gist="Alex asks about lunch at 12.")
    daemon = make_daemon(brain, body="glance")
    performance, _ = await daemon.handle_event(LUNCH)
    assert [kind for kind, _ in brain.seen] == ["gist", "react"]
    assert brain.seen[1][1].said == "" and brain.seen[1][1].body == LUNCH.body   # fell back to react
    brain.gist_line = "Alex asks about lunch."
    brain.seen.clear()
    performance, _ = await daemon.handle_event(LUNCH)
    assert performance.text == "Alex asks about lunch."   # a quip over the cap is dropped, never cut mid-sentence


async def test_per_app_mode_overrides_the_default():
    brain = Brain()
    daemon = make_daemon(brain, body="off", body_apps={"slack": "glance"})
    await daemon.handle_event(LUNCH)
    await daemon.handle_event(Event(source="notification", app="WhatsApp", title="Sam", body="pub?"))
    assert [kind for kind, _ in brain.seen] == ["gist", "react", "react"]
    assert brain.seen[2][1].body == ""


@pytest.mark.parametrize("mode", ["react", "glance"])
async def test_a_code_is_private_whatever_the_mode(mode):
    brain, gate = Brain(), ScriptedGate(0.0)
    daemon = make_daemon(brain, gate, body=mode)
    code = Event(source="notification", app="Messages", title="Acme", body="Your Acme verification code is 482913")
    performance, _ = await daemon.handle_event(code)
    assert performance.text == "Messages sent something private."
    assert brain.seen == [] and gate.asked == []             # the pattern caught it, nobody read it


async def test_the_gate_catches_what_patterns_cannot():
    brain, gate = Brain(), ScriptedGate(0.97)
    daemon = make_daemon(brain, gate, body="glance")
    bank = Event(source="notification", app="Nordbank", title="Card payment",
                 body="45.20 EUR at SUPERMARKET, card ending 4421")
    performance, _ = await daemon.handle_event(bank)
    assert performance.text == "Nordbank sent something private."
    assert brain.seen == []


async def test_fail_closed_when_the_gate_cannot_answer():
    brain = Brain()
    daemon = make_daemon(brain, ScriptedGate(None), body="react")
    performance, _ = await daemon.handle_event(LUNCH)
    assert performance.text == "Slack sent something private."
    assert brain.seen == []


async def test_a_code_in_the_title_is_private_even_with_bodies_off():
    brain = Brain()
    daemon = make_daemon(brain)
    performance, _ = await daemon.handle_event(Event(source="notification", app="Messages", title="Your code is 552013"))
    assert performance.text == "Messages sent something private." and brain.seen == []


async def test_a_burst_keeps_its_titles_and_skips_the_gate():
    brain, gate = Brain(), ScriptedGate()
    daemon = make_daemon(brain, gate)
    burst = Event(source="notification", app="Slack", title="3 notifications from Slack", body="#general · Alex · Sam")
    await daemon.handle_event(burst)
    assert brain.seen[0][1].body == "#general · Alex · Sam" and gate.asked == []


async def test_canned_notification_line_never_reads_the_body():
    performance = await CannedReactor().react(Event(source="notification", app="Slack", body="secret words"))
    assert performance.text == "Slack wants your attention."


# ----------------------------------------------------------------------------- no text in logs


def fake_gemma():
    """Ollama for the round trip: /api/chat answers a gist or a line, /api/embed a bag-of-words vector."""
    from tests.test_systemone import bag_embed

    seen: list[str] = []

    async def generate(request):
        return web.json_response({"done": True})

    async def chat(request):
        body = await request.json()
        seen.append(json.dumps(body))
        if "gist" in json.dumps(body.get("format", {})):
            content = {"gist": "Alex asks about a plan."}
        else:
            content = {"line": "Plans afoot. Answer the poor soul.", "emotion": "happy"}
        return web.json_response({"message": {"role": "assistant", "content": json.dumps(content)}, "done": True})

    async def embed(request):
        body = await request.json()
        return web.json_response({"embeddings": [bag_embed(t) for t in body["input"]]})

    app = web.Application()
    app.add_routes([web.post("/api/generate", generate), web.post("/api/chat", chat), web.post("/api/embed", embed)])
    return app, seen


CANARY = "zebra-canary-5b2e"


@pytest.mark.parametrize("backend", ["dbus", "toasts"])
@pytest.mark.parametrize("mode", ["react", "glance"])
async def test_no_message_text_in_any_log_record(aiohttp_server, caplog, monkeypatch, mode, backend):
    """Watcher -> HTTP -> daemon -> gate -> Gemma (fake) -> widget, everything at DEBUG: the
    canary reaches Gemma and no log record anywhere. Both doorways: the D-Bus one (Linux) and
    the toast one (Windows), each on its fake."""
    ollama_app, seen = fake_gemma()
    ollama = await aiohttp_server(ollama_app)
    url = str(ollama.make_url("")).rstrip("/")
    config = Config()
    config.speech.enabled = config.voice.enabled = config.tools.enabled = config.thinker.enabled = False
    config.actions.mpris = False
    config.brain.ollama_url = url
    config.brain.timeout_s = 2.0
    config.gate.query_prefix = config.gate.document_prefix = ""
    config.notifications = NotificationsConfig(body=mode, coalesce_s=0.01)
    daemon = Daemon(reactor=OllamaReactor(config.brain, fallback=CannedReactor()), config=config,
                    gate=Gate(config.gate, url))
    server = await aiohttp_server(create_app(daemon))
    monkeypatch.setattr(notify_watch, "resolve_icon", lambda *a, **k: None)
    daemon_url = str(server.make_url("")).rstrip("/")
    body = f"did you get the plan? the word is {CANARY}, see you there"
    listener = FakeListener()
    if backend == "dbus":
        watcher = notify_watch.Watcher(daemon_url, config.notifications)
    else:
        watcher = toast_watch.Watcher(daemon_url, config.notifications, toast_watch.Toasts(listener))
    with caplog.at_level(logging.DEBUG):
        await daemon.start()
        if backend == "dbus":
            await watcher.handle(bus_notify(app="Slack", summary="Alex", body=body))
        else:
            await watcher.poll_once()
            listener.toasts.append(FakeNotification(1, app="Slack", texts=("Alex", body)))
            await watcher.poll_once()
        for _ in range(100):
            if daemon.performed:
                break
            await asyncio.sleep(0.02)
        await daemon.close()
    assert daemon.performed == 1
    assert any(CANARY in s for s in seen)                   # Gemma really was shown the body
    for record in caplog.records:
        assert CANARY not in record.getMessage(), f"{record.name}: {record.getMessage()}"
    assert any("body_len=" in r.getMessage() for r in caplog.records)


def test_short_quip_keeps_whole_sentences_only():
    from strawberry_crab.daemon import short_quip
    assert short_quip("Lunchtime. Be careful of the wait time and those servers!", 8) == "Lunchtime."
    assert short_quip("Moving things? Good luck.", 8) == "Moving things? Good luck."
    assert short_quip("One very long sentence that goes on and on past the limit.", 8) == ""
    assert short_quip("", 8) == ""
    assert short_quip("No end mark here", 8) == "No end mark here"
