"""Speaker plumbing with a fake synth: no Piper, no voice files, no sound card."""

from __future__ import annotations

import json
import struct
import wave
from datetime import time as dtime
from pathlib import Path

import pytest

from strawberryd.config import Config, ConfigError, SpeechConfig, load
from strawberryd.contract import Performance
from strawberryd.daemon import Daemon
from strawberryd.events import CannedReactor
from strawberryd.speech import (
    Speaker,
    in_quiet_hours,
    parse_quiet_hours,
    resolve_voice,
    strip_for_speech,
    wav_seconds,
)


def fake_synth(text: str, wav: wave.Wave_write) -> None:
    """One frame per character at 100 Hz, so a 10-char line is a 0.1 s wav."""
    wav.setnchannels(1)
    wav.setsampwidth(2)
    wav.setframerate(100)
    wav.writeframes(struct.pack("<h", 1000) * len(text))


def fake_factory(model_path: Path, speed: float, volume: float):
    assert model_path.is_file()
    return fake_synth


def failing_synth(text: str, wav: wave.Wave_write) -> None:
    raise RuntimeError("onnx exploded")


@pytest.fixture
def voice(tmp_path: Path) -> SpeechConfig:
    (tmp_path / "voices").mkdir()
    (tmp_path / "voices" / "test-voice.onnx").write_bytes(b"not really a model")
    return SpeechConfig(enabled=True, voice="test-voice", voices_dir=str(tmp_path / "voices"), keep_files=2)


async def test_disabled_speaker_is_silent(tmp_path: Path):
    speaker = Speaker(SpeechConfig(enabled=False), synth_factory=fake_factory)
    await speaker.start()
    assert not speaker.ready
    assert await speaker.say("hello") is None
    assert speaker.stats()["reason"] == "disabled in config"


async def test_missing_voice_disables_with_reason(tmp_path: Path):
    cfg = SpeechConfig(enabled=True, voice="nope", voices_dir=str(tmp_path))
    speaker = Speaker(cfg, synth_factory=fake_factory)
    await speaker.start()
    assert not speaker.ready
    assert "voice not installed" in speaker.stats()["reason"]
    assert await speaker.say("hello") is None


async def test_say_writes_wav_and_prunes(voice: SpeechConfig):
    speaker = Speaker(voice, synth_factory=fake_factory)
    await speaker.start()
    assert speaker.ready
    paths = [await speaker.say(f"line {i}") for i in range(3)]
    assert all(paths)
    assert wav_seconds(Path(paths[-1])) == pytest.approx(0.06)  # "line 2" is 6 chars at 100 Hz
    assert not Path(paths[0]).exists()  # keep_files=2 pruned the oldest
    assert Path(paths[1]).exists() and Path(paths[2]).exists()
    assert speaker.stats()["spoken"] == 3
    await speaker.close()
    assert not Path(paths[2]).exists()
    assert speaker.out_dir is None


async def test_synth_failure_is_silent_not_fatal(voice: SpeechConfig):
    speaker = Speaker(voice, synth_factory=lambda *_: failing_synth)
    await speaker.start()
    assert await speaker.say("boom") is None
    assert speaker.stats()["failed"] == 1
    assert speaker.ready  # one bad line does not switch her off


async def test_quiet_hours_keep_her_silent(voice: SpeechConfig):
    voice.quiet_hours = "22:00-08:00"
    night = Speaker(voice, synth_factory=fake_factory, clock=lambda: dtime(23, 30))
    day = Speaker(voice, synth_factory=fake_factory, clock=lambda: dtime(12, 0))
    await night.start()
    await day.start()
    assert await night.say("shh") is None
    assert night.stats()["quiet_now"] is True
    assert await day.say("hi") is not None


async def test_empty_after_stripping_is_silent(voice: SpeechConfig):
    speaker = Speaker(voice, synth_factory=fake_factory)
    await speaker.start()
    assert await speaker.say("🦀✨") is None
    assert speaker.stats()["spoken"] == 0


def test_strip_for_speech():
    assert strip_for_speech("James, hold the phone – the time is up! 🦀") == "James, hold the phone , the time is up!"
    assert strip_for_speech("Wait…  what") == "Wait. what"


def test_resolve_voice(tmp_path: Path):
    model = tmp_path / "custom.onnx"
    model.write_bytes(b"x")
    assert resolve_voice(str(model), tmp_path / "voices") == model
    assert resolve_voice("en_US-amy-medium", tmp_path / "voices") == tmp_path / "voices" / "en_US-amy-medium.onnx"


def test_quiet_hours_parsing_and_wrap():
    assert parse_quiet_hours("") is None
    window = parse_quiet_hours("22:00-08:00")
    assert in_quiet_hours(window, dtime(23, 0)) and in_quiet_hours(window, dtime(7, 59))
    assert not in_quiet_hours(window, dtime(8, 0)) and not in_quiet_hours(window, dtime(12, 0))
    daytime = parse_quiet_hours("09:00-17:00")
    assert in_quiet_hours(daytime, dtime(12, 0)) and not in_quiet_hours(daytime, dtime(20, 0))
    with pytest.raises(ValueError):
        parse_quiet_hours("late-early")
    with pytest.raises(ValueError):
        parse_quiet_hours("25:00-08:00")


def test_config_section_and_validation(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[speech]\nenabled = true\nvoice = "en_US-amy-medium"\nspeed = 1.2\nquiet_hours = "22:00-07:30"\n')
    config = load(path, env={})
    assert config.speech.enabled and config.speech.voice == "en_US-amy-medium"
    assert config.speech.speed == pytest.approx(1.2)
    assert config.to_dict()["speech"]["quiet_hours"] == "22:00-07:30"
    path.write_text('[speech]\nquiet_hours = "bedtime"\n')
    with pytest.raises(ConfigError, match="quiet_hours"):
        load(path, env={})
    path.write_text("[speech]\nspeed = 9\n")
    with pytest.raises(ConfigError, match="speech.speed"):
        load(path, env={})


async def test_daemon_voices_lines_but_keeps_given_audio(voice: SpeechConfig, tmp_path: Path):
    config = Config()
    config.brain.enabled = False
    speaker = Speaker(voice, synth_factory=fake_factory)
    daemon = Daemon(reactor=CannedReactor(), config=config, speaker=speaker)
    await daemon.start()
    sent: list[dict] = []

    class Sink:
        closed = False

        async def send_str(self, text: str):
            sent.append(json.loads(text))

    daemon.hub.add(Sink())  # type: ignore[arg-type]
    await daemon.perform(Performance(state="talking", text="Hello there"))
    assert sent[-1]["audio"].endswith(".wav")
    assert wav_seconds(Path(sent[-1]["audio"])) == pytest.approx(0.11)

    own = tmp_path / "own.wav"
    with wave.open(str(own), "wb") as w:
        fake_synth("x", w)
    await daemon.perform(Performance(state="talking", text="Hi", audio=str(own)))
    assert sent[-1]["audio"] == str(own)

    await daemon.perform(Performance(state="idle"))
    assert "audio" not in sent[-1]
    await daemon.close()
