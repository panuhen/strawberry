"""Orchestration: the one funnel every feature ends in (WIRING.md §2)."""

from __future__ import annotations

import asyncio
import logging
import random
import re
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
from . import privacy
from .reactions import decorate, is_burst
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
        # The last state she was sent, for the tray's status row (§14). listening/thinking/talking
        # are transients the widget leaves on its own, so they expire here instead of sticking.
        self.state = "idle"
        self.state_at = 0.0
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
        # Each part is closed even if another one fails: an HTTP session left open is an
        # "Unclosed client session" error in the journal at exit.
        parts = [("reactor", self.reactor), ("speaker", self.speaker), ("listener", self.listener),
                 ("gate", self.gate), ("toolbox", self.toolbox), ("thinker", self.thinker), ("mpris", self.mpris)]
        for name, part in parts:
            close = getattr(part, "close", None)
            if close is None:
                continue
            try:
                await close()
            except Exception as exc:   # noqa: BLE001 - shutting down; say it and carry on
                log.warning("closing %s failed: %r", name, exc)
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
        self.state = performance.state
        self.state_at = time.monotonic()
        payload = performance.to_dict()
        sent = await self.hub.send(payload)
        self.performed += 1
        if sent == 0:
            log.warning("no widget connected; dropped %s", payload)
        else:
            log.info("perform -> %d widget(s): %s", sent, payload)
        return sent

    TRANSIENT_S = 6.0

    def current_state(self) -> str:
        """What she is doing now, as the tray's status row says it (§14).

        The widget returns to its resting state on its own when a line ends, and the daemon is
        not told, so a transient older than a few seconds is reported as the resting state.
        """
        if self.state in PERSISTENT_STATES:
            return self.rest_state
        if time.monotonic() - self.state_at < self.TRANSIENT_S:
            return self.state
        return self.rest_state

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
        # The body is never logged: a notification's text stays out of the journal (§4).
        log.info("event %s app=%r title=%r urgency=%s body_len=%d", event.source, event.app, event.title,
                 event.urgency, len(event.body))
        if event.source == "media" and time.monotonic() < self.quiet_media_until:
            log.info("media event swallowed: she caused it (%r)", event.title)
            return Performance(state=self.rest_state), 0
        if event.source == "action":
            # Posted by hand (or by a future doorway that did something): body is the fact.
            return await self.report(event, event.category != "failed")
        if event.source == "voice" and event.title:
            return await self.handle_voice(event)
        if event.source == "notification":
            return await self.handle_notification(event)
        performance = decorate(event, await self.reactor.react(event))
        sent = await self.perform(performance)
        return performance, sent

    GLANCE_QUIP_WORDS = 8

    async def handle_notification(self, event: Event) -> tuple[Performance, int]:
        """A desktop notification, by the app's body mode (WIRING.md §4).

        off: the body is dropped (the watcher should not have sent it; this holds the line too).
        react / glance: patterns, then IS_SENSITIVE on the gate; a hit, or a gate that cannot answer,
        drops the body and she says only that the app sent something private. glance: a neutral gist
        first (like report(): the fact, then her quip). A burst's body is the senders' titles, so it
        is only pattern-checked. Nothing of the body goes into a log line.
        """
        mode = self.config.notifications.mode_for(event.app)
        burst = is_burst(event)
        if event.body and mode == "off" and not burst:
            log.info("notification %r: body dropped, mode off (%d chars)", event.app, len(event.body))
            event = replace(event, body="")
        verdict = await privacy.check(self.gate, event.title, event.body, ask_gate=bool(event.body) and not burst)
        if verdict.sensitive:
            log.info("notification %r: private, %s; body_len=%d dropped", event.app, verdict.summary, len(event.body))
            quiet = replace(event, body="", title="")
            performance = decorate(quiet, Performance(state="talking", text=privacy.private_line(event.app),
                                                      emotion="neutral"))
            return performance, await self.perform(performance)
        if event.body and not burst:
            log.info("notification %r: body read, mode %s, %s; body_len=%d", event.app, mode, verdict.summary,
                     len(event.body))
        if event.body and not burst and mode == "glance":
            glanced = await self.glance(event)
            if glanced is not None:
                return glanced, await self.perform(glanced)
        performance = decorate(event, await self.reactor.react(event))
        why = privacy.leaks(performance.text or "", event.body) if event.body and not burst else None
        if why:
            # She quoted the message anyway: say the canned app-and-sender line instead.
            log.info("notification %r: her line gave away %s; canned line instead", event.app, why)
            performance = decorate(event, await CannedReactor().react(replace(event, body="")))
        return performance, await self.perform(performance)

    async def glance(self, event: Event) -> Performance | None:
        """Step one, a neutral gist of the message (no persona, no numbers or links); step two, her
        quip after it, written from the gist alone. None when there is no usable gist."""
        gist_of = getattr(self.reactor, "gist", None)
        gist = await gist_of(event) if gist_of else None
        why = privacy.leaks(gist, event.body, strict=True) if gist else None
        if why:
            log.info("notification %r: gist refused, it carried %s", event.app, why)
            gist = None
        if not gist:
            return None
        said = replace(event, body="", said=gist)
        quip_performance = await self.reactor.react(said)
        quip = short_quip(quip_performance.text or "", self.GLANCE_QUIP_WORDS)
        text = f"{gist} {quip}".strip() if quip else gist
        return decorate(event, replace(quip_performance, text=text))

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

    # `strawberry doctor --talk` (POST /probe): fixed sentences, so the only text this path ever
    # handles is ours, and nothing the user wrote can end up in a log line.
    PROBE_LINES = ("Hello there, how are you today?", "Tell me one short fact about crabs.")

    async def probe(self) -> dict[str, Any]:
        """Time each slot on the scripted lines: the gate, the desktop voice (Gemma), the brain
        (Qwen, offered no tools so the probe cannot change anything), Piper, and whisper reading
        Piper's wav back. Nothing is performed to the widget or kept in the ledger; a slot that is
        off or not ready is reported with its reason instead of a time."""
        skipped: dict[str, str] = {}
        if not self.gate.ready:
            skipped["gate"] = self.gate.disabled_reason or "gate off"
        if not self.config.brain.enabled or getattr(self.reactor, "session", None) is None:
            skipped["voice"] = "brain off (canned lines)"
        if not self.thinker.enabled:
            skipped["brain"] = "thinker off"
        if not self.speaker.ready:
            skipped["tts"] = self.speaker.stats().get("reason") or "speech off"
        if not self.listener.ready:
            skipped["whisper"] = self.listener.disabled_reason or "voice off"
        lines: list[dict[str, Any]] = []
        for text in self.PROBE_LINES:
            row: dict[str, Any] = {"text": text}

            async def timed(slot: str, work) -> Any:
                started = time.perf_counter()
                try:
                    result = await work
                except Exception as exc:   # noqa: BLE001 - a probe reports, it does not crash the daemon
                    row[slot] = {"ok": False, "error": type(exc).__name__}
                    return None
                row[slot] = {"ok": result is not None, "ms": round((time.perf_counter() - started) * 1000, 1)}
                return result

            if "gate" not in skipped:
                await timed("gate", self.gate.route(text))
            if "voice" not in skipped:
                await timed("voice", self.reactor.react(Event(source="voice", title=text)))
            if "brain" not in skipped:
                outcome = await timed("brain", self.thinker.run(text, "", tools=False))
                if outcome is not None and not outcome.ok:
                    row["brain"]["ok"] = False
            wav = await timed("tts", self.speaker.say(text)) if "tts" not in skipped else None
            if wav and "whisper" not in skipped:
                audio = await asyncio.to_thread(read_wav_16k, wav)
                await timed("whisper", asyncio.to_thread(self.listener.transcriber, audio))
            lines.append(row)
        return {"lines": lines, "skipped": skipped}

    QUIP_WORDS = 10
    QUIP_UNTIL_CHARS = 200   # a fact this long is a paragraph already; no quip after it

    async def report(self, event: Event, ok: bool) -> tuple[Performance, int]:
        """Say what she did: the fact (event.body, written by code) and the reactor's quip after it."""
        performance = decorate(event, await self.reactor.react(event))
        quip = short_quip(performance.text or "", self.QUIP_WORDS)
        if len(event.body) > self.QUIP_UNTIL_CHARS:
            quip = ""
        text = f"{event.body} {quip}".strip() if quip else event.body
        performance = replace(performance, text=text, emotion=performance.emotion if ok else "alert")
        sent = await self.perform(performance)
        return performance, sent


def read_wav_16k(path: str) -> Any:
    """A mono 16-bit wav (Piper's) as float32 samples at 16 kHz, the rate whisper is fed."""
    import wave

    import numpy as np

    from .voice import RATE

    with wave.open(str(path), "rb") as wav:
        rate = wav.getframerate()
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    if rate == RATE or not len(samples):
        return samples
    positions = np.arange(0, len(samples), rate / RATE)
    return np.interp(positions, np.arange(len(samples)), samples).astype(np.float32)


def short_quip(text: str, max_words: int) -> str:
    """The quip's whole sentences that fit in `max_words`; "" when even the first does not.
    Never a sentence cut in half ("Be careful of the wait time and")."""
    kept: list[str] = []
    count = 0
    for sentence in re.findall(r"[^.!?]+[.!?]*", text.strip()):
        words = sentence.split()
        if not words:
            continue
        if count + len(words) > max_words:
            break
        kept.append(" ".join(words))
        count += len(words)
    return " ".join(kept)
