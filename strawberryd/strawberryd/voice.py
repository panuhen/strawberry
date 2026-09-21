"""Voice in (WIRING.md §7): hotkey -> she listens -> faster-whisper -> a voice event.

Lives inside the daemon so the whisper model loads once and stays resident; the hotkey is a
bare `POST /listen`. One session:

    listening   pw-record from the microphone (16 kHz mono) until a second of silence after
                speech, a second poke, or max_seconds
    thinking    faster-whisper on the CPU (the GPU stays with Ollama)
    talking     the transcript enters the normal event path as source=voice, so the brain
                answers in her voice; an empty transcript gets a fixed "didn't catch that"

Recording and transcription run in worker threads. Everything about audio devices and models
is behind small callables so the flow is unit-tested without a microphone or a model.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import select
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import numpy as np

from .config import VoiceConfig
from .contract import Performance
from .events import Event

log = logging.getLogger("strawberryd.voice")

RATE = 16000
CHUNK = 1600  # 0.1 s
NO_DATA_S = 3.0  # pw-record produced nothing: the capture never got linked
DIDNT_CATCH = "Sorry, I didn't catch that."


class Transcriber(Protocol):
    def __call__(self, audio: np.ndarray) -> str: ...


@dataclass
class Recording:
    audio: np.ndarray          # float32 mono at RATE
    seconds: float
    speech_seconds: float
    stopped_by: str            # silence | poke | max | end | error


def list_sources() -> list[str]:
    """Non-monitor PulseAudio/PipeWire sources, default first if it is a real input."""
    try:
        out = subprocess.run(["pactl", "list", "sources", "short"], capture_output=True, text=True, timeout=3).stdout
        default = subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return []
    names = [line.split("\t")[1] for line in out.splitlines() if "\t" in line]
    inputs = [n for n in names if ".monitor" not in n]
    if default in inputs:
        inputs.remove(default)
        inputs.insert(0, default)
    return inputs


def pick_source(preferred: str = "") -> str | None:
    sources = list_sources()
    if preferred:
        for s in sources:
            if preferred in s:
                return s
        return None
    return sources[0] if sources else None


HEADSET_PROFILES = ("headset-head-unit-msbc", "headset-head-unit", "headset-head-unit-cvsd")  # best first


@dataclass
class HeadsetCard:
    name: str
    active_profile: str
    headset_profile: str      # the best available HSP/HFP profile

    @property
    def needs_switch(self) -> bool:
        return not self.active_profile.startswith("headset")


def parse_cards(text: str) -> list[HeadsetCard]:
    """Bluetooth cards with a usable headset profile, from `pactl list cards` output.

    A Bluetooth headset is either hi-fi (A2DP, no microphone) or a headset (HSP/HFP, mic at
    16 kHz mono); never both at once. We switch it for the length of a recording.
    """
    cards: list[HeadsetCard] = []
    name = active = ""
    available: list[str] = []
    for raw in text.splitlines() + ["Card #end"]:
        line = raw.strip()
        if line.startswith("Card #"):
            if name.startswith("bluez_card") and available:
                best = next(p for p in HEADSET_PROFILES if p in available)
                cards.append(HeadsetCard(name, active, best))
            name = active = ""
            available = []
        elif line.startswith("Name:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("Active Profile:"):
            active = line.split(":", 1)[1].strip()
        else:
            for profile in HEADSET_PROFILES:
                if line.startswith(profile + ":") and "available: yes" in line:
                    available.append(profile)
    return cards


def headset_card() -> HeadsetCard | None:
    try:
        text = subprocess.run(["pactl", "list", "cards"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    cards = parse_cards(text)
    return cards[0] if cards else None


def set_profile(card: str, profile: str) -> None:
    subprocess.run(["pactl", "set-card-profile", card, profile], check=False, timeout=5)


def acquire_microphone(preferred: str, prefer_bluetooth: bool) -> tuple[str | None, Callable[[], None]]:
    """Blocking. Returns (source name, restore) where restore() undoes any profile switch.

    Order: an explicit `preferred` fragment; else a connected Bluetooth headset (switched to its
    mic profile for the duration); else the first real input.
    """
    if preferred:
        return pick_source(preferred), (lambda: None)
    card = headset_card() if prefer_bluetooth else None
    if card is not None:
        previous = card.active_profile
        if card.needs_switch:
            set_profile(card.name, card.headset_profile)
        for _ in range(30):  # the bluez_input source appears within ~0.1 s; the audio path a bit later
            source = next((s for s in list_sources() if s.startswith("bluez_input")), None)
            if source:
                time.sleep(0.4)
                restore = (lambda: set_profile(card.name, previous)) if card.needs_switch else (lambda: None)
                return source, restore
            time.sleep(0.1)
        if card.needs_switch:
            set_profile(card.name, previous)
        log.warning("voice: headset %s switched to %s but no bluez_input source appeared", card.name, card.headset_profile)
    return pick_source(""), (lambda: None)


def dbfs(chunk: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(chunk * chunk))) if len(chunk) else 0.0
    return 20.0 * math.log10(max(rms, 1e-6))


def record(source: str, stop: threading.Event, max_seconds: float, silence_s: float,
           min_speech_s: float, level_db: float) -> Recording:
    """Blocking: capture from `source` until silence after speech, `stop`, or `max_seconds`.

    Speech is any 0.1 s chunk louder than max(noise floor + 12 dB, level_db), the noise floor
    being the quietest chunk heard so far. Leading silence before the first speech is kept short
    so whisper is not fed ten seconds of room tone.
    """
    cmd = ["pw-record", "--target", source, "--rate", str(RATE), "--channels", "1", "--format", "s16",
           "--latency", "50ms", "-"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log.error("pw-record failed: %s", exc)
        return Recording(np.zeros(0, np.float32), 0.0, 0.0, "error")
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    chunks: list[np.ndarray] = []
    floor = 0.0
    speech = 0.0
    silence = 0.0
    heard_speech = False
    stopped_by = "end"
    started = time.monotonic()
    last_data = started
    pending = b""
    try:
        while True:
            # select(): a capture PipeWire never links delivers nothing, and a blocking read
            # would hang here forever with her stuck in the listening pose.
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                raw = os.read(fd, CHUNK * 2)
                if not raw:
                    break
                last_data = time.monotonic()
                pending += raw
                while len(pending) >= CHUNK * 2:
                    chunk = np.frombuffer(pending[: CHUNK * 2], dtype=np.int16).astype(np.float32) / 32768.0
                    pending = pending[CHUNK * 2:]
                    chunks.append(chunk)
                    level = dbfs(chunk)
                    floor = level if len(chunks) == 1 else min(floor, level)
                    threshold = max(floor + 12.0, level_db)
                    if level > threshold:
                        speech += 0.1
                        silence = 0.0
                        heard_speech = heard_speech or speech >= min_speech_s
                    else:
                        silence += 0.1
            elif time.monotonic() - last_data > NO_DATA_S:
                log.warning("voice: no audio from %s for %.0fs; giving up", source, time.monotonic() - last_data)
                stopped_by = "error"
                break
            elapsed = time.monotonic() - started
            if stop.is_set():
                stopped_by = "poke"
                break
            if heard_speech and silence >= silence_s:
                stopped_by = "silence"
                break
            if elapsed >= max_seconds:
                stopped_by = "max"
                break
    finally:
        proc.kill()
        proc.wait(timeout=2)
    audio = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    return Recording(audio, len(audio) / RATE, speech if heard_speech else 0.0, stopped_by)


def whisper_transcriber(voice: VoiceConfig) -> Transcriber:
    """Load faster-whisper once; import is local so tests never need the package or a model."""
    from faster_whisper import WhisperModel

    model = WhisperModel(voice.model, device=voice.device, compute_type=voice.compute_type)

    def transcribe(audio: np.ndarray) -> str:
        segments, _info = model.transcribe(
            audio, language=voice.language or None, beam_size=voice.beam_size, vad_filter=True,
            condition_on_previous_text=False,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()

    return transcribe


class Listener:
    def __init__(
        self,
        config: VoiceConfig,
        transcriber_factory: Callable[[VoiceConfig], Transcriber] | None = None,
        recorder: Callable[..., Recording] | None = None,
        source_picker: Callable[[str], str | None] | None = None,
        microphone: Callable[[str, bool], tuple[str | None, Callable[[], None]]] | None = None,
    ) -> None:
        self.config = config
        self.transcriber_factory = transcriber_factory or whisper_transcriber
        self.recorder = recorder or record
        # Tests inject a plain picker; production acquires the mic (Bluetooth profile switch included).
        if microphone is not None:
            self.microphone = microphone
        elif source_picker is not None:
            self.microphone = lambda preferred, _bt: (source_picker(preferred), (lambda: None))
        else:
            self.microphone = acquire_microphone
        self.transcriber: Transcriber | None = None
        self.stop = threading.Event()
        self.busy = False
        self.phase = "idle"          # idle | listening | thinking
        self.sessions = 0
        self.empty = 0
        self.last_transcript: str | None = None
        self.last_ms = 0.0
        self.load_s: float | None = None
        self.bluetooth_ok = True     # cleared after a Bluetooth mic delivers nothing (SCO failure); analog then
        self.disabled_reason: str | None = None if config.enabled else "disabled in config"

    @property
    def ready(self) -> bool:
        return self.transcriber is not None and self.disabled_reason is None

    async def start(self) -> None:
        if not self.config.enabled:
            log.info("voice disabled in config")
            return
        started = time.perf_counter()
        try:
            self.transcriber = await asyncio.to_thread(self.transcriber_factory, self.config)
        except Exception as exc:  # missing package, model download failure, bad device
            self.disabled_reason = f"whisper failed to load: {exc}"
            log.error("%s", self.disabled_reason)
            return
        self.load_s = time.perf_counter() - started
        log.info("voice: whisper %s (%s/%s) ready in %.1fs", self.config.model, self.config.device,
                 self.config.compute_type, self.load_s)

    async def close(self) -> None:
        self.stop.set()
        self.transcriber = None

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "model": self.config.model if self.config.enabled else None,
            "ready": self.ready,
            "reason": self.disabled_reason,
            "phase": self.phase,
            "bluetooth_ok": self.bluetooth_ok,
            "sessions": self.sessions,
            "empty": self.empty,
            "last_transcript": self.last_transcript,
            "last_ms": round(self.last_ms, 1),
        }

    async def session(self, daemon: Any) -> dict[str, Any]:
        """One listen -> think -> answer cycle. `daemon` performs states and handles the event."""
        self.busy = True
        self.sessions += 1
        self.stop.clear()
        started = time.perf_counter()
        restore: Callable[[], None] = lambda: None
        try:
            self.phase = "listening"
            await daemon.perform(Performance(state="listening"))
            source, restore = await asyncio.to_thread(self.microphone, self.config.source, self.config.bluetooth and self.bluetooth_ok)
            if source is None:
                log.warning("voice: no microphone source found (preferred %r)", self.config.source)
                await daemon.perform(Performance(state="talking", text="I can't find a microphone.", emotion="alert"))
                return {"transcript": None, "error": "no microphone"}
            try:
                rec: Recording = await asyncio.to_thread(
                    self.recorder, source, self.stop, self.config.max_seconds, self.config.silence_s,
                    self.config.min_speech_s, self.config.level_db,
                )
            finally:
                await asyncio.to_thread(restore)  # hi-fi profile back before she starts talking
            log.info("voice: recorded %.1fs (%.1fs speech, stopped by %s) from %s", rec.seconds, rec.speech_seconds,
                     rec.stopped_by, source)
            if rec.stopped_by == "error" and source.startswith("bluez_input"):
                # The headset's HFP audio link is broken on this machine; stop switching it and
                # interrupting the music for nothing. Next session uses the wired input.
                self.bluetooth_ok = False
                log.warning("voice: Bluetooth microphone %s delivered no audio; not using it again this run", source)
            self.phase = "thinking"
            await daemon.perform(Performance(state="thinking"))
            text = ""
            if rec.speech_seconds > 0.0 and self.transcriber is not None:
                text = await asyncio.to_thread(self.transcriber, rec.audio)
            text = " ".join(text.split())[:500]
            self.last_ms = (time.perf_counter() - started) * 1000
            self.last_transcript = text or None
            if not text:
                self.empty += 1
                await daemon.perform(Performance(state="talking", text=DIDNT_CATCH))
                return {"transcript": "", "seconds": rec.seconds}
            log.info("voice: heard %r in %.0f ms", text, self.last_ms)
            performance, _sent = await daemon.handle_event(Event(source="voice", title=text))
            return {"transcript": text, "seconds": rec.seconds, "performance": performance.to_dict()}
        finally:
            self.phase = "idle"
            self.busy = False
