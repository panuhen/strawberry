"""Her voice: Piper text-to-speech on the CPU (WIRING.md §6).

The daemon hands `Speaker.say()` the line the brain wrote and gets back a wav path for the
blob's `audio` field, or None when she should stay silent (speech off, quiet hours, no
voice installed, synthesis failed). Silence is never an error: the bubble still shows.

Piper runs in a worker thread so the event loop stays free; a medium voice takes about
0.1 s per line on this machine. Wavs land in a per-daemon temp directory and the last few
are kept so a widget that is still playing one is not cut off.
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
import wave
from collections import deque
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Callable, Protocol

from . import paths
from .config import SpeechConfig

log = logging.getLogger("strawberryd.speech")

DOWNLOAD_HINT = "strawberry voices {voice} (or: python -m piper.download_voices --download-dir {dir} {voice})"
_QUIET_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


class Synth(Protocol):
    """Anything that writes speech for `text` into an open wave file."""

    def __call__(self, text: str, wav: wave.Wave_write) -> None: ...


def default_voices_dir() -> Path:
    return paths.voices_dir()


def resolve_voice(voice: str, voices_dir: Path) -> Path:
    """A voice is a Piper name (`en_GB-jenny_dioco-medium`) looked up in voices_dir, or a path."""
    candidate = Path(voice).expanduser()
    if candidate.suffix == ".onnx" and candidate.is_file():
        return candidate
    return voices_dir / f"{voice}.onnx"


def parse_quiet_hours(spec: str) -> tuple[dtime, dtime] | None:
    """'22:00-08:00' -> (22:00, 08:00). Empty means never quiet. Bad specs raise ValueError."""
    if not spec.strip():
        return None
    m = _QUIET_RE.match(spec)
    if not m:
        raise ValueError(f"quiet_hours must look like \"22:00-08:00\", got {spec!r}")
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    if not (0 <= h1 <= 23 and 0 <= h2 <= 23 and 0 <= m1 <= 59 and 0 <= m2 <= 59):
        raise ValueError(f"quiet_hours out of range: {spec!r}")
    return dtime(h1, m1), dtime(h2, m2)


def in_quiet_hours(window: tuple[dtime, dtime] | None, now: dtime) -> bool:
    if window is None:
        return False
    start, end = window
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end  # wraps midnight


def strip_for_speech(text: str) -> str:
    """Bubble text is fine to read aloud once emoji and odd symbols are gone."""
    text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿⬀-⯿️]", "", text)
    text = text.replace("–", ",").replace("—", ",").replace("…", ".")
    return re.sub(r"\s+", " ", text).strip()


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate() or 1)


def piper_synth(model_path: Path, speed: float, volume: float) -> Synth:
    """Load a Piper voice and return a synth callable. Import is local: tests never need Piper."""
    from piper import PiperVoice, SynthesisConfig

    voice = PiperVoice.load(model_path)
    # length_scale stretches phonemes: 2.0 is half speed, so speed 1.25 -> 0.8.
    syn = SynthesisConfig(length_scale=1.0 / max(speed, 0.25), volume=volume)

    def synth(text: str, wav: wave.Wave_write) -> None:
        voice.synthesize_wav(text, wav, syn_config=syn)

    return synth


class Speaker:
    def __init__(
        self,
        config: SpeechConfig,
        synth_factory: Callable[[Path, float, float], Synth] | None = None,
        clock: Callable[[], dtime] | None = None,
    ) -> None:
        self.config = config
        self.synth_factory = synth_factory or piper_synth
        self.clock = clock or (lambda: datetime.now().time())
        self.quiet = parse_quiet_hours(config.quiet_hours)
        self.voices_dir = Path(config.voices_dir).expanduser() if config.voices_dir else default_voices_dir()
        self.model_path = resolve_voice(config.voice, self.voices_dir)
        self.synth: Synth | None = None
        self.out_dir: Path | None = None
        self.recent: deque[Path] = deque()
        self.spoken = 0
        self.failed = 0
        self.last_ms = 0.0
        self.disabled_reason: str | None = None if config.enabled else "disabled in config"

    @property
    def ready(self) -> bool:
        return self.synth is not None and self.disabled_reason is None

    async def start(self) -> None:
        if not self.config.enabled:
            log.info("speech disabled in config; silent mode")
            return
        if not self.model_path.is_file():
            self.disabled_reason = f"voice not installed: {self.model_path}"
            log.warning("%s. Install it with: %s", self.disabled_reason,
                        DOWNLOAD_HINT.format(dir=self.voices_dir, voice=self.config.voice))
            return
        started = time.perf_counter()
        try:
            self.synth = await asyncio.to_thread(self.synth_factory, self.model_path, self.config.speed, self.config.volume)
        except Exception as exc:  # a bad model file should not take the daemon down
            self.disabled_reason = f"voice failed to load: {exc}"
            log.error("%s", self.disabled_reason)
            return
        self.out_dir = Path(tempfile.mkdtemp(prefix="strawberry-speech-"))
        log.info("voice %s ready in %.1fs (wavs in %s)", self.model_path.stem, time.perf_counter() - started, self.out_dir)

    async def close(self) -> None:
        for path in list(self.recent):
            path.unlink(missing_ok=True)
        self.recent.clear()
        if self.out_dir is not None:
            try:
                self.out_dir.rmdir()
            except OSError:
                pass
            self.out_dir = None
        self.synth = None

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "voice": self.model_path.stem if self.config.enabled else None,
            "ready": self.ready,
            "reason": self.disabled_reason,
            "quiet_now": in_quiet_hours(self.quiet, self.clock()),
            "spoken": self.spoken,
            "failed": self.failed,
            "last_ms": round(self.last_ms, 1),
        }

    async def say(self, text: str) -> str | None:
        """Synthesise `text`; return the wav path or None for silence."""
        if not self.ready or self.out_dir is None:
            return None
        if in_quiet_hours(self.quiet, self.clock()):
            return None
        spoken = strip_for_speech(text)[: self.config.max_chars]
        if not spoken:
            return None
        path = self.out_dir / f"line-{self.spoken + self.failed:06d}.wav"
        started = time.perf_counter()
        try:
            await asyncio.to_thread(self._write, spoken, path)
        except Exception as exc:
            self.failed += 1
            log.warning("speech failed, performing silently: %s", exc)
            path.unlink(missing_ok=True)
            return None
        self.last_ms = (time.perf_counter() - started) * 1000
        self.spoken += 1
        self.recent.append(path)
        while len(self.recent) > self.config.keep_files:
            self.recent.popleft().unlink(missing_ok=True)
        log.info("spoke %d chars in %.0f ms -> %s", len(spoken), self.last_ms, path.name)
        return str(path)

    def _write(self, text: str, path: Path) -> None:
        assert self.synth is not None
        with wave.open(str(path), "wb") as wav:
            self.synth(text, wav)
