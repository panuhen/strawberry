"""Warm on wake (wake.py): logind's PrepareForSleep(false) on a fake system bus loads the models
again, once per resume; no system bus, or one that refuses the match, is no crash."""

from __future__ import annotations

import asyncio
import logging

from jeepney import DBusAddress, new_signal
from jeepney.low_level import Parser

from strawberry_crab import wake
from strawberry_crab.bus import BusClient, BusError
from strawberry_crab.config import GateConfig
from strawberry_crab.systemone import Gate
from strawberry_crab.wake import LOGIN1_PATH, MANAGER_IFACE, SLEEP_RULE, WakeWatcher
from tests.test_privacy import Brain, make_daemon
from tests.test_systemone import FakeEmbedder

# Taken at import, before tests/conftest.py swaps it for a refusal in every test.
REAL_OPEN_SYSTEM_BUS = wake.open_system_bus

LOGIND = DBusAddress(LOGIN1_PATH, bus_name="org.freedesktop.login1", interface=MANAGER_IFACE)


def prepare_for_sleep(going: bool, serial: int = 7):
    """The signal as it comes off the wire: serialised, then parsed."""
    parser = Parser()
    parser.add_data(new_signal(LOGIND, "PrepareForSleep", "b", (going,)).serialise(serial=serial))
    return parser.get_next_message()


class FakeSystemBus(BusClient):
    """BusClient's queue and serve(), with the socket replaced: `emit` queues a message,
    `hang_up` ends run() the way a dead connection does."""

    def __init__(self, refuse_match: bool = False) -> None:
        super().__init__(connection=None)
        self.rules: list[str] = []
        self.refuse_match = refuse_match
        self.closed_by_us = False

    async def add_match(self, rule) -> None:
        if self.refuse_match:
            raise BusError(prepare_for_sleep(False))  # any message will do for the error's shape
        self.rules.append(rule.serialise())

    async def run(self):
        await self.closed.wait()
        return self.error

    def emit(self, message) -> None:
        self.incoming.put_nowait(message)

    def hang_up(self) -> None:
        self.error = ConnectionResetError()
        self.closed.set()

    async def close(self) -> None:
        self.closed_by_us = True


class SlowEmbedder(FakeEmbedder):
    """Answers after `delay`, so a warm-up is still in flight when the next signal comes."""

    def __init__(self, delay: float = 0.2) -> None:
        super().__init__()
        self.delay = delay
        self.warm_calls = 0

    async def __call__(self, texts):
        if texts == ["warm up"]:
            self.warm_calls += 1
            await asyncio.sleep(self.delay)
        return await super().__call__(texts)


async def settle(condition, timeout: float = 2.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never came true")


async def test_resume_signal_calls_back_and_sleep_does_not():
    bus = FakeSystemBus()
    resumed: list[int] = []

    async def connect():
        return bus

    watcher = WakeWatcher(lambda: resumed.append(1), connect=connect)
    task = asyncio.ensure_future(watcher.run())
    await settle(lambda: watcher.connected)
    assert bus.rules == [SLEEP_RULE.serialise()]
    assert "sender='org.freedesktop.login1'" in bus.rules[0] and "member='PrepareForSleep'" in bus.rules[0]
    bus.emit(prepare_for_sleep(True))
    bus.emit(prepare_for_sleep(False))
    await settle(lambda: watcher.resumes == 1)
    assert resumed == [1]
    assert watcher.stats() == {"watching": True, "resumes": 1, "reason": None}
    task.cancel()


async def test_resume_warms_the_gate_and_the_brain_once():
    embedder = SlowEmbedder(delay=0.2)
    gate = Gate(GateConfig(query_prefix="", document_prefix=""), embedder=embedder)
    await gate.start()
    brain = Brain()
    brain_warms: list[str] = []
    brain_task: list[asyncio.Task] = []

    def schedule_rewarm(reason):
        # OllamaReactor's contract: one load in flight, joined when asked again.
        if brain_task and not brain_task[0].done():
            return brain_task[0]
        brain_warms.append(reason)
        brain_task[:] = [asyncio.ensure_future(asyncio.sleep(0.2))]
        return brain_task[0]

    brain.schedule_rewarm = schedule_rewarm  # type: ignore[attr-defined]
    daemon = make_daemon(brain, gate)
    bus = FakeSystemBus()

    async def connect():
        return bus

    daemon.wake = WakeWatcher(daemon.on_wake, connect=connect)
    await daemon.start()
    await settle(lambda: daemon.wake.connected)
    bus.emit(prepare_for_sleep(False))
    bus.emit(prepare_for_sleep(False))            # logind (or a flaky driver) saying it twice
    await settle(lambda: daemon.wake.resumes == 2)
    assert gate.warming is not None
    assert await gate.warming is True
    assert embedder.warm_calls == 1 and gate.warmups == 1
    assert brain_warms == ["on resume"]
    await daemon.close()
    assert bus.closed_by_us


async def test_no_system_bus_is_one_log_line_and_no_crash(caplog):
    daemon = make_daemon()                        # conftest: the real system bus is out of bounds
    with caplog.at_level(logging.INFO, logger="strawberryd.wake"):
        await daemon.start()
        await asyncio.wait_for(daemon.wake_task, 1.0)   # returned, did not raise
    assert daemon.wake.stats()["watching"] is False
    assert "no system bus" in daemon.wake.stats()["reason"]
    assert any("no model warm-up on resume" in r.getMessage() for r in caplog.records)
    await daemon.close()


async def test_a_bus_that_refuses_the_match_is_no_crash():
    bus = FakeSystemBus(refuse_match=True)

    async def connect():
        return bus

    watcher = WakeWatcher(lambda: None, connect=connect)
    await asyncio.wait_for(watcher.run(), 1.0)
    assert not watcher.connected and "logind" in watcher.reason and bus.closed_by_us


async def test_a_lost_bus_is_reconnected(monkeypatch):
    buses = [FakeSystemBus(), FakeSystemBus()]
    resumed: list[int] = []

    async def connect():
        return buses.pop(0)

    monkeypatch.setattr(WakeWatcher, "RECONNECT_S", 0.01)
    first, second = buses
    watcher = WakeWatcher(lambda: resumed.append(1), connect=connect)
    task = asyncio.ensure_future(watcher.run())
    await settle(lambda: watcher.connected)
    first.hang_up()
    await settle(lambda: second.rules != [])
    second.emit(prepare_for_sleep(False))
    await settle(lambda: resumed == [1])
    task.cancel()


async def test_warm_on_wake_off_in_config_starts_no_watcher():
    from strawberry_crab.config import Config
    from strawberry_crab.daemon import Daemon

    config = Config()
    config.brain.enabled = config.speech.enabled = config.voice.enabled = False
    config.tools.enabled = config.thinker.enabled = False
    config.actions.mpris = False
    config.daemon.warm_on_wake = False
    daemon = Daemon(config=config, gate=Gate(GateConfig(enabled=False)))
    await daemon.start()
    assert daemon.wake is None and daemon.wake_task is None
    await daemon.close()


async def test_the_real_opener_asks_for_the_system_bus(monkeypatch):
    """open_system_bus asks jeepney for SYSTEM; a missing socket is the no-bus case."""
    seen: list[str] = []

    async def fake_open(bus="SESSION"):
        seen.append(bus)
        raise FileNotFoundError("/run/dbus/system_bus_socket")

    monkeypatch.setattr(wake, "open_dbus_connection", fake_open)
    monkeypatch.setattr(wake, "open_system_bus", REAL_OPEN_SYSTEM_BUS)
    watcher = WakeWatcher(lambda: None)
    await asyncio.wait_for(watcher.run(), 1.0)
    assert seen == ["SYSTEM"] and "FileNotFoundError" in watcher.reason
