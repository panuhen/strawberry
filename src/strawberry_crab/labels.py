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
import uuid
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
ACCEPTED = ("silence", "moved_on", "none", "repeat")
MAX_TEXT = 500              # longer than any spoken command: such a line is not learned from   # outcomes that do not object to a sentence's handling

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


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _num(value: Any) -> float:
    """A number from a record, or 0 for anything else (a hand-edited or damaged line)."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _names(value: Any) -> dict[str, str]:
    """A question -> option map from the store, or {} when it is anything else."""
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}


def gate_reflex(record: dict[str, Any]) -> str:
    """The gate's own reflex the record fired and that worked, or "": the reflex path on the gate's
    act decision with its music tool, and the reflex named after that tool (not one read off the words)."""
    route = _dict(record.get("route"))
    tool = route.get("tool") if isinstance(route.get("tool"), str) else ""
    if record.get("path") != "reflex" or record.get("ok") is not True or tool not in MUSIC_TOOLS:
        return ""
    if route.get("decision") != "act" or route.get("topic") != "music":
        return ""
    done = str(record.get("reflex") or "").rsplit(".", 1)[-1]
    return tool if done == tool else ""


def teacher(record: dict[str, Any]) -> str:
    """The reflex the thinker's one bare call amounts to, when nothing objected to it afterwards."""
    raw = record.get("teacher")
    raw = raw if isinstance(raw, str) else ""
    taught = REFLEX_TOOLS.get(raw, raw)
    if record.get("path") != "thinker" or taught not in MUSIC_TOOLS or record.get("outcome") not in ACCEPTED:
        return ""
    return taught


def sure(route: dict[str, Any]) -> bool:
    return _num(route.get("confidence")) >= SURE and _num(route.get("tool_confidence")) >= SURE


def usable(record: dict[str, Any], ignore_before: float = 0.0) -> bool:
    """A record that may become an example: the current format, a sentence, a reading that is not
    another voice, newer than the last `forget`, and no code or sign-in wording in it. Anything of
    another shape (a damaged or hand-edited line) is not."""
    if not isinstance(record, dict):
        return False
    text, route = record.get("text"), record.get("route")
    if record.get("v") != VERSION or not isinstance(text, str) or not text.strip() or not isinstance(route, dict):
        return False
    if len(text) > MAX_TEXT or not isinstance(route.get("kind"), str) or route.get("kind") == "other":
        return False
    if not isinstance(record.get("id"), str) or _num(record.get("ts")) <= ignore_before:
        return False
    return privacy.pattern(text) is None


def extract(records: Iterable[dict[str, Any]], ignore_before: float = 0.0) -> list[Evidence]:
    """Every piece of evidence in the outcome records (oldest first), by the table above. A record of
    another shape is skipped, never the end of the run."""
    records = [r for r in records if isinstance(r, dict)]
    by_id = {r["id"]: r for r in records if isinstance(r.get("id"), str)}
    out: list[Evidence] = []
    for r in records:
        try:
            out += _evidence(r, by_id, ignore_before)
        except Exception as exc:  # noqa: BLE001 - one bad line must not stop the others
            log.debug("learning: an outcome record skipped (%s)", type(exc).__name__)
    return out


def _evidence(r: dict[str, Any], by_id: dict[str, dict[str, Any]], ignore_before: float) -> list[Evidence]:
    out: list[Evidence] = []
    if not usable(r, ignore_before):
        return out
    outcome, route = r.get("outcome"), r["route"]
    tool = gate_reflex(r)
    found: list[tuple[str, dict[str, str], dict[str, str], float]] = []
    if outcome == "undo" and tool:
        found.append(("undo", {}, {"music_tool": tool}, STRONG))
    elif outcome in ("correction", "rephrase"):
        if tool:
            found.append((outcome, {}, {"music_tool": tool}, STRONG if outcome == "correction" else MEDIUM))
        by = r.get("by")
        after = by_id.get(by) if isinstance(by, str) else None
        if after is not None and _dict(after.get("follows")).get("id") == r["id"] and usable(after):
            meant = (gate_reflex(after) if after.get("outcome") in ACCEPTED else "") or teacher(after)
            if meant and meant != tool:
                found.append(("hint", reflex_labels(meant), {}, HINT))
    elif outcome == "silence":
        if tool and not sure(route):
            found.append(("silence", reflex_labels(tool), {}, WEAK))
        elif r.get("path") == "no_catalogue" and _num(route.get("catalogue")) < SURE:
            found.append(("silence", {"needs_catalogue": "yes"}, {}, WEAK))
    taught = teacher(r)
    if taught:
        found.append(("teacher", reflex_labels(taught), {}, STRONG))
    for signal, labels, avoid, weight in found:
        out.append(Evidence(r["id"], signal, _num(r.get("ts")), r["text"], labels, avoid, weight))
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
    evidence = entry.get("evidence") if isinstance(entry.get("evidence"), list) else []
    pos: dict[str, dict[str, float]] = {}
    neg: dict[str, dict[str, float]] = {}
    signals: dict[str, int] = {}
    for e in evidence:
        if not isinstance(e, dict):
            continue                     # a damaged entry counts for nothing
        w = max(_num(e.get("w")), 0.0)
        signal = e.get("signal") if isinstance(e.get("signal"), str) else "?"
        signals[signal] = signals.get(signal, 0) + 1
        for q, o in _names(e.get("labels")).items():
            pos.setdefault(q, {})[o] = pos.get(q, {}).get(o, 0.0) + w
        for q, o in _names(e.get("avoid")).items():
            neg.setdefault(q, {})[o] = neg.get(q, {}).get(o, 0.0) + w
    text = entry.get("text") if isinstance(entry.get("text"), str) else ""
    review = entry.get("review") if entry.get("review") in ("auto", "approved", "rejected") else "auto"
    example = Example(key, text, signals=signals, first=_num(entry.get("first")), last=_num(entry.get("last")),
                      review=review)
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
    """Write `text` to `path` atomically, readable by the user alone, in a directory only the user
    can read."""
    paths.private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    with paths.open_private(tmp, "w", encoding="utf-8") as out:
        out.write(text)
    os.replace(tmp, path)
    if os.name == "posix":
        os.chmod(path, 0o600)


WRITE_GRACE_S = 5.0          # a lock file still empty this long after it was made: its maker died first


def _holder(text: str) -> int:
    """The pid in a lock's token ("<pid>:<random>"), or 0."""
    try:
        return int(text.split(":", 1)[0])
    except ValueError:
        return 0


def _stale(path: Path, seen: str, stale_s: float) -> bool:
    """Whether the lock holding `seen` was left behind. On POSIX by its holder: a process that is
    gone (a live holder keeps its lock however long it runs). Elsewhere by its age."""
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    pid = _holder(seen)
    if pid <= 0:
        return age > WRITE_GRACE_S
    if os.name != "posix":
        return age > stale_s
    try:
        os.kill(pid, 0)                  # signal 0: asks whether it exists, sends nothing
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _take_over(path: Path, seen: str) -> None:
    """Remove a stale lock, and only that one: it is renamed aside first (one process wins the
    rename), and put back if what was renamed is not the lock that was judged stale (another process
    replaced it meanwhile)."""
    aside = path.with_name(f"{path.name}.{uuid.uuid4().hex}.stale")
    try:
        os.rename(path, aside)
    except OSError:
        return
    try:
        if aside.read_text() != seen:
            try:
                os.link(aside, path)     # back where it was, unless a new one is already there
            except OSError:
                pass
    except OSError:
        pass
    aside.unlink(missing_ok=True)


@contextlib.contextmanager
def file_lock(path: Path, timeout_s: float = 10.0, stale_s: float = 120.0) -> Iterator[None]:
    """A lock between the daemon, the CLI and the tray: an exclusive file holding this process's
    token. A lock whose holder is gone is taken over (_stale); on the way out the file is removed
    only while it still holds this token."""
    paths.private_dir(path.parent)
    token = f"{os.getpid()}:{uuid.uuid4().hex}"
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, token.encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                seen = path.read_text()
            except OSError:
                continue                 # gone meanwhile: try again
            if _stale(path, seen, stale_s):
                _take_over(path, seen)
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"{path} is held by another process") from None
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            if path.read_text() == token:
                path.unlink()
        except OSError:
            pass


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
        return {"v": VERSION, "examples": self._clean(data.get("examples")),
                "gone": {k: v for k, v in _dict(data.get("gone")).items() if isinstance(v, dict)}}

    @staticmethod
    def _clean(examples: Any) -> dict[str, Any]:
        """The well-formed entries only: a damaged or hand-edited one is left out (and gone at the next
        write), never the end of learning."""
        out = {}
        for key, entry in _dict(examples).items():
            if not isinstance(entry, dict) or not isinstance(entry.get("text"), str) or len(entry["text"]) > MAX_TEXT:
                continue
            evidence = [e for e in entry.get("evidence") or [] if isinstance(e, dict)] \
                if isinstance(entry.get("evidence"), list) else []
            out[key] = entry | {"evidence": evidence, "first": _num(entry.get("first")), "last": _num(entry.get("last"))}
        return out

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
                entry["evidence"] = sorted(entry["evidence"], key=lambda x: _num(x.get("ts")))[-MAX_EVIDENCE:]
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
        """approve: marks the example as looked at (the filters still apply: privacy, the held-out
        set); reject: it is taken out like a private one (the sentence deleted, the key kept)."""
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
