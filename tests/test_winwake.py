"""Warm on wake on Windows (winwake.py): the suspend and resume notification's callback, fed from
another thread as Windows feeds it, warms the models once per resume, and a notification right
after the resume is read normally rather than dropped as private. The machine is never suspended:
a fake registration fires the events, and on Windows the real registration's own callback is
called in-process."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading

import pytest

from strawberry_crab import wake, winwake
from strawberry_crab.winwake import PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND, PBT_APMSUSPEND, PowerWatcher
from tests.test_gate_retry import SLACK, cold_gate
from tests.test_privacy import Brain, make_daemon
from tests.test_wake import settle

# Taken at import, before tests/conftest.py swaps it for a refusal in every test.
REAL_REGISTER = winwake.register

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="powrprof.dll")


class FakeRegistration:
    """What register() hands back; `fire` calls the handler from a thread of its own, as Windows does."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.closed = False

    def fire(self, *events: int) -> None:
        def send() -> None:
            for event in events:
                self.handler(event)

        thread = threading.Thread(target=send)
        thread.start()
        thread.join()

    def close(self) -> None:
        self.closed = True


def fake_registrar(made: list[FakeRegistration]):
    def registrar(handler):
        made.append(FakeRegistration(handler))
        return made[-1]
    return registrar


async def test_a_resume_calls_back_once_and_sleep_does_not(caplog):
    made: list[FakeRegistration] = []
    resumed: list[int] = []
    watcher = PowerWatcher(lambda: resumed.append(1), registrar=fake_registrar(made))
    with caplog.at_level(logging.INFO, logger="strawberryd.wake"):
        task = asyncio.ensure_future(watcher.run())
        await settle(lambda: watcher.connected)
        # A user-woken resume: suspend, then the automatic resume, then the one saying a user is there.
        made[0].fire(PBT_APMSUSPEND, PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND)
        await settle(lambda: watcher.resumes == 1)
        await asyncio.sleep(0.05)
    assert resumed == [1]
    assert watcher.stats() == {"watching": True, "resumes": 1, "reason": None}
    text = [r.getMessage() for r in caplog.records]
    assert "wake: going to sleep" in text and "wake: resumed from sleep; warming the models" in text
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert made[0].closed and not watcher.connected


async def test_no_registration_is_one_log_line_and_no_crash(caplog):
    watcher = PowerWatcher(lambda: None)          # conftest: the real registration is refused
    with caplog.at_level(logging.INFO, logger="strawberryd.wake"):
        await asyncio.wait_for(watcher.run(), 1.0)
    assert watcher.stats() == {"watching": False, "resumes": 0, "reason": "no power notifications (PowerError)"}
    assert [r.getMessage() for r in caplog.records] == [
        "wake: no power notifications (PowerError); no model warm-up on resume"]


def test_the_daemon_picks_the_watcher_of_its_system():
    daemon = make_daemon()
    assert isinstance(daemon.wake, PowerWatcher if sys.platform == "win32" else wake.WakeWatcher)


async def test_a_notification_right_after_a_resume_is_read_not_dropped(aiohttp_server):
    """What a resume is for: Ollama unloaded the gate's model in the sleep; the resume starts the
    reload, and the first notification waits for it and is read, not "something private"."""
    gate, ollama, _ = await cold_gate(aiohttp_server, load_s=0.3)
    brain = Brain()
    daemon = make_daemon(brain, gate, body="react")
    made: list[FakeRegistration] = []
    daemon.wake = PowerWatcher(daemon.on_wake, registrar=fake_registrar(made))
    await daemon.start()
    await settle(lambda: daemon.wake.connected)
    ollama.unload()                                # what the sleep did
    made[0].fire(PBT_APMSUSPEND, PBT_APMRESUMEAUTOMATIC)
    await settle(lambda: gate.warming is not None)
    performance, _ = await daemon.handle_event(SLACK)
    assert performance.text == "Somebody wants a word."          # her line, not "Slack sent something private."
    assert [kind for kind, _ in brain.seen] == ["react"]
    assert ollama.loads == 1 and gate.warmups == 1                 # the one reload the resume started
    await daemon.close()
    assert made[0].closed


@windows_only
async def test_the_real_registration_calls_back_through_windows_pointer():
    """Registers with powrprof for real (nothing about the machine's power changes), then calls
    the callback through the very function pointer Windows was given, from another thread."""
    made: list[winwake.Registration] = []

    def registrar(handler):
        made.append(REAL_REGISTER(handler))
        return made[-1]

    resumed: list[int] = []
    watcher = PowerWatcher(lambda: resumed.append(1), registrar=registrar)
    task = asyncio.ensure_future(watcher.run())
    await settle(lambda: watcher.connected)
    registration = made[0]
    assert registration.handle                                   # Windows accepted it
    pointer = registration.parameters.Callback                   # what Windows calls on a resume
    results: list[int] = []
    thread = threading.Thread(target=lambda: results.append(pointer(None, PBT_APMRESUMEAUTOMATIC, None)))
    thread.start()
    thread.join()
    assert results == [winwake.ERROR_SUCCESS]
    await settle(lambda: resumed == [1])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not registration.handle                               # unregistered on the way out
