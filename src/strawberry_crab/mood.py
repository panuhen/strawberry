"""Her living mood (WIRING.md §29): two dials, energy (calm … lively) and feeling (grumpy … pleased), each -1..1.

They drift with small nudges from what happens and ease back to her temperament (persona.md's ## Temperament) on
their own, exponentially: about two thirds of the way back in `recovery` minutes, or in `sulk` minutes for a dip in
feeling, which lingers.

**What moves it** is the table NUDGES, and nothing else: the user's own acts and objective signals.
  - the user: any sentence said or typed to her, a whole-sentence thanks or praise, a whole-sentence "no, wrong", a
    run they stopped; a poke on her, pokes in a row, a pat (a trusted body's `touch` on an entity of kind `crab`);
  - the tools: a call that worked or failed (the run events; a refusal is neither);
  - the music: the beat watcher's estimates (a fast steady beat livelier, a slow one calmer), its sections (a drop,
    a build, a break) and the music stopping;
  - the clock: her resting energy is lower late at night and sinks slowly while nothing happens (`baseline`).
No notification, track title, web page or tool result moves it through its words: none of that text comes here. The
user's own sentence is matched against fixed phrase lists and dropped at once; a reason names its source
(`voice.thanks`), never the words, and nothing here logs a sentence.

**What it moves**, expression only:
  - the bodies: `{"type": "mood", "energy": e, "feeling": f}` to each trusted v2 body that asked for it
    (`capabilities.mood`, PROTOCOL Part 1e), once it moved SEND_STEP and at most every SEND_GAP_S; the crab shapes
    her sway, dance, posture, eyes and gaze with it, within fixed bounds (widget/mood.gd);
  - Piper's pace: the length scale times `pace()`, at most PACE_MAX either way;
  - the reaction model's word choice: one sentence in its prompt (`line()`), never a number.
It never reaches a decision: the gate, the reflexes, the thinker (its prompt, its tools), tiers, approvals and
whether she answers read nothing of it. tests/test_mood.py runs the daemon's decisions at extreme moods and compares.

**Bounded.** The dials clamp to -1..1; each source moves a dial at most its `cap` in any minute, all sources
together TOTAL_CAP; the outputs have fixed ranges (PACE_MAX here, the widget's own constants there).

**Kept lightly** in `<state>/mood.json`: the dials and when, the last KEEP_S of samples (once a minute) and the
reasons (a source and two sizes each, never text). At start the time since is eased away as if she had been
running, so after a night off she wakes near her temperament. `[mood] enabled = false`: no nudges, nothing sent, the
shipped pace and no line, as before the mood.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import paths
from .persona import Temperament, shipped_temperament
from .runs import _whole as whole_sentence

log = logging.getLogger("strawberryd.mood")


@dataclass(frozen=True)
class Nudge:
    energy: float     # one step on the energy dial (a scaled nudge: times -1..1)
    feeling: float    # one step on the feeling dial
    cap: float        # the most this source moves either dial in any one minute
    what: str         # for the Brain UI's legend


# The one table of what moves her mood. Sizes are steps on a dial of -1..1.
NUDGES: dict[str, Nudge] = {
    "voice.spoke": Nudge(0.04, 0.0, 0.12, "the user said or typed a sentence to her"),
    "voice.thanks": Nudge(0.03, 0.15, 0.30, "the whole sentence a thanks or a praise (\"thanks\", \"well done\")"),
    "voice.wrong": Nudge(-0.02, -0.12, 0.24, "the whole sentence a correction (\"no, wrong\", \"that's not it\")"),
    "voice.stop": Nudge(0.0, -0.06, 0.12, "the user stopped what she was doing"),
    "touch.poke": Nudge(0.05, 0.0, 0.15, "a poke on her"),
    "touch.pokes": Nudge(0.02, -0.08, 0.24, "the third poke in a row, and each one after"),
    "touch.pat": Nudge(-0.02, 0.12, 0.30, "a pat: a still press she leans into"),
    "tool.ok": Nudge(0.0, 0.03, 0.09, "a tool call that worked"),
    "tool.failed": Nudge(0.0, -0.05, 0.15, "a tool call that failed (a refusal is not a failure)"),
    "music.beat": Nudge(0.008, 0.0, 0.06, "a steady beat, each estimate: fast music livelier, slow music calmer"),
    "music.drop": Nudge(0.15, 0.03, 0.30, "the music drops"),
    "music.build": Nudge(0.06, 0.0, 0.12, "the music builds up"),
    "music.break": Nudge(-0.06, 0.0, 0.12, "a break in the music"),
    "music.stopped": Nudge(-0.08, 0.0, 0.08, "the music stopped"),
}
TOTAL_CAP = 0.4                # all sources together, per dial, in any one minute
WINDOW_S = 60.0

# Her resting energy moves with the clock (not a nudge: where she eases back to). Late at night it is lower;
# with nothing from the user or the music for IDLE_AFTER_S it sinks, down to IDLE_ENERGY at IDLE_FULL_S.
NIGHT = ((23.0, 24.0, -0.35), (0.0, 6.0, -0.35), (21.0, 23.0, -0.15), (6.0, 8.0, -0.15))   # from, to, energy
IDLE_AFTER_S = 600.0
IDLE_FULL_S = 2400.0
IDLE_ENERGY = -0.3
BASELINE_STEP = 0.05           # a baseline shift this big goes into the reasons

PACE_MAX = 0.08                # Piper's length scale, at most this much shorter (lively) or longer (calm)
TICK_S = 1.0
SEND_STEP = 0.03               # a body is sent the mood once a dial moved this much…
SEND_GAP_S = 2.0               # …and at most this often
SETTLE_S = 20.0                # a smaller change still goes out after this long, so the body comes to rest with her
SAMPLE_S = 60.0                # the graph's resolution
KEEP_S = 6 * 3600.0            # the graph and the reasons cover this long
MAX_REASONS = 400
SAVE_S = 60.0
STREAK_S = 4.0                 # = widget/touch.gd STREAK_S: a poke this soon after the last one continues a streak…
STREAK_AT = 3                  # …and from this one on she is annoyed (touch.gd ANNOYED_AT)
SECTIONS = {"drop": "music.drop", "build": "music.build", "break": "music.break"}

# Whole sentences only (runs._whole: these phrases and the fillers, nothing else), so "thanks, now play jazz" is no
# thanks and "what's wrong with my code" no correction. "no" is never a thanks filler: "no thanks" declines.
THANKS = ("thanks", "thank you", "thanks a lot", "thanks a bunch", "many thanks", "cheers", "ta", "good job",
          "great job", "well done", "nice one", "nice work", "good work", "good girl", "good crab", "you're the best",
          "you are the best", "love you", "i love you", "brilliant", "perfect", "awesome", "lovely", "great", "nice",
          "spot on", "much appreciated", "you're a star", "legend")
THANKS_FILLER = ("oh", "ok", "okay", "strawberry", "so", "very", "much", "really", "wow", "hey", "yes", "yeah", "that",
                 "that's", "was", "mate", "again", "for", "the", "help", "just", "always", "you")
WRONG = ("wrong", "that's wrong", "that is wrong", "you're wrong", "you are wrong", "that's not right",
         "that is not right", "not right", "that's not it", "that is not it", "not that", "not that one", "wrong one",
         "wrong song", "wrong track", "wrong thing", "you got it wrong", "got it wrong", "that's not what i asked",
         "that's not what i said", "that's not what i meant", "not what i asked", "not what i said",
         "not what i meant", "you misheard", "you misheard me", "bad crab")
WRONG_FILLER = ("no", "nope", "nah", "strawberry", "oh", "um", "uh", "actually", "hmm", "again", "for", "all",
                "totally", "completely", "sorry", "that's", "it's", "still")
_THANKS = sorted({(p, True) for p in THANKS} | {(p, False) for p in THANKS_FILLER}, key=lambda i: -len(i[0].split()))
_WRONG = sorted({(p, True) for p in WRONG} | {(p, False) for p in WRONG_FILLER}, key=lambda i: -len(i[0].split()))


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return low if value < low else high if value > high else value


def is_thanks(text: str) -> bool:
    return whole_sentence(text, _THANKS)


def is_wrong(text: str) -> bool:
    return whole_sentence(text, _WRONG)


def night_energy(hour: float) -> float:
    """Her resting energy's shift at this local hour (0..24)."""
    for start, end, energy in NIGHT:
        if start <= hour < end:
            return energy
    return 0.0


def idle_energy(idle_s: float) -> float:
    """…and after this long with nothing from the user or the music."""
    if idle_s <= IDLE_AFTER_S:
        return 0.0
    return IDLE_ENERGY * min(1.0, (idle_s - IDLE_AFTER_S) / (IDLE_FULL_S - IDLE_AFTER_S))


def local_hour() -> float:
    now = datetime.now()
    return now.hour + now.minute / 60.0


def words(energy: float, feeling: float) -> str:
    """The reaction model's line about her mood: words only, never a number."""
    lively = ("sleepy" if energy <= -0.55 else "calm" if energy <= -0.2 else "" if energy < 0.2
              else "a bit lively" if energy < 0.55 else "very lively")
    pleased = ("grumpy" if feeling <= -0.55 else "a little grumpy" if feeling <= -0.2 else "" if feeling < 0.2
               else "pleased" if feeling < 0.55 else "delighted")
    said = " and ".join(part for part in (lively, pleased) if part)
    return f"You feel {said or 'even and settled'} right now."


def mood_path() -> Path:
    return paths.state_dir() / "mood.json"


class Mood:
    """The two dials, what moved them, and what they move. `send` (set by the daemon): the hub's send_mood."""

    def __init__(self, config: Any, temperament: Callable[[], Temperament] | None = None, path: Path | None = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 hour: Callable[[], float] = local_hour) -> None:
        self.enabled = bool(getattr(config, "enabled", True))
        self.temperament = temperament or shipped_temperament
        self._path = path
        self.clock, self.wall, self.hour = clock, wall, hour
        base = self.temperament()
        self.energy, self.feeling = base.energy, base.feeling
        self.at = clock()
        self.active_at = clock()           # the user or the music, last (the idle shift)
        self.windows: dict[str, deque] = {}
        self.total: deque = deque()
        self.reasons: deque[dict[str, Any]] = deque(maxlen=MAX_REASONS)
        self.history: deque[list[float]] = deque()
        self.sampled_at = -1e9
        self.shifts = {"time": 0.0, "idle": 0.0}   # the baseline's shifts as last put in the reasons
        self.section = ""
        self.playing = False
        self.streak = 0
        self.poked_at = -1e9
        self.counts = {"nudges": 0, "capped": 0, "sent": 0}
        self.sent: tuple[float, float] | None = None
        self.sent_at = -1e9
        self.dirty = False
        self.saved_at = clock()
        self.send: Callable[[dict[str, Any]], Awaitable[int]] | None = None
        self.task: asyncio.Task | None = None
        self.events_task: asyncio.Task | None = None
        self.book: Any = None
        self.queue: asyncio.Queue | None = None

    @property
    def path(self) -> Path:
        return self._path or mood_path()

    # ------------------------------------------------------------------ the dials

    def baseline(self, now: float | None = None) -> tuple[float, float]:
        """Where she eases back to now: her temperament, its energy shifted by the hour and by idle time."""
        now = self.clock() if now is None else now
        base = self.temperament()
        energy = base.energy + night_energy(self.hour()) + idle_energy(now - self.active_at)
        return clamp(energy), clamp(base.feeling)

    def advance(self, now: float | None = None) -> None:
        """Ease the dials toward the baseline for the time since the last call (exponential recovery)."""
        now = self.clock() if now is None else now
        dt = now - self.at
        if dt <= 0.0:
            return
        self.at = now
        base = self.temperament()
        energy, feeling = self.baseline(now)
        self.energy = energy + (self.energy - energy) * math.exp(-dt / (base.recovery * 60.0))
        minutes = base.sulk if self.feeling < feeling else base.recovery
        self.feeling = feeling + (self.feeling - feeling) * math.exp(-dt / (minutes * 60.0))

    def current(self) -> tuple[float, float]:
        self.advance()
        return self.energy, self.feeling

    def nudge(self, source: str, scale: float = 1.0) -> tuple[float, float]:
        """One step of `source` (NUDGES), times `scale` (-1..1), within its cap and the total cap for the last
        minute. Returns what moved."""
        if not self.enabled or source not in NUDGES:
            return 0.0, 0.0
        now = self.clock()
        self.advance(now)
        step = NUDGES[source]
        scale = clamp(scale)
        wanted = (step.energy * scale, step.feeling * scale)
        window = self.windows.setdefault(source, deque())
        for queue in (window, self.total):
            while queue and now - queue[0][0] > WINDOW_S:
                queue.popleft()
        moved = []
        for i, value in enumerate(wanted):
            room = min(step.cap - sum(entry[i + 1] for entry in window),
                       TOTAL_CAP - sum(entry[i + 1] for entry in self.total))
            moved.append(math.copysign(min(abs(value), max(room, 0.0)), value))
        energy, feeling = moved
        if (abs(energy), abs(feeling)) != (abs(wanted[0]), abs(wanted[1])):
            self.counts["capped"] += 1
        if energy == 0.0 and feeling == 0.0:
            return 0.0, 0.0
        before = (self.energy, self.feeling)
        self.energy, self.feeling = clamp(self.energy + energy), clamp(self.feeling + feeling)
        energy, feeling = self.energy - before[0], self.feeling - before[1]
        window.append((now, abs(energy), abs(feeling)))
        self.total.append((now, abs(energy), abs(feeling)))
        self.counts["nudges"] += 1
        self.reasons.append({"t": round(self.wall(), 1), "source": source, "energy": round(energy, 3),
                             "feeling": round(feeling, 3)})
        self.dirty = True
        return energy, feeling

    def active(self) -> None:
        """Something from the user or the music: the idle shift starts over."""
        self.advance()
        self.active_at = self.clock()

    def reset(self) -> tuple[float, float]:
        """Back to her baseline now (the Brain UI's button)."""
        self.advance()
        energy, feeling = self.baseline()
        moved = (energy - self.energy, feeling - self.feeling)
        self.energy, self.feeling = energy, feeling
        self.windows.clear()
        self.total.clear()
        self.reasons.append({"t": round(self.wall(), 1), "source": "reset", "energy": round(moved[0], 3),
                             "feeling": round(moved[1], 3)})
        self.dirty = True
        self.sent_at = -1e9
        return energy, feeling

    # ------------------------------------------------------------------ the inputs

    def heard(self, text: str) -> list[str]:
        """The user's own sentence (said or typed; Daemon.handle_voice): a little livelier for being spoken to, and a
        thanks or a correction when the whole sentence is one. The text is not kept. Returns the sources."""
        if not self.enabled:
            return []
        self.active()
        sources = ["voice.spoke"]
        if is_thanks(text):
            sources.append("voice.thanks")
        elif is_wrong(text):
            sources.append("voice.wrong")
        for source in sources:
            self.nudge(source)
        return sources

    def touched(self, entity_kind: str, kind: str) -> str:
        """A trusted body's `touch` (Daemon.body_touch). Only one on her (an entity of kind `crab`) moves the dials:
        a pat, a poke, and from the third poke in a row each one more annoying. Returns the source, or ""."""
        if not self.enabled:
            return ""
        self.active()
        if entity_kind != "crab" or kind not in ("poke", "pat"):
            return ""
        if kind == "pat":
            self.nudge("touch.pat")
            return "touch.pat"
        now = self.clock()
        self.streak = self.streak + 1 if now - self.poked_at < STREAK_S else 1
        self.poked_at = now
        source = "touch.pokes" if self.streak >= STREAK_AT else "touch.poke"
        self.nudge(source)
        return source

    def tempo(self, estimate: dict[str, Any]) -> list[str]:
        """A beat estimate (Daemon.set_tempo, already checked): its numbers and its section, never a title."""
        if not self.enabled:
            return []
        if estimate.get("silent"):
            stopped = self.playing
            self.playing, self.section = False, ""
            if stopped:
                self.nudge("music.stopped")
                return ["music.stopped"]
            return []
        self.playing = True
        self.active()
        sources = []
        section = str(estimate.get("section") or "")
        if section != self.section and section in SECTIONS:
            sources.append(SECTIONS[section])
            self.nudge(SECTIONS[section])
        self.section = section
        confidence = float(estimate.get("confidence", 0.0) or 0.0)
        if estimate.get("steady", True) and confidence >= 0.3:
            # 70 BPM and below calms, about 100 is even, 128 lively, 150 and over the most.
            pace = clamp((float(estimate.get("bpm", 0.0) or 0.0) - 70.0) / 80.0, 0.0, 1.0)
            self.nudge("music.beat", clamp(2.0 * pace - 0.7) * min(confidence, 1.0))
            sources.append("music.beat")
        return sources

    def run_event(self, message: dict[str, Any]) -> str:
        """A run event (runs.RunBook's stream): a tool's outcome, a run the user stopped. Codes only."""
        if not self.enabled:
            return ""
        kind = message.get("type")
        if kind == "tool.completed":
            if message.get("ok") is True:
                self.nudge("tool.ok")
                return "tool.ok"
            if message.get("ok") is False and message.get("error") != "refused":
                self.nudge("tool.failed")
                return "tool.failed"
        elif kind == "run.cancelled" and message.get("reason") == "stopped":
            self.nudge("voice.stop")
            return "voice.stop"
        return ""

    # ------------------------------------------------------------------ the outputs

    def message(self) -> dict[str, Any]:
        """The bus message (PROTOCOL Part 1e)."""
        energy, feeling = self.current()
        return {"type": "mood", "energy": round(energy, 2), "feeling": round(feeling, 2)}

    def pace(self) -> float:
        """Piper's length scale is multiplied by this: shorter (faster) when lively, longer when calm."""
        if not self.enabled:
            return 1.0
        energy, _ = self.current()
        return round(1.0 - PACE_MAX * clamp(energy), 4)

    def line(self) -> str:
        """One sentence for the reaction model's prompt; "" with the mood off."""
        if not self.enabled:
            return ""
        return words(*self.current())

    def stats(self) -> dict[str, Any]:
        energy, feeling = self.current()
        return {"enabled": self.enabled, "energy": round(energy, 3), "feeling": round(feeling, 3)} | dict(self.counts)

    def view(self) -> dict[str, Any]:
        """The Brain UI's Mood view: the dials, where they rest, the temperament, the graph and the reasons."""
        energy, feeling = self.current()
        rest = self.baseline()
        now = self.wall()
        history = [list(point) for point in self.history if now - point[0] <= KEEP_S]
        history.append([round(now, 1), round(energy, 3), round(feeling, 3)])
        return {"enabled": self.enabled, "energy": round(energy, 3), "feeling": round(feeling, 3),
                "baseline": {"energy": round(rest[0], 3), "feeling": round(rest[1], 3)},
                "temperament": self.temperament().to_dict(), "line": words(energy, feeling),
                "pace": self.pace(), "history": history,
                "reasons": [r for r in self.reasons if now - r["t"] <= KEEP_S][::-1],
                "nudges": {name: {"energy": n.energy, "feeling": n.feeling, "cap": n.cap, "what": n.what}
                           for name, n in NUDGES.items()},
                "limits": {"total_cap": TOTAL_CAP, "pace": PACE_MAX, "keep_s": KEEP_S},
                "counts": dict(self.counts), "file": str(self.path)}

    # ------------------------------------------------------------------ the clock

    def tick(self) -> None:
        """Once a second: ease, note a baseline shift in the reasons, sample the graph, save now and then."""
        now = self.clock()
        self.advance(now)
        for name, shift in (("time", night_energy(self.hour())), ("idle", idle_energy(now - self.active_at))):
            moved = shift - self.shifts[name]
            if abs(moved) >= BASELINE_STEP or (shift == 0.0 and moved != 0.0):
                self.shifts[name] = shift
                self.reasons.append({"t": round(self.wall(), 1), "source": f"baseline.{name}", "energy": round(moved, 3),
                                     "feeling": 0.0})
                self.dirty = True
        if now - self.sampled_at >= SAMPLE_S:
            self.sampled_at = now
            wall = self.wall()
            self.history.append([round(wall, 1), round(self.energy, 3), round(self.feeling, 3)])
            while self.history and wall - self.history[0][0] > KEEP_S:
                self.history.popleft()
            self.dirty = True
        if self.dirty and now - self.saved_at >= SAVE_S:
            self.save()

    def due(self) -> dict[str, Any] | None:
        """The message to send the bodies now, or None: moved SEND_STEP and SEND_GAP_S since the last, or any change
        after SETTLE_S."""
        if not self.enabled:
            return None
        message = self.message()
        now = self.clock()
        if self.sent is not None:
            moved = max(abs(message["energy"] - self.sent[0]), abs(message["feeling"] - self.sent[1]))
            gap = now - self.sent_at
            if moved == 0.0 or gap < SEND_GAP_S or (moved < SEND_STEP and gap < SETTLE_S):
                return None
        self.sent, self.sent_at = (message["energy"], message["feeling"]), now
        return message

    async def run(self) -> None:
        while True:
            try:
                self.tick()
                message = self.due()
                if message is not None and self.send is not None:
                    self.counts["sent"] += await self.send(message)
            except Exception as exc:   # noqa: BLE001 - the mood must never take anything down
                log.warning("mood: a tick failed (%s)", type(exc).__name__)
            await asyncio.sleep(TICK_S)

    async def follow(self, queue: asyncio.Queue) -> None:
        while True:
            message = await queue.get()
            try:
                self.run_event(message)
            except Exception as exc:   # noqa: BLE001
                log.warning("mood: a run event failed (%s)", type(exc).__name__)

    def start(self, book: Any = None) -> None:
        """Load the file, follow the run events (tool outcomes, a stop) and tick. Nothing with the mood off."""
        if not self.enabled:
            return
        self.load()
        loop = asyncio.get_running_loop()
        if book is not None and hasattr(book, "subscribe"):
            self.book, self.queue = book, book.subscribe()
            self.events_task = loop.create_task(self.follow(self.queue))
        self.task = loop.create_task(self.run())

    async def close(self) -> None:
        for task in (self.task, self.events_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.task = self.events_task = None
        if self.book is not None and self.queue is not None:
            self.book.unsubscribe(self.queue)
            self.queue = None
        if self.enabled and self.dirty:
            self.save()

    # ------------------------------------------------------------------ the file

    def save(self) -> None:
        self.saved_at = self.clock()
        self.advance()
        data = {"version": 1, "energy": round(self.energy, 4), "feeling": round(self.feeling, 4),
                "at": round(self.wall(), 1), "history": list(self.history), "reasons": list(self.reasons)}
        try:
            paths.write_atomic(self.path, json.dumps(data, separators=(",", ":")) + "\n", private=True)
            self.dirty = False
        except OSError as exc:
            log.warning("mood: %s not saved (%s)", self.path, type(exc).__name__)

    def load(self) -> bool:
        """The saved dials, eased for the time since (at most a day of it counts), and the last hours' graph and
        reasons. A file that does not check out is left alone: she starts at her temperament."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except (OSError, ValueError) as exc:
            log.info("mood: %s not read (%s); starting at her temperament", self.path, type(exc).__name__)
            return False
        if not isinstance(data, dict):
            return False
        numbers = [data.get(k) for k in ("energy", "feeling", "at")]
        if not all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) for n in numbers):
            log.info("mood: %s does not check out; starting at her temperament", self.path)
            return False
        wall = self.wall()
        self.energy, self.feeling = clamp(float(numbers[0])), clamp(float(numbers[1]))
        self.at = self.clock() - min(max(wall - float(numbers[2]), 0.0), 86400.0)
        self.advance()
        for point in data.get("history") or []:
            if (isinstance(point, list) and len(point) == 3 and all(isinstance(v, (int, float)) for v in point)
                    and wall - point[0] <= KEEP_S):
                self.history.append([float(point[0]), clamp(float(point[1])), clamp(float(point[2]))])
        for reason in data.get("reasons") or []:
            if (isinstance(reason, dict) and isinstance(reason.get("t"), (int, float)) and wall - reason["t"] <= KEEP_S
                    and (reason.get("source") in NUDGES or str(reason.get("source")) in
                         ("reset", "baseline.time", "baseline.idle"))
                    and all(isinstance(reason.get(k), (int, float)) for k in ("energy", "feeling"))):
                self.reasons.append({"t": float(reason["t"]), "source": str(reason["source"]),
                                     "energy": clamp(float(reason["energy"])), "feeling": clamp(float(reason["feeling"]))})
        log.info("mood: carried over from %s", self.path)
        return True
