"""Orchestration: the one funnel every feature ends in (WIRING.md §2)."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import replace
from typing import Any

from .actions import Actor, Outcome
from .adapters import gate_examples
from .config import Config
from .contract import Performance
from .events import CannedReactor, Event, Reactor
from .hub import WidgetHub
from .ledger import Ledger
from .mpris import Mpris
from .reactions import decorate
from .speech import Speaker
from .systemone import Gate, Route
from .thinker import Thinker
from .tools import Toolbox
from .voice import Listener

log = logging.getLogger("strawberryd")

PERSISTENT_STATES = ("idle", "dancing")  # mirrors widget.gd PERSISTENT (WIRING.md §1)


class Daemon:
    def __init__(self, reactor: Reactor | None = None, config: Config | None = None, speaker: Speaker | None = None,
                 listener: Listener | None = None, gate: Gate | None = None, toolbox: Toolbox | None = None,
                 actor: Actor | None = None, thinker: Thinker | None = None) -> None:
        self.config = config or Config()
        self.hub = WidgetHub()
        self.reactor: Reactor = reactor or self._default_reactor()
        self.speaker = speaker or Speaker(self.config.speech)
        self.listener = listener or Listener(self.config.voice)
        self.toolbox = toolbox or Toolbox(self.config.tools)
        # A configured server with an adapter brings its own phrases for the gate ("save this song"
        # only means something with a library behind it) and its own reflexes; the bare music
        # commands no server covers go to MPRIS, which every desktop player speaks (§8b).
        self.gate = gate or Gate(self.config.gate, self.config.brain.ollama_url,
                                 examples=gate_examples(self.toolbox.adapters))
        self.mpris = Mpris() if actor is None and self.config.actions.mpris else None
        self.actor = actor or Actor(self.config.actions, self.toolbox, mpris=self.mpris)
        self.thinker = thinker or Thinker(self.config.thinker, self.toolbox, self.config.brain.action_model,
                                          self.config.brain.ollama_url)
        self.rng = random.Random()
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
        # After she changes the music herself, the MPRIS doorway reports the new track; her
        # account of the action already covers it, so that one reaction is swallowed.
        self.quiet_media_until = 0.0
        # Names for the speech recogniser: yours from the config, plus what the music server
        # knows (artists, playlists), refreshed in the background (voice.vocabulary_refresh_s).
        self.vocabulary: list[str] = list(self.config.voice.vocabulary)
        self.vocabulary_task: asyncio.Task | None = None
        self.vocabulary_at = 0.0
        # Her memory across turns (§8b): the last few exchanges, given to whoever answers.
        self.ledger = Ledger(self.config.actions.ledger_turns, self.config.actions.ledger_age_s)
        self.background_tasks: set[asyncio.Task] = set()

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
        await self.toolbox.start()
        await self.thinker.start()
        if self.config.voice.enabled and self.config.voice.hotwords and self.toolbox.servers:
            self.vocabulary_task = asyncio.get_running_loop().create_task(self._vocabulary_loop())

    def background(self, work, label: str) -> asyncio.Task:
        """Run `work` on the side (a typed sentence from the widget): kept referenced, failures logged."""
        task = asyncio.get_running_loop().create_task(work)
        self.background_tasks.add(task)

        def done(t: asyncio.Task) -> None:
            self.background_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("background %s failed: %r", label, t.exception())

        task.add_done_callback(done)
        return task

    async def close(self) -> None:
        for task in list(self.background_tasks):
            task.cancel()
        close = getattr(self.reactor, "close", None)
        if close:
            await close()
        await self.speaker.close()
        await self.listener.close()
        await self.gate.close()
        await self.toolbox.close()
        await self.thinker.close()
        if self.mpris:
            await self.mpris.close()
        if self.vocabulary_task and not self.vocabulary_task.done():
            self.vocabulary_task.cancel()
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

    async def refresh_vocabulary(self) -> None:
        names = list(self.config.voice.vocabulary)
        for word in await self.actor.vocabulary():
            if word not in names:
                names.append(word)
        self.vocabulary = names
        self.vocabulary_at = time.monotonic()
        log.info("voice: %d names for the recogniser (%s…)", len(names), ", ".join(names[:5]))

    async def _vocabulary_loop(self) -> None:
        await asyncio.sleep(2.0)  # let the servers preconnect
        while True:
            try:
                await self.refresh_vocabulary()
            except Exception as exc:  # a library call failing must not end the loop
                log.warning("voice: vocabulary refresh failed (%s)", exc)
            await asyncio.sleep(self.config.voice.vocabulary_refresh_s)

    def hotwords(self) -> str:
        """The recogniser's hint: the first max_hotwords names, comma separated."""
        if not self.config.voice.hotwords:
            return ""
        return ", ".join(self.vocabulary[: self.config.voice.max_hotwords])

    async def route(self, text: str) -> Route | None:
        """The gate's reading of a spoken sentence (WIRING.md §8a); None means "treat as chat"."""
        return await self.gate.route(text)

    QUIET_MEDIA_S = 8.0

    async def handle_event(self, event: Event) -> tuple[Performance, int]:
        log.info("event %s app=%r title=%r urgency=%s", event.source, event.app, event.title, event.urgency)
        if event.source == "media" and time.monotonic() < self.quiet_media_until:
            log.info("media event swallowed: she caused it (%r)", event.title)
            return Performance(state=self.rest_state), 0
        if event.source == "action":
            # Posted by hand (or by a future doorway that did something): body is the fact.
            return await self.report(event, event.category != "failed")
        if event.source == "voice" and event.title:
            return await self.handle_voice(event)
        performance = decorate(event, await self.reactor.react(event))
        sent = await self.perform(performance)
        return performance, sent

    async def handle_voice(self, event: Event) -> tuple[Performance, int]:
        """A sentence the user said or typed: the gate (§8a), a bare reflex if it is plainly one,
        otherwise Qwen in her own voice with the tools (§8b). Gemma answers only if Qwen is off."""
        text = event.title
        route = await self.route(text)
        music = route is not None and route.topic == "music"
        if music:
            # Set before acting: the MPRIS doorway reports the new track while the skip is still
            # confirming it, and that reaction would come out ahead of hers.
            self.quiet_media_until = time.monotonic() + self.QUIET_MEDIA_S
        outcome = await self.actor.act(text, route) if route is not None else None
        if outcome is not None:
            # A reflex: code wrote the fact, the reaction path adds the quip.
            self.quiet_media_until = time.monotonic() + self.QUIET_MEDIA_S
            performance, sent = await self.report(outcome.event(text), outcome.ok)
            self.ledger.record(text, performance.text or "", did=outcome.did)
            return performance, sent
        if not self.thinker.enabled:
            if music:
                self.quiet_media_until = 0.0  # Gemma cannot touch the music; it is not hers to explain
            return await self.chat(event)
        outcome = await self.think(text, route)
        if music and not outcome.calls:
            self.quiet_media_until = 0.0  # she only talked; a track change now is somebody else's
        elif outcome.calls:
            # Again after the tools: the thinker can take longer than the window.
            self.quiet_media_until = time.monotonic() + self.QUIET_MEDIA_S
        performance = decorate(event, Performance(state="talking", text=outcome.fact,
                                                  emotion=outcome.emotion or "neutral"))
        sent = await self.perform(performance)
        self.ledger.record(text, performance.text or "", did=outcome.did)
        return performance, sent

    async def chat(self, event: Event) -> tuple[Performance, int]:
        """Gemma answers, with the recent exchanges for context. The voice fallback when the
        thinker is off (and the path every desktop event takes)."""
        context = self.ledger.context(limit=3) if event.source == "voice" else ""
        performance = decorate(event, await self.reactor.react(event, context))
        sent = await self.perform(performance)
        if event.source == "voice":
            self.ledger.record(event.title, performance.text or "")
        return performance, sent

    async def think(self, text: str, route: Route | None) -> Outcome:
        """Qwen, with cover that scales with the wait: the thinking pose at once and nothing said (a
        warm round is ~2 s, and "On it." before an answer to "what's up?" reads odd), a spoken
        acknowledgement from `acks` only after `ack_after_s`, "Still on it." after `still_on_it_s`
        (a cold load is 7-17 s), then her reply as the outcome."""
        await self.perform(Performance(state="thinking"))

        async def cover() -> None:
            await asyncio.sleep(self.config.thinker.ack_after_s)
            await self.perform(Performance(state="thinking", text=self.rng.choice(self.config.thinker.acks)))
            await asyncio.sleep(max(self.config.thinker.still_on_it_s - self.config.thinker.ack_after_s, 0.1))
            await self.perform(Performance(state="thinking", text="Still on it."))

        reminder = asyncio.get_running_loop().create_task(cover())
        try:
            careful = route is not None and route.library_change >= 0.5
            return await self.thinker.run(text, await self.situation(), careful=careful,
                                          topic=route.topic if route is not None else "")
        finally:
            reminder.cancel()

    async def situation(self) -> str:
        """What Qwen is told before the sentence: the date, what the servers say is going on, the
        names in the user's library (speech-to-text mishears them), and the recent exchanges."""
        parts = [time.strftime("Today is %A %d %B %Y, %H:%M local time.")]
        here = await self.actor.situation()
        if here:
            parts.append(here)
        if self.vocabulary:
            parts.append(f"Names in the user's library: {self.hotwords()}.")
        situation = " ".join(parts)
        recent = self.ledger.context()
        return f"{situation}\n{recent}" if recent else situation

    QUIP_WORDS = 10
    QUIP_UNTIL_CHARS = 200   # a fact this long is a paragraph already; no quip after it

    async def report(self, event: Event, ok: bool) -> tuple[Performance, int]:
        """Say what she did: the fact (event.body, written by code) and the reactor's quip after it."""
        performance = decorate(event, await self.reactor.react(event))
        quip = " ".join((performance.text or "").split()[: self.QUIP_WORDS]).strip()
        if len(event.body) > self.QUIP_UNTIL_CHARS:
            quip = ""
        text = f"{event.body} {quip}".strip() if quip else event.body
        performance = replace(performance, text=text, emotion=performance.emotion if ok else "alert")
        sent = await self.perform(performance)
        return performance, sent
