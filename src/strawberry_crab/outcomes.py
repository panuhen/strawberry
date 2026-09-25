"""The router's learning loop, first half: what came of each routed sentence (WIRING.md §8c).

The gate (systemone.py) routes every sentence the user says or types: a reflex, or the thinker.
A later step trains the router on *outcomes*, so this one only collects them, one JSON line per
sentence in `<state>/outcomes.jsonl` (paths.outcomes_file), and only with `[learning]
log_outcomes = true`. What a record says about the sentence before it is decided by the next
one, or by nothing coming at all:

    correction   "no, I meant the next song" within rephrase_s    strong: the route was wrong
    undo         the opposite reflex within undo_s (skip, then     strong negative for that reflex
                 previous; pause, then resume; up, then down)
    rephrase     a sentence whose embedding is close (cosine >=     medium: it did not land
                 rephrase_similarity) within rephrase_s
    repeat       the same reflex again (skip, skip)                 neutral: the user wanted two
    silence      nothing for silence_s                              weak positive
    moved_on     another, unrelated sentence before silence_s       none
    none         the daemon stopped first                           none

And for a sentence the thinker answered, `teacher`: the thinker called exactly one tool, with no
arguments, that a reflex covers (a bare pause) -- the sentence should have been a reflex.

The guards. Only outcomes are stored, never "the router was sure": the route is kept as the
gate read it, and the label comes from what the user did next. A sentence the gate reads as
kind=other (the TV, someone else in the room) is ignored altogether: not stored, and it does not
break a silence. A sentence that reads as private (privacy.pattern, or IS_SENSITIVE on the gate's
vector, >= 0.5; missing counts as private) is not stored, though it still ends the one before.
Tool results and her replies are never stored, only the tool names and whether arguments were
given. The journal gets counts and labels, never a sentence (tests/test_outcomes.py has the
canary). The file is the user's, mode 0600, pruned to max_days and max_records.

Corrections are a small phrase check on purpose (CORRECTION): a false one mislabels a good route,
and the trained router will learn the rest from the rephrases.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import paths, privacy
from .config import LearningConfig
from .systemone import IS_SENSITIVE, Route

log = logging.getLogger("strawberryd.outcomes")

VERSION = 1

# The reflexes that undo each other (the gate's music tools).
OPPOSITE = {"skip": "previous", "previous": "skip", "pause": "resume", "resume": "pause",
            "volume_up": "volume_down", "volume_down": "volume_up"}
UNDO_TOOL_CONFIDENCE = 0.5   # how sure the gate must be of the second sentence's tool to call it an undo

# A server tool that does what a reflex does, when called with no arguments: the thinker using one
# of these alone is the "System Two as teacher" signal. By name, across servers; set_volume and
# friends take an argument and never count.
REFLEX_TOOLS = {
    "next": "skip", "skip": "skip", "next_track": "skip", "skip_next": "skip", "skip_to_next": "skip",
    "previous": "previous", "previous_track": "previous", "skip_previous": "previous", "skip_to_previous": "previous",
    "pause": "pause", "pause_playback": "pause",
    "resume": "resume", "play": "resume", "start_playback": "resume", "resume_playback": "resume",
    "get_current_track": "now_playing", "now_playing": "now_playing", "get_currently_playing": "now_playing",
}

# A correction of the sentence before, said soon after it. Conservative: at the start, "no" or
# "nope" followed by a comma or a stop, or by "I"/"not"/"the other"; "not that", "wrong", "I meant",
# "I said"; anywhere, "I meant", "not that one", "that's not what I"; Finnish "ei kun", "tarkoitin",
# "en tarkoittanut", "väärä". "no more music" and "no worries" are not corrections.
CORRECTION = re.compile(
    r"^\s*(?:no+|nope|nah|ei)\s*[,.!;:-]"
    r"|^\s*(?:no|nope)\s+(?:i\b|not\b|the\s+other\b)"
    r"|^\s*(?:not\s+that|wrong|i\s+said)\b"
    r"|\bi\s+meant\b|\bnot\s+that\s+one\b|\bthat'?s\s+not\s+what\s+i\b|\bthat'?s\s+wrong\b"
    r"|\bei\s+kun\b|\btarkoitin\b|\ben\s+tarkoittanut\b|\bväärä\b",
    re.IGNORECASE,
)


def is_correction(text: str) -> bool:
    return bool(CORRECTION.search(text))


def cosine(a: Iterable[float], b: Iterable[float]) -> float | None:
    """Cosine of two vectors; None when either is missing (a scripted route has none)."""
    a, b = list(a), list(b)
    if not a or not b or len(a) != len(b):
        return None
    norm = math.sqrt(math.fsum(x * x for x in a) * math.fsum(y * y for y in b))
    return math.fsum(x * y for x, y in zip(a, b)) / norm if norm else None


def private(text: str, route: Route) -> bool:
    """Never stored: a code or sign-in wording, or IS_SENSITIVE >= 0.5 on the gate's own vector.
    Fail closed: a route without that answer counts as private."""
    if privacy.pattern(text):
        return True
    answer = route.answers.get(IS_SENSITIVE.name)
    return answer is None or (answer.score or 0.0) >= privacy.SENSITIVE_P


def reading(route: Route) -> dict[str, Any]:
    """The whole route as the gate read it: every question's answer and confidence, and the
    decision. Not the vector (the trainer embeds the text again) nor IS_SENSITIVE."""
    answers = {}
    for name, answer in route.answers.items():
        if name == IS_SENSITIVE.name:
            continue
        entry: dict[str, Any] = {"confidence": round(answer.confidence, 4),
                                 "probabilities": {k: round(v, 4) for k, v in answer.probabilities.items()}}
        if answer.choice is not None:
            entry["choice"] = answer.choice
        if answer.score is not None:
            entry["score"] = round(answer.score, 4)
        answers[name] = entry
    return {
        "kind": route.kind, "topic": route.topic, "confidence": round(route.confidence, 4),
        "decision": route.decision, "tool": route.tool, "tool_confidence": round(route.tool_confidence, 4),
        "has_argument": round(route.has_argument, 4), "library_change": round(route.library_change, 4),
        "catalogue": round(route.catalogue, 4), "is_urgent": round(route.is_urgent, 4),
        "is_about_her": round(route.is_about_her, 4), "answers": answers,
    }


@dataclass
class Record:
    """One routed sentence, until its outcome is known."""

    id: str
    ts: float                  # wall clock, for the file and retention
    at: float                  # monotonic, for the windows
    source: str                # voice | typed
    text: str
    route: Route
    path: str = ""             # reflex | thinker | no_catalogue | chat
    reflex: str = ""           # "mpris.skip": who did it, and the gate's tool
    ok: bool | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    teacher: str = ""          # the reflex the thinker's one bare tool call amounts to
    follows: dict[str, Any] | None = None   # {"id", "as"}: this sentence corrects, undoes, rephrases the one before

    def to_json(self, outcome: str, after_s: float | None, by: str | None) -> dict[str, Any]:
        return {
            "v": VERSION, "id": self.id, "ts": round(self.ts, 3),
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.ts)),
            "source": self.source, "text": self.text, "route": reading(self.route),
            "path": self.path or None, "reflex": self.reflex or None, "ok": self.ok,
            "calls": self.calls, "teacher": self.teacher or None, "follows": self.follows,
            "outcome": outcome, "after_s": None if after_s is None else round(after_s, 2), "by": by,
        }


class OutcomeLog:
    """Collects the records and their outcomes, and keeps the file (WIRING.md §8c).

    The daemon calls `heard` after the gate reads a sentence (it settles the sentence before and
    returns the new record, or None when this one is not kept), then `acted` once the sentence has
    been handled. A record is written when its outcome is known: the next sentence, or silence_s.
    """

    PRUNE_EVERY = 200     # appends between prunes (and one at start)

    def __init__(self, config: LearningConfig, path: Path | None = None) -> None:
        self.config = config
        self.path = path or paths.outcomes_file()
        self.pending: Record | None = None
        self.timer: asyncio.TimerHandle | None = None
        self.written = 0
        self.skipped = {"other": 0, "private": 0, "unrouted": 0}
        self.signals: dict[str, int] = {}
        self.errors = 0
        self.since_prune = 0
        self._count: tuple[tuple[int, int], int] | None = None   # (mtime_ns, size) -> lines

    @property
    def enabled(self) -> bool:
        return self.config.log_outcomes

    def start(self) -> None:
        if not self.enabled:
            return
        try:
            self.prune()
        except OSError as exc:
            self.errors += 1
            log.warning("outcomes: could not prune %s (%s)", self.path, exc)
        c = self.config
        log.info("outcomes: keeping routed sentences in %s (%d records; %d days, %d records at most)",
                 self.path, self.count(), c.max_days, c.max_records)

    def close(self) -> None:
        """Write the one still waiting, as `none`: nothing came of it before the stop."""
        if self.pending is not None:
            self._settle("none", None)

    # --- the funnel ------------------------------------------------------------

    def heard(self, text: str, route: Route | None, source: str) -> Record | None:
        if not self.enabled:
            return None
        if route is None:
            self.skipped["unrouted"] += 1   # the gate is off or failed: there is no reading to learn from
            return None
        if route.kind == "other":
            self.skipped["other"] += 1      # not the user talking to her; the silence before goes on
            return None
        now = time.monotonic()
        relation = self._relate(text, route, now)
        new_id = uuid.uuid4().hex[:12]
        before = self.pending
        if before is not None:
            self._settle(relation or ("silence" if now - before.at >= self.config.silence_s else "moved_on"),
                         new_id)
        if private(text, route):
            self.skipped["private"] += 1
            log.info("outcomes: a sentence read as private; not kept")
            return None
        record = Record(new_id, time.time(), now, source, text, route)
        if relation and before is not None:
            record.follows = {"id": before.id, "as": relation}
        self.pending = record
        self._arm()
        return record

    def acted(self, record: Record | None, path: str, ok: bool, reflex: str = "", calls: Iterable[Any] = ()) -> None:
        """How the sentence was handled. `calls` are the thinker's ToolResults: names and whether
        arguments were given, never the arguments or the results."""
        if record is None:
            return
        record.path, record.ok, record.reflex = path, ok, reflex
        record.calls = [{"server": c.server, "name": c.name, "arguments": bool(c.arguments), "ok": c.ok} for c in calls]
        if path == "thinker" and len(record.calls) == 1:
            only = record.calls[0]
            if only["ok"] and not only["arguments"] and only["name"] in REFLEX_TOOLS:
                record.teacher = REFLEX_TOOLS[only["name"]]

    def _relate(self, text: str, route: Route, now: float) -> str | None:
        """What the new sentence says about the one waiting, if anything."""
        before = self.pending
        if before is None:
            return None
        gap = now - before.at
        c = self.config
        if gap <= c.rephrase_s and is_correction(text):
            return "correction"
        tool = route.tool if route.tool_confidence >= UNDO_TOOL_CONFIDENCE else ""
        done = before.route.tool if before.path == "reflex" else ""
        if done and gap <= c.undo_s and tool and OPPOSITE.get(done) == tool:
            return "undo"
        if done and tool == done and gap <= c.rephrase_s:
            return "repeat"
        similarity = cosine(before.route.vector, route.vector)
        if gap <= c.rephrase_s and similarity is not None and similarity >= c.rephrase_similarity:
            return "rephrase"
        return None

    def _arm(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.timer = None
            return
        record = self.pending
        self.timer = loop.call_later(self.config.silence_s, self._silence, record)

    def _silence(self, record: Record | None) -> None:
        if record is not None and self.pending is record:
            self._settle("silence", None)

    def _settle(self, outcome: str, by: str | None) -> None:
        record, self.pending = self.pending, None
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        if record is None:
            return
        after = time.monotonic() - record.at if by is not None or outcome == "silence" else None
        self.signals[outcome] = self.signals.get(outcome, 0) + 1
        try:
            self._append(record.to_json(outcome, after, by))
        except OSError as exc:
            self.errors += 1
            log.warning("outcomes: could not write %s (%s)", self.path, exc)
            return
        self.written += 1
        log.info("outcomes: %s %s -> %s%s", record.source, record.path or "-", outcome,
                 f", teacher {record.teacher}" if record.teacher else "")

    # --- the file --------------------------------------------------------------

    def _append(self, entry: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        self.since_prune += 1
        if self.since_prune >= self.PRUNE_EVERY:
            self.prune()

    def prune(self) -> int:
        """Keep the records younger than max_days, at most max_records of them (the newest), in a
        file only the user can read. Returns how many were dropped."""
        self.since_prune = 0
        if not self.path.exists():
            return 0
        records = read(self.path)
        cutoff = time.time() - self.config.max_days * 86400
        kept = [r for r in records if r.get("ts", 0) >= cutoff][-self.config.max_records:]
        dropped = len(records) - len(kept)
        if dropped:
            temp = self.path.with_suffix(".jsonl.tmp")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                for record in kept:
                    out.write(json.dumps(record, ensure_ascii=False) + "\n")
            os.replace(temp, self.path)
            log.info("outcomes: pruned %d record(s), %d kept", dropped, len(kept))
        if os.name == "posix":
            os.chmod(self.path, 0o600)   # a file made by hand, or before this rule
        return dropped

    def count(self) -> int:
        """Records in the file, recounted only when it changed (the tray polls /health every 2 s)."""
        try:
            stat = self.path.stat()
        except OSError:
            return 0
        key = (stat.st_mtime_ns, stat.st_size)
        if self._count is None or self._count[0] != key:
            with open(self.path, "rb") as f:
                self._count = (key, sum(1 for line in f if line.strip()))
        return self._count[1]

    def stats(self) -> dict[str, Any]:
        """For /health: on or off, and numbers. Never a sentence."""
        if not self.enabled:
            return {"enabled": False, "records": self.count()}
        return {"enabled": True, "records": self.count(), "written": self.written, "waiting": self.pending is not None,
                "signals": dict(self.signals), "skipped": dict(self.skipped), "errors": self.errors,
                "path": str(self.path)}


def read(path: Path) -> list[dict[str, Any]]:
    """Every record in the file, oldest first; a line that does not parse is skipped."""
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                records.append(entry)
    return records
