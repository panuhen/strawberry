"""Orchestration: the one funnel every feature ends in (WIRING.md §2)."""

from __future__ import annotations

import logging
import time
from typing import Any

from .config import Config
from .contract import Performance
from .events import CannedReactor, Event, Reactor
from .hub import WidgetHub
from .reactions import decorate

log = logging.getLogger("strawberryd")


class Daemon:
    def __init__(self, reactor: Reactor | None = None, config: Config | None = None) -> None:
        self.config = config or Config()
        self.hub = WidgetHub()
        self.reactor: Reactor = reactor or self._default_reactor()
        self.started = time.monotonic()
        self.performed = 0

    def _default_reactor(self) -> Reactor:
        canned = CannedReactor()
        if not self.config.brain.enabled:
            log.info("brain disabled in config; canned reactions")
            return canned
        from .brain import OllamaReactor  # local import keeps tests of the plumbing model-free

        return OllamaReactor(self.config.brain, fallback=canned)

    @property
    def uptime(self) -> float:
        return time.monotonic() - self.started

    async def start(self) -> None:
        start = getattr(self.reactor, "start", None)
        if start:
            await start()

    async def close(self) -> None:
        close = getattr(self.reactor, "close", None)
        if close:
            await close()

    def brain_stats(self) -> dict[str, Any]:
        stats = getattr(self.reactor, "stats", None)
        return stats() if stats else {"model": None, "canned": True}

    async def perform(self, performance: Performance) -> int:
        """Send one performance to the widget. TTS (Phase 4) slots in here, before send."""
        payload = performance.to_dict()
        sent = await self.hub.send(payload)
        self.performed += 1
        if sent == 0:
            log.warning("no widget connected; dropped %s", payload)
        else:
            log.info("perform -> %d widget(s): %s", sent, payload)
        return sent

    async def handle_event(self, event: Event) -> tuple[Performance, int]:
        log.info("event %s app=%r title=%r urgency=%s", event.source, event.app, event.title, event.urgency)
        performance = decorate(event, await self.reactor.react(event))
        sent = await self.perform(performance)
        return performance, sent
