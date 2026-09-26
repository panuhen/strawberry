"""Scoring the gate on a labelled set (WIRING.md §8a): the held-out set (data/gate_heldout.json) or
scripts/gate_phrases.json. Used by `strawberry gate eval`, `strawberry gate train` and
scripts/gate_heldout_check.py.

Per field ("raw"): kind, topic and tool are the question's own choice, decision is the gate's (with
the thresholds of the scorer in use), catalogue / urgent / about_her / argument / library /
sensitive are p(yes) >= 0.5. Strict is scripts/gate_check.py's per-sentence pass: kind, the routed
topic (topic_min), the routed tool (only when the routed topic is music), decision and catalogue.
Confidence is each field's TypeSafe confidence; AUROC is how well it ranks the right answers above
the wrong ones (0.5 is chance).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

NOUL_FIELDS = {"catalogue": "needs_catalogue", "urgent": "is_urgent", "about_her": "is_about_her",
               "argument": "has_argument", "library": "wants_library_change"}
FIELDS = ("kind", "topic", "tool", "decision", "catalogue", "urgent", "about_her", "argument", "library",
          "sensitive")
QUESTION_OF = {"kind": "kind", "topic": "topic", "tool": "music_tool", "decision": "kind", **NOUL_FIELDS,
               "sensitive": "is_sensitive"}


@dataclass
class Result:
    name: str
    fields: dict[str, list[tuple[bool, float]]] = field(default_factory=dict)   # field -> (right, confidence)
    fields_fi: dict[str, list[tuple[bool, float]]] = field(default_factory=dict)
    strict: list[bool] = field(default_factory=list)
    strict_fi: list[bool] = field(default_factory=list)
    misses: list[tuple[str, list[str]]] = field(default_factory=list)
    ms: list[float] = field(default_factory=list)
    reflexes: list[tuple[str, str, bool]] = field(default_factory=list)  # (sentence, tool, right) per reflex fired

    def add(self, name: str, right: bool, conf: float, finnish: bool) -> None:
        self.fields.setdefault(name, []).append((right, conf))
        if finnish:
            self.fields_fi.setdefault(name, []).append((right, conf))

    def tally(self, which: str = "all") -> tuple[int, int]:
        source = self.fields_fi if which == "fi" else self.fields
        flat = [r for values in source.values() for r, _ in values]
        return sum(flat), len(flat)

    def per_field(self, name: str) -> tuple[int, int]:
        values = self.fields.get(name, [])
        return sum(r for r, _ in values), len(values)

    def calibration(self) -> dict[str, Any]:
        flat = [(r, c) for values in self.fields.values() for r, c in values]
        right = np.array([r for r, _ in flat], bool)
        conf = np.array([c for _, c in flat])
        return {
            "n": len(flat), "wrong": int((~right).sum()),
            "conf_right": float(conf[right].mean()) if right.any() else None,
            "conf_wrong": float(conf[~right].mean()) if (~right).any() else None,
            "auroc": auroc(conf, right),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "fields": list(self.tally()), "fields_fi": list(self.tally("fi")),
            "strict": [sum(self.strict), len(self.strict)], "strict_fi": [sum(self.strict_fi), len(self.strict_fi)],
            "per_field": {f: list(self.per_field(f)) for f in FIELDS if f in self.fields},
            "calibration": self.calibration(),
            "ms_median": float(np.median(self.ms)) if self.ms else None,
            "reflexes": self.reflexes,
            "misses": self.misses,
        }


def fires_reflex(route) -> bool:
    """Would this reading fire a bare reflex (actions.Actor.reflex_for, with [actions]' defaults)?"""
    from .actions import carries_argument
    from .config import ActionsConfig

    actions = ActionsConfig()
    return (route.decision == "act" and route.topic == "music" and route.tool not in ("", "other")
            and route.tool_confidence >= actions.reflex and route.has_argument < actions.argument
            and not carries_argument(route.text, route.tool))


def auroc(scores: np.ndarray, labels: np.ndarray) -> float | None:
    """The probability that a right answer's confidence is above a wrong one's (ties count half)."""
    pos, neg = scores[labels], scores[~labels]
    if len(pos) == 0 or len(neg) == 0:
        return None
    order = np.argsort(scores, kind="mergesort")
    ranks = np.empty(len(scores))
    sorted_scores = scores[order]
    i = 0
    while i < len(scores):
        j = i
        while j + 1 < len(scores) and sorted_scores[j + 1] == sorted_scores[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j + 2) / 2.0
        i = j + 1
    return float((ranks[labels].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def load_set(path: Path) -> tuple[list[dict], list[dict]]:
    """(phrases, notifications) of a held-out file or of scripts/gate_phrases.json (phrases only)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("phrases", []), data.get("notifications", [])


async def run(gate, name: str, phrases: list[dict], notifications: list[dict]) -> Result:
    """Route every phrase and check every notification on a started gate."""
    from .privacy import gate_state

    result = Result(name)
    for case in phrases:
        started = time.perf_counter()
        route = await gate.route(case["text"])
        result.ms.append((time.perf_counter() - started) * 1000)
        finnish = case.get("lang") == "fi"
        if route is None:
            result.strict.append(False)
            result.misses.append((case["text"], ["gate error"]))
            continue
        answers = route.answers
        wrong: list[str] = []
        got = {"kind": answers["kind"], "topic": answers["topic"], "tool": answers["music_tool"]}
        for f in ("kind", "topic", "tool"):
            if f in case:
                right = got[f].choice == case[f]
                result.add(f, right, got[f].confidence, finnish)
                if not right:
                    wrong.append(f"{f}={got[f].choice}")
        if "decision" in case:
            right = route.decision == case["decision"]
            result.add("decision", right, answers["kind"].confidence, finnish)
            if not right:
                wrong.append(f"decision={route.decision}")
        for f, q in NOUL_FIELDS.items():
            if f in case:
                p = answers[q].score or 0.0
                right = (p >= 0.5) == case[f]
                result.add(f, right, answers[q].confidence, finnish)
                if not right:
                    wrong.append(f"{f} p={p:.2f}")
        strict = route.kind == case["kind"]
        if "topic" in case:
            strict &= route.topic == case["topic"]
        if "decision" in case:
            strict &= route.decision == case["decision"]
        if "tool" in case:
            strict &= route.tool == case["tool"]
        if "catalogue" in case:
            strict &= (route.catalogue >= 0.5) == case["catalogue"]
        result.strict.append(strict)
        if finnish:
            result.strict_fi.append(strict)
        if fires_reflex(route):
            wrong_reflex = (case["kind"] not in ("request", "question") or case.get("topic", "music") != "music"
                            or case.get("tool", route.tool) != route.tool)
            result.reflexes.append((case["text"], route.tool, not wrong_reflex))
        if wrong:
            result.misses.append((case["text"], wrong))
    from .systemone import IS_SENSITIVE

    for case in notifications:
        answers = await gate.systemone.ask(gate_state(case.get("title", ""), case["body"]), IS_SENSITIVE)
        answer = answers[IS_SENSITIVE.name]
        p = answer.score or 0.0
        right = (p >= 0.5) == case["sensitive"]
        result.add("sensitive", right, answer.confidence, case.get("lang") == "fi")
        if not right:
            result.misses.append((f"{case.get('app', '')}: {case['body']}", [f"sensitive p={p:.2f}"]))
    return result


def pct(pair: tuple[int, int] | list[int]) -> str:
    right, n = pair
    return f"{right}/{n} ({100 * right / n:.0f}%)" if n else "-"


def table(results: list[Result]) -> str:
    """The comparison, as markdown: overall with calibration and the reflexes it would fire (and how
    many of those are wrong: not a request, not music, or another tool), then per field."""
    lines = ["| scorer | fields | strict | Finnish fields | Finnish strict | conf right | conf wrong | AUROC "
             "| reflexes (wrong) | median ms |",
             "|---|---|---|---|---|---|---|---|---|---|"]

    def fmt(x: float | None) -> str:
        return "-" if x is None else f"{x:.3f}"

    for r in results:
        c = r.calibration()
        ms = f"{np.median(r.ms):.1f}" if r.ms else "-"
        lines.append(f"| {r.name} | {pct(r.tally())} | {pct((sum(r.strict), len(r.strict)))} | {pct(r.tally('fi'))} | "
                     f"{pct((sum(r.strict_fi), len(r.strict_fi)))} | {fmt(c['conf_right'])} | {fmt(c['conf_wrong'])} | "
                     f"{fmt(c['auroc'])} | {len(r.reflexes)} ({sum(not ok for _, _, ok in r.reflexes)}) | {ms} |")
    present = [f for f in FIELDS if any(f in r.fields for r in results)]
    lines += ["", "| scorer | " + " | ".join(present) + " |", "|---|" + "---|" * len(present)]
    for r in results:
        lines.append(f"| {r.name} | " + " | ".join(pct(r.per_field(f)) for f in present) + " |")
    return "\n".join(lines)
