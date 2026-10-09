"""Orchestration: the one funnel every feature ends in (WIRING.md §2)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import time
from dataclasses import replace
from typing import Any

from .actions import NO_CATALOGUE, READ_REFLEXES, Actor, Outcome
from .adapters import gate_examples
from .approvals import Approval, ApprovalBook
from . import confirm
from .confirm import Held
from .config import Config
from .contract import Performance
from .events import CannedReactor, Event, Reactor
from .hub import WidgetHub
from .learning import IdleTrainer
from .ledger import Ledger
from .logtext import sentence
from . import logtext, media, privacy
from .outcomes import OutcomeLog
from .pokes import Pokes
from .reactions import decorate, is_burst
from .routefeed import RouteFeed
from . import runs
from .runs import Run, RunBook, emit, is_stop
from .speech import Speaker
from .systemone import Gate, Route
from .thinker import WEB_REPLY, Thinker, error_code
from .tools import Toolbox
from .voice import EARS_LOADING, Listener
from . import wake

log = logging.getLogger("strawberryd")

PERSISTENT_STATES = ("idle", "dancing")  # mirrors widget.gd PERSISTENT (WIRING.md §1)


class Daemon:
    def __init__(self, reactor: Reactor | None = None, config: Config | None = None, speaker: Speaker | None = None,
                 listener: Listener | None = None, gate: Gate | None = None, toolbox: Toolbox | None = None,
                 actor: Actor | None = None, thinker: Thinker | None = None) -> None:
        self.config = config or Config()
        logtext.configure(self.config.daemon.log_sentences)
        self.hub = WidgetHub()
        self.reactor: Reactor = reactor or self._default_reactor()
        self.speaker = speaker or Speaker(self.config.speech)
        self.listener = listener or Listener(self.config.voice)
        self.toolbox = toolbox or Toolbox(self.config.tools)
        # A configured server with an adapter brings its own phrases for the gate ("save this song"
        # only means something with a library behind it) and its own reflexes; the bare music
        # commands no server covers go to the desktop's media controls, which every player
        # answers: MPRIS on Linux, SMTC on Windows (§8b, media.py).
        self.gate = gate or Gate(self.config.gate, self.config.brain.ollama_url,
                                 examples=gate_examples(self.toolbox.adapters))
        self.mpris = media.controls() if actor is None and self.config.actions.mpris else None
        self.actor = actor or Actor(self.config.actions, self.toolbox, mpris=self.mpris)
        self.thinker = thinker or Thinker(self.config.thinker, self.toolbox, self.config.brain.action_model,
                                          self.config.brain.ollama_url)
        self.rng = random.Random()
        self.listen_task: asyncio.Task | None = None
        self.last_poke = -1e9
        self.ears_said_at = -1e9
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
        # The router's learning loop, data only (§8c): each routed sentence and what came of it,
        # in a local file, when [learning] log_outcomes is on. Off, every call is a no-op.
        self.outcomes = OutcomeLog(self.config.learning)
        # The same sentences as they are routed, in memory, for the Brain UI's live view (routefeed.py).
        self.feed = RouteFeed(self.config.learning)
        # Every input she handles is a run (runs.py, WIRING.md §18): its steps go to the bodies that
        # asked for them and to the Brain UI, and a run can be stopped. One foreground run at a time.
        self.runs = RunBook(keep=self.config.runs.keep, events=self.config.runs.events)
        self.pokes = Pokes()          # the widget's "Talk when poked" lines (pokes.py)
        self.phases: asyncio.Queue | None = None    # the hub's feed of run events (start)
        # Its second half (§8d): labels from those outcomes, a candidate head trained on them when she
        # has been idle a while, and the gate following the heads dir's `current` without a restart.
        self.voice_at = time.monotonic()     # the last sentence handled (or the start): the trainer waits for quiet
        self.voice_handling = 0              # sentences being handled right now
        self.trainer = IdleTrainer(self)
        self.background_tasks: set[asyncio.Task] = set()
        # A call the thinker stopped at to ask first (confirm.py, approvals.py): the run that asked waits
        # for the answer (the user's next sentence, her card on a body, the Brain UI) and makes the call
        # only after a yes; no answer in [approvals] change_s (sends_s, destructive_s) is a no.
        self.approvals = ApprovalBook(self.config.approvals)
        if self.config.approvals.risk:
            self.toolbox.risks = dict(self.config.approvals.risk)
        self.dropped_at = -1e9   # when a held call was last dropped by silence or a no (confirm.LATE_S)
        self.unmade_line = ""    # "I stopped before doing it…", for the sentence that overtook a yes
        # Resume from suspend (logind on the system bus; Windows' power notification): the models
        # are loaded again before the first notification needs them (wake.py, winwake.py). No bus,
        # no registration: one log line, nothing else.
        self.wake = wake.watcher(self.on_wake) if self.config.daemon.warm_on_wake else None
        self.wake_task: asyncio.Task | None = None

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
        """Start every part without waiting for a model, so the port opens at once (WIRING.md §2).
        The reaction model's warm-up, Piper's voice, whisper and the gate's examples load in the
        background, and each copes with what arrives first: Gemma's short call falls back to the
        canned line, a line waits for Piper (a second), /listen says she is getting her ears on,
        a sentence waits up to 2 s for the gate and is then chat, a notification body waits for it
        as for a reload. A part without begin() (a stand-in in the tests) is started in full."""
        for part in (self.reactor, self.speaker, self.listener, self.gate):
            begin = getattr(part, "begin", None)
            if begin is not None:
                begin()
                continue
            start = getattr(part, "start", None)
            if start:
                await start()
        await self.toolbox.start()
        await self.thinker.start()
        if self.hub.pump_task is None:
            self.phases = self.runs.subscribe()
            self.hub.pump_task = asyncio.get_running_loop().create_task(self.hub.pump(self.phases))
        self.outcomes.start()
        self.trainer.start()
        if self.config.voice.enabled and self.config.voice.hotwords and self.toolbox.servers:
            self.vocabulary_task = asyncio.get_running_loop().create_task(self._vocabulary_loop())
        if self.wake is not None:
            self.wake_task = asyncio.get_running_loop().create_task(self.wake.run())

    def on_wake(self) -> None:
        self.warm_models("on resume")

    def warm_models(self, reason: str) -> list[asyncio.Task]:
        """Load the gate's model and the reaction model in the background (a one-word embedding, an
        empty generate with keep_alive). Each part keeps one load in flight and joins it when asked
        again, so a second resume signal or a timeout meanwhile starts nothing new. Both parts log
        their own time; the thinker's big model is left alone (it loads on demand, with cover)."""
        tasks: list[asyncio.Task] = []
        for name, part, method in (("gate", self.gate, "warm"), ("brain", self.reactor, "schedule_rewarm")):
            warm = getattr(part, method, None)
            if warm is None:
                continue
            task = warm(reason)
            if task is not None:
                tasks.append(task)
        log.info("warming %d model(s) %s", len(tasks), reason)
        return tasks

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
        # Each run still going ends with run.cancelled (shutdown); a change in flight finishes first. A
        # question still open is cancelled with it: nothing waiting for a yes is ever made.
        self.runs.cancel_all("shutdown")
        if self.approvals.open is not None:
            self.approvals.resolve(self.approvals.open, "cancelled")
        if self.wake_task and not self.wake_task.done():
            # Awaited, so its system-bus socket is closed before the loop goes.
            self.wake_task.cancel()
            try:
                await self.wake_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down
                pass
        for task in list(self.background_tasks):
            task.cancel()
        await self.trainer.close()
        self.outcomes.close()
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
        if self.hub.pump_task is not None:
            self.hub.pump_task.cancel()
            self.hub.pump_task = None

    POKE_GAP_S = 0.5
    EARS_GAP_S = 5.0     # "still getting my ears on" at most this often

    def listen(self) -> dict[str, Any]:
        """The hotkey: start a voice session, or end the recording early if one is running."""
        if self.listener.loading:
            # Whisper is still loading (or downloading, on a first start): she says so at once
            # instead of the key doing nothing. Held-key repeats and quick presses say it once.
            now = time.monotonic()
            gap, self.last_poke = now - self.last_poke, now
            if gap >= self.POKE_GAP_S and now - self.ears_said_at >= self.EARS_GAP_S:
                self.ears_said_at = now
                self.background(self.perform(Performance(state="talking", text=EARS_LOADING, emotion="neutral")),
                                "ears loading line")
            return {"listening": False, "loading": self.listener.reason}
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

    def listening(self, phase: str, seconds: float | None = None, speech: bool | None = None) -> None:
        """The `listening` gauge (runs.RunBook.signal): a voice capture started or ended, with how long it
        recorded and whether it heard speech. No audio, no transcript."""
        self.runs.signal("listening", phase=phase, seconds=seconds, speech=speech)

    async def didnt_catch(self, line: str) -> tuple[Performance, int]:
        """A voice capture with nothing understood in it: a run of its own that says so and ends
        `run.cancelled` with the reason `didnt_catch`. Never the foreground run: an empty capture stops
        nothing and answers nothing (her question, if one is open, keeps waiting)."""
        run = self.runs.start("voice", foreground=False)
        run.cancel_reason = "didnt_catch"
        token = runs.active.set(run)
        try:
            performance = Performance(state="talking", text=line)
            return performance, await self.perform(performance)
        finally:
            runs.active.reset(token)
            self.runs.finish(run)

    def brain_stats(self) -> dict[str, Any]:
        stats = getattr(self.reactor, "stats", None)
        return stats() if stats else {"model": None, "canned": True}

    async def perform(self, performance: Performance) -> int:
        """Send one performance to the widget, voicing the line first when speech is on (§6).

        A caller that already supplies `audio` keeps it; a line with no audio gets Piper's wav,
        or stays silent when speech is off, quiet, or failing. The bubble shows either way.
        """
        # A line that answers the run going on in this task (runs.active): its `speaking` event, and
        # the run's id on the performance for v2 bodies. Cover lines ("On it.") are `thinking`.
        run = runs.active.get()
        answering = (run is not None and not run.done and performance.state == "talking"
                     and bool(performance.text or performance.audio))
        if performance.text and not performance.audio:
            audio = await self.speaker.say(performance.text)
            if audio:
                performance = replace(performance, audio=audio)
        if answering:
            emit(run, "speaking", duration=speech_seconds(performance), emotion=performance.emotion)
        if self.phases is not None:
            # The run's steps so far go out first, so a body sees them in order with the line.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.phases.join(), self.PHASES_WAIT_S)
        if performance.state in PERSISTENT_STATES:
            self.rest_state = performance.state
        self.state = performance.state
        self.state_at = time.monotonic()
        payload = performance.to_dict()
        sent = await self.hub.send(payload, run_id=run.run_id if answering else "")
        self.performed += 1
        logged = payload | {"text": logtext.line(payload["text"])} if "text" in payload else payload
        if sent == 0:
            log.warning("no widget connected; dropped %s", logged)
        else:
            log.info("perform -> %d widget(s): %s", sent, logged)
        return sent

    PHASES_WAIT_S = 0.5
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
        # A voice event's title is the user's own sentence (logtext.py, [daemon] log_sentences).
        title = sentence(event.title) if event.source == "voice" else repr(event.title)
        log.info("event %s app=%r title=%s urgency=%s body_len=%d", event.source, event.app, title,
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

    def reload_notifications(self) -> str:
        """Read [notifications] from the config file again and use it from the next event on
        (the tray's "Message bodies" rows, §14). Only this section: the rest needs a restart.
        Raises ConfigError, keeping the settings she has, when the file does not load."""
        from .config import default_path, load

        fresh = load(self.config.path or default_path()).notifications
        self.config.notifications = fresh
        return fresh.body

    GLANCE_QUIP_WORDS = 8

    async def handle_notification(self, event: Event) -> tuple[Performance, int]:
        """A notification is a run of its own (runs.py): `speaking` and its end, beside whatever
        sentence is being handled (it is never the foreground run, and a sentence never stops it)."""
        run = self.runs.start("notification")
        token = runs.active.set(run)
        try:
            return await self._handle_notification(event)
        except asyncio.CancelledError:
            run.cancel_reason = run.cancel_reason or "shutdown"
            raise
        except Exception:
            run.error = run.error or "other"
            raise
        finally:
            runs.active.reset(token)
            self.runs.finish(run)

    async def _handle_notification(self, event: Event) -> tuple[Performance, int]:
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
        otherwise Qwen in her own voice with the tools (§8b). Gemma answers only if Qwen is off.

        Each sentence is a foreground run (runs.py), and there is one at a time. A sentence while
        another is still going stops it first ([runs] supersede; "superseded"), unless it answers
        her question (confirm.py), and a whole-sentence "stop" or "never mind" stops it and is
        answered with a short line ("stopped"); a yes or no to her question is neither. Either way
        it waits for that run to end.

        A yes or no while she waits for one (approvals.py) is no run of its own: it answers the run
        that asked, which makes the call or leaves it, and this returns what that run said. Any other
        sentence drops her question (`superseded`, whatever [runs] supersede says: nothing is under
        way) and is handled as usual, after "I've left that, then.\""""
        self.voice_handling += 1
        self.voice_at = time.monotonic()
        try:
            pending = self.approvals.open
            said = confirm.answer(event.title) if pending is not None else None
            if pending is not None and said is not None:
                answered = await self.answered(event, pending, said)
                if answered is not None:
                    return answered
                pending = None   # it ran out (or was answered) a moment ago: a late answer, below
            return await self._sentence(event, pending)
        finally:
            self.voice_handling -= 1
            self.voice_at = time.monotonic()

    async def _sentence(self, event: Event, pending: Approval | None) -> tuple[Performance, int]:
        previous = self.runs.busy()
        run = self.runs.start("voice" if event.spoken else "typed")
        live = self.feed.start(run.source)
        live["run_id"] = run.run_id
        try:
            answer = self.answers_question(event.title)
            stop = previous is not None and not answer and is_stop(event.title)
            preface = ""
            if pending is not None and self.approvals.resolve(pending, "superseded"):
                if pending.run is not None:
                    self.runs.cancel(pending.run.run_id, "superseded")
                preface = confirm.LEFT_OTHER   # said ahead of whatever this sentence gets: one line, not two
                log.info("confirm: %s not made (another sentence)", pending.held.key)
            if previous is not None:
                if stop:
                    self.runs.cancel_all("stopped", but=run)
                elif self.config.runs.supersede and not answer:
                    self.runs.cancel(previous.run_id, "superseded")
                await previous.finished.wait()
            if self.unmade_line:   # a yes this sentence overtook before its call started (_await_answer)
                preface, self.unmade_line = f"{self.unmade_line} {preface}".strip(), ""
            with logtext.hearing(event.title):   # her lines in the log leave it out (log_sentences)
                work = self._stopped(event, previous) if stop else self._handle_voice(event, live, run, preface)
                return await self._in_run(run, work)
        except asyncio.CancelledError:
            run.cancel_reason = run.cancel_reason or "shutdown"
            raise
        except Exception:
            run.error = run.error or "other"
            raise
        finally:
            self.feed.done(live)
            if not run.continued:   # else the wait for her answer ends it (Daemon.hold)
                self.runs.finish(run)

    async def answered(self, event: Event, approval: Approval, said: str) -> tuple[Performance, int] | None:
        """The user's own yes or no to the open approval: the run that asked goes on (the call, or her line
        that she left it) and its last line is the answer to this sentence too. None when the approval
        closed a moment before (a timeout): the sentence is then a late answer like any other."""
        by = "voice" if event.spoken else "typed"
        live = self.feed.start(by)
        live["run_id"] = approval.run_id
        try:
            if self.approvals.answer(approval.approval_id, said, by, text=event.title) is not None:
                return None
            log.info("confirm: %s answered %s (%s)", approval.held.key, said, by)
            if approval.run is not None:
                await approval.run.finished.wait()
            return approval.reply or (Performance(state=self.rest_state), 0)
        finally:
            self.feed.done(live)

    def answers_question(self, text: str) -> bool:
        """Is this sentence a yes or no to her question (open, or dropped a moment ago)? It never
        supersedes: it is the answer the run before it was waiting for."""
        late = time.monotonic() - self.dropped_at < confirm.LATE_S
        return (self.approvals.open is not None or late) and confirm.answer(text) is not None

    @property
    def held(self) -> Held | None:
        """The call she is waiting for a yes to, exactly as it will be made (the open approval's)."""
        pending = self.approvals.open
        return pending.held if pending is not None else None

    async def _in_run(self, run: Run, work) -> tuple[Performance, int]:
        """`work` in a task of its own, the one RunBook.cancel cancels, with `run` as the active run
        (what perform answers). A cancel of the run ends here: she leaves the thinking pose, or says
        what had already gone through. A cancel of this task itself (shutdown) goes on up."""
        if run.cancel_reason:   # stopped while it waited its turn
            work.close()
            return Performance(state=self.rest_state), 0

        async def as_run():
            runs.active.set(run)   # in this task's own context only
            return await work

        task = asyncio.get_running_loop().create_task(as_run())
        run.task = task
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if (current is not None and current.cancelling()) or not run.cancel_reason or not task.cancelled():
                raise
        return await self._after_cancel(run)

    STOPPED = "Okay, stopped."

    async def _after_cancel(self, run: Run) -> tuple[Performance, int]:
        """After a stop: one short line if a change had gone through before it (a shielded call),
        else her resting pose again (a newer sentence takes over by itself)."""
        if run.shielded:
            done = ", ".join(dict.fromkeys(label.replace(":", "") for label in run.shielded))
            performance = Performance(state="talking", text=f"Stopped, but {done} had already gone through.",
                                      emotion="neutral")
            token = runs.active.set(run)
            try:
                sent = await self.perform(performance)
            finally:
                runs.active.reset(token)
            self.ledger.record("(stopped)", performance.text or "", did=f"stopped after {done}")
            return performance, sent
        performance = Performance(state=self.rest_state)
        if run.cancel_reason == "superseded":
            return performance, 0
        return performance, await self.perform(performance)

    async def _stopped(self, event: Event, previous: Run | None) -> tuple[Performance, int]:
        """"Stop" while she was on something: that run is stopped already; she says so, briefly. If a
        change had gone through first, the stopped run says that instead and this one stays quiet."""
        if previous is not None and previous.shielded:
            return Performance(state=self.rest_state), 0
        if previous is not None and any(a.run is previous and a.outcome == "yes" and not a.made
                                        for a in self.approvals.history):
            return Performance(state=self.rest_state), 0   # it said "I stopped before doing it" itself
        performance = decorate(event, Performance(state="talking", text=self.STOPPED, emotion="neutral"))
        sent = await self.perform(performance)
        self.ledger.record(event.title, performance.text or "", did="stopped what she was doing")
        return performance, sent

    async def _handle_voice(self, event: Event, live: dict[str, Any], run: Run | None = None,
                            preface: str = "") -> tuple[Performance, int]:
        text = event.title
        late, self.dropped_at = time.monotonic() - self.dropped_at < confirm.LATE_S, -1e9
        said = confirm.answer(text) if late else None
        if said is not None:
            # A yes or no to a question she already dropped: hers to answer, not the thinker's.
            line = confirm.LATE_YES if said == "yes" else confirm.LEFT_NO
            performance = decorate(event, Performance(state="talking", text=line, emotion="neutral"))
            sent = await self.perform(performance)
            self.ledger.record(text, line, did="nothing; the question was already dropped")
            return performance, sent
        route = await self.route(text)
        record = self.outcomes.heard(text, route, "voice" if event.spoken else "typed")
        self.feed.routed(live, text, route)
        music = route is not None and route.topic == "music"
        if music:
            # Set before acting: the MPRIS doorway reports the new track while the skip is still
            # confirming it, and that reaction would come out ahead of hers.
            self.quiet_media_until = time.monotonic() + self.QUIET_MEDIA_S
        routing = self._routing(run, route)
        outcome = await self.actor.act(text, route, on_call=self._reflex_events(run, routing)) if route is not None else None
        if outcome is not None:
            # A reflex: code wrote the fact, the reaction path adds the quip.
            if not outcome.ok and run is not None:
                run.error = "tools"
            self.quiet_media_until = time.monotonic() + self.QUIET_MEDIA_S
            last = getattr(self.actor, "last", None) or {}
            # The reflex's own name: the gate's tool, or one read off the words ("spotify.like").
            self.acted(record, live, "reflex", outcome.ok,
                       reflex=f"{last.get('server', '')}.{last.get('tool') or route.tool}")
            performance, sent = await self.report(outcome.event(text), outcome.ok, preface)
            self.ledger.record(text, performance.text or "", did=outcome.did)
            return performance, sent
        if route is not None and self.actor.needs_catalogue(route):
            routing("fixed")
            self.acted(record, live, "no_catalogue", True)
            return await self.no_catalogue(event, route, preface)
        if not self.thinker.enabled:
            routing("chat")
            if music:
                self.quiet_media_until = 0.0  # Gemma cannot touch the music; it is not hers to explain
            self.acted(record, live, "chat", True)
            return await self.chat(event, preface)
        routing("escalate")
        outcome = await self.think(text, route, run)
        self.acted(record, live, "thinker", outcome.ok, calls=outcome.calls)
        if music and not outcome.calls:
            self.quiet_media_until = 0.0  # she only talked; a track change now is somebody else's
        elif outcome.calls:
            # Again after the tools: the thinker can take longer than the window.
            self.quiet_media_until = time.monotonic() + self.QUIET_MEDIA_S
        performance = decorate(event, Performance(state="talking", text=prefaced(preface, outcome.fact),
                                                  emotion=outcome.emotion or "neutral"))
        sent = await self.perform(performance)
        if outcome.held is not None:
            self.hold(outcome.held, run)   # the clock starts once she has asked
        # An answer from web results is not kept in her words: the ledger goes into the next
        # sentence's prompt before anything marks it as strangers' text (Thinker._run).
        web = self.thinker.used_untrusted(outcome) if hasattr(self.thinker, "used_untrusted") else False
        # Her question before a held call is not kept in her words either: with it in the ledger, Qwen
        # asked "Remove Blue Monday from your Liked Songs? Say yes." itself, with no call held (confirm.py).
        said = WEB_REPLY if web else confirm.LEDGER_HELD if outcome.held is not None else performance.text or ""
        self.ledger.record(text, said, did=outcome.did)
        return performance, sent

    @staticmethod
    def _routing(run: Run | None, route: Route | None):
        """routing(path, tool=""): the run's `routing` event, once, when the path is known (a reflex
        says so as it starts). None from the gate (off, or no answer in time) sends none."""
        sent = False

        def routing(path: str, tool: str = "") -> None:
            nonlocal sent
            if sent or route is None:
                return
            sent = True
            emit(run, "routing", kind=route.kind, topic=route.topic, decision=route.decision, path=path,
                 tool=tool or None, confidence=route.confidence, ms=route.ms)

        return routing

    def _reflex_events(self, run: Run | None, routing):
        """Actor.act's on_call: the reflex's tool events, and the routing ahead of them."""
        started: dict[str, Any] = {}

        def on_call(phase: str, server: str, tool: str, ok: bool | None = None, error: str = "") -> None:
            key = f"{server}.{tool}"
            if phase == "started":
                routing("reflex", tool)
                started.update(call_id=run.call_id() if run is not None else "", at=time.monotonic(),
                               label=self.actor.label(server, tool))
                emit(run, "tool.started", call_id=started["call_id"], tool=key, label=started["label"], careful=False)
                return
            emit(run, "tool.completed", call_id=started.get("call_id", ""), tool=key,
                 duration=time.monotonic() - started.get("at", time.monotonic()), ok=bool(ok), error=error or None)
            if run is not None and run.cancel_reason and ok and tool not in READ_REFLEXES:
                run.shielded.append(started.get("label", key))

        return on_call

    def acted(self, record, live: dict[str, Any], path: str, ok: bool, reflex: str = "", calls=()) -> None:
        """How a sentence was handled: for the outcome log (§8c) and the live view (routefeed.py)."""
        self.outcomes.acted(record, path, ok, reflex=reflex, calls=calls)
        self.feed.acted(live, path, ok, reflex=reflex, calls=calls)

    async def no_catalogue(self, event: Event, route: Route, preface: str = "") -> tuple[Performance, int]:
        """A request for particular music with only MPRIS to act through: her fixed line that it
        needs a music add-on, instead of a model that would claim it played something (§8b)."""
        self.quiet_media_until = 0.0   # she changed nothing; a track change now is somebody else's
        log.info("voice: %s wants music found (needs_catalogue %.2f) and no music server is configured; "
                 "saying so (ADAPTERS.md: adding a server)", sentence(event.title), route.catalogue)
        performance = decorate(event, Performance(state="talking", text=prefaced(preface, self.rng.choice(NO_CATALOGUE)),
                                                  emotion="neutral"))
        sent = await self.perform(performance)
        self.ledger.record(event.title, performance.text or "", did="needs a music add-on")
        return performance, sent

    async def chat(self, event: Event, preface: str = "") -> tuple[Performance, int]:
        """Gemma answers, with the recent exchanges for context. The voice fallback when the
        thinker is off (and the path every desktop event takes)."""
        context = self.ledger.context(limit=3) if event.source == "voice" else ""
        performance = decorate(event, await self.reactor.react(event, context))
        if preface:
            performance = replace(performance, text=prefaced(preface, performance.text or ""))
        sent = await self.perform(performance)
        if event.source == "voice":
            self.ledger.record(event.title, performance.text or "")
        return performance, sent

    # What the ledger says the user said when the answer was not a sentence of theirs.
    ANSWERED_ON = {"body": "(answered {} on her card)", "ui": "(answered {} in the Brain UI)"}

    def hold(self, held: Held, run: Run | None) -> Approval:
        """Wait for a yes to `held` (confirm.py, approvals.py): its approval opens on `run`
        (`approval.request`, `awaiting_approval`) and the run goes on in a task of its own, which ends
        it once the answer is in and acted on. One at a time: a newer question supersedes an older one."""
        approval = self.approvals.request(run, held)
        task = self.background(self._await_answer(run, approval), "the wait for a yes")
        if run is not None:
            run.continued = True
            run.task = task   # what RunBook.cancel stops: the ✕, "stop", supersede, shutdown
        return approval

    async def _await_answer(self, run: Run | None, approval: Approval) -> None:
        """The rest of the run that asked: wait for the answer (not counted while the user is speaking:
        that is the answer on its way), then make the call after a yes, or say she left it. A cancel of
        the run resolves the approval as cancelled (superseded, for a newer sentence) and makes nothing."""
        if run is not None:
            runs.active.set(run)   # this task's own context: her lines answer this run
        try:
            outcome = await self.approvals.wait(approval, busy=lambda: bool(getattr(self.listener, "busy", False)))
            approval.reply = await self._after_answer(run, approval, outcome)
        except asyncio.CancelledError:
            reason = run.cancel_reason if run is not None else ""
            self.approvals.resolve(approval, "superseded" if reason == "superseded" else "cancelled")
            # A yes that a stop or a newer sentence overtook before the call started: the approval keeps
            # its `yes` (the user's answer), `made` stays False, the run ends cancelled with no
            # tool.started, and the user is told nothing was done.
            unmade = approval.outcome == "yes" and not approval.made
            if unmade:
                log.info("confirm: %s not made: %s after the yes, before the call", approval.held.key,
                         reason or "shutdown")
                self.ledger.record("(stopped)", confirm.LEFT_UNMADE, did=f"left {approval.held.name} undone after a yes")
            if run is None or not reason or reason == "shutdown":
                if run is not None:
                    run.cancel_reason = "shutdown"
                raise
            if unmade and reason == "superseded":
                self.unmade_line = confirm.LEFT_UNMADE   # said ahead of the newer sentence's line
                approval.reply = Performance(state=self.rest_state), 0
            elif unmade:
                performance = Performance(state="talking", text=confirm.LEFT_UNMADE, emotion="neutral")
                approval.reply = performance, await self.perform(performance)
            else:
                approval.reply = await self._after_cancel(run)
        finally:
            if run is not None:
                self.runs.finish(run)

    async def _after_answer(self, run: Run | None, approval: Approval, outcome: str) -> tuple[Performance, int]:
        """What the run does with the outcome: the call after a yes, her line after a no or a timeout,
        nothing more when it was superseded or cancelled."""
        held = approval.held
        said = approval.text or self.ANSWERED_ON.get(approval.by, "({})").format(outcome)
        if outcome == "yes":
            return await self.confirmed(said, approval, run)
        if outcome in ("no", "timeout"):
            self.dropped_at = time.monotonic()
            log.info("confirm: %s not made (%s)", held.key, "a no" if outcome == "no" else
                     f"no answer in {approval.timeout_s:.0f}s")
            if outcome == "no":
                performance = decorate(Event(source="voice", title=said),
                                       Performance(state="talking", text=confirm.LEFT_NO, emotion="neutral"))
            else:
                performance = Performance(state="talking", text=confirm.LEFT_SILENT, emotion="neutral")
            sent = await self.perform(performance)
            # In her memory too: a late "yes" then reads as what it is to the thinker, not as the answer.
            self.ledger.record(said if outcome == "no" else "(no answer)", performance.text or "",
                               did=f"left {held.name} undone")
            return performance, sent
        # Superseded by a newer question or sentence (which says she left it), or cancelled.
        if run is not None:
            run.cancel_reason = run.cancel_reason or ("superseded" if outcome == "superseded" else "stopped")
            if outcome == "cancelled":
                return await self._after_cancel(run)
        return Performance(state=self.rest_state), 0

    async def confirmed(self, text: str, approval: Approval, run: Run | None = None) -> tuple[Performance, int]:
        """The yes: the stored call, exactly as it was asked about (its digest checked first), and no model
        asked again. Its tool events go out like any call; once under way a stop lets it finish. Code
        writes the fact and the reaction path adds the quip, as for a reflex."""
        held = approval.held
        if not self.approvals.verify(approval):
            log.warning("confirm: %s not made: the stored call no longer matches what was asked about", held.key)
            if run is not None:
                run.error = "other"
            performance = Performance(state="talking", text=confirm.LEFT_CHANGED, emotion="alert")
            return performance, await self.perform(performance)
        key, label = held.key, self.toolbox.label(held.server, held.name)
        # Last look before anything is made: the yes still stands and the run is still going, neither
        # stopped nor superseded on the way here. If not, nothing starts (_await_answer says so). A newer
        # sentence that only waits its turn ([runs] supersede = false) does not stop it.
        stopped = run.cancel_reason if run is not None else ""
        if run is not None and not stopped and (run.done or self.runs.live.get(run.run_id) is not run):
            stopped = run.cancel_reason = "stopped"
        if stopped or approval.outcome != "yes":
            log.info("confirm: %s not made: the run was %s after the yes", key, stopped or "ended")
            raise asyncio.CancelledError
        call_id = run.call_id() if run is not None else ""
        careful = held.name in getattr(self.toolbox.servers.get(held.server), "careful", ())
        emit(run, "tool.started", call_id=call_id, tool=key, label=label, careful=careful)
        started = time.monotonic()
        approval.made = True
        work = asyncio.ensure_future(confirm.run(self.toolbox, self.toolbox.adapters.get(held.server), held,
                                                 expected=approval.digest))
        try:
            outcome = await asyncio.shield(work)
        except asyncio.CancelledError:
            outcome = await work   # bounded by the server's call_timeout_s: a yes is never left half made
            self._call_done(run, call_id, key, started, outcome)
            if run is not None and outcome.ok:
                run.shielded.append(label)
            raise
        self._call_done(run, call_id, key, started, outcome)
        log.info("confirm: %s made after a yes -> %s", key, "ok" if outcome.ok else "failed")
        if run is not None and not outcome.ok:
            run.error = "tools"
        performance, sent = await self.report(outcome.event(text), outcome.ok)
        self.ledger.record(text, performance.text or "", did=outcome.did)
        return performance, sent

    @staticmethod
    def _call_done(run: Run | None, call_id: str, key: str, started: float, outcome: Outcome) -> None:
        result = outcome.calls[0] if outcome.calls else None
        emit(run, "tool.completed", call_id=call_id, tool=key, duration=time.monotonic() - started, ok=outcome.ok,
             error=None if outcome.ok else (error_code(result) if result is not None else "failed"))

    def confirm_stats(self) -> dict[str, Any]:
        pending = self.approvals.open
        return {"waiting": pending.held.key if pending is not None else None,
                "timeout_s": pending.timeout_s if pending is not None else self.config.approvals.change_s}

    async def think(self, text: str, route: Route | None, run: Run | None = None) -> Outcome:
        """Qwen, with cover that scales with the wait: the thinking pose at once and nothing said (a
        warm round is ~2 s, and "On it." before an answer to "what's up?" reads odd), a spoken
        acknowledgement from `acks` only after `ack_after_s`, "Still on it." after `still_on_it_s`
        (a cold load is 7-17 s), then her reply as the outcome."""
        await self.perform(Performance(state="thinking"))

        async def cover() -> None:
            # Not once the run is being stopped (a change finishing first): she is not still on it.
            await asyncio.sleep(self.config.thinker.ack_after_s)
            if run is None or not run.cancel_reason:
                await self.perform(Performance(state="thinking", text=self.rng.choice(self.config.thinker.acks)))
            await asyncio.sleep(max(self.config.thinker.still_on_it_s - self.config.thinker.ack_after_s, 0.1))
            if run is None or not run.cancel_reason:
                await self.perform(Performance(state="thinking", text="Still on it."))

        reminder = asyncio.get_running_loop().create_task(cover())
        try:
            careful = route is not None and route.library_change >= 0.5
            today = self.today()
            return await self.thinker.run(text, await self.situation(today), careful=careful,
                                          topic=route.topic if route is not None else "", recent=self.ledger.lines(),
                                          route=route, public_context=today, run=run)
        finally:
            reminder.cancel()

    @staticmethod
    def today() -> str:
        """The date and time: the one part of the situation with nothing private in it, and all of it
        that stays once a web result is in the thinker's conversation (Thinker._run)."""
        return time.strftime("Today is %A %d %B %Y, %H:%M local time.")

    async def situation(self, today: str = "") -> str:
        """What Qwen is told before the sentence: the date, what the servers say is going on and the
        names in the user's library (speech-to-text mishears them). The recent exchanges go to it
        as ledger lines of their own, so the thinker can drop the oldest when the prompt is long."""
        parts = [today or self.today()]
        here = await self.actor.situation()
        if here:
            parts.append(here)
        if self.vocabulary:
            parts.append(f"Names in the user's library: {self.hotwords()}.")
        return " ".join(parts)

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
            skipped["gate"] = self.gate.disabled_reason or ("still starting" if getattr(self.gate, "is_starting", False)
                                                            else "gate off")
        if not self.config.brain.enabled or getattr(self.reactor, "session", None) is None:
            skipped["voice"] = "brain off (canned lines)"
        if not self.thinker.enabled:
            skipped["brain"] = "thinker off"
        if not self.speaker.ready:
            stats = self.speaker.stats()
            skipped["tts"] = stats.get("reason") or ("still loading" if stats.get("loading") else "speech off")
        if not self.listener.ready:
            skipped["whisper"] = self.listener.reason or "voice off"
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

    async def report(self, event: Event, ok: bool, preface: str = "") -> tuple[Performance, int]:
        """Say what she did: the fact (event.body, written by code) and the reactor's quip after it."""
        performance = decorate(event, await self.reactor.react(event))
        quip = short_quip(performance.text or "", self.QUIP_WORDS)
        if len(event.body) > self.QUIP_UNTIL_CHARS:
            quip = ""
        text = prefaced(preface, f"{event.body} {quip}".strip() if quip else event.body)
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


def speech_seconds(performance: Performance) -> float:
    """How long the line takes: the wav's length, else the widget's reading pace (bubble.gd)."""
    if performance.audio:
        import wave

        try:
            with wave.open(str(performance.audio), "rb") as wav:
                return wav.getnframes() / float(wav.getframerate() or 1)
        except (OSError, EOFError, wave.Error):
            pass
    return min(max(0.6 + len(performance.text or "") * 0.055, 1.6), 9.0)


def prefaced(preface: str, text: str) -> str:
    """Her line with `preface` said first (that she left a held call undone), as one line: the widget
    cuts a line short when the next one arrives."""
    return f"{preface} {text}".strip() if preface else text


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
