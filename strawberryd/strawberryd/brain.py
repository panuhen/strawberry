"""The reaction path (WIRING.md §3): one small always-resident model, one line per event.

No tools, no thinking mode, JSON output forced by schema, a hard timeout, and the canned
reactor as the fallback. If the model is slow, down, or emits nonsense, she still reacts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import aiohttp

from .config import BrainConfig
from .contract import EMOTIONS, Performance, anim_for
from .events import MAX_LINE, Event, Reactor

log = logging.getLogger("strawberryd.brain")

SCHEMA = {
    "type": "object",
    "properties": {"line": {"type": "string"}, "emotion": {"type": "string", "enum": list(EMOTIONS)}},
    "required": ["line", "emotion"],
}


class BrainError(RuntimeError):
    pass


def describe(event: Event) -> str:
    """The event as the model sees it; same shape as the few-shot examples."""
    parts = [f"source: {event.source}"]
    if event.app:
        parts.append(f"app: {event.app}")
    if event.title:
        parts.append(f"title: {event.title}")
    if event.body:
        parts.append(f"body: {event.body}")
    if event.urgency != "normal":
        parts.append(f"urgency: {event.urgency}")
    return "\n".join(parts)


def tidy(line: str, max_words: int) -> str:
    """One clean sentence: no markdown, collapsed whitespace, no wrapping quotes, length capped gently."""
    line = re.sub(r"[*_`#~]+", "", line)  # a 1B model likes **bold**; the bubble would show the asterisks
    line = re.sub(r"\s+", " ", line).strip().strip('"“”')
    words = line.split()
    if len(words) > max_words + 5:
        line = " ".join(words[:max_words]).rstrip(",;:") + "…"
    return line[:MAX_LINE]


class OllamaReactor:
    def __init__(self, brain: BrainConfig, fallback: Reactor) -> None:
        self.brain = brain
        self.fallback = fallback
        self.session: aiohttp.ClientSession | None = None
        self.calls = 0
        self.fallbacks = 0
        self.last_latency: float | None = None
        self.loaded = False

    def stats(self) -> dict[str, Any]:
        return {
            "model": self.brain.reaction_model,
            "loaded": self.loaded,
            "calls": self.calls,
            "fallbacks": self.fallbacks,
            "last_latency_s": round(self.last_latency, 3) if self.last_latency is not None else None,
        }

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(base_url=self.brain.ollama_url)
        await self.warm_up()

    async def close(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def warm_up(self) -> None:
        """Load the model now, so the first event isn't the one that waits for a cold load."""
        assert self.session
        started = time.perf_counter()
        try:
            async with self.session.post(
                "/api/generate",
                json={"model": self.brain.reaction_model, "keep_alive": self.brain.keep_alive},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as response:
                if response.status != 200:
                    raise BrainError(f"HTTP {response.status}: {(await response.text())[:200]}")
            self.loaded = True
            log.info("brain: %s loaded in %.1fs (keep_alive %s)", self.brain.reaction_model,
                     time.perf_counter() - started, self.brain.keep_alive)
        except (aiohttp.ClientError, asyncio.TimeoutError, BrainError) as exc:
            self.loaded = False
            log.warning("brain: could not load %s (%s); reacting with canned lines until it answers",
                        self.brain.reaction_model, exc)

    def _messages(self, event_text: str) -> list[dict[str, str]]:
        out = [{"role": "system", "content": self.brain.persona}]
        for example in self.brain.examples:
            out.append({"role": "user", "content": example["event"]})
            out.append({"role": "assistant", "content": json.dumps({"line": example["line"], "emotion": example["emotion"]})})
        out.append({"role": "user", "content": event_text})
        return out

    async def _ask(self, event_text: str) -> dict[str, Any]:
        assert self.session
        payload = {
            "model": self.brain.reaction_model,
            "messages": self._messages(event_text),
            "format": SCHEMA,
            "think": False,
            "stream": False,
            "keep_alive": self.brain.keep_alive,
            "options": {"temperature": self.brain.temperature, "num_predict": 80},
        }
        async with self.session.post(
            "/api/chat", json=payload, timeout=aiohttp.ClientTimeout(total=self.brain.timeout_s)
        ) as response:
            if response.status != 200:
                raise BrainError(f"HTTP {response.status}: {(await response.text())[:200]}")
            reply = await response.json()
        content = reply.get("message", {}).get("content", "")
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            raise BrainError(f"not JSON: {content[:120]!r}")
        if not isinstance(data, dict) or not isinstance(data.get("line"), str) or data.get("emotion") not in EMOTIONS:
            raise BrainError(f"schema miss: {content[:120]!r}")
        return data

    async def react(self, event: Event) -> Performance:
        if not self.session:
            return await self.fallback.react(event)
        self.calls += 1
        started = time.perf_counter()
        try:
            data = await self._ask(describe(event))
        except (aiohttp.ClientError, asyncio.TimeoutError, BrainError) as exc:
            self.fallbacks += 1
            log.warning("brain: fallback after %.2fs (%s)", time.perf_counter() - started, exc)
            return await self.fallback.react(event)
        self.last_latency = time.perf_counter() - started
        self.loaded = True
        line = tidy(data["line"], self.brain.max_words)
        if not line:
            self.fallbacks += 1
            return await self.fallback.react(event)
        emotion = data["emotion"]
        log.info("brain: %.2fs [%s] %s", self.last_latency, emotion, line)
        return Performance(state="talking", anim=anim_for(emotion), text=line, emotion=emotion)
