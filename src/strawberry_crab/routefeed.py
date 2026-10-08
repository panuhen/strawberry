"""The router as it happens, for the Brain UI's live view (WIRING.md §17): each sentence's route,
what handled it and how long it took, in memory only.

    feed = RouteFeed(config.learning)
    live = feed.start(source)            # handle_voice: a sentence arrives
    feed.routed(live, text, route)       # the gate read it
    feed.acted(live, "reflex", ok, ...)  # reflex | thinker | no_catalogue | chat
    feed.done(live)                      # published to the list and to every open stream

An entry holds the gate's reading (kind, topic, decision, tool, confidences, the gate's ms), the
path, the tool calls by name and the whole sentence's ms. The sentence itself is kept only where
the outcome log would keep it ([learning] log_outcomes on, not private, not kind=other): off, the
UI shows the routes without their words. Nothing here logs, and nothing is written to disk.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from typing import Any, Iterable

from .config import LearningConfig

KEEP = 100          # entries kept for a page that opens later
QUEUE = 64          # entries a slow stream may lag behind before it misses some


class RouteFeed:
    def __init__(self, config: LearningConfig) -> None:
        self.config = config
        self.entries: deque[dict[str, Any]] = deque(maxlen=KEEP)
        self.subscribers: set[asyncio.Queue] = set()
        self.ids = itertools.count(1)

    def start(self, source: str) -> dict[str, Any]:
        return {"source": source, "at": time.time(), "t0": time.monotonic(), "path": "", "route": None}

    def routed(self, live: dict[str, Any], text: str, route) -> None:
        from .outcomes import private

        if route is None:
            live["path"] = "unrouted"
            return
        live["route"] = {
            "kind": route.kind, "topic": route.topic, "decision": route.decision, "tool": route.tool,
            "confidence": round(route.confidence, 4), "tool_confidence": round(route.tool_confidence, 4),
            "has_argument": round(route.has_argument, 4), "catalogue": round(route.catalogue, 4),
            "library_change": round(route.library_change, 4), "is_about_her": round(route.is_about_her, 4),
            "gate_ms": round(route.ms, 1),
        }
        if self.config.log_outcomes and route.kind != "other" and not private(text, route):
            live["text"] = text

    def acted(self, live: dict[str, Any], path: str, ok: bool, reflex: str = "", calls: Iterable[Any] = ()) -> None:
        live["path"], live["ok"] = path, ok
        if reflex:
            live["reflex"] = reflex
        live["calls"] = [{"server": c.server, "name": c.name, "ok": c.ok} for c in calls]

    def done(self, live: dict[str, Any]) -> dict[str, Any]:
        entry = {k: v for k, v in live.items() if k != "t0"}
        entry["id"] = next(self.ids)
        entry["ms"] = round((time.monotonic() - live["t0"]) * 1000.0, 1)
        entry["path"] = entry["path"] or "answer"      # a yes or no to her question: no route asked
        entry["at"] = round(entry["at"], 3)
        self.entries.append(entry)
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(entry)
            except asyncio.QueueFull:
                pass        # a stream that fell behind misses this one; the page reloads the list
        return entry

    def recent(self, with_text: bool) -> list[dict[str, Any]]:
        return [view(e, with_text) for e in self.entries]

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(QUEUE)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)


def view(entry: dict[str, Any], with_text: bool) -> dict[str, Any]:
    """An entry for the page: the sentence only while outcome logging is on."""
    return entry if with_text else {k: v for k, v in entry.items() if k != "text"}
