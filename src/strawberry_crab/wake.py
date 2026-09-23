"""Resume from suspend: logind's `PrepareForSleep(false)` on the system bus (WIRING.md §2, §8a).

Ollama may unload its models while the machine sleeps, and the first call after a resume then
waits for a cold load: embeddinggemma took ~13 s once, far past the gate's 2 s budget, and a
notification body was dropped as private because of it. So the daemon watches logind and, on
resume, loads the gate's model and the reaction model in the background before anything needs
them (Daemon.warm_models).

The system bus is optional. A container, a CI runner or a machine without logind simply has no
warm-up on wake: `WakeWatcher.run()` logs one line and returns. A bus that goes away later (a
dbus restart) is reconnected after `RECONNECT_S`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

try:
    from jeepney import MatchRule, MessageType
    from jeepney.io.asyncio import open_dbus_connection

    from .bus import BusClient, field
except ImportError:  # jeepney is a Linux-only dependency: elsewhere there is no system bus to watch
    MatchRule = None

log = logging.getLogger("strawberryd.wake")

LOGIN1 = "org.freedesktop.login1"
LOGIN1_PATH = "/org/freedesktop/login1"
MANAGER_IFACE = "org.freedesktop.login1.Manager"
SLEEP_RULE = MatchRule(type="signal", sender=LOGIN1, interface=MANAGER_IFACE, member="PrepareForSleep",
                       path=LOGIN1_PATH) if MatchRule else None


async def open_system_bus(queue_size: int = 16) -> BusClient:
    """Connect to the system bus (jeepney does the handshake). Raises OSError and friends without one."""
    if MatchRule is None:
        raise ConnectionError("jeepney is not installed")
    return BusClient(await open_dbus_connection("SYSTEM"), queue_size=queue_size)


def is_prepare_for_sleep(message) -> bool:
    return (message.header.message_type == MessageType.signal
            and field(message, "interface") == MANAGER_IFACE
            and field(message, "member") == "PrepareForSleep")


class WakeWatcher:
    """Calls `on_resume()` each time logind says the machine has woken up."""

    RECONNECT_S = 30.0

    def __init__(self, on_resume: Callable[[], Awaitable[None] | None],
                 connect: Callable[[], Awaitable[BusClient]] | None = None) -> None:
        self.on_resume = on_resume
        self.connect = connect
        self.resumes = 0
        self.connected = False
        self.reason = ""

    async def run(self) -> None:
        """Watch until cancelled. Returns at once, after one log line, when there is no system bus."""
        first = True
        while True:
            try:
                client = await (self.connect or open_system_bus)()
            except Exception as exc:  # noqa: BLE001 - no bus (tests, containers) is a normal state
                self.connected = False
                self.reason = f"no system bus ({type(exc).__name__})"
                if first:
                    log.info("wake: %s; no model warm-up on resume", self.reason)
                    return
                log.warning("wake: system bus still gone (%s); trying again in %.0fs", type(exc).__name__,
                            self.RECONNECT_S)
                await asyncio.sleep(self.RECONNECT_S)
                continue
            first = False
            lost = await self._watch(client)
            if lost is None:
                return
            log.warning("wake: system bus connection lost (%s); reconnecting in %.0fs", lost, self.RECONNECT_S)
            await asyncio.sleep(self.RECONNECT_S)

    async def _watch(self, client: BusClient) -> str | None:
        """Serve one connection; the reason it ended, or None when logind cannot be subscribed to."""
        reader = asyncio.ensure_future(client.run())
        consumer: asyncio.Future | None = None
        try:
            try:
                await client.add_match(SLEEP_RULE)
            except Exception as exc:  # noqa: BLE001 - a bus that refuses the match: no warm-up, no crash
                self.reason = f"could not subscribe to logind ({type(exc).__name__})"
                log.info("wake: %s; no model warm-up on resume", self.reason)
                return None
            self.connected = True
            self.reason = ""
            log.info("wake: watching logind PrepareForSleep on the system bus")
            consumer = asyncio.ensure_future(client.serve(self.handle))
            await asyncio.wait([reader, consumer], return_when=asyncio.FIRST_COMPLETED)
            self.connected = False
            return type(client.error).__name__ if client.error else "closed"
        finally:
            if consumer is not None:
                consumer.cancel()
            reader.cancel()
            await client.close()

    async def handle(self, message) -> None:
        if not is_prepare_for_sleep(message):
            return
        going = bool(message.body[0]) if message.body else False
        if going:
            log.info("wake: going to sleep")
            return
        self.resumes += 1
        log.info("wake: resumed from sleep; warming the models")
        result = self.on_resume()
        if asyncio.iscoroutine(result):
            await result

    def stats(self) -> dict[str, object]:
        return {"watching": self.connected, "resumes": self.resumes, "reason": self.reason or None}
