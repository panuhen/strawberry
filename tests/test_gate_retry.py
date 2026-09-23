"""Slow is not down (WIRING.md §4, §8a): a notification check that times out waits for the gate's
model to load and asks once more; a hard error fails closed at once; a burst shares one wait; the
voice path never waits. Against a fake Ollama that behaves like the real one after a suspend."""

from __future__ import annotations

import asyncio
import logging
import time

from aiohttp import web

from strawberry_crab import privacy
from strawberry_crab.config import GateConfig
from strawberry_crab.events import Event
from strawberry_crab.systemone import Gate
from tests.test_privacy import Brain, make_daemon
from tests.test_systemone import bag_embed

SLACK = Event(source="notification", app="Slack", title="Alex", body="lunch at 12? the usual place",
              category="im.received")


class ColdOllama:
    """/api/embed with a model that can be unloaded. A call to a cold model takes `load_s` and
    loads it; a caller that hangs up first aborts the load, as Ollama does. `status` other than 200
    is a hard error on every call."""

    def __init__(self, load_s: float = 0.3) -> None:
        self.load_s = load_s
        self.loaded = True
        self.loads = 0            # loads that finished
        self.calls = 0
        self.status = 200

    def unload(self) -> None:     # what a suspend did
        self.loaded = False

    async def embed(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.calls += 1
        if self.status != 200:
            return web.Response(status=self.status, text="model runner has unexpectedly stopped")
        if not self.loaded:
            await asyncio.sleep(self.load_s)     # cancelled here when the client hangs up
            if not self.loaded:
                self.loaded = True
                self.loads += 1
        return web.json_response({"embeddings": [bag_embed(t) for t in body["input"]]})

    def app(self) -> web.Application:
        app = web.Application()
        app.add_routes([web.post("/api/embed", self.embed)])
        return app


async def cold_gate(aiohttp_server, load_s: float = 0.3, **config) -> tuple[Gate, ColdOllama, object]:
    ollama = ColdOllama(load_s)
    server = await aiohttp_server(ollama.app())
    settings = dict(query_prefix="", document_prefix="", timeout_s=0.1, retry_timeout_s=2.0) | config
    gate = Gate(GateConfig(**settings), str(server.make_url("")).rstrip("/"))
    await gate.start()
    assert gate.ready
    return gate, ollama, server


async def test_a_timeout_is_retried_once_the_model_has_loaded(aiohttp_server, caplog):
    gate, ollama, _ = await cold_gate(aiohttp_server)
    ollama.unload()
    with caplog.at_level(logging.INFO, logger="strawberryd.gate"):
        verdict = await privacy.check(gate, "Alex", "are we still on for lunch on Friday?")
    await gate.close()
    assert not verdict.sensitive and verdict.reason.startswith("clear ")
    assert ollama.loads == 1 and gate.retries == 1 and gate.warmups == 1 and gate.failures == 0
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "timed out after" in text and "reloaded after a timeout in" in text and "on the retry" in text
    assert "lunch" not in text                                  # timings only, never the message


async def test_the_daemon_reads_the_body_normally_after_the_retry(aiohttp_server):
    gate, ollama, _ = await cold_gate(aiohttp_server)
    brain = Brain()
    daemon = make_daemon(brain, gate, body="react")
    ollama.unload()
    performance, _ = await daemon.handle_event(SLACK)
    await gate.close()
    assert [kind for kind, _ in brain.seen] == ["react"] and brain.seen[0][1].body == SLACK.body
    assert performance.text == "Somebody wants a word."          # her line, not "something private"


async def test_a_retry_that_times_out_too_fails_closed(aiohttp_server, caplog):
    gate, ollama, _ = await cold_gate(aiohttp_server, load_s=5.0, retry_timeout_s=0.3)
    ollama.unload()
    started = time.monotonic()
    with caplog.at_level(logging.INFO, logger="strawberryd.gate"):
        verdict = await privacy.check(gate, "Alex", "are we still on for lunch on Friday?")
    elapsed = time.monotonic() - started
    assert verdict.sensitive and verdict.reason == "gate unavailable"
    assert 0.3 < elapsed < 1.5                                   # the short call, then the retry budget
    assert gate.retries == 1 and gate.failures == 1
    assert gate.warming is not None and not gate.warming.done()  # the load goes on for the next one
    assert "still loading after" in " ".join(r.getMessage() for r in caplog.records)
    await gate.close()


async def test_the_daemon_says_something_private_when_the_retry_fails(aiohttp_server):
    gate, ollama, _ = await cold_gate(aiohttp_server, load_s=5.0, retry_timeout_s=0.2)
    brain = Brain()
    daemon = make_daemon(brain, gate, body="react")
    ollama.unload()
    performance, _ = await daemon.handle_event(SLACK)
    await gate.close()
    assert brain.seen == []                                      # Gemma never saw it
    assert performance.text == "Slack sent something private."


async def test_an_http_error_fails_closed_at_once(aiohttp_server):
    gate, ollama, _ = await cold_gate(aiohttp_server)
    ollama.status = 500
    calls = ollama.calls
    started = time.monotonic()
    verdict = await privacy.check(gate, "Alex", "lunch?")
    assert verdict.sensitive and verdict.reason == "gate unavailable"
    assert time.monotonic() - started < 0.5
    assert ollama.calls == calls + 1 and gate.retries == 0 and gate.warming is None
    await gate.close()


async def test_ollama_gone_fails_closed_at_once(aiohttp_server):
    gate, _, server = await cold_gate(aiohttp_server)
    await server.close()                                         # connection refused from now on
    started = time.monotonic()
    verdict = await privacy.check(gate, "Alex", "lunch?")
    assert verdict.sensitive and verdict.reason == "gate unavailable"
    assert time.monotonic() - started < 0.5 and gate.retries == 0
    await gate.close()


async def test_no_retry_when_retry_timeout_is_zero(aiohttp_server):
    gate, ollama, _ = await cold_gate(aiohttp_server, retry_timeout_s=0.0)
    ollama.unload()
    verdict = await privacy.check(gate, "Alex", "lunch?")
    assert verdict.sensitive and gate.retries == 0
    await gate.close()


async def test_a_burst_at_wake_shares_one_load(aiohttp_server):
    gate, ollama, _ = await cold_gate(aiohttp_server, load_s=0.4)
    ollama.unload()
    started = time.monotonic()
    readings = await asyncio.gather(*(gate.sensitive(f"message number {i} about lunch") for i in range(6)))
    elapsed = time.monotonic() - started
    await gate.close()
    assert all(r is not None for r in readings)
    assert ollama.loads == 1 and gate.warmups == 1
    assert elapsed < 1.2                                         # one load, not six in a row (2.4 s)


async def test_a_notification_during_a_reload_waits_for_it_instead_of_a_short_call(aiohttp_server):
    gate, ollama, _ = await cold_gate(aiohttp_server, load_s=0.3)
    ollama.unload()
    gate.warm("on resume")
    calls = ollama.calls
    reading = await gate.sensitive("are we still on for lunch?")
    await gate.close()
    assert reading is not None
    assert ollama.calls == calls + 2                             # the reload and one real call, no doomed one


async def test_voice_does_not_wait_it_reads_as_chat_and_starts_the_reload(aiohttp_server):
    gate, ollama, _ = await cold_gate(aiohttp_server, load_s=0.3)
    ollama.unload()
    started = time.monotonic()
    assert await gate.route("pause the music") is None
    assert time.monotonic() - started < 0.3                      # the 0.1 s budget, no retry
    assert gate.warming is not None
    assert await gate.warming is True
    assert (await gate.route("pause the music")) is not None     # warm now
    await gate.close()


async def test_a_reload_brings_up_a_gate_that_could_not_start(aiohttp_server):
    ollama = ColdOllama()
    ollama.status = 500
    server = await aiohttp_server(ollama.app())
    gate = Gate(GateConfig(query_prefix="", document_prefix="", timeout_s=0.5),
                str(server.make_url("")).rstrip("/"))
    await gate.start()
    assert not gate.ready and gate.disabled_reason
    ollama.status = 200
    assert await gate.warm("on resume") is True
    assert gate.ready and not gate.disabled_reason
    assert await gate.sensitive("are we still on for lunch?") is not None
    await gate.close()


async def test_a_slow_notification_does_not_hold_up_other_events(aiohttp_server):
    gate, ollama, _ = await cold_gate(aiohttp_server, load_s=0.6)
    brain = Brain()
    daemon = make_daemon(brain, gate, body="react")
    ollama.unload()
    notification = asyncio.ensure_future(daemon.handle_event(SLACK))
    await asyncio.sleep(0.05)
    started = time.monotonic()
    await daemon.handle_event(Event(source="git", app="post-commit", title="repo", body="Fix the thing"))
    assert time.monotonic() - started < 0.3                      # the commit did not queue behind it
    assert not notification.done()
    performance, _ = await notification
    await gate.close()
    assert performance.text == "Somebody wants a word."


def test_retry_timeout_is_in_the_config_and_checked(tmp_path):
    import pytest

    from strawberry_crab.config import ConfigError, Config, default_toml, load

    assert Config().gate.retry_timeout_s == 15.0 and Config().daemon.warm_on_wake is True
    assert "retry_timeout_s = 15.0" in default_toml() and "warm_on_wake = true" in default_toml()
    path = tmp_path / "config.toml"
    path.write_text("[gate]\nretry_timeout_s = 5.0\n[daemon]\nwarm_on_wake = false\n")
    config = load(path)
    assert config.gate.retry_timeout_s == 5.0 and config.daemon.warm_on_wake is False
    path.write_text("[gate]\nretry_timeout_s = -1.0\n")
    with pytest.raises(ConfigError):
        load(path)
