"""The thinker (WIRING.md §8b): Qwen, in her voice, with the tools, for everything the user says.

A reflex ("skip this", "pause", "what song is this") is still done by `actions.py` with no model
in the loop. Everything else the user says lands here in ONE call: small talk, questions, and
requests that carry an argument ("play some Nina Simone", "queue the live version"). Qwen gets
the tools, the situation (what is playing, the library names, the date), the ledger, and *her
persona*, so the line it writes is hers and is performed as it stands — no second model on top.

The reply starts with a mood tag it is asked for, `[happy] Skipped. Blue Monday next.`, which is
parsed off into the performance's `emotion` (missing tag = neutral, a failed tool = alert).

`think` is off by default (Ollama accepts false, "low", "medium", true = xhigh): these are
gut-check tool choices, not puzzles. Every call is a fresh conversation bounded by `max_rounds`
tool rounds, truncated tool results (tools.result_chars), `num_ctx`, and one overall timeout;
nothing accumulates across requests but the ledger.

Measured on a 24 GB RTX 3090 (qwen3.8:27b Q4_K_M): cold load 7-17 s, a warm round ~2 s.
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
from .contract import EMOTIONS
from .tools import Toolbox, ToolResult, ToolSpec

log = logging.getLogger("strawberryd.thinker")

Chat = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]   # payload -> Ollama /api/chat reply

# Who is speaking. The reaction path's persona (persona.py) describes events to a 1B model; this
# is the same crab talking to the user directly, and it is the only voice in a Qwen reply.
VOICE = (
    "You are Strawberry, a small cheerful cartoon crab who lives on the user's desktop. The user is talking to you "
    "now: the sentence below is theirs, heard through speech-to-text. Answer them yourself, as Strawberry. Your "
    "voice: playful, warm, a little cheeky, dry, never mean. British English, British spelling, understated wit, "
    "no American slang, no emojis, no markdown, no lists, no follow-up questions, and never a name for the user - "
    "say 'you' to them. Small talk and confirmations get ONE short sentence of at most 15 words. An answer that "
    "carries facts may run to two or three plain sentences, no more. Vary your wording and never lean on one "
    "favourite adjective.\n"
    "Start every reply with your mood in square brackets - [neutral], [happy], [alert] (something needs attention) "
    "or [angry] (something went wrong) - then a space, then what you say. Example: [happy] Skipped. Blue Monday next."
)

# How to use the tools. Tuned on real sentences; every clause here was a live failure once.
TOOLS_GUIDE = (
    "You have tools for the things on the user's computer. If the sentence asks for something a tool can do, do it "
    "and then say what you did. Be decisive: call tools rather than asking questions; if a search returns several "
    "matches, pick the most likely one. Speech-to-text mishears names ('Dove Punk' was Daft Punk): if a name in the "
    "sentence resembles one of the user's library names given below, or a well-known artist or track, assume that is "
    "what they said and search for that. Prefer a well-known reading of an odd phrase over a literal search of it. "
    "Never play a result just because its title happens to contain the misheard words; when nothing well-known fits, "
    "play nothing and say what you heard. Do only what was asked: 'play' means play, never save, like, remove or "
    "change a playlist unless you were told to. If the situation says music is already playing, a plain 'play it' "
    "needs no tool: say it is already playing. 'This song', 'this', 'it' mean whatever is playing now (given below "
    "when known; otherwise look it up first). Tools are for the player, not for facts: a question about the music, "
    "the artist or the world is answered from your own knowledge and the situation, without tools. Small talk needs "
    "no tools at all. Claim only what you actually did with a tool in this conversation; if it would not work, say so."
)

# The same voice with nothing to act through (no servers, or they are all down).
NO_TOOLS = (
    "You have no tools right now and no internet. Answer from what you know and from the situation below. If the "
    "answer depends on recent events or on something you cannot know, say so in one sentence instead of guessing."
)

HONEST = (
    "You can call no more tools. Tell the user honestly what you did and did not manage to do, in your own voice and "
    "at most two sentences, starting with your mood in square brackets. Never claim an action (playing, queueing, "
    "saving) that you did not perform with a tool in this conversation."
)

# "[happy] Skipped." -> ("happy", "Skipped."); also tolerates (happy), [HAPPY]: and a missing tag.
EMOTION_TAG = re.compile(r"^\s*[\[(<]\s*(%s)\s*[\])>]\s*[:,-]?\s*" % "|".join(EMOTIONS), re.IGNORECASE)


def split_emotion(text: str, default: str = "neutral") -> tuple[str, str]:
    """Her mood tag off the front of a reply; `default` and the text unchanged when it is missing."""
    match = EMOTION_TAG.match(text)
    if not match:
        return default, text.strip()
    rest = text[match.end():].strip()
    if not rest:
        return default, text.strip()   # the tag was the whole reply: keep the words, drop nothing
    return match.group(1).lower(), rest


def system_prompt(has_tools: bool) -> str:
    return f"{VOICE}\n\n{TOOLS_GUIDE if has_tools else NO_TOOLS}"


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

    async def tools(self, careful: bool = False, topic: str = "") -> list[ToolSpec]:
        """Every configured server's tools: one brain, one toolbox. `careful=False` leaves out the
        tools with consequences (save, remove, add to a playlist) until the sentence asks for one.

        `topic` is the gate's reading of the sentence: that topic's servers go first, so when there
        are more tools than `max_tools` the ones cut are the ones furthest from what was asked."""
        topics = sorted(self.toolbox.topics())
        order = ([topic] if topic in topics else []) + [t for t in topics if t != topic]
        specs: list[ToolSpec] = []
        for name in order:
            specs += await self.toolbox.tools_for(name, careful=careful)
        return self.fit(specs)

    def fit(self, specs: list[ToolSpec]) -> list[ToolSpec]:
        """At most `max_tools` schemas in the prompt (25 Spotify tools are ~360 tokens of an 8192
        context). The order they arrive in is already topic-first; within a server the adapter's
        `common_tools` come first, and the tail is cut and logged."""
        limit = self.config.max_tools
        if limit < 1 or len(specs) <= limit:
            return specs
        by_server: dict[str, list[ToolSpec]] = {}
        for spec in specs:
            by_server.setdefault(spec.server, []).append(spec)
        ordered: list[ToolSpec] = []
        for server, group in by_server.items():
            rank = {name: i for i, name in enumerate(self.toolbox.common_tools(server))}
            ordered += sorted(group, key=lambda s: rank.get(s.name, len(rank)))   # stable: the rest keep their order
        kept, cut = ordered[:limit], ordered[limit:]
        log.info("thinker: %d tools is more than max_tools=%d; offering %d, leaving out %s",
                 len(specs), limit, len(kept), ", ".join(s.key for s in cut))
        return kept

    async def run(self, text: str, context: str = "", careful: bool = False, topic: str = "") -> Outcome:
        """One sentence, start to finish: her reply, its mood, and whatever tools it took to get
        there. Never raises: a failure is an Outcome with ok=False and something to say about it."""
        self.calls += 1
        started = time.perf_counter()
        calls: list[ToolResult] = []
        try:
            outcome = await asyncio.wait_for(self._run(text, context, calls, careful, topic), self.config.timeout_s)
        except asyncio.TimeoutError:
            outcome = Outcome("thought about it too long", "I tried, but my thinking took too long. Sorry.", False,
                              tuple(calls), "alert")
        except ThinkerError as exc:
            log.warning("thinker: %s", exc)
            outcome = Outcome("tried to think", "I tried, but my thinking part is not answering.", False,
                              tuple(calls), "alert")
        self.last_s = time.perf_counter() - started
        if not outcome.ok:
            self.failures += 1
        self.last = {
            "asked": text, "did": outcome.did, "said": outcome.fact, "emotion": outcome.emotion, "ok": outcome.ok,
            "s": round(self.last_s, 2), "calls": [c.to_dict() | {"text": c.text[:200]} for c in outcome.calls],
        }
        log.info("thinker: %r -> %s -> [%s] %r in %.1fs", text, outcome.did, outcome.emotion, outcome.fact, self.last_s)
        return outcome

    async def _run(self, text: str, context: str, calls: list[ToolResult], careful: bool, topic: str = "") -> Outcome:
        specs = await self.tools(careful, topic)
        tools = [s.for_ollama() for s in specs]
        user = text if not context else f"Situation: {context}\n\nThe user says: {text}"
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt(bool(tools))},
                                          {"role": "user", "content": user}]
        used: list[str] = []
        for round_no in range(self.config.max_rounds + 1):
            last_round = round_no == self.config.max_rounds or not tools
            payload = {
                "model": self.model,
                "messages": messages,
                "think": self.config.think,
                "stream": False,
                "keep_alive": self.config.keep_alive,
                "options": {"num_ctx": self.config.num_ctx, "num_predict": self.config.num_predict, "temperature": 0.2},
            }
            if tools and not last_round:
                payload["tools"] = tools
            elif tools:
                messages.append({"role": "user", "content": HONEST})
            reply = await self.chat(payload)
            message = reply.get("message") or {}
            tool_calls = message.get("tool_calls") or []
            content = (message.get("content") or "").strip()
            if not tool_calls or last_round:
                if not content:
                    return Outcome(_did(used), "I got lost doing that, sorry.", False, tuple(calls), "alert")
                emotion, line = split_emotion(content)
                if any(not c.ok for c in calls) and emotion not in ("alert", "angry"):
                    emotion = "alert"   # a tool said no; the face should not be cheerful about it
                return Outcome(_did(used), tidy_sentence(line), True, tuple(calls), emotion)
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
        return Outcome(_did(used), "I got lost doing that, sorry.", False, tuple(calls), "alert")  # unreachable

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
    """Her reply as spoken text: first paragraph, no markdown, capped (speech.max_chars is 400)."""
    line = re.sub(r"[*_`#>]+", "", text.strip().split("\n")[0])
    line = " ".join(line.split())
    if len(line) > limit:
        # Cut at the last sentence end that fits; only when there is none, at a word.
        ends = [m.end() for m in re.finditer(r"[.!?][\"')]?(?=\s)", line[:limit])]
        if ends and ends[-1] > limit // 3:
            line = line[: ends[-1]].strip()
        else:
            line = line[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return line
