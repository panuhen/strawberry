#!/usr/bin/env python3
"""Score the gate on its held-out set (src/strawberry_crab/data/gate_heldout.json, WIRING.md §8a).

Per field, strict per sentence, the Finnish subset, and the confidence on right against wrong answers
with its AUROC, for the nearest examples and the trained head side by side (gateeval.py has the rules).
Unlike scripts/gate_check.py this is a measurement, not a contract: it exits 0 unless the gate fails.

    scripts/gate_heldout_check.py [--config FILE] [--scorer both|head|nearest] [--head FILE] [--set FILE]
                                  [--misses] [--json FILE]

Run from the repo root; uses the daemon's uv environment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from strawberry_crab import gateeval, gatehead  # noqa: E402
from strawberry_crab.config import default_path, load  # noqa: E402
from strawberry_crab.gatecmd import GateCommandError, evaluate  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--scorer", choices=("both", "head", "nearest"), default="both")
    parser.add_argument("--head", default="", help="a head file instead of the one in use")
    parser.add_argument("--set", type=Path, default=gatehead.HELDOUT_SET)
    parser.add_argument("--misses", action="store_true", help="print every sentence with a wrong field")
    parser.add_argument("--json", type=Path, default=None, help="write the full results here")
    args = parser.parse_args()

    config = load(args.config or default_path())
    scorers = ("nearest", "head") if args.scorer == "both" else (args.scorer,)
    try:
        results = [await evaluate(config, s, args.head if s == "head" else "", args.set) for s in scorers]
    except (GateCommandError, gatehead.HeadError) as exc:
        print(exc, file=sys.stderr)
        return 2
    phrases, notifications = gateeval.load_set(args.set)
    print(f"{args.set}: {len(phrases)} phrases ({sum(p.get('lang') == 'fi' for p in phrases)} Finnish), "
          f"{len(notifications)} notifications\n")
    print(gateeval.table(results))
    if args.misses:
        for r in results:
            print(f"\n{r.name}: {len(r.misses)} sentence(s) with a miss")
            for text, wrong in r.misses:
                print(f"  {text!r}: {', '.join(wrong)}")
    if args.json:
        args.json.write_text(json.dumps({r.name: r.to_dict() for r in results}, indent=1, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
