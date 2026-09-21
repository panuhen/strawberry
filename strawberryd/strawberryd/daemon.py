"""Orchestration: the one funnel every feature ends in (WIRING.md §2)."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any

from .config import Config
from .contract import Performance
from .events import CannedReactor, Event, Reactor
from .hub import WidgetHub
from .reactions import decorate
from .speech import Speaker
from .systemone import Gate, Route
from .voice import Listener

log = logging.getLogger("strawberryd")

PERSISTENT_STATES = ("idle", "dancing")  # mirrors widget.gd PERSISTENT (WIRING.md §1)


class Daemon:
    def __init__(self, reactor: Reactor | None = None, config: Config | None = None, speaker: Speaker | None = None,
                 listener: Listener | None = None, gate: Gate | None = None) -> None:
        self.config = config or Config()
        self.hub = WidgetHub()
        self.reactor: Reactor = reactor or self._default_reactor()
        self.speaker = speaker or Speaker(self.config.speech)
        self.listener = listener or Listener(self.config.voice)
        self.gate = gate or Gate(self.config.gate, self.config.brain.ollama_url)
        self.listen_task: asyncio.Task | None = None
        self.last_poke = -1e9
        self.started = time.monotonic()
        self.performed = 0
        # Her resting state (idle|dancing) outlives any one widget: a widget that (re)connects
        # while music plays gets it on arrival instead of standing still until the next pause.
        self.rest_state = "idle"
        # Latest beat estimate from doorways/beat_watch.py and when it arrived (§4c).
        self.tempo: dict[str, Any] | None = None
        self.tempo_at = 0.0

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
        await self.speaker.start()
        await self.listener.start()
        await self.gate.start()

    async def close(self) -> None:
        close = getattr(self.reactor, "close", None)
        if close:
            await close()
        await self.speaker.close()
        await self.listener.close()
        await self.gate.close()
        if self.listen_task and not self.listen_task.done():
            self.listen_task.cancel()

    POKE_GAP_S = 0.5

    def listen(self) -> dict[str, Any]:
        """The hotkey: start a voice session, or end the recording early if one is running."""
        if not self.listener.ready:
            return {"listening": False, "error": self.listener.disabled_reason or "voice not ready"}
        now = time.monotonic()
        gap = now - self.last_poke
        self.last_poke = now
        if gap < self.POKE_GAP_S:
            # GNOME re-runs the shortcut ~30 times a second while the key is held. A poke only
            # counts after the key has been released: a gap since the previous poke.
            return {"listening": self.listener.phase == "listening", "debounced": True}
        if self.listener.busy:
            if self.listener.phase == "listening":
                self.listener.stop.set()
                return {"listening": False, "stopped": True}
            return {"listening": False, "busy": self.listener.phase}
        self.listen_task = asyncio.get_running_loop().create_task(self.listener.session(self))
        return {"listening": True}

    def brain_stats(self) -> dict[str, Any]:
        stats = getattr(self.reactor, "stats", None)
        return stats() if stats else {"model": None, "canned": True}

    async def perform(self, performance: Performance) -> int:
        """Send one performance to the widget, voicing the line first when speech is on (§6).

        A caller that already supplies `audio` keeps it; a line with no audio gets Piper's wav,
        or stays silent when speech is off, quiet, or failing. The bubble shows either way.
        """
        if performance.text and not performance.audio:
            audio = await self.speaker.say(performance.text)
            if audio:
                performance = replace(performance, audio=audio)
        if performance.state in PERSISTENT_STATES:
            self.rest_state = performance.state
        payload = performance.to_dict()
        sent = await self.hub.send(payload)
        self.performed += 1
        if sent == 0:
            log.warning("no widget connected; dropped %s", payload)
        else:
            log.info("perform -> %d widget(s): %s", sent, payload)
        return sent

    TEMPO_FRESH_S = 6.0

    def fresh_tempo(self) -> dict[str, Any] | None:
        if self.tempo is None or time.monotonic() - self.tempo_at > self.TEMPO_FRESH_S:
            return None
        return self.tempo

    async def set_tempo(self, tempo: dict[str, Any]) -> int:
        """Forward one beat estimate to the widgets as {"tempo": {...}} and remember it."""
        self.tempo = tempo
        self.tempo_at = time.monotonic()
        return await self.hub.send({"tempo": tempo})

    async def route(self, text: str) -> Route | None:
        """The gate's reading of a spoken sentence (WIRING.md §8a); None means "treat as chat"."""
        return await self.gate.route(text)

    async def handle_event(self, event: Event) -> tuple[Performance, int]:
        log.info("event %s app=%r title=%r urgency=%s", event.source, event.app, event.title, event.urgency)
        if event.source == "voice" and event.title:
            # Phase 6a: every spoken sentence is routed and logged; the action path (6b) takes
            # `act` and `offer` from here. Until then she answers everything herself.
            await self.route(event.title)
        performance = decorate(event, await self.reactor.react(event))
        sent = await self.perform(performance)
        return performance, sent
