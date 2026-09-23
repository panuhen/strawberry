"""The Windows microphone with a fake sounddevice shaped like the real one: the device choice,
the WASAPI stream it asks for, and a recording fed through PortAudio's callback. The real
module is unreachable in tests (tests/conftest.py)."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from strawberry_crab import voice, winmic

HOSTS = ({"name": "MME", "devices": [0, 1], "default_input_device": 0},
         {"name": "Windows WASAPI", "devices": [2, 3, 4], "default_input_device": 4})
DEVICES = {0: {"name": "Microphone (MME)", "max_input_channels": 2},
           1: {"name": "Speakers (MME)", "max_input_channels": 0},
           2: {"name": "Speakers (Test Audio)", "max_input_channels": 0},
           3: {"name": "Headset Microphone (Test Headset)", "max_input_channels": 1},
           4: {"name": "Microphone Array (Test Audio)", "max_input_channels": 2}}


def tone(seconds: float, level: float) -> bytes:
    n = int(seconds * voice.RATE)
    wave = level * np.sin(2 * np.pi * 220 * np.arange(n) / voice.RATE)
    return (wave * 32767).astype(np.int16).tobytes()


class FakeSounddevice:
    """query_hostapis, query_devices, WasapiSettings and a RawInputStream whose callback is fed
    from `audio` in 0.1 s blocks on a thread of its own, as PortAudio's is."""

    def __init__(self, audio: bytes = b"", hosts=HOSTS, fail_open: bool = False):
        self.audio, self.hosts, self.fail_open = audio, hosts, fail_open
        self.opened: list[dict] = []
        self.closed = 0

    def query_hostapis(self, index=None):
        return self.hosts if index is None else self.hosts[index]

    def query_devices(self, index):
        return DEVICES[index]

    def WasapiSettings(self, **kwargs):
        return {"wasapi": kwargs}

    def RawInputStream(self, **kwargs):
        if self.fail_open:
            raise OSError("Error opening RawInputStream: Invalid device [PaErrorCode -9996]")
        self.opened.append(kwargs)
        fake = self

        class Stream:
            def __init__(self):
                self.running = threading.Event()

            def start(self):
                self.running.set()
                threading.Thread(target=self.feed, daemon=True).start()

            def feed(self):
                block = kwargs["blocksize"] * 2
                for at in range(0, len(fake.audio), block):
                    if not self.running.is_set():
                        return
                    kwargs["callback"](memoryview(fake.audio[at:at + block]), kwargs["blocksize"], None, None)
                    time.sleep(0.001)

            def stop(self):
                self.running.clear()

            def close(self):
                fake.closed += 1

        return Stream()


def test_the_default_input_is_the_wasapi_default():
    assert winmic.pick_device("", FakeSounddevice()) == (4, "Microphone Array (Test Audio)")


def test_a_source_fragment_picks_by_name_without_case():
    assert winmic.pick_device("headset", FakeSounddevice()) == (3, "Headset Microphone (Test Headset)")
    assert winmic.pick_device("nothing like it", FakeSounddevice()) is None


def test_no_default_input_takes_the_first_and_none_at_all_is_none():
    hosts = ({"name": "Windows WASAPI", "devices": [2, 3], "default_input_device": -1},)
    assert winmic.pick_device("", FakeSounddevice(hosts=hosts)) == (3, "Headset Microphone (Test Headset)")
    hosts = ({"name": "Windows WASAPI", "devices": [2], "default_input_device": -1},)
    assert winmic.pick_device("", FakeSounddevice(hosts=hosts)) is None
    assert winmic.pick_device("", FakeSounddevice(hosts=({"name": "MME", "devices": [0]},))) is None


def test_the_real_module_is_out_of_bounds_in_tests():
    assert winmic.acquire_microphone("", True)[0] is None
    assert winmic.record("x", threading.Event(), 1.0, 1.0, 0.4, -50.0).stopped_by == "error"


def test_a_recording_is_16k_mono_s16_through_auto_convert_and_stops_on_silence():
    audio = tone(0.3, 0.001) + tone(1.0, 0.3) + tone(2.0, 0.001)
    sd = FakeSounddevice(audio)
    rec = winmic.record("Microphone Array (Test Audio)", threading.Event(), 15.0, 1.1, 0.4, -50.0, sd=sd)
    assert rec.stopped_by == "silence"
    assert rec.speech_seconds == pytest.approx(1.0, abs=0.11)
    assert rec.audio.dtype == np.float32 and 2.3 <= rec.seconds <= 2.5
    [opened] = sd.opened
    assert opened["samplerate"] == 16000 and opened["channels"] == 1 and opened["dtype"] == "int16"
    assert opened["blocksize"] == voice.CHUNK and opened["device"] == 4
    assert opened["extra_settings"] == {"wasapi": {"auto_convert": True}}
    assert sd.closed == 1


def test_a_poke_stops_it_and_a_silent_device_gives_up(monkeypatch):
    stop = threading.Event()
    stop.set()
    sd = FakeSounddevice(tone(5.0, 0.3))
    assert winmic.record("Microphone Array (Test Audio)", stop, 15.0, 1.1, 0.4, -50.0, sd=sd).stopped_by == "poke"
    monkeypatch.setattr(voice, "NO_DATA_S", 0.3)
    rec = winmic.record("Microphone Array (Test Audio)", threading.Event(), 15.0, 1.1, 0.4, -50.0,
                        sd=FakeSounddevice(b""))
    assert rec.stopped_by == "error" and rec.seconds == 0.0


def test_a_device_that_will_not_open_is_an_error_recording():
    sd = FakeSounddevice(fail_open=True)
    assert winmic.record("Microphone Array (Test Audio)", threading.Event(), 15.0, 1.1, 0.4, -50.0,
                         sd=sd).stopped_by == "error"


def test_a_device_gone_since_it_was_picked_falls_back_to_the_default():
    sd = FakeSounddevice(tone(0.5, 0.001))
    winmic.record("USB Microphone (unplugged)", threading.Event(), 0.3, 1.1, 0.4, -50.0, sd=sd)
    assert sd.opened[0]["device"] == 4


def test_windows_listens_through_winmic(monkeypatch):
    monkeypatch.setattr(voice.sys, "platform", "win32")
    assert voice.default_backend() == (winmic.record, winmic.acquire_microphone)
    monkeypatch.setattr(voice.sys, "platform", "linux")
    assert voice.default_backend() == (voice.record, voice.acquire_microphone)
