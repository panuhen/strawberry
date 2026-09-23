"""Voice in (WIRING.md §7): hotkey -> she listens -> faster-whisper -> a voice event.

Lives inside the daemon so the whisper model loads once and stays resident; the hotkey is a
bare `POST /listen`. One session:

    listening   the microphone at 16 kHz mono (pw-record on Linux, WASAPI on Windows through
                winmic.py) until a second of silence after speech, a second poke, or max_seconds
    thinking    faster-whisper on the CPU (the GPU stays with Ollama; `device = "cuda"` if it has room)
    talking     the transcript enters the normal event path as source=voice, so the brain
                answers in her voice; an empty transcript gets a fixed "didn't catch that"

The model loads in a thread of its own at start, so the daemon answers meanwhile (a first start
downloads it, 1.5 GB for `medium`); until then /listen gets EARS_LOADING.

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
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import numpy as np
from pathlib import Path

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
    """Blocking: pw-record from `source` until silence after speech, `stop`, or `max_seconds`
    (capture() says when)."""
    cmd = ["pw-record", "--target", source, "--rate", str(RATE), "--channels", "1", "--format", "s16",
           "--latency", "50ms", "-"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log.error("pw-record failed: %s", exc)
        return Recording(np.zeros(0, np.float32), 0.0, 0.0, "error")
    assert proc.stdout is not None
    fd = proc.stdout.fileno()

    def read(timeout: float) -> bytes | None:
        # select(): a capture PipeWire never links delivers nothing, and a blocking read
        # would hang here forever with her stuck in the listening pose.
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return b""
        return os.read(fd, CHUNK * 2) or None

    try:
        return capture(read, source, stop, max_seconds, silence_s, min_speech_s, level_db)
    finally:
        proc.kill()
        proc.wait(timeout=2)


def capture(read: Callable[[float], bytes | None], source: str, stop: threading.Event, max_seconds: float,
            silence_s: float, min_speech_s: float, level_db: float) -> Recording:
    """Blocking: 16 kHz mono s16 from `read` until silence after speech, `stop`, or `max_seconds`.

    `read(timeout)` returns what arrived within `timeout` (b"" for nothing yet) or None at the end
    of the stream; the recorder of each system supplies it (pw-record on Linux, WASAPI on
    Windows: winmic.py). Speech is any 0.1 s chunk louder than max(noise floor + 12 dB,
    level_db), the noise floor being the quietest chunk heard so far.
    """
    chunks: list[np.ndarray] = []
    floor = 0.0
    speech = 0.0
    silence = 0.0
    heard_speech = False
    stopped_by = "end"
    started = time.monotonic()
    last_data = started
    pending = b""
    while True:
        raw = read(0.2)
        if raw is None:
            break
        if raw:
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
    audio = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    return Recording(audio, len(audio) / RATE, speech if heard_speech else 0.0, stopped_by)


def default_backend() -> tuple[Callable[..., Recording], Callable[[str, bool], tuple[str | None, Callable[[], None]]]]:
    """(recorder, microphone) for this system: pw-record and pactl on Linux, WASAPI on Windows
    (winmic.py, which imports sounddevice only when it opens the microphone)."""
    if sys.platform == "win32":
        from . import winmic

        return winmic.record, winmic.acquire_microphone
    return record, acquire_microphone


# Windows: what ctranslate2 loads by name and does not ship, in dependency order (cublas64
# imports cublasLt64). Its wheel carries its own cudnn64_9.dll, which loads the rest of cuDNN
# only if it needs it, by name, from the search path (whisper on this machine did not).
WINDOWS_CUDA_DLLS = ("cublasLt64_", "cublas64_")


def preload_cuda_libraries() -> list[str]:
    """ctranslate2 dlopens cuBLAS and cuDNN by soname; the pip wheels (dependency group `gpu`:
    nvidia-cublas-cu12, nvidia-cudnn-cu12) put them where no loader looks. Load them by path
    first, the way faster-whisper's own docs suggest. Returns what was loaded."""
    import ctypes
    import importlib.util

    loaded = []
    for package in ("nvidia.cublas", "nvidia.cudnn"):
        try:
            spec = importlib.util.find_spec(package)
        except ModuleNotFoundError:          # no `nvidia` namespace at all: the gpu group is not installed
            break
        if spec is None or not spec.submodule_search_locations:
            continue
        if sys.platform == "win32":
            loaded += _preload_windows_dlls(Path(list(spec.submodule_search_locations)[0]) / "bin")
            continue
        lib_dir = Path(list(spec.submodule_search_locations)[0]) / "lib"
        for so in sorted(lib_dir.glob("*.so*")):
            if so.name.startswith(("libcublas", "libcudnn")) and ".so." in so.name and so.name.count(".") <= 3:
                try:
                    ctypes.CDLL(str(so), mode=ctypes.RTLD_GLOBAL)
                    loaded.append(so.name)
                except OSError as exc:
                    log.debug("voice: could not preload %s (%s)", so.name, exc)
    return loaded


def _preload_windows_dlls(bin_dir: Path) -> list[str]:
    """Windows keeps the wheels' DLLs in `nvidia/<name>/bin`, and ctranslate2 asks for
    cublas64_12.dll by name, which the loader looks for beside the program and on PATH, not
    there. (A CUDA toolkit on PATH hides this: its bin has cuBLAS too.) A DLL loaded by full path
    first is the one a later load by name gets; the directory also goes on PATH and the DLL
    search path for what those load in turn."""
    import ctypes

    if not bin_dir.is_dir():
        return []
    os.add_dll_directory(str(bin_dir))
    if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
    loaded = []
    for prefix in WINDOWS_CUDA_DLLS:
        for dll in sorted(bin_dir.glob(f"{prefix}*.dll")):
            try:
                ctypes.WinDLL(str(dll))
                loaded.append(dll.name)
            except OSError as exc:
                log.debug("voice: could not preload %s (%s)", dll.name, exc)
    return loaded


# What faster-whisper downloads for each size (model.bin and the tokenizer, Hugging Face's own
# numbers rounded), for the first-start line and `strawberry setup`. A path or an unknown name: "".
WHISPER_SIZES = {
    "tiny": "75 MB", "tiny.en": "75 MB", "base": "145 MB", "base.en": "145 MB",
    "small": "480 MB", "small.en": "480 MB", "distil-small.en": "335 MB",
    "medium": "1.5 GB", "medium.en": "1.5 GB", "distil-medium.en": "790 MB",
    "large-v1": "3.1 GB", "large-v2": "3.1 GB", "large-v3": "3.1 GB", "large": "3.1 GB",
    "distil-large-v2": "1.5 GB", "distil-large-v3": "1.5 GB", "distil-large-v3.5": "1.5 GB",
    "large-v3-turbo": "1.6 GB", "turbo": "1.6 GB",
}
EARS_LOADING = "I'm still getting my ears on."   # /listen while whisper loads


def whisper_cached(model: str) -> bool | None:
    """True when faster-whisper has `model` on disk (a path, or a size in the Hugging Face
    cache), False when loading it would download it first, None when faster-whisper is not
    installed. Local files only: nothing goes to huggingface.co."""
    if os.path.isdir(model):
        return True
    try:
        from faster_whisper.utils import download_model
    except ImportError:
        return None
    try:
        snapshot = download_model(model, local_files_only=True)
    except Exception:  # not in the cache (huggingface_hub's LocalEntryNotFoundError), or no such size
        return False
    # An interrupted download leaves the snapshot without its model.bin.
    return (Path(snapshot) / "model.bin").is_file()


def fetch_whisper(model: str) -> str:
    """Download `model` into the Hugging Face cache with its progress bars on this terminal
    (`strawberry setup`; faster-whisper's own download is silent). Returns the snapshot path."""
    from faster_whisper import utils

    repo = getattr(utils, "_MODELS", {}).get(model)
    if repo is None:                      # a repo id, or a faster-whisper without the table
        return utils.download_model(model)
    import huggingface_hub

    return huggingface_hub.snapshot_download(
        repo, allow_patterns=["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"])


def whisper_transcriber(voice: VoiceConfig) -> Transcriber:
    """Load faster-whisper once; import is local so tests never need the package or a model."""
    if voice.device in ("cuda", "auto"):
        loaded = preload_cuda_libraries()
        log.info("voice: CUDA libraries preloaded: %s", ", ".join(loaded) or "none found (uv sync --group gpu)")
    from faster_whisper import WhisperModel

    try:
        # A cached model must not phone huggingface.co on every start (nothing leaves the machine).
        model = WhisperModel(voice.model, device=voice.device, compute_type=voice.compute_type, local_files_only=True)
    except Exception:  # not downloaded yet: this once, fetch it
        model = WhisperModel(voice.model, device=voice.device, compute_type=voice.compute_type)

    def transcribe(audio: np.ndarray, hotwords: str = "") -> str:
        # `hotwords` biases decoding toward these names without asserting they were said.
        segments, _info = model.transcribe(
            audio, language=voice.language or None, beam_size=voice.beam_size, vad_filter=True,
            condition_on_previous_text=False, hotwords=hotwords or None,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()

    return transcribe


def in_thread(fn: Callable[[], Any], name: str) -> asyncio.Future:
    """`fn()` in a daemon thread of its own, as a future of this loop. Not asyncio.to_thread: the
    loop's executor is joined at interpreter exit, so a stop during a download would wait for it."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def settle(result: Any, exc: BaseException | None) -> None:
        if future.done():             # cancelled meanwhile (the daemon is stopping)
            return
        if exc is not None:
            future.set_exception(exc)
        else:
            future.set_result(result)

    def work() -> None:
        result, error = None, None
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - handed to the loop
            error = exc
        try:
            loop.call_soon_threadsafe(settle, result, error)
        except RuntimeError:          # the loop is closed: the daemon stopped before the load ended
            pass

    threading.Thread(target=work, name=name, daemon=True).start()
    return future


class Listener:
    def __init__(
        self,
        config: VoiceConfig,
        transcriber_factory: Callable[[VoiceConfig], Transcriber] | None = None,
        recorder: Callable[..., Recording] | None = None,
        source_picker: Callable[[str], str | None] | None = None,
        microphone: Callable[[str, bool], tuple[str | None, Callable[[], None]]] | None = None,
        model_cached: Callable[[str], bool | None] | None = None,
    ) -> None:
        self.config = config
        self.transcriber_factory = transcriber_factory or whisper_transcriber
        # Whether the load will download first; unknown (None) for an injected factory.
        self.model_cached = model_cached or (whisper_cached if transcriber_factory is None else (lambda model: None))
        system_recorder, system_microphone = default_backend()
        self.recorder = recorder or system_recorder
        # Tests inject a plain picker; production acquires the mic (Bluetooth profile switch included).
        if microphone is not None:
            self.microphone = microphone
        elif source_picker is not None:
            self.microphone = lambda preferred, _bt: (source_picker(preferred), (lambda: None))
        else:
            self.microphone = system_microphone
        self.transcriber: Transcriber | None = None
        self.stop = threading.Event()
        self.busy = False
        self.phase = "idle"          # loading | idle | listening | thinking
        self.sessions = 0
        self.empty = 0
        self.last_transcript: str | None = None
        self.last_ms = 0.0
        self.load_s: float | None = None
        self.load_task: asyncio.Task | None = None
        self.load_done = threading.Event()   # set by the load's thread when it returns, even after close()
        self.load_started = 0.0
        self.loading_reason = ""     # what /health says while whisper loads
        self.bluetooth_ok = True     # cleared after a Bluetooth mic delivers nothing (SCO failure); analog then
        self.disabled_reason: str | None = None if config.enabled else "disabled in config"

    @property
    def ready(self) -> bool:
        return self.transcriber is not None and self.disabled_reason is None

    @property
    def loading(self) -> bool:
        return self.load_task is not None and not self.load_task.done()

    @property
    def load_running(self) -> bool:
        """The load's thread is still at work (also after close() gave up on it)."""
        return self.load_task is not None and not self.load_done.is_set()

    @property
    def reason(self) -> str | None:
        """Why she cannot listen now: off, failed, or still loading; None when ready."""
        if self.disabled_reason:
            return self.disabled_reason
        return self.loading_reason if self.loading else None

    async def start(self) -> None:
        """Start loading whisper and return: the load runs in a thread of its own and the daemon
        answers meanwhile. A model not in the Hugging Face cache is downloaded first (1.5 GB for
        `medium`), which took minutes on a first start while the daemon did not answer at all."""
        if not self.config.enabled:
            log.info("voice disabled in config")
            return
        self.phase = "loading"
        self.loading_reason = f"loading whisper {self.config.model}"
        self.load_started = time.perf_counter()
        self.load_task = asyncio.get_running_loop().create_task(self._load())

    async def _load(self) -> None:
        downloaded: list[bool | None] = [None]

        def load() -> Transcriber:
            try:
                return work()
            finally:
                self.load_done.set()

        def work() -> Transcriber:
            cached = self.model_cached(self.config.model)
            downloaded[0] = None if cached is None else not cached
            where = f"{self.config.model} ({self.config.device}/{self.config.compute_type})"
            if cached is False:
                size = WHISPER_SIZES.get(self.config.model)
                self.loading_reason = (f"loading whisper {self.config.model} (first use: downloading "
                                       f"{'~' + size if size else 'it'} from Hugging Face)")
                log.info("voice: whisper %s is not in the cache; downloading %s first, in the background",
                         where, f"~{size}" if size else "it")
            else:
                log.info("voice: loading whisper %s in the background", where)
            return self.transcriber_factory(self.config)

        try:
            self.transcriber = await in_thread(load, "whisper-load")
        except Exception as exc:  # missing package, model download failure, bad device
            self.disabled_reason = f"whisper failed to load: {exc}"
            log.error("%s (after %.1fs)", self.disabled_reason, time.perf_counter() - self.load_started)
            return
        finally:
            if self.phase == "loading":
                self.phase = "idle"
        self.load_s = time.perf_counter() - self.load_started
        how = {True: "downloaded and loaded", False: "loaded from the cache"}.get(downloaded[0], "loaded")
        log.info("voice: whisper %s (%s/%s) ready in %.1fs (%s)", self.config.model, self.config.device,
                 self.config.compute_type, self.load_s, how)

    async def loaded(self) -> bool:
        """Wait for the load that start() began (tests, the probe); True when she can listen."""
        if self.load_task is not None:
            await asyncio.shield(self.load_task)
        return self.ready

    async def close(self) -> None:
        self.stop.set()
        if self.load_task is not None and not self.load_task.done():
            # The thread itself cannot be stopped; it is a daemon thread, so the exit does not
            # wait for a download to finish, and what it returns is dropped.
            self.load_task.cancel()
        self.transcriber = None

    def load_seconds(self) -> float | None:
        """How long whisper took to load, or has been loading so far; None before and after a failure."""
        if self.loading:
            return round(time.perf_counter() - self.load_started, 1)
        return round(self.load_s, 1) if self.load_s is not None else None

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "model": self.config.model if self.config.enabled else None,
            "device": f"{self.config.device}/{self.config.compute_type}" if self.config.enabled else None,
            "ready": self.ready,
            "reason": self.reason,
            "phase": self.phase,
            "load_s": self.load_seconds(),
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
                hotwords = daemon.hotwords() if hasattr(daemon, "hotwords") else ""
                text = await asyncio.to_thread(self.transcriber, rec.audio, hotwords)
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


def main(argv: list[str] | None = None) -> int:
    """`python -m strawberry_crab.voice --fetch MODEL`: what `strawberry setup` runs to put a
    whisper model into the Hugging Face cache before the first start needs it."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m strawberry_crab.voice")
    parser.add_argument("--fetch", metavar="MODEL", required=True, help="a faster-whisper size, e.g. medium")
    args = parser.parse_args(argv)
    try:
        print(fetch_whisper(args.fetch))
    except Exception as exc:  # noqa: BLE001 - no network, a bad name: said, not a traceback
        print(f"could not download whisper {args.fetch}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
