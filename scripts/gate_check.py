#!/usr/bin/env python3
"""Run the gate over scripts/gate_phrases.json against live Ollama (WIRING.md §8a).

Prints every miss with its probabilities, then the tally and the per-sentence latency.
Exit code 1 on any miss, so it can sit in the acceptance run once a model is present.

    scripts/gate_check.py [--config ~/.config/strawberry/config.toml] [--verbose]

Run from the repo root; uses the daemon's uv environment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "strawberryd"))

from strawberryd.config import default_path, load  # noqa: E402
from strawberryd.systemone import Gate  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--phrases", type=Path, default=ROOT / "scripts" / "gate_phrases.json")
    parser.add_argument("--verbose", action="store_true", help="print every sentence, not just the misses")
    args = parser.parse_args()

    config = load(args.config or default_path())
    phrases = json.loads(args.phrases.read_text())["phrases"]
    gate = Gate(replace(config.gate, enabled=True), config.brain.ollama_url)
    await gate.start()
    if not gate.ready:
        print(f"gate not ready: {gate.disabled_reason}", file=sys.stderr)
        return 2
    misses = 0
    latencies = []
    try:
        for case in phrases:
            route = await gate.route(case["text"])
            if route is None:
                print(f"FAIL  {case['text']!r}: gate error")
                misses += 1
                continue
            latencies.append(route.ms)
            wrong = []
            if route.kind != case["kind"]:
                wrong.append(f"kind {route.kind} (wanted {case['kind']})")
            if "topic" in case and route.topic != case["topic"]:
                wrong.append(f"topic {route.topic} (wanted {case['topic']})")
            if "decision" in case and route.decision != case["decision"]:
                wrong.append(f"decision {route.decision} (wanted {case['decision']})")
            if "tool" in case and route.tool != case["tool"]:
                wrong.append(f"tool {route.tool} {route.tool_confidence:.2f} (wanted {case['tool']})")
            probs = {k: round(v, 2) for k, v in route.answers["kind"].probabilities.items()}
            if wrong:
                misses += 1
                print(f"MISS  {case['text']!r}: {', '.join(wrong)}  conf {route.confidence:.2f} {probs}")
            elif args.verbose:
                print(f"ok    {case['text']!r}: {route.kind}/{route.topic} -> {route.decision}  conf {route.confidence:.2f} "
                      f"tool {route.tool or '-'} {route.tool_confidence:.2f} arg {route.has_argument:.2f}")
    finally:
        await gate.close()
    n = len(phrases)
    print(f"\n{n - misses}/{n} right; {statistics.median(latencies):.0f} ms median, {max(latencies):.0f} ms max per sentence "
          f"({config.gate.model}, T={config.gate.temperature}, act>={config.gate.act}, offer>={config.gate.offer})")
    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
