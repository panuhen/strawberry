"""The thinker (WIRING.md §8b): the big model with a topic's tools, for what the reflexes can't.

A reflex handles "skip this". This handles "play some Nina Simone", "queue up the live version
of this", "what year did this come out": sentences that carry an argument the embedding cannot
extract, or that take several steps. Qwen gets the sentence and only the topic's tools; it calls
tools until it has an answer, then returns one plain factual sentence. That sentence is the
fact she says; the reaction path adds her quip after it (actions.Outcome, Daemon.report).

Qwen does not speak to the user and does not get her persona: it is the hands, not the voice.
`think` is off by default (Ollama accepts false, "low", "medium", true = xhigh): the gate has
already done the routing and these are gut-check tool choices. Every call is a fresh
conversation bounded by `max_rounds` tool rounds, truncated tool results (tools.result_chars),
`num_ctx`, and one overall timeout; nothing accumulates across requests.

Measured on the RTX 3090 (qwen3.8:27b Q4_K_M): cold load 7–17 s, a warm round ~2 s.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable

import aiohttp

from .actions import Outcome
from .config import ThinkerConfig
from .tools import Toolbox, ToolResult

log = logging.getLogger("strawberryd.thinker")

Chat = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]   # payload -> Ollama /api/chat reply

SYSTEM = (
    "You are the hands of Strawberry, a small assistant living on the user's desktop. The user just said "
    "the sentence below out loud (speech-to-text, so a name may be misheard: 'Dove Punk' was Daft Punk). Do "
    "what they asked using the tools. Be decisive: call tools rather than asking questions; if a search returns "
    "several matches, pick the most likely one. If a name in the sentence resembles one of the user's library names "
    "given below, or a well-known artist or track, assume that is what they said and search for that. Prefer a "
    "well-known interpretation of an odd phrase over a literal search of it. Never play a result just because its "
    "title happens to contain the misheard words; when nothing well-known fits, do nothing and say what you heard. "
    "Do only what was asked: 'play' means play, never save, like, remove or change a playlist unless told to. "
    "A question about the music (who this is, tell me about the artist, what else they made) is answered from "
    "your own knowledge plus the situation, in two or three plain sentences; tools are for the player, not for facts. 'This song', 'this', 'it' mean whatever is playing now (given "
    "below when known; otherwise look it up first). When done, answer with ONE plain factual sentence for the "
    "user, in plain English, stating what you did and the result (name the track, artist, number). No "
    "preamble, no markdown, no questions. If it cannot be done, say so in one sentence and why."
)


KNOWLEDGE = (
    "You are the hands of Strawberry, a small assistant living on the user's desktop. The user just asked the "
    "question below out loud (speech-to-text, so a word may be misheard). Answer from your own knowledge in one to "
    "three plain sentences (at most 60 words), in plain English, with the fact itself (a name, a number, a date). No preamble, no markdown, "
    "no follow-up question. You have no tools and no internet: if the answer depends on recent events or on "
    "something you cannot know, say so in one sentence instead of guessing."
)


class ThinkerError(RuntimeError):
    pass


class Thinker:
    def __init__(self, config: ThinkerConfig, toolbox: Toolbox, model: str, ollama_url: str = "",
                 chat: Chat | None = None) -> None:
        self.config = config
        self.toolbox = toolbox
        self.model = config.model or model
        self.ollama_url = ollama_url
        self.chat = chat or self._ollama_chat
        self.session: aiohttp.ClientSession | None = None
        self.calls = 0
        self.failures = 0
        self.last: dict[str, Any] | None = None
        self.last_s: float | None = None

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    async def start(self) -> None:
        if self.config.enabled and self.chat == self._ollama_chat:
            self.session = aiohttp.ClientSession(base_url=self.ollama_url)

    async def close(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def _ollama_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.session:
            raise ThinkerError("thinker not started")
        try:
            async with self.session.post("/api/chat", json=payload,
                                         timeout=aiohttp.ClientTimeout(total=self.config.timeout_s)) as response:
                if response.status != 200:
                    raise ThinkerError(f"HTTP {response.status}: {(await response.text())[:200]}")
                return await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ThinkerError(str(exc) or type(exc).__name__) from exc

    def can_handle(self, topic: str) -> bool:
        return self.config.enabled and topic in self.toolbox.topics()

    async def answer(self, text: str, context: str = "") -> Outcome:
        """A question with no tools for its topic: Qwen answers from what it knows, one sentence."""
        self.calls += 1
        started = time.perf_counter()
        user = text if not context else f"Situation: {context}\n\nThe user asks: {text}"
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": KNOWLEDGE}, {"role": "user", "content": user}],
            "think": self.config.think,
            "stream": False,
            "keep_alive": self.config.keep_alive,
            "options": {"num_ctx": self.config.num_ctx, "num_predict": self.config.num_predict, "temperature": 0.2},
        }
        try:
            reply = await asyncio.wait_for(self.chat(payload), self.config.timeout_s)
            content = ((reply.get("message") or {}).get("content") or "").strip()
            outcome = Outcome("answered from memory", tidy_sentence(content), True) if content else \
                Outcome("tried to answer", "I got lost thinking about that, sorry.", False)
        except asyncio.TimeoutError:
            outcome = Outcome("thought about it too long", "I tried, but my thinking took too long. Sorry.", False)
        except ThinkerError as exc:
            log.warning("thinker: %s", exc)
            outcome = Outcome("tried to think", "I tried, but my thinking part is not answering.", False)
        self.last_s = time.perf_counter() - started
        if not outcome.ok:
            self.failures += 1
        self.last = {"asked": text, "topic": "knowledge", "did": outcome.did, "fact": outcome.fact, "ok": outcome.ok,
                     "s": round(self.last_s, 2), "calls": []}
        log.info("thinker: %r (knowledge) -> %r in %.1fs", text, outcome.fact, self.last_s)
        return outcome

    async def run(self, text: str, topic: str, context: str = "", careful: bool = False) -> Outcome:
        """One request, start to finish. Never raises: a failure is an Outcome with ok=False.
        `careful`: offer the servers' careful tools too (the sentence asked for such a change)."""
        self.calls += 1
        started = time.perf_counter()
        calls: list[ToolResult] = []
        try:
            outcome = await asyncio.wait_for(self._run(text, topic, context, calls, careful), self.config.timeout_s)
        except asyncio.TimeoutError:
            outcome = Outcome("thought about it too long", "I tried, but my thinking took too long. Sorry.", False, tuple(calls))
        except ThinkerError as exc:
            log.warning("thinker: %s", exc)
            outcome = Outcome("tried to think", "I tried, but my thinking part is not answering.", False, tuple(calls))
        self.last_s = time.perf_counter() - started
        if not outcome.ok:
            self.failures += 1
        self.last = {
            "asked": text, "topic": topic, "did": outcome.did, "fact": outcome.fact, "ok": outcome.ok,
            "s": round(self.last_s, 2), "calls": [c.to_dict() | {"text": c.text[:200]} for c in outcome.calls],
        }
        log.info("thinker: %r (%s) -> %s -> %r in %.1fs", text, topic, outcome.did, outcome.fact, self.last_s)
        return outcome

    async def _run(self, text: str, topic: str, context: str, calls: list[ToolResult], careful: bool) -> Outcome:
        specs = await self.toolbox.tools_for(topic, careful=careful)
        if not specs:
            return Outcome("looked for tools", f"I have nothing to do that with; no {topic} tools are answering.", False)
        tools = [s.for_ollama() for s in specs]
        user = text if not context else f"Situation: {context}\n\nThe user says: {text}"
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
        used: list[str] = []
        for round_no in range(self.config.max_rounds + 1):
            last_round = round_no == self.config.max_rounds
            payload = {
                "model": self.model,
                "messages": messages,
                "think": self.config.think,
                "stream": False,
                "keep_alive": self.config.keep_alive,
                "options": {"num_ctx": self.config.num_ctx, "num_predict": self.config.num_predict, "temperature": 0.2},
            }
            if not last_round:
                payload["tools"] = tools
            else:
                messages.append({"role": "user", "content": "Stop calling tools now. Answer the user in one sentence with what you have."})
            reply = await self.chat(payload)
            message = reply.get("message") or {}
            tool_calls = message.get("tool_calls") or []
            content = (message.get("content") or "").strip()
            if not tool_calls or last_round:
                if not content:
                    return Outcome(_did(used), "I got lost doing that, sorry.", False, tuple(calls))
                return Outcome(_did(used), tidy_sentence(content), True, tuple(calls))
            messages.append(message)
            for call in tool_calls:
                function = call.get("function") or {}
                name = function.get("name", "")
                arguments = function.get("arguments") or {}
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                result = await self.toolbox.call_function(name, arguments)
                calls.append(result)
                used.append(name)
                messages.append({"role": "tool", "tool_name": name, "content": result.text or ("ok" if result.ok else "failed")})
        return Outcome(_did(used), "I got lost doing that, sorry.", False, tuple(calls))  # unreachable

    def stats(self) -> dict[str, Any]:
        return {"enabled": self.config.enabled, "model": self.model if self.config.enabled else None, "calls": self.calls,
                "failures": self.failures, "last_s": round(self.last_s, 2) if self.last_s is not None else None,
                "last": self.last}


def _did(used: list[str]) -> str:
    if not used:
        return "answered without tools"
    names = list(dict.fromkeys(used))
    return "used " + ", ".join(names)


def tidy_sentence(text: str, limit: int = 380) -> str:
    """Qwen's final answer as spoken text: first paragraph, no markdown, capped (speech.max_chars is 400)."""
    line = re.sub(r"[*_`#>]+", "", text.strip().split("\n")[0])
    line = " ".join(line.split())
    if len(line) > limit:
        cut = line[:limit].rsplit(" ", 1)[0]
        line = cut.rstrip(",;:") + "…"
    return line
