#!/usr/bin/env python3
"""Run the sensitive-body filter over scripts/sensitive_phrases.json against live Ollama (WIRING.md §4).

The filter is what the daemon runs before a notification body reaches the model: the patterns
(src/strawberry_crab/privacy.py), then IS_SENSITIVE on the gate. Prints every miss, then the tally for the
whole filter and for the gate alone, and the gate's latency. Exit code 1 on any miss of the filter.

    scripts/sensitive_check.py [--config ~/.config/strawberry/config.toml] [--verbose]

Run from the repo root; uses the daemon's uv environment. Builds the gate the way the daemon does.
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
sys.path.insert(0, str(ROOT / "src"))

from strawberry_crab import privacy  # noqa: E402
from strawberry_crab.adapters import gate_examples, load as load_adapters  # noqa: E402
from strawberry_crab.config import default_path, load  # noqa: E402
from strawberry_crab.systemone import Gate  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--phrases", type=Path, default=ROOT / "scripts" / "sensitive_phrases.json")
    parser.add_argument("--verbose", action="store_true", help="print every notification, not just the misses")
    args = parser.parse_args()

    config = load(args.config or default_path())
    cases = json.loads(args.phrases.read_text(encoding="utf-8"))["phrases"]
    adapters = load_adapters(config.tools.servers) if config.tools.enabled else {}
    gate = Gate(replace(config.gate, enabled=True), config.brain.ollama_url, examples=gate_examples(adapters))
    await gate.start()
    if not gate.ready:
        print(f"gate not ready: {gate.disabled_reason}", file=sys.stderr)
        await gate.close()
        return 2
    misses = gate_misses = 0
    latencies: list[float] = []
    try:
        for case in cases:
            want = case["sensitive"]
            verdict = await privacy.check(gate, case.get("title", ""), case["body"])
            # The gate's own answer too, even where a pattern already decided: how much it carries alone.
            reading = await gate.sensitive(privacy.gate_state(case.get("title", ""), case["body"]))
            if reading is None:
                print(f"FAIL  {case['app']}: gate error")
                misses += 1
                continue
            p, ms = reading
            latencies.append(ms)
            gate_says = p >= privacy.SENSITIVE_P
            gate_misses += gate_says != want
            label = f"{case['app']} / {case.get('title', '')}: {case['body'][:70]!r}"
            if verdict.sensitive != want:
                misses += 1
                print(f"MISS  {label}: {verdict.reason} (wanted {'sensitive' if want else 'clear'}), gate p {p:.2f}")
            elif args.verbose:
                print(f"ok    {label}: {verdict.reason}, gate p {p:.2f}{'' if gate_says == want else '  (gate alone wrong)'}")
    finally:
        await gate.close()
    n = len(cases)
    print(f"\nfilter {n - misses}/{n} right; gate alone {n - gate_misses}/{n} (p >= {privacy.SENSITIVE_P}); "
          f"gate {statistics.median(latencies):.0f} ms median, {max(latencies):.0f} ms max per body ({config.gate.model})")
    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
