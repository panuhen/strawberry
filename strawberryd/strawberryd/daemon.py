"""Orchestration: the one funnel every feature ends in (WIRING.md §2)."""

from __future__ import annotations

import logging
import time

from .contract import Performance
from .events import CannedReactor, Event, Reactor
from .hub import WidgetHub

log = logging.getLogger("strawberryd")


class Daemon:
    def __init__(self, reactor: Reactor | None = None) -> None:
        self.hub = WidgetHub()
        self.reactor: Reactor = reactor or CannedReactor()
        self.started = time.monotonic()
        self.performed = 0

    @property
    def uptime(self) -> float:
        return time.monotonic() - self.started

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
        performance = await self.reactor.react(event)
        sent = await self.perform(performance)
        return performance, sent
