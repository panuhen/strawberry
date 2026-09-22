#!/usr/bin/env python3
"""Try candidate reaction models against real-shaped events (WIRING.md §3).

For each model: one warm-up call, then every event through the same persona prompt with a
forced JSON schema. Reports warm latency, schema compliance, and the lines themselves so
the choice can be made by reading them, not by benchmark.

    scripts/reactor_bakeoff.py gemma3:1b [qwen3.5:0.8b ...] [--runs 1] [--ollama http://127.0.0.1:11434]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request

EMOTIONS = ["neutral", "happy", "alert", "angry"]

PERSONA = (
    "You are Strawberry, a small cheerful cartoon crab who lives on the user's desktop and watches what "
    "happens on the computer. Something just happened. React with ONE short sentence, at most 15 words, "
    "in your own voice: playful, warm, a little cheeky, never mean, no emojis. Do not repeat the event "
    "text word for word; react to it. Pick the emotion that fits: neutral, happy, alert (something needs "
    "attention), or angry (something went wrong). Answer only with JSON."
)

# Small models copy a register far better than they follow a description of one.
EXAMPLES = [
    (
        "source: git\napp: post-commit\ntitle: lighthouse\nbody: Fix flaky login test",
        {"line": "A fix! Did that wobbly login test finally stop wiggling?", "emotion": "happy"},
    ),
    (
        "source: notification\napp: Power\ntitle: Battery critically low\nbody: 5% remaining\nurgency: critical",
        {"line": "Five percent is not a lifestyle. Plug in, please!", "emotion": "alert"},
    ),
    (
        "source: media\napp: Spotify\ntitle: Nina Simone — Feeling Good",
        {"line": "Nina Simone? Claws up, we are dancing.", "emotion": "happy"},
    ),
    (
        "source: notification\napp: GitHub\ntitle: CI failed on main\nbody: 3 tests failed",
        {"line": "Three tests down on main. Somebody is getting pinched.", "emotion": "angry"},
    ),
    (
        "source: notification\napp: Software Updater\ntitle: Updates available\nbody: 17 packages\nurgency: low",
        {"line": "Seventeen updates waiting. They can keep waiting, I am comfy here.", "emotion": "neutral"},
    ),
]

SCHEMA = {
    "type": "object",
    "properties": {"line": {"type": "string"}, "emotion": {"type": "string", "enum": EMOTIONS}},
    "required": ["line", "emotion"],
}

# (source, app, title, body, urgency) — the same shape /event receives.
EVENTS = [
    ("git", "post-commit", "strawberry", "Fix reconnect: daemon closes widget sockets on shutdown", "normal"),
    ("git", "post-commit", "lighthouse", "WIP do not merge", "normal"),
    ("git", "post-commit", "dotfiles", "Remove 400 lines of dead config", "normal"),
    ("git", "pre-push", "strawberry", "pushing 3 commits on main to origin", "normal"),
    ("git", "post-commit", "rapu-web", "Bump dependencies", "normal"),
    ("media", "Spotify", "Daft Punk — Around the World", "", "normal"),
    ("media", "Spotify", "Sibelius — Finlandia", "", "normal"),
    ("media", "Firefox", "lofi hip hop radio - beats to relax/study to", "", "normal"),
    ("notification", "Power", "Battery critically low", "5% remaining, plug in now", "critical"),
    ("notification", "Calendar", "Standup in 5 minutes", "Daily sync with the team", "normal"),
    ("notification", "WhatsApp", "Mum", "Are you coming for dinner on Sunday?", "normal"),
    ("notification", "GitHub", "CI failed on main", "strawberry: 3 tests failed", "normal"),
    ("notification", "GitHub", "CI passed", "lighthouse: all 212 tests green", "low"),
    ("notification", "Slack", "#incidents", "prod API latency p99 above 4s", "critical"),
    ("notification", "Software Updater", "Updates available", "17 packages can be upgraded", "low"),
    ("voice", "", "", "skip this track please", "normal"),
]


def describe(source: str, app: str, title: str, body: str, urgency: str) -> str:
    parts = [f"source: {source}"]
    if app:
        parts.append(f"app: {app}")
    if title:
        parts.append(f"title: {title}")
    if body:
        parts.append(f"body: {body}")
    if urgency != "normal":
        parts.append(f"urgency: {urgency}")
    return "\n".join(parts)


def messages(event_text: str, fewshot: bool) -> list[dict]:
    out = [{"role": "system", "content": PERSONA}]
    if fewshot:
        for user, reply in EXAMPLES:
            out.append({"role": "user", "content": user})
            out.append({"role": "assistant", "content": json.dumps(reply)})
    out.append({"role": "user", "content": event_text})
    return out


def ask(ollama: str, model: str, event_text: str, fewshot: bool, timeout: float = 30.0) -> tuple[dict | None, float, str]:
    payload = {
        "model": model,
        "messages": messages(event_text, fewshot),
        "format": SCHEMA,
        "think": False,
        "stream": False,
        "keep_alive": "10m",
        "options": {"temperature": 0.8, "num_predict": 60},
    }
    request = urllib.request.Request(
        f"{ollama}/api/chat", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            reply = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return None, time.perf_counter() - start, f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:200]}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return None, time.perf_counter() - start, str(exc)
    elapsed = time.perf_counter() - start
    content = reply.get("message", {}).get("content", "")
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None, elapsed, f"not JSON: {content[:120]!r}"
    if not isinstance(data, dict) or not isinstance(data.get("line"), str) or data.get("emotion") not in EMOTIONS:
        return None, elapsed, f"schema miss: {content[:120]!r}"
    return data, elapsed, ""


def run_model(ollama: str, model: str, runs: int, fewshot: bool) -> dict:
    print(f"\n=== {model}  ({'few-shot' if fewshot else 'plain'} prompt) ===")
    _, warm, err = ask(ollama, model, describe(*EVENTS[0]), fewshot, timeout=120.0)
    print(f"warm-up (includes load): {warm:.2f}s" + (f"  [{err}]" if err else ""))
    latencies: list[float] = []
    misses = 0
    words: list[int] = []
    for _ in range(runs):
        for event in EVENTS:
            data, elapsed, err = ask(ollama, model, describe(*event), fewshot)
            latencies.append(elapsed)
            label = f"{event[0]:<12} {(event[2] or event[3])[:38]:<38}"
            if data is None:
                misses += 1
                print(f"  {label} {elapsed:5.2f}s  MISS {err}")
                continue
            words.append(len(data["line"].split()))
            print(f"  {label} {elapsed:5.2f}s  [{data['emotion']:<7}] {data['line']}")
    total = len(latencies)
    summary = {
        "model": model,
        "prompt": "few-shot" if fewshot else "plain",
        "calls": total,
        "schema_misses": misses,
        "latency_median_s": round(statistics.median(latencies), 3),
        "latency_p95_s": round(sorted(latencies)[int(0.95 * (total - 1))], 3),
        "words_median": statistics.median(words) if words else None,
        "words_max": max(words) if words else None,
    }
    print(f"  -> median {summary['latency_median_s']}s, p95 {summary['latency_p95_s']}s, "
          f"misses {misses}/{total}, words median {summary['words_median']} max {summary['words_max']}")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("models", nargs="+")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--ollama", default="http://127.0.0.1:11434")
    parser.add_argument("--json", help="write summaries here")
    parser.add_argument("--plain", action="store_true", help="persona description only, no example exchanges")
    args = parser.parse_args()
    summaries = [run_model(args.ollama, model, args.runs, not args.plain) for model in args.models]
    if args.json:
        with open(args.json, "w") as handle:
            json.dump({"persona": PERSONA, "events": len(EVENTS), "models": summaries}, handle, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
