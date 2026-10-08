"""The router's learning loop, second half (WIRING.md §8d): labels from outcomes, a candidate head
trained on them, the held-out gate, the switch and the rollback. This module is the API the CLI
(learncmd.py), the daemon, the tray and the Brain UI call; nothing here prints.

    Learning(config)
      .extract()                  outcomes.jsonl -> evidence -> the example store (labels.py)
      .train(gate, ...)           embed with the gate's own embedder, fit a candidate, score it and the
                                  head in use on the held-out set (learnfit.py). A candidate that is
                                  not at least as good is rejected; one that is waits for `accept`,
                                  or is switched in at once with [learning] auto_switch.
      .accept() .reject()         the candidate waiting
      .rollback() .use(version)   move the heads dir's `current`; every head is kept
      .status() .versions() .report() .examples() .health()     read-only; sentences only in
                                  examples(text=True), for the user's own screen
      .review(key, verdict) .forget()
    IdleTrainer(daemon)           in the daemon: trains when nobody has spoken for a while and enough
                                  new labels wait, follows `current` without a restart, and says the
                                  weekly line when it is on

The files, all in `<data>/gate/` and the user's alone (0600):

    learned.json                  the examples and their evidence (sentences; labels.py)
    learning.json                 the candidate, the switch's history and stack, events, what the last
                                  run trained with (store keys, never sentences)
    heads/head-<version>.npz      every head (gatehead.py), with `current` naming the one in use
    heads/head-<version>.json     its manifest: what it was trained with (store keys and counts), the
                                  data set's hash, the held-out scores of it and of the head it was
                                  compared with, its parent and what became of it

The held-out set (data/gate_heldout.json) is package data and only ever read; a learned sentence
close to one of its sentences is left out of training (learnfit.py), so the gate stays honest.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterator

from . import gatehead, labels, paths
from .config import Config
from .labels import Example, ExampleStore, file_lock, write_private

log = logging.getLogger("strawberryd.learning")

VERSION = 1
CHUNK = 16                  # sentences per embedding call in the daemon: the user speaking stops the run between two
MAX_EVENTS = 1000
WEEK_S = 7 * 86400
RUN_GAP_S = 15 * 60         # at most one idle run in this long, whatever became of the last
TICK_S = 5.0                # the daemon's look at `current`, the outcome file and the clock
WEEKLY_IDLE_S = 120.0       # the weekly line waits for this much quiet
TRAIN_LOCK_STALE_S = 3600.0   # where a lock's holder cannot be asked (Windows): its age
CHILD_TIMEOUT_S = 30 * 60.0  # the child's fit and scoring; a first run is a minute or two

# What she is getting better at, by the reflex most of the week's labels were about.
SKILLS = {"skip": "skipping songs", "previous": "going back a song", "pause": "pausing the music",
          "resume": "getting the music going again", "volume_down": "turning it down",
          "volume_up": "turning it up", "now_playing": "saying what's playing"}
NUMBERS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve")


class LearningError(RuntimeError):
    pass


class LearningBusy(LearningError):
    """Another training run holds the lock (the daemon's, or another terminal's)."""


def questions() -> dict[str, list[str]]:
    from .gatecmd import _questions

    return _questions()


def phrases_file() -> Path | None:
    """scripts/gate_phrases.json in a source checkout: the secondary check, reported and never gated
    on (step 4 tuned the data against it, so it is no longer independent)."""
    root = paths.checkout_root()
    path = root / "scripts" / "gate_phrases.json" if root else None
    return path if path is not None and path.is_file() else None


def version_of(path: Path | None) -> str:
    return "shipped" if path is None else gatehead.version_of_path(path)


VERSION_NAME = re.compile(r"[A-Za-z0-9._-]+")


def check_version(version: str) -> str:
    """A head's version as a file name may carry it: letters, digits, '.', '_' and '-', no '..'."""
    if not isinstance(version, str) or not VERSION_NAME.fullmatch(version) or ".." in version:
        raise LearningError(f"not a head version: {version!r}"[:80])
    return version


def pct(pair: list[int] | None) -> float | None:
    return round(100.0 * pair[0] / pair[1], 1) if pair and pair[1] else None


def number(n: int) -> str:
    return NUMBERS[n] if 0 <= n < len(NUMBERS) else str(n)


class Learning:
    def __init__(self, config: Config | None = None, root: Path | None = None, outcomes: Path | None = None) -> None:
        self.config = config or Config()
        self.root = root or paths.data_dir() / "gate"
        self.heads = gatehead.Heads(self.root / "heads")
        self.store = ExampleStore(self.root / "learned.json")
        self.state_path = self.root / "learning.json"
        self.outcomes_path = outcomes or paths.outcomes_file()

    # ------------------------------------------------------------------ the files

    def manifest_path(self, version: str) -> Path:
        return self.heads.directory / f"head-{check_version(version)}.json"

    def manifest(self, version: str | None) -> dict[str, Any] | None:
        if not version or version == "shipped":
            return None
        try:
            data = json.loads(self.manifest_path(version).read_text(encoding="utf-8"))
        except (OSError, ValueError, LearningError):
            return None
        return data if isinstance(data, dict) else None

    def _write_manifest(self, version: str, data: dict[str, Any]) -> None:
        write_private(self.manifest_path(version), json.dumps(data, ensure_ascii=False, indent=1) + "\n")

    def manifests(self) -> list[dict[str, Any]]:
        """Every manifest, oldest first."""
        out = []
        for path in self.heads.versions():
            m = self.manifest(version_of(path))
            if m is not None:
                out.append(m)
        return sorted(out, key=lambda m: m.get("created_at", 0.0))

    def state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict) or data.get("v") != VERSION:
            data = {}
        data.setdefault("v", VERSION)
        for key, empty in (("candidate", None), ("history", []), ("stack", []), ("events", []), ("ignore_before", 0.0),
                           ("weekly_line_at", None), ("tried", {}), ("last_train", None)):
            data.setdefault(key, empty)
        return data

    @contextlib.contextmanager
    def _state(self) -> Iterator[dict[str, Any]]:
        """Read, change and write learning.json under the lock the daemon, the CLI and the tray share."""
        with file_lock(self.state_path.with_name("learning.json.lock")):
            state = self.state()
            yield state
            state["events"] = state["events"][-MAX_EVENTS:]
            write_private(self.state_path, json.dumps(state, ensure_ascii=False, indent=1) + "\n")

    @staticmethod
    def _event(state: dict[str, Any], what: str, **fields: Any) -> None:
        state["events"].append({"at": round(time.time(), 3), "what": what, **fields})

    # ------------------------------------------------------------------ where things stand

    def resolved(self) -> tuple[gatehead.Head | None, str]:
        head, source, _ = gatehead.resolve(self.config.gate.head, self.heads)
        return head, source

    def current_version(self) -> str:
        """The head the gate uses (gatehead.resolve): a version, "shipped", or "config:<file>"."""
        head, source = self.resolved()
        if head is None:
            return "none"
        if source == "config":
            return f"config:{head.path}"
        return head.version if source == "user" else "shipped"

    def in_use(self) -> dict[str, Any]:
        head, source = self.resolved()
        out: dict[str, Any] = {"version": self.current_version(), "source": source or None,
                               "scorer": self.config.gate.scorer, "note": None}
        if self.config.gate.head:
            out["note"] = "[gate] head names a file, so the learning loop's heads are not used"
        elif self.config.gate.scorer != "head":
            out["note"] = '[gate] scorer = "nearest": no head is used'
        if head is not None and head.created:
            out["created"] = head.created
        return out

    def examples(self) -> list[Example]:
        """Every example, resolved (Example.summary(text=True) has the sentence, for the user's own screen)."""
        return self.store.examples(questions())

    def new_examples(self, examples: list[Example] | None = None, state: dict[str, Any] | None = None) -> list[Example]:
        """Usable examples the last training run did not have (or had with other labels)."""
        examples = self.examples() if examples is None else examples
        tried = (state or self.state()).get("tried") or {}
        return [e for e in examples if e.usable and tried.get(e.key) != e.digest]

    def rollback_target(self, state: dict[str, Any] | None = None) -> str | None:
        """Where a rollback goes: the head in use before this one (the switch's stack), else this
        head's parent, else the shipped head; None when the shipped one is in use already."""
        current = self.current_version()
        if current in ("shipped", "none") or current.startswith("config:"):
            return None
        state = state or self.state()
        for version in reversed(state.get("stack") or []):
            if version == current:
                continue
            if version == "shipped" or self._exists(version):
                return version
        parent = (self.manifest(current) or {}).get("parent")
        if parent and parent != current and (parent == "shipped" or self._exists(parent)):
            return parent
        return "shipped"

    def _exists(self, version: str) -> bool:
        try:
            self.heads.find(check_version(version))
        except (gatehead.HeadError, LearningError):
            return False
        return True

    def candidate(self, state: dict[str, Any] | None = None) -> dict[str, Any] | None:
        version = (state or self.state()).get("candidate")
        m = self.manifest(version)
        return self.head_summary(m) if m is not None else None

    @staticmethod
    def head_summary(m: dict[str, Any]) -> dict[str, Any]:
        held = m.get("heldout") or {}
        return {"version": m.get("version"), "created": m.get("created"), "status": m.get("status"),
                "passed": m.get("passed"), "why": m.get("why") or None, "parent": m.get("parent"),
                "compared_with": m.get("compared_with"),
                "heldout": {"current": pct((held.get("current") or {}).get("fields")),
                            "candidate": pct((held.get("candidate") or {}).get("fields")),
                            "strict_current": pct((held.get("current") or {}).get("strict")),
                            "strict_candidate": pct((held.get("candidate") or {}).get("strict"))},
                "learned": len(m.get("learned") or {}), "counts": m.get("counts")}

    def status(self) -> dict[str, Any]:
        state = self.state()
        examples = self.examples()
        new = self.new_examples(examples, state)
        stats = self.store.stats()
        c = self.config.learning
        last = dict(state["last_train"]) if state.get("last_train") else None
        if last:
            last.pop("left_out", None)
        return {
            "logging": c.log_outcomes,
            "in_use": self.in_use(),
            "candidate": self.candidate(state),
            "examples": {"total": stats["examples"], "usable": stats["usable"], "new": len(new),
                         "conflicts": stats["conflicts"], "signals": stats["signals"], "gone": stats["gone"]},
            "last_train": last,
            "rollback_to": self.rollback_target(state),
            "versions": len(self.heads.versions()),
            "settings": {"idle_train": c.idle_train, "idle_minutes": c.idle_minutes, "min_new_labels": c.min_new_labels,
                         "auto_switch": c.auto_switch, "weekly_line": c.weekly_line, "max_share": c.max_share},
        }

    def versions(self) -> list[dict[str, Any]]:
        """Every head, the shipped one first, with what its manifest says (a head made by
        `strawberry gate train` has none)."""
        current = self.current_version()
        out = [{"version": "shipped", "status": "shipped", "in_use": current == "shipped",
                "heldout": self.heldout_of("shipped")}]
        for path in self.heads.versions():
            version = version_of(path)
            m = self.manifest(version)
            row: dict[str, Any] = {"version": version, "in_use": version == current}
            if m is None:
                row |= {"status": "by hand", "heldout": self.heldout_of(version)}
            else:
                summary = self.head_summary(m)
                row |= {"status": m.get("status"), "created": m.get("created"), "parent": m.get("parent"),
                        "learned": summary["learned"], "heldout": summary["heldout"]["candidate"],
                        "compared_with": m.get("compared_with"), "heldout_parent": summary["heldout"]["current"],
                        "why": m.get("why") or None, "decided_by": m.get("decided_by")}
            out.append(row)
        return out

    def heldout_of(self, version: str) -> float | None:
        """A head's held-out fields score, from the manifests: its own, or one that was compared with it."""
        m = self.manifest(version)
        if m is not None:
            return pct(((m.get("heldout") or {}).get("candidate") or {}).get("fields"))
        for m in reversed(self.manifests()):
            if m.get("compared_with") == version:
                return pct(((m.get("heldout") or {}).get("current") or {}).get("fields"))
        return None

    # ------------------------------------------------------------------ labels

    def extract(self) -> dict[str, int]:
        """Read the outcome file and add what it says to the store. Safe to run again: a record is
        counted once."""
        from .outcomes import read

        if not self.outcomes_path.exists():
            return {"records": 0, "examples": 0, "evidence": 0, "gone": 0}
        records = read(self.outcomes_path)
        evidence = labels.extract(records, float(self.state().get("ignore_before") or 0.0))
        counts = {"records": len(records), **self.store.merge(evidence)}
        if counts["evidence"]:
            log.info("learning: %d new label(s) from %d outcome record(s), %d new example(s)", counts["evidence"],
                     len(records), counts["examples"])
        return counts

    def review(self, key: str, verdict: str) -> bool:
        """approve: marks the example as looked at; reject: deletes its sentence for good (the Brain UI)."""
        done = self.store.review(key, verdict)
        if done:
            with self._state() as state:
                self._event(state, "review", verdict=verdict)
        return done

    def forget(self, everything: bool = False, wait_s: float = 5.0) -> dict[str, Any]:
        """Delete the learned examples (and the vectors cached for them), and ignore the outcome
        records written so far. With `everything`, also every head the loop made and its history:
        the shipped head is in use again.

        The outcome records are ignored first, so nothing read after this point brings a sentence
        back; a candidate waiting is dropped. A training run in flight is waited for up to a few
        seconds (the lock) and otherwise left to finish: its result is discarded (_record), never
        offered or switched to."""
        from .gatecmd import cache_file

        with self._state() as state:
            state["ignore_before"] = time.time()
            state["tried"] = {}
            waiting = state.get("candidate")
            state["candidate"] = None
        if waiting:
            self._decide(waiting, "superseded", "forget")
        with self._try_training(wait_s):
            count = self.store.clear()
            caches = list(cache_file("x").parent.glob("vectors-*.npz")) if cache_file("x").parent.is_dir() else []
            for path in caches:
                path.unlink(missing_ok=True)
            for m in self.manifests():
                if m.get("learned"):
                    m["learned"] = {}
                    m["forgotten"] = True
                    self._write_manifest(m["version"], m)
            removed = 0
            if everything:
                for path in self.heads.versions():
                    if self.manifest(version_of(path)) is not None:
                        self.manifest_path(version_of(path)).unlink(missing_ok=True)
                        if self.heads.current() == path:
                            self.heads.use(None)
                        path.unlink(missing_ok=True)
                        removed += 1
            with self._state() as state:
                if everything:
                    state.update(candidate=None, history=[], stack=[], last_train=None)
                if state.get("last_train"):
                    state["last_train"].pop("left_out", None)
                self._event(state, "forget", examples=count, heads=removed)
        log.info("learning: forgot %d example(s)%s", count, f" and {removed} head(s)" if everything else "")
        return {"examples": count, "heads": removed, "caches": len(caches)}

    # ------------------------------------------------------------------ training

    @contextlib.contextmanager
    def _training(self, wait_s: float = 0.0) -> Iterator[None]:
        """The one training run: LearningBusy when another holds it. Only taking the lock can say
        busy; a TimeoutError inside the run is the run's own."""
        lock = file_lock(self.root / "train.lock", timeout_s=wait_s, stale_s=TRAIN_LOCK_STALE_S)
        try:
            lock.__enter__()
        except TimeoutError:
            raise LearningBusy("a training run is already going (the daemon's, or another terminal's)") from None
        try:
            yield
        finally:
            lock.__exit__(None, None, None)

    @contextlib.contextmanager
    def _try_training(self, wait_s: float = 5.0) -> Iterator[bool]:
        """The training lock when it comes within `wait_s`; else go on without it (True: held)."""
        lock = self._training(wait_s)
        try:
            lock.__enter__()
            held = True
        except LearningBusy:
            held = False
        try:
            yield held
        finally:
            if held:
                lock.__exit__(None, None, None)

    async def train(self, gate, *, runner: Callable | None = None, should_stop: Callable[[], bool] | None = None,
                    progress: Callable[[str], None] | None = None, trigger: str = "by hand") -> dict[str, Any]:
        """Build and score a candidate. `gate` is a started systemone.Gate (the daemon's own, or one the
        CLI started): its embedder, questions and extra examples are what the candidate is trained
        and scored with. `runner(job)` does the CPU work (learnfit.run in this process by default;
        the daemon passes run_child). `should_stop` is asked between embedding chunks."""
        from . import learnfit

        say = progress or (lambda _: None)
        with self._training():
            started = time.time()
            await asyncio.to_thread(self.extract)
            examples = [e for e in await asyncio.to_thread(self.examples) if e.usable]
            if not examples:
                return await asyncio.to_thread(self._record, {"trained": False, "why": "no labelled examples yet"},
                                               examples, trigger, "", started)
            say(f"{len(examples)} labelled example(s); embedding what is new")
            job = await self.job(gate, examples, should_stop)
            say("fitting a candidate and scoring it on the held-out set")
            result = await (runner or (lambda j: learnfit.run(j, say)))(job)
            return await asyncio.to_thread(self._record, result, examples, trigger, job.current_version, started)

    async def job(self, gate, examples: list[Example], should_stop: Callable[[], bool] | None = None):
        """Everything the CPU part needs, embedded with the gate's own embedder (cached by text in the
        cache dir, as `strawberry gate train` does)."""
        from . import gatecmd, gateeval, learnfit
        from .privacy import gate_state
        from .systemone import IS_SENSITIVE, _options

        base, _ = await asyncio.to_thread(gatehead.read_dataset, gatehead.TRAIN_SET)
        phrases, notifications = gateeval.load_set(gatehead.HELDOUT_SET)
        more = phrases_file()
        extra: dict[str, list[str]] = {k: list(v) for k, v in gate.extra_examples.items()}
        query = [s.text for s in base] + [e.text for e in examples] + [p["text"] for p in phrases]
        query += [gate_state(n.get("title", ""), n["body"]) for n in notifications]
        query += [p for ps in extra.values() for p in ps]
        if more is not None:
            query += [p["text"] for p in gateeval.load_set(more)[0]]
        docs = [t for q in (*gate.questions, IS_SENSITIVE) for o in _options(q) for t in o.texts()]
        prefix, doc_prefix = gate.config.query_prefix, gate.config.document_prefix
        cache = gatecmd.cache_file(f"{gate.embedder_id}-{gate.backend}")
        chunk = CHUNK if should_stop is not None else 256
        vectors: dict[str, Any] = {}
        for texts, pre in ((query, prefix), (docs, doc_prefix)):
            got = await gatecmd.embed(gate, texts, pre, cache, chunk=chunk, should_stop=should_stop)
            vectors |= {learnfit.vector_key(pre + t): v for t, v in zip(texts, got)}
        head, source = self.resolved()
        current = str(head.path) if head is not None and head.path and self.config.gate.scorer == "head" else ""
        gate_config = {k: v for k, v in asdict(gate.config).items() if k != "examples"}
        return learnfit.Job(gate=gate_config, extra=extra, embedder=gate.embedder_id, base=str(gatehead.TRAIN_SET),
                            heldout=str(gatehead.HELDOUT_SET), heads=str(self.heads.directory),
                            learned=[{"key": e.key, "text": e.text, "labels": e.labels, "avoid": e.avoid,
                                      "weights": e.weights} for e in examples],
                            current=current, current_version=self.current_version(),
                            phrases=str(more) if more else "", max_share=self.config.learning.max_share,
                            vectors=vectors)

    def _record(self, result: dict[str, Any], examples: list[Example], trigger: str, parent: str,
                started: float = 0.0) -> dict[str, Any]:
        """What came of a run: the manifest, the store (a sentence found private is deleted), the
        state; and with auto_switch, the switch. A run that started before a `forget` is discarded,
        head and all: what it learned from is gone."""
        if "error" in result:
            raise LearningError(f"the trainer failed ({result['error']})")
        now = time.time()
        left = result.get("left_out") or {}
        private = [k for k, why in left.items() if why == "private"]
        digests = {e.key: e.digest for e in examples}
        used = set(result.get("used") or [])
        out = {k: v for k, v in result.items() if k not in ("left_out", "used")}
        with self._state() as state:
            if float(state.get("ignore_before") or 0.0) >= started:
                self._discard(result.get("path"))
                state["last_train"] = {"at": round(now, 3), "trigger": trigger, "result": "forgotten",
                                       "why": "what it learned from was forgotten while it ran"}
                self._event(state, "train", result="forgotten", trigger=trigger)
                log.info("learning: the run's result is discarded: a forget came while it ran")
                return {"trained": False, "why": "what it learned from was forgotten while it ran"}
            if private:
                self.store.drop(private, "private")
            state["tried"] = digests
            last: dict[str, Any] = {"at": round(now, 3), "trigger": trigger, "left_out": left,
                                    "counts": result.get("counts")}
            if not result.get("trained"):
                last |= {"result": "nothing", "why": result.get("why")}
                self._event(state, "train", result="nothing", why=result.get("why"), trigger=trigger,
                            left_out=len(left))
                log.info("learning: no candidate (%s)", result.get("why"))
            else:
                version = result["version"]
                passed = bool(result.get("passed"))
                manifest = {
                    "v": VERSION, "version": version, "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "created_at": round(now, 3), "parent": parent, "compared_with": result.get("compared_with"),
                    "trigger": trigger, "dataset": result.get("dataset"),
                    "learned": {k: digests[k] for k in sorted(used) if k in digests},
                    "counts": result.get("counts"), "left_out": _tally(left.values()),
                    "heldout": result.get("heldout"), "phrases": result.get("phrases"),
                    "act": result.get("act"), "offer": result.get("offer"),
                    "passed": passed, "why": result.get("why") or "",
                    "status": "candidate" if passed else "rejected",
                    "decided_by": None if passed else "held-out gate", "decided_at": None if passed else round(now, 3),
                }
                switched = None
                if passed and self.config.learning.auto_switch and parent == self.current_version():
                    switched = self._switch(state, version, "auto")
                    manifest |= {"status": "accepted", "decided_by": "auto", "decided_at": round(now, 3)}
                elif passed:
                    old = state.get("candidate")
                    if old and old != version:
                        self._decide(old, "superseded", "a newer candidate")
                    state["candidate"] = version
                self._write_manifest(version, manifest)
                held = result.get("heldout") or {}
                cur, cand = pct(held["current"]["fields"]), pct(held["candidate"]["fields"])
                outcome = "switched" if switched else "candidate" if passed else "rejected"
                last |= {"result": outcome, "version": version, "passed": passed, "why": result.get("why") or None,
                         "heldout": [cur, cand]}
                self._event(state, "train", result=outcome, version=version, trigger=trigger,
                            learned=len(manifest["learned"]), left_out=len(left), heldout=[cur, cand])
                log.info("learning: candidate %s %s: held-out fields %s%% -> %s%%%s", version,
                         {"switched": "switched in (auto_switch)", "candidate": "waits for accept",
                          "rejected": "rejected"}[outcome], cur, cand, f" ({result.get('why')})" if not passed else "")
                out["status"] = manifest["status"]
                out["switched"] = switched
            state["last_train"] = last
        return out

    def _discard(self, path: str | None) -> None:
        """Delete a head file the trainer wrote (only a head-*.npz in the heads dir)."""
        if not path:
            return
        head = Path(path)
        if head.parent.resolve() == self.heads.directory.resolve() and gatehead.POINTER_NAME.fullmatch(head.name):
            head.unlink(missing_ok=True)
            self.manifest_path(version_of(head)).unlink(missing_ok=True)

    # ------------------------------------------------------------------ the switch

    def _decide(self, version: str, status: str, by: str) -> None:
        m = self.manifest(version)
        if m is not None:
            m |= {"status": status, "decided_by": by, "decided_at": round(time.time(), 3)}
            self._write_manifest(version, m)

    def _switch(self, state: dict[str, Any], to: str, by: str, push: bool = True) -> dict[str, str]:
        check_version(to)
        before = self.current_version()
        path = None if to == "shipped" else self.heads.find(to)
        if path is not None:
            gatehead.load(path)                      # a broken file is refused here, not at the next start
        self.heads.use(path)
        if push and before != to and not before.startswith("config:") and before != "none":
            state["stack"].append(before)
        state["history"].append({"from": before, "to": to, "at": round(time.time(), 3), "by": by})
        self._event(state, by, version=to, previous=before)
        log.info("learning: head %s -> %s (%s)", before, to, by)
        return {"from": before, "to": to}

    def accept(self, version: str | None = None) -> dict[str, str]:
        with self._state() as state:
            version = version or state.get("candidate")
            if not version:
                raise LearningError("no candidate is waiting")
            m = self.manifest(check_version(version))
            if m is None:
                raise LearningError(f"no candidate {version!r}")
            if m.get("status") != "candidate":
                raise LearningError(f"{version} is {m.get('status')}, not a candidate waiting")
            if not m.get("passed"):
                raise LearningError(f"{version} scored worse than the head it was compared with; not accepted")
            current = self.current_version()
            if m.get("parent") != current:
                raise LearningError(f"{version} was compared with {m.get('parent')}, and {current} is in use now: "
                                    "train again")
            switched = self._switch(state, version, "accept")
            state["candidate"] = None
        self._decide(version, "accepted", "user")
        return switched

    def reject(self, version: str | None = None) -> str:
        with self._state() as state:
            version = version or state.get("candidate")
            if not version:
                raise LearningError("no candidate is waiting")
            m = self.manifest(check_version(version))
            if m is None or m.get("status") != "candidate":
                raise LearningError(f"{version} is not a candidate waiting")
            if state.get("candidate") == version:
                state["candidate"] = None
            self._event(state, "reject", version=version)
        self._decide(version, "rejected", "user")
        log.info("learning: candidate %s rejected", version)
        return version

    def rollback(self) -> dict[str, str]:
        with self._state() as state:
            target = self.rollback_target(state)
            if target is None:
                raise LearningError("nothing to roll back to: the shipped head is in use")
            gone = self.current_version()
            if target in state["stack"]:
                while state["stack"] and state["stack"].pop() != target:
                    pass
            switched = self._switch(state, target, "rollback", push=False)
            if state.get("candidate") and (self.manifest(state["candidate"]) or {}).get("parent") == gone:
                self._decide(state["candidate"], "superseded", "rollback")
                state["candidate"] = None
        m = self.manifest(gone)
        if m is not None:
            m["rolled_back_at"] = round(time.time(), 3)
            self._write_manifest(gone, m)
        return switched

    def use(self, version: str) -> dict[str, str]:
        """`strawberry gate use VERSION|shipped`: any head, by hand; a rollback comes back from it."""
        check_version(version)
        with self._state() as state:
            return self._switch(state, version, "use")

    # ------------------------------------------------------------------ reporting

    def report(self, days: float = 7.0, now: float | None = None) -> dict[str, Any]:
        """The last `days`: examples learned and left out, candidates and what became of them, and the
        held-out score of the head in use then and now. Counts only."""
        now = time.time() if now is None else now
        since = now - days * 86400
        state = self.state()
        examples = self.examples()
        fresh = [e for e in examples if e.first >= since]
        left = (state.get("last_train") or {}).get("left_out") or {}
        learned = [e for e in fresh if e.usable and e.key not in left]
        gone = [g for g in self.store.gone().values() if (g.get("at") or 0.0) >= since]
        rejected = len(gone) + sum(1 for e in fresh if e.key in left or (not e.usable and e.conflicts))
        events = [e for e in state["events"] if e.get("at", 0.0) >= since]
        trains = [e for e in events if e["what"] == "train" and e.get("version")]
        accepted = [e for e in events if e["what"] in ("accept", "auto")]
        heads_rejected = [e for e in trains if e.get("result") == "rejected"] + [e for e in events if e["what"] == "reject"]
        current = self.current_version()
        before = None
        for h in state["history"]:
            if h.get("at", 0.0) < since:
                before = h["to"]
        if before is None:
            before = state["history"][0]["from"] if state["history"] else current
        tools: dict[str, int] = {}
        for e in learned:
            tool = e.labels.get("music_tool") or e.avoid.get("music_tool")
            if tool:
                tools[tool] = tools.get(tool, 0) + 1
        signals: dict[str, int] = {}
        for e in learned:
            for s, n in e.signals.items():
                signals[s] = signals.get(s, 0) + n
        return {"days": days, "since": round(since, 3), "learned": len(learned), "rejected": rejected,
                "signals": signals, "tools": tools, "candidates": len(trains), "accepted": len(accepted),
                "heads_rejected": len(heads_rejected), "rollbacks": sum(1 for e in events if e["what"] == "rollback"),
                "head_before": before, "head_now": current,
                "heldout_before": self.heldout_of(before), "heldout_now": self.heldout_of(current)}

    @staticmethod
    def report_line(report: dict[str, Any]) -> str:
        """"learned 14 new examples, 2 rejected, held-out 95.0% → 95.6%"."""
        line = f"learned {report['learned']} new example{'s' if report['learned'] != 1 else ''}, {report['rejected']} rejected"
        before, now = report.get("heldout_before"), report.get("heldout_now")
        if before is not None and now is not None:
            line += f", held-out {before:.1f}% → {now:.1f}%"
        elif now is not None:
            line += f", held-out {now:.1f}%"
        return line

    def weekly_line(self, now: float | None = None) -> str | None:
        """Her one line about the week, in her voice; None when there is nothing to say."""
        report = self.report(7.0, now)
        n = report["learned"]
        if n == 0:
            return None
        things = f"{number(n)} new thing{'s' if n != 1 else ''}"
        tool = max(report["tools"], key=lambda t: (report["tools"][t], t)) if report["tools"] else ""
        if report["accepted"] and tool:
            return f"Learned {things} this week. I've got better at {SKILLS[tool]}."
        if report["accepted"]:
            return f"Learned {things} this week. I should get you right more often now."
        return f"Picked up {things} this week. They'll stick once you've had a look."

    def mark_weekly(self, said: bool, now: float | None = None) -> None:
        with self._state() as state:
            state["weekly_line_at"] = round(time.time() if now is None else now, 3)
            if said:
                self._event(state, "weekly_line")

    # ------------------------------------------------------------------ /health

    def health(self) -> dict[str, Any]:
        """Read-only, counts and versions, never a sentence."""
        state = self.state()
        examples = self.examples()
        candidate = self.candidate(state)
        last = state.get("last_train") or {}
        return {
            "head": self.current_version(),
            "candidate": {k: candidate[k] for k in ("version", "passed", "heldout")} if candidate else None,
            "examples": sum(1 for e in examples if e.usable), "new": len(self.new_examples(examples, state)),
            "last_train": {k: last.get(k) for k in ("at", "result", "version", "heldout", "why", "trigger")} if last else None,
            "rollback": self.rollback_target(state),
            "auto_switch": self.config.learning.auto_switch,
            "versions": len(self.heads.versions()),
        }

    def signature(self) -> tuple:
        """Changes whenever anything health() reads does (the tray polls /health every 2 s)."""
        out = []
        for path in (self.state_path, self.store.path, self.heads.pointer, self.heads.directory):
            try:
                st = path.stat()
                out.append((st.st_mtime_ns, st.st_size))
            except OSError:
                out.append(None)
        return tuple(out)


def _tally(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


# ----------------------------------------------------------------------------- the child process


async def run_child(job) -> dict[str, Any]:
    """learnfit in a process of its own: the lowest priority, two BLAS threads, killed if the daemon
    stops. The job (sentences included) goes through a pipe, never a file."""
    env = dict(os.environ, OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2", MKL_NUM_THREADS="2")
    options: dict[str, Any] = {}
    if paths.windows():
        options["creationflags"] = subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "strawberry_crab.learnfit", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, **options)
    try:
        out, _ = await asyncio.wait_for(process.communicate(job.to_bytes()), CHILD_TIMEOUT_S)
    except (asyncio.CancelledError, asyncio.TimeoutError) as exc:
        if process.returncode is None:
            process.kill()
            await process.wait()
        if isinstance(exc, asyncio.TimeoutError):
            raise LearningError(f"the trainer took over {CHILD_TIMEOUT_S / 60:.0f} minutes and was stopped") from None
        raise
    try:
        result = json.loads(out.decode("utf-8") or "{}")
    except ValueError:
        result = {}
    # Its stderr is never read back: a library could print something of a sentence there.
    if process.returncode != 0 or not isinstance(result, dict) or not result:
        error = result.get("error") if isinstance(result, dict) and isinstance(result.get("error"), str) else ""
        raise LearningError(f"the trainer failed ({error[:60]})" if error
                            else f"the trainer exited with code {process.returncode}")
    return result


# ----------------------------------------------------------------------------- in the daemon

_UNSEEN = object()


class IdleTrainer:
    """The daemon's side of the loop. Every TICK_S: follow `current` (a CLI accept, a rollback from the
    tray: the gate swaps heads without a restart), take new outcome records into the store, train a
    candidate when nobody has spoken for `idle_minutes` and `min_new_labels` new labels wait (only
    with outcome logging on: off, nothing new is learned), and say the weekly line when it is on and due.

    The run never holds up a sentence: the embedding is the gate's own (a worker thread, CHUNK
    sentences a call, stopped between two calls as soon as the user speaks), and the fit and the
    held-out scoring run in a child process at the lowest priority (run_child)."""

    def __init__(self, daemon, runner: Callable | None = None) -> None:
        self.daemon = daemon
        self.config = daemon.config.learning
        self.learning = Learning(daemon.config)
        self.runner = runner or run_child
        self.task: asyncio.Task | None = None
        self.run_task: asyncio.Task | None = None
        self.phase = "idle"
        self.last_attempt = -1e18
        self.pointer_seen: Any = None
        self.outcomes_seen: Any = _UNSEEN
        self.runs = 0
        self.interrupted = 0
        self.errors = 0
        self.last_error = ""
        self.new_labels = 0
        self._health: tuple[tuple, dict[str, Any]] | None = None

    def start(self) -> None:
        self.pointer_seen = self._pointer()
        self.task = asyncio.get_running_loop().create_task(self._loop())

    async def close(self) -> None:
        for task in (self.task, self.run_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (self.task, self.run_task) if t is not None), return_exceptions=True)

    # --- when

    def voice_busy(self) -> bool:
        listener = getattr(self.daemon, "listener", None)
        return bool(getattr(listener, "busy", False)) or getattr(self.daemon, "voice_handling", 0) > 0

    def quiet_for(self) -> float:
        return time.monotonic() - getattr(self.daemon, "voice_at", 0.0)

    def due(self) -> tuple[bool, str]:
        """Whether to train now, and why not."""
        if not self.config.idle_train:
            return False, "idle_train is off"
        if not self.config.log_outcomes:
            return False, "outcome logging is off"
        if self.run_task is not None and not self.run_task.done():
            return False, "running"
        gate = self.daemon.gate
        if not getattr(gate, "ready", False) or getattr(gate, "embedder", None) is None:
            return False, "the gate is not ready"
        if self.voice_busy():
            return False, "voice is active"
        if self.quiet_for() < self.config.idle_minutes * 60:
            return False, "not idle long enough"
        if self.new_labels < self.config.min_new_labels:
            return False, f"{self.new_labels} new label(s), {self.config.min_new_labels} needed"
        if time.monotonic() - self.last_attempt < RUN_GAP_S:
            return False, "tried recently"
        return True, ""

    # --- the loop

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must outlive one bad tick
                log.warning("learning: a tick failed (%s)", type(exc).__name__)   # the type only: a message can quote values

    async def tick(self) -> None:
        await self.follow_pointer()
        outcomes = self._stat(self.learning.outcomes_path)
        if outcomes != self.outcomes_seen and not self.voice_busy():
            self.outcomes_seen = outcomes
            if outcomes is not None and self.config.log_outcomes:
                await asyncio.to_thread(self.learning.extract)
            self.new_labels = len(await asyncio.to_thread(self.learning.new_examples))
        ok, _ = self.due()
        if ok:
            self.run_task = asyncio.get_running_loop().create_task(self.run())
        await self.maybe_weekly_line()

    def train_now(self) -> tuple[bool, str]:
        """The Brain UI's "train now": one run in the background, as the idle one runs (the gate's own
        embedder, stopped when the user speaks, the child process), without waiting for quiet or
        new labels. (False, why) when one is running or the gate is not ready."""
        if self.run_task is not None and not self.run_task.done():
            return False, "a run is going already"
        gate = self.daemon.gate
        if not getattr(gate, "ready", False) or getattr(gate, "embedder", None) is None:
            return False, "the gate is not ready"
        self.run_task = asyncio.get_running_loop().create_task(self.run(trigger="by hand"))
        return True, ""

    async def run(self, trigger: str = "idle") -> dict[str, Any] | None:
        from .gatecmd import EmbedInterrupted

        self.last_attempt = time.monotonic()
        self.runs += 1
        started_at = getattr(self.daemon, "voice_at", 0.0)

        def stop() -> bool:
            return self.voice_busy() or getattr(self.daemon, "voice_at", 0.0) != started_at

        def progress(line: str) -> None:
            self.phase = "fitting" if line.startswith("fitting") else self.phase

        self.phase = "embedding"
        if trigger == "idle":
            log.info("learning: idle for %.0f min with %d new label(s); training a candidate", self.quiet_for() / 60,
                     self.new_labels)
        else:
            log.info("learning: training a candidate (%s, %d new label(s))", trigger, self.new_labels)
        result = None
        try:
            result = await self.learning.train(self.daemon.gate, runner=self.runner, should_stop=stop,
                                               progress=progress, trigger=trigger)
        except EmbedInterrupted as exc:
            self.interrupted += 1
            log.info("learning: the user spoke; the run stops (%s) and waits for the next quiet spell", exc)
        except LearningBusy as exc:
            log.info("learning: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported in /health, retried after RUN_GAP_S
            self.errors += 1
            # The type only, in /health and the log: a message can carry what a library printed.
            self.last_error = type(exc).__name__
            log.warning("learning: the run failed (%s)", self.last_error)
        finally:
            self.phase = "idle"
        self.new_labels = len(await asyncio.to_thread(self.learning.new_examples))
        if result and result.get("switched"):
            await self.follow_pointer()
        return result

    def _pointer(self) -> Any:
        return self._stat(self.learning.heads.pointer)

    @staticmethod
    def _stat(path: Path) -> Any:
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    async def follow_pointer(self) -> bool:
        """`current` changed since the last look: the gate scores with the head it names now."""
        seen = self._pointer()
        if seen == self.pointer_seen or not getattr(self.daemon.gate, "ready", False):
            return False             # unchanged, or the gate is still starting (it reads `current` itself)
        self.pointer_seen = seen
        reload = getattr(self.daemon.gate, "reload_head", None)
        if reload is None:
            return False
        return bool(await reload())

    async def maybe_weekly_line(self) -> bool:
        if not self.config.weekly_line:
            return False
        state = await asyncio.to_thread(self.learning.state)
        last = state.get("weekly_line_at")
        if last is None:
            await asyncio.to_thread(self.learning.mark_weekly, False)   # the first week starts now
            return False
        if time.time() - last < WEEK_S or self.voice_busy() or self.quiet_for() < WEEKLY_IDLE_S:
            return False
        if self.quiet_hours() or getattr(getattr(self.daemon, "hub", None), "count", 0) == 0:
            return False
        line = await asyncio.to_thread(self.learning.weekly_line)
        await asyncio.to_thread(self.learning.mark_weekly, bool(line))
        if not line:
            return False
        from .contract import Performance

        log.info("learning: the weekly line (%d chars)", len(line))
        await self.daemon.perform(Performance(state="talking", text=line, emotion="happy"))
        return True

    def quiet_hours(self) -> bool:
        from datetime import datetime

        from .speech import in_quiet_hours, parse_quiet_hours

        speaker = getattr(self.daemon, "speaker", None)
        window = getattr(speaker, "quiet", None)
        clock = getattr(speaker, "clock", None)
        if window is None:
            window = parse_quiet_hours(self.daemon.config.speech.quiet_hours)
        now = clock() if callable(clock) else datetime.now().time()
        return in_quiet_hours(window, now)

    # --- /health

    def health(self) -> dict[str, Any]:
        """/health.learning's loop part: counts and versions, never a sentence. Re-read only when a
        file changed."""
        key = self.learning.signature()
        if self._health is None or self._health[0] != key:
            try:
                self._health = (key, self.learning.health())
            except (OSError, TimeoutError) as exc:
                return {"error": type(exc).__name__}
        due, why = self.due()
        return self._health[1] | {"trainer": {"phase": self.phase, "idle_train": self.config.idle_train,
                                              "waiting_for": None if due else why, "runs": self.runs,
                                              "interrupted": self.interrupted, "errors": self.errors,
                                              "last_error": self.last_error or None}}
