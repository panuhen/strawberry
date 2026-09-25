"""The daemon starts without waiting for a model (WIRING.md §2): the gate's examples, Piper's voice
and the reaction model's warm-up load in the background, the port answers at once, and whatever
arrives first is handled the way it would be with that part unavailable, or waits a little."""

from __future__ import annotations

import asyncio
import time
import wave
from pathlib import Path

import pytest
from aiohttp import web

from strawberry_crab.brain import OllamaReactor
from strawberry_crab.config import BrainConfig, Config, GateConfig, SpeechConfig
from strawberry_crab.daemon import Daemon
from strawberry_crab.events import CannedReactor, Event
from strawberry_crab.server import create_app
from strawberry_crab.speech import Speaker
from strawberry_crab.systemone import Gate
from tests.test_privacy import Brain
from tests.test_speech import fake_synth
from tests.test_systemone import bag_embed


class HeldEmbedder:
    """Embeds like the unit tests' fake, but the first call (the examples, at start) waits for
    `release`: a gate whose start takes as long as the test wants."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls = 0

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls == 1:
            await self.release.wait()
        return [bag_embed(t) for t in texts]


def held_gate(**config) -> tuple[Gate, HeldEmbedder]:
    embedder = HeldEmbedder()
    return Gate(GateConfig(query_prefix="", document_prefix="", **config), embedder=embedder), embedder


def plain_config() -> Config:
    config = Config()
    config.brain.enabled = config.speech.enabled = config.voice.enabled = False
    config.tools.enabled = config.thinker.enabled = False
    config.actions.mpris = False
    return config


async def test_the_daemon_is_up_while_the_gate_still_starts(aiohttp_client):
    gate, embedder = held_gate()
    daemon = Daemon(reactor=CannedReactor(), config=plain_config(), gate=gate)
    client = await aiohttp_client(create_app(daemon))     # on_startup ran daemon.start()
    health = await (await client.get("/health")).json()
    assert health["ok"] and health["gate"]["ready"] is False and health["gate"]["starting"] is True
    embedder.release.set()
    await asyncio.wait_for(gate.starting, 2)
    health = await (await client.get("/health")).json()
    assert health["gate"]["ready"] is True and health["gate"]["starting"] is False


async def test_a_sentence_before_the_gate_is_ready_waits_briefly_then_is_chat(monkeypatch):
    monkeypatch.setattr(Gate, "START_WAIT_S", 0.05)
    gate, embedder = held_gate()
    brain = Brain(line="Hello to you too.")
    daemon = Daemon(reactor=brain, config=plain_config(), gate=gate)  # type: ignore[arg-type]
    await daemon.start()
    started = time.perf_counter()
    performance, _ = await daemon.handle_event(Event(source="voice", title="skip this track"))
    assert time.perf_counter() - started < 1.0
    assert performance.text == "Hello to you too."                # the gate-unavailable path: chat
    assert [kind for kind, _ in brain.seen] == ["react"] and gate.calls == 0
    assert gate.is_starting                                        # giving up did not cancel the start
    embedder.release.set()
    assert await asyncio.wait_for(gate.starting, 2) is True
    assert (await gate.route("skip this track")).tool == "skip"
    await daemon.close()


async def test_a_sentence_just_before_the_gate_is_ready_is_routed():
    gate, embedder = held_gate()
    gate.begin()
    asyncio.get_running_loop().call_later(0.1, embedder.release.set)
    route = await gate.route("skip this track")
    assert route is not None and route.tool == "skip"
    await gate.close()


async def test_a_notification_body_waits_for_the_start_and_is_read():
    gate, embedder = held_gate(retry_timeout_s=2.0)
    gate.begin()
    asyncio.get_running_loop().call_later(0.1, embedder.release.set)
    answer = await gate.sensitive("Alex: lunch at 12?")
    assert answer is not None and answer[0] < 0.5                 # read, not dropped as private
    # With no retry allowed it fails closed at once, as for a reload.
    gate, embedder = held_gate(retry_timeout_s=0.0)
    gate.begin()
    assert await gate.sensitive("Alex: lunch at 12?") is None
    await gate.close()
    assert gate.starting.cancelled()


async def test_a_start_that_crashes_leaves_the_gate_off_with_the_reason(caplog):
    class Broken:
        async def __call__(self, texts):
            raise RuntimeError("not the kind of error start expects")

    gate = Gate(GateConfig(), embedder=Broken())  # type: ignore[arg-type]
    gate.begin()
    await asyncio.gather(gate.starting, return_exceptions=True)
    await asyncio.sleep(0)                                         # the done callback
    assert not gate.ready and "not the kind of error" in gate.disabled_reason
    assert await gate.route("skip this track") is None
    assert "failed to start" in caplog.text


async def test_a_resume_during_the_start_joins_it():
    gate, embedder = held_gate()
    task = gate.begin()
    assert gate.warm("on resume") is task                          # no second load of the examples
    embedder.release.set()
    assert await task is True and embedder.calls == 1


async def test_a_line_before_piper_is_loaded_waits_for_it(tmp_path: Path):
    (tmp_path / "test-voice.onnx").write_bytes(b"not really a model")

    def slow_factory(model_path, speed, volume):
        time.sleep(0.2)
        return fake_synth

    speaker = Speaker(SpeechConfig(enabled=True, voice="test-voice", voices_dir=str(tmp_path)), synth_factory=slow_factory)
    speaker.begin()
    assert not speaker.ready and speaker.stats()["loading"] is True
    wav = await speaker.say("hello there")                         # waits for the voice, is not silent
    assert wav and Path(wav).is_file() and speaker.ready and not speaker.stats()["loading"]
    with wave.open(wav) as w:
        assert w.getnframes() > 0
    await speaker.close()


async def test_the_brain_warms_up_in_the_background(aiohttp_server):
    release = asyncio.Event()
    generated = []

    async def generate(request):
        generated.append(await request.json())
        await release.wait()                                       # a cold load
        return web.json_response({"done": True})

    async def chat(request):
        await asyncio.sleep(1.0)                                   # the model is still loading
        return web.json_response({})

    app = web.Application()
    app.add_routes([web.post("/api/generate", generate), web.post("/api/chat", chat)])
    server = await aiohttp_server(app)
    reactor = OllamaReactor(BrainConfig(ollama_url=str(server.make_url("")).rstrip("/"), timeout_s=0.1),
                            fallback=CannedReactor())
    task = reactor.begin()
    assert not task.done() and not reactor.loaded
    # An event first: the short call times out, she says the canned line, and joins the load.
    performance = await reactor.react(Event(source="git", app="post-commit", title="strawberry", body="Fix"))
    assert performance.text and reactor.fallbacks == 1 and reactor.rewarm is task
    release.set()
    await asyncio.wait_for(task, 2)
    assert reactor.loaded and len(generated) == 1
    await reactor.close()


async def test_daemon_start_waits_for_no_model(tmp_path: Path, aiohttp_server):
    """Every slow part at once: nothing of it is awaited by Daemon.start."""
    (tmp_path / "test-voice.onnx").write_bytes(b"not really a model")
    release = asyncio.Event()

    async def generate(request):
        await release.wait()
        return web.json_response({"done": True})

    app = web.Application()
    app.add_routes([web.post("/api/generate", generate)])
    server = await aiohttp_server(app)
    config = plain_config()
    config.brain.enabled = True
    config.brain.ollama_url = str(server.make_url("")).rstrip("/")
    config.speech = SpeechConfig(enabled=True, voice="test-voice", voices_dir=str(tmp_path))

    def slow_factory(model_path, speed, volume):
        time.sleep(0.3)
        return fake_synth

    gate, embedder = held_gate()
    daemon = Daemon(config=config, gate=gate, speaker=Speaker(config.speech, synth_factory=slow_factory))
    started = time.perf_counter()
    await daemon.start()
    assert time.perf_counter() - started < 0.2
    assert gate.is_starting and daemon.speaker.stats()["loading"] and not daemon.reactor.loaded
    await daemon.close()                                           # cancels what is still loading
    assert gate.starting.cancelled() and daemon.speaker.loading.cancelled()
    release.set()


@pytest.mark.parametrize("slot", ["gate", "tts"])
async def test_the_probe_says_what_is_still_starting(slot, tmp_path: Path):
    (tmp_path / "test-voice.onnx").write_bytes(b"not really a model")
    config = plain_config()
    gate, embedder = held_gate()

    def slow_factory(model_path, speed, volume):
        time.sleep(0.2)
        return fake_synth

    config.speech = SpeechConfig(enabled=True, voice="test-voice", voices_dir=str(tmp_path))
    daemon = Daemon(reactor=CannedReactor(), config=config, gate=gate,
                    speaker=Speaker(config.speech, synth_factory=slow_factory))
    await daemon.start()
    probe = await daemon.probe()
    assert probe["skipped"][slot].startswith("still")
    embedder.release.set()
    await daemon.close()
