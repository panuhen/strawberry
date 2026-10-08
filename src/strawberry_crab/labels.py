"""The router's learning loop: outcomes become labelled examples (WIRING.md §8d).

The outcome logger (outcomes.py, §8c) keeps what came of each routed sentence. This module reads
those records and turns the ones that say something into *evidence*: for one sentence, a label
("this was the skip reflex") or an avoid ("this was not the skip reflex") on one or more of the
gate's questions, with a weight. The evidence goes into a local store, one example per distinct
sentence, and each example's labels are resolved from all of its evidence when the trainer asks.

    signal        when                                              what it labels          weight
    undo          the gate's reflex T, undone within undo_s         avoid music_tool T      1.0
    correction    the gate's reflex T, then "no, I meant…"          avoid music_tool T      1.0
    rephrase      the gate's reflex T, then the same said again     avoid music_tool T      0.6
    hint          after a correction or rephrase, the next          the reflex labels of    0.3
                  sentence's reflex R that nobody objected to       R for this sentence
    teacher       the thinker called R's tool alone, with no         the reflex labels of R  1.0
                  arguments, and nobody objected
    silence       the gate's reflex T and nothing said after it     the reflex labels of T  0.25
                  (not when the gate was already sure: >= 0.95)
                  "needs a music add-on" and nothing said           needs_catalogue yes     0.25
    repeat, moved_on, none                                          nothing

The reflex labels of R: kind request (question for now_playing), topic music, music_tool R,
has_argument no, needs_catalogue no. Only the gate's own reflexes count (the path was reflex on
the gate's act decision, with that tool, and it worked); a reflex read off the words (an
adapter's "I like this") or one that failed says nothing about the gate.

The guards. A label never comes from the router being sure, only from what the user did next or
from what System Two did. kind=other and private sentences are never examples: the outcome
logger does not keep them, and the store checks the patterns again (the trainer also asks
IS_SENSITIVE once more on the vector, learning.py). Nothing here logs a sentence.

Conflicts resolve the same way every time: per question, each option's positive weight minus its
avoid weight. The best positive wins when the runner-up has less than half of it; a closer
runner-up drops the question for that sentence ("conflict"). With no positive left, the most
avoided option is the avoid. A weight is capped at 1.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import paths, privacy
from .outcomes import REFLEX_TOOLS

log = logging.getLogger("strawberryd.learning")

VERSION = 1

STRONG = 1.0
MEDIUM = 0.6
HINT = 0.3
WEAK = 0.25
SURE = 0.95                 # a silence after a reflex the gate was this sure of teaches nothing
CONFLICT_RATIO = 0.5        # the runner-up at this share of the best positive or more: no label
MAX_WEIGHT = 1.0
MAX_EVIDENCE = 20           # per example, the newest
MAX_EXAMPLES = 3000         # in the store, the most recently seen
ACCEPTED = ("silence", "moved_on", "none", "repeat")   # outcomes that do not object to a sentence's handling

MUSIC_TOOLS = ("skip", "previous", "pause", "resume", "volume_down", "volume_up", "now_playing")
SIGNALS = ("teacher", "undo", "correction", "rephrase", "hint", "silence")


def store_file() -> Path:
    """The learned examples: sentences, so the user's alone (0600), in the data dir beside the heads."""
    return paths.data_dir() / "gate" / "learned.json"


def normalise_text(text: str) -> str:
    return " ".join(text.casefold().split()).strip(" .!?,;:")


def key_of(text: str) -> str:
    """One example per distinct sentence: case, spacing and end punctuation do not count."""
    return hashlib.sha256(normalise_text(text).encode("utf-8")).hexdigest()[:16]


def reflex_labels(tool: str) -> dict[str, str]:
    return {"kind": "question" if tool == "now_playing" else "request", "topic": "music", "music_tool": tool,
            "has_argument": "no", "needs_catalogue": "no"}


# ----------------------------------------------------------------------------- the extractor


@dataclass(frozen=True)
class Evidence:
    record: str                    # the outcome record's id: the same record never counts twice
    signal: str
    ts: float
    text: str
    labels: dict[str, str]
    avoid: dict[str, str]
    weight: float

    def to_json(self) -> dict[str, Any]:
        return {"id": self.record, "signal": self.signal, "ts": round(self.ts, 3), "labels": self.labels,
                "avoid": self.avoid, "w": self.weight}


def gate_reflex(record: dict[str, Any]) -> str:
    """The gate's own reflex the record fired and that worked, or "": the reflex path on the gate's
    act decision with its music tool, and the reflex named after that tool (not one read off the words)."""
    route = record.get("route") or {}
    tool = route.get("tool") or ""
    if record.get("path") != "reflex" or record.get("ok") is not True or tool not in MUSIC_TOOLS:
        return ""
    if route.get("decision") != "act" or route.get("topic") != "music":
        return ""
    done = str(record.get("reflex") or "").rsplit(".", 1)[-1]
    return tool if done == tool else ""


def teacher(record: dict[str, Any]) -> str:
    """The reflex the thinker's one bare call amounts to, when nothing objected to it afterwards."""
    taught = REFLEX_TOOLS.get(str(record.get("teacher") or ""), record.get("teacher") or "")
    if record.get("path") != "thinker" or taught not in MUSIC_TOOLS or record.get("outcome") not in ACCEPTED:
        return ""
    return taught


def sure(route: dict[str, Any]) -> bool:
    return (route.get("confidence") or 0.0) >= SURE and (route.get("tool_confidence") or 0.0) >= SURE


def usable(record: dict[str, Any], ignore_before: float = 0.0) -> bool:
    """A record that may become an example: the current format, a sentence, a reading that is not
    another voice, newer than the last `forget`, and no code or sign-in wording in it."""
    text, route = record.get("text"), record.get("route")
    if record.get("v") != VERSION or not isinstance(text, str) or not text.strip() or not isinstance(route, dict):
        return False
    if route.get("kind") in (None, "other") or not isinstance(record.get("id"), str):
        return False
    if (record.get("ts") or 0.0) <= ignore_before:
        return False
    return privacy.pattern(text) is None


def extract(records: Iterable[dict[str, Any]], ignore_before: float = 0.0) -> list[Evidence]:
    """Every piece of evidence in the outcome records (oldest first), by the table above."""
    records = list(records)
    by_id = {r["id"]: r for r in records if isinstance(r.get("id"), str)}
    out: list[Evidence] = []
    for r in records:
        if not usable(r, ignore_before):
            continue
        outcome, route = r.get("outcome"), r["route"]
        tool = gate_reflex(r)
        found: list[tuple[str, dict[str, str], dict[str, str], float]] = []
        if outcome == "undo" and tool:
            found.append(("undo", {}, {"music_tool": tool}, STRONG))
        elif outcome in ("correction", "rephrase"):
            if tool:
                found.append((outcome, {}, {"music_tool": tool}, STRONG if outcome == "correction" else MEDIUM))
            after = by_id.get(r.get("by") or "")
            if after is not None and (after.get("follows") or {}).get("id") == r["id"] and usable(after):
                meant = (gate_reflex(after) if after.get("outcome") in ACCEPTED else "") or teacher(after)
                if meant and meant != tool:
                    found.append(("hint", reflex_labels(meant), {}, HINT))
        elif outcome == "silence":
            if tool and not sure(route):
                found.append(("silence", reflex_labels(tool), {}, WEAK))
            elif r.get("path") == "no_catalogue" and (route.get("catalogue") or 0.0) < SURE:
                found.append(("silence", {"needs_catalogue": "yes"}, {}, WEAK))
        taught = teacher(r)
        if taught:
            found.append(("teacher", reflex_labels(taught), {}, STRONG))
        for signal, labels, avoid, weight in found:
            out.append(Evidence(r["id"], signal, float(r.get("ts") or 0.0), r["text"], labels, avoid, weight))
    return out


# ----------------------------------------------------------------------------- resolution


@dataclass
class Example:
    """One sentence and what its evidence says, resolved."""

    key: str
    text: str
    labels: dict[str, str] = field(default_factory=dict)
    avoid: dict[str, str] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)       # questions dropped for disagreeing evidence
    signals: dict[str, int] = field(default_factory=dict)
    first: float = 0.0
    last: float = 0.0
    review: str = "auto"                                      # auto | approved | rejected (the Brain UI)

    @property
    def usable(self) -> bool:
        return bool(self.labels or self.avoid) and self.review != "rejected"

    @property
    def digest(self) -> str:
        """What a head was trained with for this sentence: changes when its labels or weights do."""
        body = json.dumps([self.labels, self.avoid, {q: round(w, 3) for q, w in self.weights.items()}], sort_keys=True)
        return hashlib.sha256(body.encode()).hexdigest()[:12]

    def summary(self, text: bool = False) -> dict[str, Any]:
        """For status, the CLI and the UI; the sentence only when asked for (the user's own screen)."""
        out: dict[str, Any] = {"key": self.key, "labels": self.labels, "avoid": self.avoid,
                               "weights": {q: round(w, 3) for q, w in self.weights.items()},
                               "conflicts": self.conflicts, "signals": self.signals, "first": round(self.first, 3),
                               "last": round(self.last, 3), "review": self.review}
        if text:
            out["text"] = self.text
        return out


def resolve(key: str, entry: dict[str, Any], options: dict[str, list[str]] | None = None) -> Example:
    """An example from its stored evidence, by the rule in the module's docstring. `options` orders
    ties (the question's own option order); without it, alphabetically."""
    evidence = entry.get("evidence") or []
    pos: dict[str, dict[str, float]] = {}
    neg: dict[str, dict[str, float]] = {}
    signals: dict[str, int] = {}
    for e in evidence:
        w = float(e.get("w") or 0.0)
        signals[e.get("signal", "?")] = signals.get(e.get("signal", "?"), 0) + 1
        for q, o in (e.get("labels") or {}).items():
            pos.setdefault(q, {})[o] = pos.get(q, {}).get(o, 0.0) + w
        for q, o in (e.get("avoid") or {}).items():
            neg.setdefault(q, {})[o] = neg.get(q, {}).get(o, 0.0) + w
    example = Example(key, entry.get("text", ""), signals=signals, first=float(entry.get("first") or 0.0),
                      last=float(entry.get("last") or 0.0), review=entry.get("review", "auto"))
    for q in sorted(set(pos) | set(neg)):
        order = (options or {}).get(q)

        def rank(o: str) -> tuple:
            return (order.index(o) if order and o in order else len(order or ()), o)

        net = {o: pos.get(q, {}).get(o, 0.0) - neg.get(q, {}).get(o, 0.0) for o in set(pos.get(q, {})) | set(neg.get(q, {}))}
        ahead = sorted((o for o in net if net[o] > 1e-9), key=lambda o: (-net[o], rank(o)))
        if ahead:
            best = ahead[0]
            if len(ahead) > 1 and net[ahead[1]] >= CONFLICT_RATIO * net[best]:
                example.conflicts.append(q)
                continue
            example.labels[q] = best
            example.weights[q] = min(net[best], MAX_WEIGHT)
            continue
        behind = sorted((o for o in net if net[o] < -1e-9), key=lambda o: (net[o], rank(o)))
        if behind:
            example.avoid[q] = behind[0]
            example.weights[q] = min(-net[behind[0]], MAX_WEIGHT)
    return example


# ----------------------------------------------------------------------------- the store


def write_private(path: Path, text: str) -> None:
    """Write `text` to `path` atomically, readable by the user alone."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(text)
    os.replace(tmp, path)
    if os.name == "posix":
        os.chmod(path, 0o600)


def _alive(path: Path) -> bool:
    """Whether the process that wrote the lock still runs (POSIX; elsewhere only its age counts)."""
    if os.name != "posix":
        return True
    try:
        pid = int(path.read_text().strip() or "0")
    except (OSError, ValueError):
        return True                      # being written right now
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextlib.contextmanager
def file_lock(path: Path, timeout_s: float = 10.0, stale_s: float = 120.0) -> Iterator[None]:
    """A short lock between the daemon, the CLI and the tray around a read-modify-write of the
    learning files: an exclusive file, taken over when older than `stale_s` (a crashed holder)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > stale_s or not _alive(path):
                    path.unlink(missing_ok=True)
                    continue
            except OSError:
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"{path} is held by another process") from None
            time.sleep(0.05)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


class ExampleStore:
    """`<data>/gate/learned.json`: every example with its evidence, and `gone`, the keys taken out
    (rejected in review, or found private by the trainer) with no sentence, so the same sentence is
    never taken in again. Rewritten whole, atomically, 0600."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or store_file()

    @property
    def lock(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"v": VERSION, "examples": {}, "gone": {}}
        except (OSError, ValueError) as exc:
            log.warning("learning: the example store could not be read (%s); starting from none", type(exc).__name__)
            return {"v": VERSION, "examples": {}, "gone": {}}
        if not isinstance(data, dict) or data.get("v") != VERSION:
            return {"v": VERSION, "examples": {}, "gone": {}}
        data.setdefault("examples", {})
        data.setdefault("gone", {})
        return data

    def save(self, data: dict[str, Any]) -> None:
        write_private(self.path, json.dumps(data, ensure_ascii=False, separators=(",", ":")))

    def merge(self, evidence: Iterable[Evidence]) -> dict[str, int]:
        """Add new evidence (a record already in the store is skipped). Returns counts: new examples,
        new evidence, and evidence for a sentence taken out before (dropped)."""
        counts = {"examples": 0, "evidence": 0, "gone": 0}
        with file_lock(self.lock):
            data = self.load()
            examples, gone = data["examples"], data["gone"]
            changed = False
            for e in evidence:
                key = key_of(e.text)
                if key in gone:
                    counts["gone"] += 1
                    continue
                entry = examples.get(key)
                if entry is None:
                    entry = examples[key] = {"text": e.text, "first": e.ts, "last": e.ts, "evidence": [], "review": "auto"}
                    counts["examples"] += 1
                if any(x.get("id") == e.record and x.get("signal") == e.signal for x in entry["evidence"]):
                    continue
                entry["evidence"].append(e.to_json())
                entry["evidence"] = sorted(entry["evidence"], key=lambda x: x.get("ts", 0.0))[-MAX_EVIDENCE:]
                entry["first"] = min(entry.get("first", e.ts), e.ts)
                entry["last"] = max(entry.get("last", e.ts), e.ts)
                counts["evidence"] += 1
                changed = True
            if len(examples) > MAX_EXAMPLES:
                for key in sorted(examples, key=lambda k: examples[k].get("last", 0.0))[: len(examples) - MAX_EXAMPLES]:
                    del examples[key]
                changed = True
            if changed:
                self.save(data)
        return counts

    def examples(self, options: dict[str, list[str]] | None = None) -> list[Example]:
        """Every example, resolved, oldest first."""
        data = self.load()
        out = [resolve(k, v, options) for k, v in data["examples"].items()]
        return sorted(out, key=lambda e: (e.first, e.key))

    def gone(self) -> dict[str, dict[str, Any]]:
        return dict(self.load()["gone"])

    def drop(self, keys: Iterable[str], why: str) -> int:
        """Take examples out for good: the sentence is deleted, the key kept so it never comes back."""
        keys = set(keys)
        if not keys:
            return 0
        with file_lock(self.lock):
            data = self.load()
            dropped = 0
            for key in keys:
                entry = data["examples"].pop(key, None)
                data["gone"][key] = {"why": why, "at": round(time.time(), 3),
                                     "first": entry.get("first") if entry else None}
                dropped += entry is not None
            self.save(data)
        return dropped

    def review(self, key: str, verdict: str) -> bool:
        """approve: the example stays whatever the trainer's filters later say about its labels;
        reject: it is taken out like a private one (the sentence deleted, the key kept)."""
        if verdict not in ("approve", "reject"):
            raise ValueError("verdict must be approve or reject")
        if verdict == "reject":
            return self.drop([key], "rejected") > 0
        with file_lock(self.lock):
            data = self.load()
            entry = data["examples"].get(key)
            if entry is None:
                return False
            entry["review"] = "approved"
            self.save(data)
        return True

    def clear(self) -> int:
        """Delete the store (examples and keys). Returns how many examples there were."""
        with file_lock(self.lock):
            count = len(self.load()["examples"])
            self.path.unlink(missing_ok=True)
        return count

    def stats(self) -> dict[str, Any]:
        """Counts only: for /health and the status."""
        data = self.load()
        examples = [resolve(k, v) for k, v in data["examples"].items()]
        usable = [e for e in examples if e.usable]
        by_signal: dict[str, int] = {}
        for e in usable:
            for s, n in e.signals.items():
                by_signal[s] = by_signal.get(s, 0) + n
        gone: dict[str, int] = {}
        for g in data["gone"].values():
            gone[g.get("why", "?")] = gone.get(g.get("why", "?"), 0) + 1
        return {"examples": len(examples), "usable": len(usable),
                "conflicts": sum(1 for e in examples if e.conflicts), "signals": by_signal, "gone": gone}
