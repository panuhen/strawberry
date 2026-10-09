"""Runs (WIRING.md §18, PROTOCOL.md §11): every input she handles, watched from start to end.

A sentence the user says or types, or a notification she reacts to, is one `Run`. It goes through

    routing -> thinking <-> tool -> (awaiting_approval) -> speaking -> completed | failed | cancelled

and each step is an event on the bus, for the widget's step chip, the Brain UI's Runs view and any
other body that asked for them (protocol v2). Exactly one terminal event ends a run: `finish()` is
idempotent and the daemon calls it in a `finally`.

    run = book.start("typed")                  # Daemon.handle_voice
    emit(run, "routing", kind=..., path=...)   # anywhere that holds the run (None: a no-op)
    book.cancel(run.run_id, "stopped")         # the widget's ✕, the Brain UI, "stop", supersede
    book.finish(run)                           # run.completed / run.failed / run.cancelled

`emit` keeps only the fields PROTOCOL §11 lists for the event's type, as scalars, strings cut
short: a tool's arguments, its result, the user's sentence and her line never reach the bus, the
Brain UI or the log through here. It never waits: each sink is a queue fed with put_nowait, and a
sink that falls behind misses events (the run's own record keeps them). A run carries at most
MAX_EVENTS; after that only its terminal event goes out.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("strawberryd.runs")

SOURCES = ("voice", "typed", "notification", "job")       # job: reserved for scheduled work
FOREGROUND = ("voice", "typed")                          # one at a time (Daemon.handle_voice)
TERMINAL = {"run.completed": "completed", "run.failed": "failed", "run.cancelled": "cancelled"}
STATE_OF = {"routing": "routing", "thinking": "thinking", "tool.started": "tool", "tool.completed": "thinking",
            "approval.request": "awaiting_approval", "speaking": "speaking", **TERMINAL}

# What each event may carry (PROTOCOL.md §11), besides type, run_id, seq and t.
FIELDS: dict[str, frozenset[str]] = {
    "routing": frozenset({"kind", "topic", "decision", "path", "tool", "confidence", "ms"}),
    "thinking": frozenset({"backend", "model"}),
    "tool.started": frozenset({"call_id", "tool", "label", "careful"}),
    "tool.completed": frozenset({"call_id", "tool", "duration", "ok", "error"}),
    "speaking": frozenset({"duration", "emotion"}),
    "run.completed": frozenset({"duration", "outcome"}),
    "run.failed": frozenset({"duration", "error"}),
    "run.cancelled": frozenset({"duration", "reason"}),
}
# Fields that are fixed codes: any other value is dropped.
CODES: dict[tuple[str, str], frozenset[str]] = {
    ("routing", "path"): frozenset({"reflex", "escalate", "fixed", "chat"}),
    ("tool.completed", "error"): frozenset({"timeout", "refused", "failed", "unavailable"}),
    ("run.completed", "outcome"): frozenset({"spoken", "silent", "nothing"}),
    ("run.failed", "error"): frozenset({"timeout", "backend", "tools", "other"}),
    ("run.cancelled", "reason"): frozenset({"superseded", "stopped", "didnt_catch", "shutdown"}),
}
NUMBERS = frozenset({"confidence", "ms", "duration"})
FLAGS = frozenset({"careful", "ok"})
MAX_TEXT = 64          # a tool name, a label, a model name: never a sentence
MAX_EVENTS = 200       # per run; the terminal event always goes out
QUEUE = 256            # events a sink may lag behind before it misses some
KEEP = 50              # finished runs kept for the Brain UI ([runs] keep)
_PLAIN = re.compile(r"[^\w .:/+@…-]")

# The run whose work is going on in this task (and the tasks it starts): what `perform` answers.
active: ContextVar["Run | None"] = ContextVar("strawberry_run", default=None)


@dataclass(eq=False)
class Run:
    run_id: str
    source: str
    t0: float
    foreground: bool = True
    at: float = field(default_factory=time.time)
    seq: int = 0
    state: str = "routing"
    task: asyncio.Task | None = None
    tools: list[str] = field(default_factory=list)       # server.tool, in the order they were called
    outcome: str = ""                                     # completed | failed | cancelled, once done
    detail: str = ""                                      # the terminal event's outcome, error or reason
    error: str = ""                                       # set by whoever saw it fail (run.failed's code)
    cancel_reason: str = ""
    spoke: bool = False
    shielded: list[str] = field(default_factory=list)     # labels of change calls finished after a cancel
    events: list[dict[str, Any]] = field(default_factory=list)
    ended: float | None = None
    calls: itertools.count = field(default_factory=lambda: itertools.count(1))
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    book: "RunBook | None" = None

    @property
    def done(self) -> bool:
        return bool(self.outcome)

    @property
    def busy(self) -> bool:
        return not self.done

    def call_id(self) -> str:
        return f"c{next(self.calls)}"

    def view(self) -> dict[str, Any]:
        """For the Brain UI: the record and its events, all of them whitelisted already."""
        end = self.ended if self.ended is not None else time.monotonic()
        return {"run_id": self.run_id, "source": self.source, "at": round(self.at, 3), "state": self.state,
                "outcome": self.outcome or None, "detail": self.detail or None, "tools": list(self.tools),
                "duration": round(end - self.t0, 3), "foreground": self.foreground,
                "events": [dict(e) for e in self.events]}


def clean(kind: str, fields: dict[str, Any]) -> dict[str, Any]:
    """The fields `kind` may carry, each a plain scalar; anything else is dropped."""
    allowed = FIELDS.get(kind, frozenset())
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in allowed or value is None:
            continue
        if key in FLAGS:
            if isinstance(value, bool):
                out[key] = value
            continue
        if key in NUMBERS:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[key] = round(float(value), 3)
            continue
        if not isinstance(value, str):
            continue
        codes = CODES.get((kind, key))
        if codes is not None:
            if value in codes:
                out[key] = value
            continue
        text = _PLAIN.sub("", value)[:MAX_TEXT]
        if text:
            out[key] = text
    return out


class RunBook:
    """The runs: the current foreground one, the recent ones, and who is listening."""

    def __init__(self, keep: int = KEEP, events: bool = True) -> None:
        self.ids = itertools.count(1)
        self.recent_runs: deque[Run] = deque(maxlen=max(1, keep))
        self.live: dict[str, Run] = {}
        self.current: Run | None = None
        self.events = events
        self.sinks: set[asyncio.Queue] = set()
        self.started = 0
        self.dropped = 0

    def start(self, source: str, foreground: bool | None = None) -> Run:
        if source not in SOURCES:
            raise ValueError(f"unknown run source {source!r}")
        fg = source in FOREGROUND if foreground is None else foreground
        run = Run(f"r-{next(self.ids)}", source, time.monotonic(), foreground=fg, book=self)
        self.live[run.run_id] = run
        self.started += 1
        if fg:
            self.current = run
        return run

    def get(self, run_id: str) -> Run | None:
        found = self.live.get(run_id)
        if found is not None:
            return found
        return next((r for r in self.recent_runs if r.run_id == run_id), None)

    def busy(self) -> Run | None:
        """The foreground run still going, if any."""
        run = self.current
        return run if run is not None and run.busy else None

    def recent(self, keep: int | None = None) -> list[Run]:
        """Newest first: the ones going on, then the finished ones."""
        going = sorted(self.live.values(), key=lambda r: r.t0, reverse=True)
        done = list(reversed(self.recent_runs))
        runs = going + done
        return runs[:keep] if keep is not None else runs

    def cancel(self, run_id: str, reason: str) -> bool:
        """Stop a run that is going on: True when it was (or already is being) stopped. A change call
        already in flight is shielded and finishes first (Thinker._call, Actor.act)."""
        run = self.live.get(run_id)
        if run is None or run.done or not run.foreground or (run.task is not None and run.task.done()):
            return False   # gone, a notification's (never stopped), or its work is over already
        if not run.cancel_reason:
            run.cancel_reason = reason
            log.info("run %s: cancel (%s)", run.run_id, reason)
        if run.task is not None and not run.task.done():
            run.task.cancel()
        return True

    def cancel_all(self, reason: str, but: Run | None = None) -> int:
        """Every foreground run going on (but `but`): shutdown, and "stop" with one waiting its turn."""
        return sum(self.cancel(run_id, reason) for run_id, run in list(self.live.items()) if run is not but)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(QUEUE)
        self.sinks.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.sinks.discard(queue)

    def emit(self, run: Run, kind: str, /, **fields: Any) -> dict[str, Any] | None:
        """One event of `run`, whitelisted, to every sink. None when it was not sent (a finished
        run, an unknown type, the cap)."""
        if run.done or kind not in FIELDS:
            return None
        terminal = kind in TERMINAL
        if run.seq >= MAX_EVENTS and not terminal:
            self.dropped += 1
            return None
        run.seq += 1
        message = {"type": kind, "run_id": run.run_id, "seq": run.seq, "t": round(time.monotonic(), 3)}
        message.update(clean(kind, fields))
        run.state = STATE_OF.get(kind, run.state)
        if kind == "speaking":
            run.spoke = True
        if kind == "tool.started" and "tool" in message:
            run.tools.append(message["tool"])
        run.events.append(message)
        if not self.events:
            return message
        for sink in list(self.sinks):
            try:
                sink.put_nowait(message)
            except asyncio.QueueFull:
                self.dropped += 1
        return message

    def finish(self, run: Run, error: str = "") -> dict[str, Any] | None:
        """The run's one terminal event; a second call does nothing."""
        if run.done:
            return None
        duration = time.monotonic() - run.t0
        if run.cancel_reason:
            kind, detail = "run.cancelled", run.cancel_reason
            message = self.emit(run, kind, duration=duration, reason=detail)
        elif error or run.error:
            kind, detail = "run.failed", error or run.error
            if detail not in CODES[("run.failed", "error")]:
                detail = "other"
            message = self.emit(run, kind, duration=duration, error=detail)
        else:
            kind, detail = "run.completed", "spoken" if run.spoke else "nothing"
            message = self.emit(run, kind, duration=duration, outcome=detail)
        run.outcome, run.detail = TERMINAL[kind], detail
        run.ended = time.monotonic()
        run.finished.set()
        self.live.pop(run.run_id, None)
        self.recent_runs.append(run)
        log.info("run %s (%s): %s %s in %.2fs; tools %s", run.run_id, run.source, run.outcome, detail, duration,
                 ", ".join(run.tools) or "none")
        return message

    def stats(self) -> dict[str, Any]:
        busy = self.busy()
        return {"started": self.started, "going": len(self.live), "current": busy.run_id if busy else None,
                "dropped": self.dropped}


def emit(run: Run | None, kind: str, /, **fields: Any) -> dict[str, Any] | None:
    """`run.book.emit`, and nothing for no run (a probe, a test without a book)."""
    if run is None or run.book is None:
        return None
    return run.book.emit(run, kind, **fields)


# A whole sentence that stops what she is doing: read like confirm.answer, by word lists.
STOP = ("stop", "stop it", "stop that", "cancel", "cancel that", "cancel it", "never mind", "nevermind",
        "forget it", "forget that", "abort", "halt", "stop stop")
STOP_FILLER = ("please", "strawberry", "oh", "just", "ok", "okay", "no", "wait", "actually", "hey", "thanks",
               "now", "um", "uh", "er")
_STOP_PHRASES = sorted({(p, True) for p in STOP} | {(p, False) for p in STOP_FILLER}, key=lambda i: -len(i[0].split()))


def is_stop(text: str) -> bool:
    """"stop", "cancel that", "never mind, Strawberry": the whole sentence asks her to stop."""
    words = re.sub(r"[^\w']+", " ", (text or "").lower().replace("’", "'")).split()
    found = False
    at = 0
    while at < len(words):
        for phrase, stops in _STOP_PHRASES:
            size = len(phrase.split())
            if words[at:at + size] == phrase.split():
                found = found or stops
                at += size
                break
        else:
            return False
    return found
