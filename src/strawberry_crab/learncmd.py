"""`strawberry learning ...`: the router's learning loop by hand (WIRING.md §8d, learning.py).

    strawberry learning status              what is in use, the candidate waiting, the labels
    strawberry learning train               build a candidate now and score it on the held-out set
    strawberry learning accept | reject     the candidate waiting
    strawberry learning rollback            back to the head in use before this one
    strawberry learning versions            every head, with its held-out score and what became of it
    strawberry learning report [--days N]   the weekly summary
    strawberry learning examples [--text]   the labelled sentences (--text shows them, here only)
    strawberry learning review KEY approve|reject
    strawberry learning forget [--all]      delete what was learned (--all: and the heads it made)

The daemon need not run. A running daemon follows `current` within a few seconds, so accept and
rollback need no restart.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

from . import gatecmd, gatehead
from .learning import Learning, LearningBusy, LearningError, pct


def _when(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def _score(before: float | None, after: float | None) -> str:
    fmt = lambda x: "-" if x is None else f"{x:.1f}%"  # noqa: E731
    return f"{fmt(before)} → {fmt(after)}"


def _tally(counts: dict[str, int]) -> str:
    return ", ".join(f"{k} {n}" for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))) or "none"


def status(loop: Learning, out=print) -> int:
    s = loop.status()
    use = s["in_use"]
    out(f"in use     {use['version']} ({use['source'] or 'none'})" + (f"  NOTE: {use['note']}" if use["note"] else ""))
    c = s["candidate"]
    if c:
        out(f"candidate  {c['version']}: held-out fields {_score(c['heldout']['current'], c['heldout']['candidate'])}, "
            f"strict {_score(c['heldout']['strict_current'], c['heldout']['strict_candidate'])}; "
            f"{c['learned']} learned sentence(s)")
        out("           strawberry learning accept   (or reject)")
    else:
        out("candidate  none waiting")
    e = s["examples"]
    out(f"examples   {e['usable']} labelled, {e['new']} new since the last run, {e['conflicts']} with a conflict; "
        f"signals: {_tally(e['signals'])}")
    if e["gone"]:
        out(f"           taken out for good: {_tally(e['gone'])}")
    last = s["last_train"]
    if last:
        line = f"last run   {_when(last.get('at'))} ({last.get('trigger')}): {last.get('result')}"
        if last.get("heldout"):
            line += f", held-out {_score(*last['heldout'])}"
        if last.get("why"):
            line += f" ({last['why']})"
        out(line)
    out(f"rollback   {'to ' + s['rollback_to'] if s['rollback_to'] else 'nothing to roll back to'}")
    st = s["settings"]
    logging_state = "on" if s["logging"] else "off ([learning] log_outcomes = false: nothing new is learned)"
    out(f"settings   outcome logging {logging_state}; idle training {'on' if st['idle_train'] else 'off'} "
        f"(after {st['idle_minutes']:g} min quiet, {st['min_new_labels']} new labels); auto_switch "
        f"{'on' if st['auto_switch'] else 'off'}; weekly line {'on' if st['weekly_line'] else 'off'}")
    return 0


async def train(loop: Learning, config, out=print) -> int:
    gate = await gatecmd._gate(config, scorer="nearest")
    try:
        result = await loop.train(gate, progress=out, trigger="by hand")
    finally:
        await gate.close()
    if not result.get("trained"):
        out(f"no candidate: {result.get('why')}")
        return 0
    held = result["heldout"]
    out(f"candidate {result['version']} against {result.get('compared_with')}: held-out fields "
        f"{_score(pct(held['current']['fields']), pct(held['candidate']['fields']))}, strict "
        f"{_score(pct(held['current']['strict']), pct(held['candidate']['strict']))}, privacy "
        f"{held['candidate']['sensitive'][0]}/{held['candidate']['sensitive'][1]}, wrong reflexes "
        f"{held['current']['reflexes_wrong']} → {held['candidate']['reflexes_wrong']}")
    if result.get("phrases"):
        p = result["phrases"]
        out(f"gate_phrases.json (secondary, not gated): {_score(pct(p['current']['fields']), pct(p['candidate']['fields']))}")
    counts = result.get("counts") or {}
    out(f"trained on {counts.get('base')} + {counts.get('learned')} sentence(s); left out: "
        f"{_tally((counts.get('left_out') or {}))}")
    if result["status"] == "rejected":
        out(f"rejected: {result.get('why')}")
    elif result.get("switched"):
        out("in use now (auto_switch); a running daemon switches within a few seconds")
    else:
        out("waits for you: strawberry learning accept   (or reject)")
    return 0


def versions(loop: Learning, out=print) -> int:
    for row in loop.versions():
        mark = "*" if row["in_use"] else " "
        held = "-" if row.get("heldout") is None else f"{row['heldout']:.1f}%"
        extra = f", {row['learned']} learned, parent {row.get('parent')}" if "learned" in row else ""
        why = f" ({row['why']})" if row.get("why") else ""
        out(f"{mark} {row['version']:32} {row['status'] or '-':11} held-out {held}{extra}{why}")
    return 0


def report(loop: Learning, days: float, out=print) -> int:
    r = loop.report(days)
    out(f"last {days:g} days: {loop.report_line(r)}")
    out(f"  signals     {_tally(r['signals'])}")
    out(f"  heads       {r['candidates']} candidate(s) built, {r['accepted']} put in use, {r['heads_rejected']} rejected, "
        f"{r['rollbacks']} rollback(s)")
    out(f"  in use      {r['head_before']} → {r['head_now']}")
    return 0


def examples(loop: Learning, text: bool, out=print) -> int:
    rows = loop.examples()
    out(f"{loop.store.path}: {len(rows)} example(s)")
    for e in rows:
        what = ", ".join([f"{q}={o}" for q, o in e.labels.items()] + [f"{q}!={o}" for q, o in e.avoid.items()]) or "-"
        if e.conflicts:
            what += f"  conflict: {', '.join(e.conflicts)}"
        line = f"  {e.key}  {_when(e.last)}  {_tally(e.signals):24} {what}"
        out(line + (f"  {e.text!r}" if text else ""))
    return 0


def main(args, config) -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    loop = Learning(config)
    command = args.learning_command
    try:
        if command == "status":
            return status(loop)
        if command == "train":
            return asyncio.run(train(loop, config))
        if command == "accept":
            switched = loop.accept(args.version)
            print(f"{switched['to']} is in use (was {switched['from']}); a running daemon switches within a few seconds")
            return 0
        if command == "reject":
            print(f"{loop.reject(args.version)} rejected; the head in use stays")
            return 0
        if command == "rollback":
            switched = loop.rollback()
            print(f"rolled back: {switched['to']} is in use (was {switched['from']}); a running daemon switches "
                  "within a few seconds")
            return 0
        if command == "versions":
            return versions(loop)
        if command == "report":
            return report(loop, args.days)
        if command == "examples":
            return examples(loop, args.text)
        if command == "review":
            if not loop.review(args.key, args.verdict):
                print(f"no example {args.key!r}", file=sys.stderr)
                return 1
            print(f"{args.key}: {'approved' if args.verdict == 'approve' else 'rejected, its sentence deleted'}")
            return 0
        if command == "forget":
            done = loop.forget(everything=args.all)
            print(f"forgot {done['examples']} example(s)" + (f" and {done['heads']} head(s)" if args.all else "")
                  + "; outcome records written before now are ignored")
            return 0
    except LearningBusy as exc:
        print(f"strawberry learning: {exc}", file=sys.stderr)
        return 1
    except (LearningError, gatecmd.GateCommandError, gatehead.HeadError) as exc:
        print(f"strawberry learning: {exc}", file=sys.stderr)
        return 1
    raise SystemExit(f"unknown learning command {command!r}")
