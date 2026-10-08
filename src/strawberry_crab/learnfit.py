"""The learning loop's CPU work (WIRING.md §8d): the filters that need vectors, the balance, the fit and
the held-out gate. Everything here works on vectors already embedded (learning.py does that with the
gate's own embedder), so it needs no model.

The daemon's idle trainer runs it as a child process at the lowest priority with two BLAS threads,
`python -m strawberry_crab.learnfit`: the job on stdin (an .npz: the job as JSON and the vectors),
the result as JSON on stdout. The voice path never waits for it. `strawberry learning train` and
the tests call `run` in their own process.

The result names sentences by their store key only; nothing here logs a sentence.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np

from . import gateeval, gatehead
from .config import GateConfig

log = logging.getLogger("strawberryd.learning")

HELDOUT_SIMILARITY = 0.95     # a learned sentence this close to a held-out one is left out (§8a's rule)
DUPLICATE_SIMILARITY = 0.97   # two learned sentences this close with the same labels are one (§8a's rule)
LEARNED_GROUP = "learned:"


def vector_key(text: str) -> str:
    """The vector cache's key (gatecmd.embed): the text as embedded, prefix included."""
    return hashlib.sha256(text.encode()).hexdigest()[:24]


@dataclass
class Job:
    gate: dict[str, Any]                  # GateConfig as a dict, examples left out (they are in `extra`)
    extra: dict[str, list[str]]           # the gate's extra examples: the adapters' and the config's
    embedder: str                         # what the head says it was trained on ("embeddinggemma")
    base: str                             # the data set (gatehead.TRAIN_SET)
    heldout: str                          # the held-out set (gatehead.HELDOUT_SET): read here, never written
    heads: str                            # the heads dir the candidate is written to
    learned: list[dict[str, Any]]         # {key, text, labels, avoid, weights}
    current: str = ""                     # the head in use now (a file), or "" for the nearest examples
    current_version: str = ""
    phrases: str = ""                     # scripts/gate_phrases.json when there is a checkout: reported, not gated
    max_share: float = 0.25
    vectors: dict[str, np.ndarray] = field(default_factory=dict, repr=False)

    def to_bytes(self) -> bytes:
        meta = asdict(self)
        meta.pop("vectors")
        keys = list(self.vectors)
        buffer = io.BytesIO()
        np.savez(buffer, job=np.array(json.dumps(meta, ensure_ascii=False)), keys=np.array(keys, dtype=str),
                 vectors=np.stack([self.vectors[k] for k in keys]).astype(np.float32) if keys else np.zeros((0, 0)))
        return buffer.getvalue()

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Job":
        with np.load(io.BytesIO(raw), allow_pickle=False) as data:
            meta = json.loads(str(data["job"]))
            keys = data["keys"].tolist()
            vectors = np.asarray(data["vectors"], dtype=np.float64)
        return cls(**meta, vectors={k: vectors[i] for i, k in enumerate(keys)})


class DictEmbedder:
    """The embedder a job's gates use: every text was embedded before (learning.py), by its key."""

    def __init__(self, vectors: dict[str, np.ndarray]) -> None:
        self.vectors = vectors

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        from .systemone import GateError

        out = []
        for text in texts:
            vector = self.vectors.get(vector_key(text))
            if vector is None:
                raise GateError("a text was not embedded for this job")
            out.append(vector.tolist())
        return out


async def shadow_gate(job: Job, head: str):
    """A gate like the daemon's (its questions, extra examples and anchors) scoring with `head`, or
    with the nearest examples when `head` is ""."""
    from .systemone import Gate

    config = replace(GateConfig(**{k: v for k, v in job.gate.items() if k != "examples"}), enabled=True,
                     examples={}, scorer="head" if head else "nearest", head=head)
    gate = Gate(config, embedder=DictEmbedder(job.vectors), examples=job.extra)
    if not await gate.start():
        raise gatehead.HeadError(f"the job's gate did not start: {gate.disabled_reason}")
    if head and gate.scorer != "head":
        raise gatehead.HeadError(f"the head is not usable: {gate.head_fallback}")
    return gate


def summary(result: gateeval.Result) -> dict[str, Any]:
    """A held-out score without sentences: the counts the switch compares and the report shows."""
    data = result.to_dict()
    return {"fields": data["fields"], "strict": data["strict"], "fields_fi": data["fields_fi"],
            "per_field": data["per_field"], "sensitive": data["per_field"].get("sensitive", [0, 0]),
            "reflexes": len(result.reflexes), "reflexes_wrong": sum(not ok for _, _, ok in result.reflexes),
            "auroc": data["calibration"]["auroc"]}


def better_or_equal(current: dict[str, Any], candidate: dict[str, Any]) -> tuple[bool, str]:
    """The held-out gate: at least as many fields right, as many sentences strictly right, as many
    notifications read right for privacy, and no more wrong reflexes. Returns (passed, why not)."""
    failed = []
    for name, label in (("fields", "fields"), ("strict", "strict sentences"), ("sensitive", "privacy readings")):
        if candidate[name][0] < current[name][0]:
            failed.append(f"{label} {candidate[name][0]} < {current[name][0]}")
    if candidate["reflexes_wrong"] > current["reflexes_wrong"]:
        failed.append(f"wrong reflexes {candidate['reflexes_wrong']} > {current['reflexes_wrong']}")
    return not failed, "; ".join(failed)


def pct(pair: list[int]) -> float | None:
    return round(100.0 * pair[0] / pair[1], 1) if pair and pair[1] else None


# ----------------------------------------------------------------------------- the filters


def balance(learned: list[gatehead.Sample], base: list[gatehead.Sample], max_share: float) -> int:
    """Keep the user's labels from swamping the data set: per question and option, the learned
    weight (labels and avoids of that option) at most `max_share` of the base rows with that option,
    and per question at most `max_share` of the base rows for the question. Over a cap, the rows
    involved are scaled down together. Returns how many rows were scaled."""
    base_counts: dict[str, dict[str, int]] = {}
    for s in base:
        for q, o in s.labels.items():
            base_counts.setdefault(q, {})[o] = base_counts.get(q, {}).get(o, 0) + 1
    scaled: set[int] = set()
    questions = sorted({q for s in learned for q in (*s.labels, *s.avoid)})
    for q in questions:
        rows = [(i, s.labels.get(q) or s.avoid.get(q)) for i, s in enumerate(learned) if q in s.labels or q in s.avoid]
        for option in sorted({o for _, o in rows}):
            mine = [i for i, o in rows if o == option]
            cap = max_share * base_counts.get(q, {}).get(option, 0)
            total = sum(learned[i].weight_for(q) for i in mine)
            if total > cap:
                factor = cap / total if total else 0.0
                for i in mine:
                    learned[i].weights[q] = learned[i].weight_for(q) * factor
                    scaled.add(i)
        cap = max_share * sum(base_counts.get(q, {}).values())
        total = sum(learned[i].weight_for(q) for i, _ in rows)
        if total > cap:
            factor = cap / total if total else 0.0
            for i, _ in rows:
                learned[i].weights[q] = learned[i].weight_for(q) * factor
                scaled.add(i)
    return len(scaled)


async def run(job: Job, progress=None) -> dict[str, Any]:
    """Filter the learned examples, fit a candidate on the data set and them, score it and the head
    in use on the held-out set. The candidate is written to the heads dir (every head is kept); the
    switch is the caller's (learning.py)."""
    from .gatecmd import _questions
    from .privacy import SENSITIVE_P
    from .systemone import IS_SENSITIVE

    say = progress or (lambda _: None)
    prefix = job.gate.get("query_prefix", "")
    left_out: dict[str, str] = {}
    base, dataset = gatehead.read_dataset(Path(job.base))

    current_version = job.current_version
    try:
        current_gate = await shadow_gate(job, job.current)
    except gatehead.HeadError:
        # The head in use does not fit (the gate falls back the same way): the nearest examples are
        # what the candidate has to beat.
        current_gate, current_version = await shadow_gate(job, ""), "nearest"
    try:
        # Privacy once more, on the vector, with the head in use and failing closed (outcomes.py did
        # it when the sentence was said; the head may have changed since).
        for item in job.learned:
            answer = (await current_gate.systemone.ask(item["text"], IS_SENSITIVE))[IS_SENSITIVE.name]
            if (answer.score or 0.0) >= SENSITIVE_P:
                left_out[item["key"]] = "private"
        phrases, notifications = gateeval.load_set(Path(job.heldout))
        current_score = summary(await gateeval.run(current_gate, "current", phrases, notifications))
        secondary: dict[str, Any] = {}
        if job.phrases:
            more, _ = gateeval.load_set(Path(job.phrases))
            secondary["current"] = summary(await gateeval.run(current_gate, "current", more, []))
    finally:
        await current_gate.close()

    def vec(text: str) -> np.ndarray:
        return job.vectors[vector_key(prefix + text)]

    heldout = np.stack([vec(p["text"]) for p in phrases]) if phrases else np.zeros((0, 1))
    kept: list[tuple[dict[str, Any], np.ndarray]] = []
    for item in sorted(job.learned, key=lambda i: i["key"]):
        if item["key"] in left_out:
            continue
        v = vec(item["text"])
        if len(heldout) and float((heldout @ v).max()) >= HELDOUT_SIMILARITY:
            left_out[item["key"]] = "heldout"       # the held-out set stays out of training, both ways
            continue
        twin = next((k for k, (other, w) in enumerate(kept) if float(w @ v) >= DUPLICATE_SIMILARITY
                     and other["labels"] == item["labels"] and other["avoid"] == item["avoid"]), None)
        if twin is not None:
            other = kept[twin][0]
            other["weights"] = {q: max(other["weights"].get(q, 0.0), w) for q, w in item["weights"].items()}
            left_out[item["key"]] = "near_duplicate"
            continue
        kept.append((dict(item, weights=dict(item["weights"])), v))
    counts = {"base": len(base), "learned": len(kept), "left_out": {}}
    for why in left_out.values():
        counts["left_out"][why] = counts["left_out"].get(why, 0) + 1
    if not kept:
        return {"trained": False, "why": "nothing left to learn after the filters", "left_out": left_out,
                "compared_with": current_version,
                "counts": counts, "heldout": {"current": current_score}, "dataset": dataset}

    learned = [gatehead.Sample(item["text"], dict(item["labels"]), LEARNED_GROUP + item["key"],
                               avoid=dict(item["avoid"]), weights=dict(item["weights"])) for item, _ in kept]
    counts["scaled"] = balance(learned, base, job.max_share)
    by_question: dict[str, int] = {}
    for s in learned:
        for q in {*s.labels, *s.avoid}:
            by_question[q] = by_question.get(q, 0) + 1
    counts["by_question"] = by_question
    base_vectors = np.stack([vec(s.text) for s in base])
    vectors = np.vstack([base_vectors, np.stack([v for _, v in kept])])
    say(f"fitting on {len(base)} + {len(learned)} sentences")
    head = gatehead.train(base + learned, vectors, _questions(), embedder=job.embedder, query_prefix=prefix,
                          dataset=dataset)
    head.meta["learned"] = {"rows": len(learned), "by_question": by_question}
    path = gatehead.Heads(Path(job.heads)).add(head)
    say(f"candidate {head.version} written; scoring it on the held-out set")
    candidate_gate = await shadow_gate(job, str(path))
    try:
        candidate_score = summary(await gateeval.run(candidate_gate, "candidate", phrases, notifications))
        if job.phrases:
            more, _ = gateeval.load_set(Path(job.phrases))
            secondary["candidate"] = summary(await gateeval.run(candidate_gate, "candidate", more, []))
    finally:
        await candidate_gate.close()
    passed, why = better_or_equal(current_score, candidate_score)
    return {"trained": True, "version": head.version, "path": str(path), "passed": passed, "why": why,
            "compared_with": current_version,
            "dataset": dataset, "act": head.act, "offer": head.offer, "counts": counts, "left_out": left_out,
            "used": [item["key"] for item, _ in kept],
            "heldout": {"current": current_score, "candidate": candidate_score},
            "phrases": secondary or None}


def lower_priority() -> None:
    """The child's own priority: the lowest the system has for a normal process."""
    if os.name == "posix":
        try:
            os.nice(19)
        except OSError:
            pass


def main() -> int:
    """The child process: a job on stdin, the result on stdout, nothing about sentences on stderr."""
    lower_priority()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    try:
        job = Job.from_bytes(sys.stdin.buffer.read())
        result = asyncio.run(run(job))
    except Exception as exc:  # noqa: BLE001 - reported to the parent as a type and a message
        sys.stdout.write(json.dumps({"error": f"{type(exc).__name__}: {exc}"[:300]}))
        return 1
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
