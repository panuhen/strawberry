"""The voice session without a microphone or a model: fakes for recording and transcription."""

from __future__ import annotations

import asyncio
import json
import threading

import numpy as np
import pytest

from strawberryd.config import Config, VoiceConfig
from strawberryd.daemon import Daemon
from strawberryd.events import CannedReactor
from strawberryd.voice import DIDNT_CATCH, Listener, Recording, dbfs


class Sink:
    closed = False

    def __init__(self) -> None:
        self.got: list[dict] = []

    async def send_str(self, text: str) -> None:
        self.got.append(json.loads(text))

    async def close(self, **kwargs) -> None:
        self.closed = True


def fake_recording(seconds: float = 2.0, speech: float = 1.2, stopped_by: str = "silence") -> Recording:
    return Recording(np.zeros(int(seconds * 16000), np.float32), seconds, speech, stopped_by)


def make_daemon(transcript: str = "hello there", recording: Recording | None = None, enabled: bool = True,
                source: str | None = "alsa_input.test", recorder=None):
    config = Config()
    config.brain.enabled = False
    config.speech.enabled = False
    config.voice = VoiceConfig(enabled=enabled)
    rec = recording or fake_recording()
    listener = Listener(
        config.voice,
        transcriber_factory=lambda cfg: (lambda audio: transcript),
        recorder=recorder or (lambda *args, **kwargs: rec),
        source_picker=lambda preferred: source,
    )
    daemon = Daemon(reactor=CannedReactor(), config=config, listener=listener)
    sink = Sink()
    daemon.hub.add(sink)  # type: ignore[arg-type]
    return daemon, sink


async def test_session_listens_thinks_and_answers():
    daemon, sink = make_daemon("what time is it")
    await daemon.start()
    assert daemon.listener.ready
    assert daemon.listen() == {"listening": True}
    result = await daemon.listen_task
    states = [m["state"] for m in sink.got]
    assert states == ["listening", "thinking", "talking"]
    assert sink.got[-1]["text"] == "You said: what time is it"
    assert result["transcript"] == "what time is it"
    assert daemon.listener.stats()["sessions"] == 1
    assert daemon.listener.stats()["last_transcript"] == "what time is it"
    assert daemon.rest_state == "idle"  # listening/thinking are transients, not resting states
    await daemon.close()


async def test_empty_transcript_gets_the_fixed_line():
    daemon, sink = make_daemon("", recording=fake_recording(speech=0.0, stopped_by="max"))
    await daemon.start()
    daemon.listen()
    await daemon.listen_task
    assert sink.got[-1]["text"] == DIDNT_CATCH
    assert daemon.listener.stats()["empty"] == 1
    await daemon.close()


async def test_second_poke_stops_the_recording_early():
    stop_seen = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow_recorder(source, stop: threading.Event, *args):
        loop.call_soon_threadsafe(stop_seen.set)
        stop.wait(timeout=5.0)
        return fake_recording(stopped_by="poke" if stop.is_set() else "max")

    daemon, sink = make_daemon("stop that", recorder=slow_recorder)
    await daemon.start()
    assert daemon.listen()["listening"] is True
    await stop_seen.wait()
    assert daemon.listen() == {"listening": False, "stopped": True}
    result = await daemon.listen_task
    assert result["transcript"] == "stop that"
    await daemon.close()


async def test_disabled_or_missing_microphone():
    daemon, _ = make_daemon(enabled=False)
    await daemon.start()
    assert daemon.listen()["error"] == "disabled in config"
    await daemon.close()

    daemon, sink = make_daemon(source=None)
    await daemon.start()
    daemon.listen()
    await daemon.listen_task
    assert sink.got[-1]["state"] == "talking" and "microphone" in sink.got[-1]["text"]
    await daemon.close()


async def test_listen_route(aiohttp_client):
    from strawberryd.server import create_app

    daemon, sink = make_daemon("route test")
    client = await aiohttp_client(create_app(daemon))
    response = await client.post("/listen")
    assert response.status == 200 and (await response.json()) == {"listening": True}
    await daemon.listen_task
    assert sink.got[-1]["text"] == "You said: route test"
    health = await (await client.get("/health")).json()
    assert health["voice"]["sessions"] == 1 and health["voice"]["ready"] is True
    refused = await client.post("/listen", headers={"Origin": "https://evil.example"})
    assert refused.status == 403


async def test_whisper_load_failure_disables_voice_not_daemon():
    def boom(cfg):
        raise RuntimeError("no model")

    config = Config()
    config.brain.enabled = False
    config.speech.enabled = False
    listener = Listener(VoiceConfig(enabled=True), transcriber_factory=boom)
    daemon = Daemon(reactor=CannedReactor(), config=config, listener=listener)
    await daemon.start()
    assert not daemon.listener.ready
    assert "no model" in daemon.listen()["error"]
    await daemon.close()


def test_dbfs():
    assert dbfs(np.zeros(160, np.float32)) == pytest.approx(-120.0)
    assert dbfs(np.full(160, 0.1, np.float32)) == pytest.approx(-20.0, abs=0.01)
