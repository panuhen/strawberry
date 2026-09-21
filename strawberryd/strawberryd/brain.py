"""The reaction path (WIRING.md §3): one small always-resident model, one line per event.

No tools, no thinking mode, JSON output forced by schema, a hard timeout, and the canned
reactor as the fallback. If the model is slow, down, or emits nonsense, she still reacts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections import deque
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
    if event.source == "voice":
        # Not something that happened: the user spoke to her. Answer them.
        return f"source: voice (the user is talking to you; reply to them)\nsaid: {event.title or event.body}"
    if event.source == "action":
        # She just did something for the user (or tried). The fact is already said by code; the
        # model adds a short quip after it, nothing else.
        return (f"source: action (you did this for the user and have just said: \"{event.body}\" "
                f"Add ONE short quip to follow it, at most 8 words, no facts, no repetition)\n"
                f"asked: {event.title}\ndid: {event.app}")
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


STOPWORDS = frozenset("""
that this with have from your they them then than what when were will would could should there their
about just like into over some more very much such only also been being does done into onto
""".split())
RECENT_LINES = 8


def content_words(line: str) -> list[str]:
    """Lower-cased words of four letters or more that carry flavour (not stopwords)."""
    return [w for w in re.findall(r"[a-zA-Z']{4,}", line.lower()) if w not in STOPWORDS]


def stale_words(line: str, recent: list[str]) -> list[str]:
    """Words in `line` she has already used in two or more recent lines, plus a repeated opener.

    A small model finds a pet adjective and puts it in every line ("lovely" twelve times in an
    evening). Catching the repeat lets the reactor ask once more with those words banned.
    """
    if not recent:
        return []
    counts: dict[str, int] = {}
    for old in recent:
        for w in set(content_words(old)):
            counts[w] = counts.get(w, 0) + 1
    stale = [w for w in dict.fromkeys(content_words(line)) if counts.get(w, 0) >= 2]
    opener = line.split()[0].lower().strip(",.!?") if line.split() else ""
    if opener and sum(1 for old in recent[-3:] if old.split() and old.split()[0].lower().strip(",.!?") == opener) >= 2:
        stale.append(opener)
    return stale


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
        self.retries = 0
        self.last_latency: float | None = None
        self.loaded = False
        self.recent: deque[str] = deque(maxlen=RECENT_LINES)
        self.rng = random.Random()
        self.rewarm: asyncio.Task | None = None

    def stats(self) -> dict[str, Any]:
        return {
            "model": self.brain.reaction_model,
            "loaded": self.loaded,
            "calls": self.calls,
            "fallbacks": self.fallbacks,
            "retries": self.retries,
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

    def schedule_rewarm(self) -> None:
        """A timeout usually means the model was evicted from VRAM (a big model loaded) and is
        reloading. Ollama aborts a load when the client hangs up, so the short reaction calls
        would keep killing it forever; load it again with the long allowance, in the background."""
        if self.rewarm and not self.rewarm.done():
            return
        self.loaded = False
        self.rewarm = asyncio.get_running_loop().create_task(self.warm_up())

    def _messages(self, event_text: str, avoid: list[str] | None = None) -> list[dict[str, str]]:
        out = [{"role": "system", "content": self.brain.persona}]
        # Example order is shuffled per call: a fixed order makes the last example the template
        # for everything, and the same opener comes back every time.
        examples = list(self.brain.examples)
        self.rng.shuffle(examples)
        for example in examples:
            out.append({"role": "user", "content": example["event"]})
            out.append({"role": "assistant", "content": json.dumps({"line": example["line"], "emotion": example["emotion"]})})
        if avoid:
            event_text += "\n(Say it differently this time. Do not use these words: " + ", ".join(avoid) + ".)"
        out.append({"role": "user", "content": event_text})
        return out

    async def _ask(self, event_text: str, avoid: list[str] | None = None, temperature: float | None = None) -> dict[str, Any]:
        assert self.session
        payload = {
            "model": self.brain.reaction_model,
            "messages": self._messages(event_text, avoid),
            "format": SCHEMA,
            "think": False,
            "stream": False,
            "keep_alive": self.brain.keep_alive,
            "options": {"temperature": temperature if temperature is not None else self.brain.temperature, "num_predict": 80},
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
            if isinstance(exc, asyncio.TimeoutError):
                self.schedule_rewarm()
            return await self.fallback.react(event)
        self.loaded = True
        line = tidy(data["line"], self.brain.max_words)
        emotion = data["emotion"]
        stale = stale_words(line, list(self.recent))
        elapsed = time.perf_counter() - started
        # Repeating herself? One more go with the tired words banned and a hotter sample, if
        # there is time left in the budget. Keep the second line unless it is no better.
        if line and stale and elapsed < self.brain.timeout_s * 0.6:
            self.retries += 1
            try:
                again = await self._ask(describe(event), avoid=stale, temperature=min(self.brain.temperature + 0.3, 1.5))
                second = tidy(again["line"], self.brain.max_words)
                if second and len(stale_words(second, list(self.recent))) < len(stale):
                    log.info("brain: reworded (avoided %s): %r -> %r", stale, line, second)
                    line, emotion = second, again["emotion"]
            except (aiohttp.ClientError, asyncio.TimeoutError, BrainError) as exc:
                log.debug("brain: retry failed (%s); keeping the first line", exc)
        self.last_latency = time.perf_counter() - started
        if not line:
            self.fallbacks += 1
            return await self.fallback.react(event)
        self.recent.append(line)
        log.info("brain: %.2fs [%s] %s", self.last_latency, emotion, line)
        return Performance(state="talking", anim=anim_for(emotion), text=line, emotion=emotion)
