"""The microphone on Windows (WIRING.md §7, WINDOWS.md step 6): WASAPI through PortAudio
(`sounddevice`), the counterpart of pw-record and pactl in voice.py.

The same shape as Linux: `acquire_microphone(preferred, bluetooth)` names the input and
`record(source, stop, ...)` captures 16 kHz mono s16 into voice.capture(), which decides when to
stop. The input is `[voice] source` (a fragment of the device's name) or Windows' default
recording device. On Windows the default is a real input, not the speaker monitor it is on a
PipeWire desktop, so it is used as it is. Windows switches a Bluetooth headset to its headset
profile by itself when its microphone is opened, so `bluetooth` changes nothing here.

The stream is shared-mode WASAPI with `auto_convert`: Windows converts from the device's own
format (48 kHz stereo, usually) to 16 kHz mono, so whisper gets what it gets on Linux. The
samples go from PortAudio's callback thread through a queue to capture(); a device that stops
delivering (unplugged, taken exclusively by another app) ends the recording after NO_DATA_S, as
a capture PipeWire never links does on Linux.

sounddevice is imported only here and only when a microphone is wanted, so nothing that runs
on Linux needs it (tests/test_imports.py).
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable

import numpy as np

from .voice import CHUNK, RATE, Recording, capture

log = logging.getLogger("strawberryd.voice")

WASAPI = "Windows WASAPI"


class MicrophoneError(RuntimeError):
    """PortAudio is missing or has no WASAPI host."""


def _sounddevice() -> Any:
    """The module, imported on first use (tests/conftest.py makes it unreachable in tests)."""
    import sounddevice

    return sounddevice


def wasapi_inputs(sd: Any) -> tuple[list[tuple[int, str]], int]:
    """([(device index, name)], default input index or -1) for the WASAPI host."""
    host = next((i for i, h in enumerate(sd.query_hostapis()) if h["name"] == WASAPI), None)
    if host is None:
        raise MicrophoneError("PortAudio has no WASAPI host")
    info = sd.query_hostapis(host)
    inputs = []
    for index in info["devices"]:
        device = sd.query_devices(index)
        if device["max_input_channels"] > 0:
            inputs.append((index, device["name"]))
    return inputs, int(info.get("default_input_device", -1))


def pick_device(preferred: str = "", sd: Any = None) -> tuple[int, str] | None:
    """The input whose name contains `preferred` (case-insensitive), else the default input."""
    try:
        sd = sd or _sounddevice()
        inputs, default = wasapi_inputs(sd)
    except (ImportError, OSError, MicrophoneError) as exc:
        log.warning("voice: no microphone list (%s)", exc)
        return None
    if preferred:
        wanted = preferred.lower()
        return next(((i, n) for i, n in inputs if wanted in n.lower()), None)
    return next(((i, n) for i, n in inputs if i == default), inputs[0] if inputs else None)


def acquire_microphone(preferred: str, prefer_bluetooth: bool) -> tuple[str | None, Callable[[], None]]:
    """(the device's name, nothing to restore): Windows does the headset profile itself."""
    device = pick_device(preferred)
    return (device[1] if device else None), (lambda: None)


def record(source: str, stop: threading.Event, max_seconds: float, silence_s: float,
           min_speech_s: float, level_db: float, sd: Any = None) -> Recording:
    """Blocking: WASAPI capture from the input named `source` into voice.capture()."""
    empty = Recording(np.zeros(0, np.float32), 0.0, 0.0, "error")
    try:
        sd = sd or _sounddevice()
        inputs, default = wasapi_inputs(sd)
    except (ImportError, OSError, MicrophoneError) as exc:
        log.error("voice: no microphone (%s)", exc)
        return empty
    # The name, not an index: indices move when a device comes or goes between acquire and here.
    index = next((i for i, n in inputs if n == source), default)
    if index < 0:
        log.error("voice: %s is gone and there is no default input", source)
        return empty
    blocks: queue.Queue[bytes | None] = queue.Queue()

    def callback(data, frames, time_info, status) -> None:
        blocks.put(bytes(data))

    def finished() -> None:
        blocks.put(None)

    try:
        stream = sd.RawInputStream(samplerate=RATE, blocksize=CHUNK, device=index, channels=1, dtype="int16",
                                   extra_settings=sd.WasapiSettings(auto_convert=True), callback=callback,
                                   finished_callback=finished)
        stream.start()
    except Exception as exc:  # noqa: BLE001 - PortAudioError, or a device that went away
        log.error("voice: could not open %s (%s)", source, exc)
        return empty

    def read(timeout: float) -> bytes | None:
        try:
            return blocks.get(timeout=timeout)
        except queue.Empty:
            return b""

    try:
        return capture(read, source, stop, max_seconds, silence_s, min_speech_s, level_db)
    finally:
        try:
            stream.stop()
        finally:
            stream.close()
