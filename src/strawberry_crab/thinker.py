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

Ollama does not compress a prompt longer than `num_ctx`: it drops the oldest tokens, which are
the system prompt, and says nothing. So every round's prompt is estimated first (`prompt_tokens`)
and made to fit `num_ctx - num_predict` (`Thinker.fit_prompt`): the oldest ledger turns go first,
then the tool results lose their tails; the system prompt, the tool schemas and the
sentence are never cut, and when they alone do not fit the request fails instead.

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
from .ledger import as_context
from .logtext import line, sentence
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

# How to use the tools. Tuned on real sentences; every clause here was a live failure once. The
# clause on facts kept Qwen from searching the music catalogue to answer "who is Aphex Twin"; with a
# web search offered it says which tools that is about (FACTS_WITH_LOOKUP), and the search's own
# rules come from its adapter (adapters/web.py).
FACTS = ("Tools are for the player, not for facts: a question about the music, the artist or the world is answered "
         "from your own knowledge and the situation, without tools.")
FACTS_WITH_LOOKUP = ("The player's tools are for the player, not for facts: a question about the music, the artist "
                     "or the world is never answered with them. Answer it from your own knowledge and the situation, "
                     "unless the web search rules below say to search.")
TOOLS_RULES = (
    "You have tools for the things on the user's computer. If the sentence asks for something a tool can do, do it "
    "and then say what you did. Be decisive: call tools rather than asking questions; if a search returns several "
    "matches, pick the most likely one. Speech-to-text mishears names ('Dove Punk' was Daft Punk): if a name in the "
    "sentence resembles one of the user's library names given below, or a well-known artist or track, assume that is "
    "what they said and search for that. Prefer a well-known reading of an odd phrase over a literal search of it. "
    "Never play a result just because its title happens to contain the misheard words; when nothing well-known fits, "
    "play nothing and say what you heard. Do only what was asked: 'play' means play, never save, like, remove or "
    "change a playlist unless you were told to. If the situation says music is already playing, a plain 'play it' "
    "needs no tool: say it is already playing. 'This song', 'this', 'it' mean whatever is playing now (given below "
    "when known; otherwise look it up first). {facts} Small talk needs no tools at all. Claim only what you actually "
    "did with a tool in this conversation; if it would not work, say so."
)
TOOLS_GUIDE = TOOLS_RULES.format(facts=FACTS)

# The same voice with nothing to act through (no servers, or they are all down). The add-on
# sentence alone was over-applied: "any good techno from <a country>?" got "Choosing music needs a
# music add-on" 5/5 and "can you recommend some techno artists" "I don't know much about techno
# artists", so recommending and talking about music are named as hers to answer. "tell me about
# <a name it did not know>" got an invented 18th-century composer, hence the clause on names.
MUSIC_FROM_MEMORY = (
    "Recommending music and talking about artists, genres, albums and songs need nothing but your own knowledge: "
    "answer those yourself, naming a few real artists or records you are sure of; asked about one you have never "
    "heard of, say so rather than invent it. Only when you are asked to play, queue, open or save particular music, "
    "say in one short sentence that playing it needs a music add-on, such as the Spotify one. Never say you did "
    "something you did not do."
)
NO_TOOLS = (
    "You have no tools right now and no internet. Answer from what you know and from the situation below. If the "
    "answer depends on recent events or on something you cannot know, say so in one sentence instead of guessing. "
    + MUSIC_FROM_MEMORY
)
# Only tools that look things up (web search): nothing on the computer to act through, so the music
# clauses of NO_TOOLS still hold; what to do about recent events is the search's own rules.
LOOKUP_ONLY = (
    "You have no tools for the things on the user's computer, only the web search below. Answer from what you know "
    "and from the situation below. " + MUSIC_FROM_MEMORY
)

OVER_LIMIT = ("Not done: that is as many of these calls as one question gets. Answer now from what you already "
              "have; if it is not enough, say you couldn't find it.")

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


def system_prompt(has_tools: bool, guides: list[str] | tuple[str, ...] = (), acting: bool | None = None,
                  lookup: bool = False) -> str:
    """Her voice, then the rules for what she has: the tool rules when there is something on the
    computer to act through (`acting`, the default whenever there are tools), the look-up-only rules
    when there is only a web search, NO_TOOLS when there is nothing; then the adapters' paragraphs
    (`guides`: how to use a web search, or that it is not answering). `lookup`: a web search is
    among the tools, so the clause on facts says which tools it is about."""
    acting = has_tools if acting is None else acting
    if acting:
        rules = TOOLS_RULES.format(facts=FACTS_WITH_LOOKUP if lookup else FACTS)
    elif has_tools:
        rules = LOOKUP_ONLY
    else:
        rules = NO_TOOLS
    return "\n\n".join([VOICE, rules, *[g for g in guides if g]])


def user_message(text: str, context: str, recent: list[str], note: str = "") -> str:
    """The situation, the ledger lines under their heading, then the sentence (and the adapter's
    note under it when an adapter has one for this sentence, Thinker.offer)."""
    situation = "\n".join(part for part in (context, as_context(recent)) if part)
    said = text if not situation else f"Situation: {situation}\n\nThe user says: {text}"
    return f"{said}\n\n({note})" if note else said


# Ollama has no tokenize endpoint and no tokenizer is a dependency, so the prompt is measured in
# characters of its JSON, which also counts the keys and quoting the chat template wraps around
# each message. Three characters a token is on the safe side: measured on qwen3.8:27b the
# messages are 4.1-4.5 and tool schemas ~3.7. Offering any tools at all also adds the template's
# tool instructions, ~200 tokens whatever the schemas.
CHARS_PER_TOKEN = 3.0
TOOLS_TEMPLATE_TOKENS = 250
MIN_RESULT_CHARS = 200     # a tool result is never shortened below this
CUT = " …(cut to fit)"


def prompt_tokens(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
    """A conservative estimate of what Ollama will count for these messages and tool schemas."""
    chars = len(json.dumps(messages, ensure_ascii=False))
    extra = 0
    if tools:
        chars += len(json.dumps(tools, ensure_ascii=False))
        extra = TOOLS_TEMPLATE_TOKENS
    return int(chars / CHARS_PER_TOKEN) + extra + 1


class ThinkerError(RuntimeError):
    pass


class PromptTooLong(ThinkerError):
    """Even with nothing left to trim the prompt does not fit num_ctx."""


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

    async def tools(self, careful: bool = False, topic: str = "", skip: frozenset[str] = frozenset(),
                    first: frozenset[str] = frozenset()) -> list[ToolSpec]:
        """Every configured server's tools: one brain, one toolbox. `careful=False` leaves out the
        tools with consequences (save, remove, add to a playlist) until the sentence asks for one.

        `topic` is the gate's reading of the sentence: when there are more tools than `max_tools`,
        that topic's servers go first, so the ones cut are the ones furthest from what was asked.
        While everything fits, the order is the same for every sentence (the prompt cache, below).
        `skip` names servers left out of this sentence and `first` servers asked for outright, which
        go ahead of everything when the list has to be cut (Thinker.offer)."""
        topics = sorted(self.toolbox.topics())
        by_topic: dict[str, list[ToolSpec]] = {}
        for name in topics:
            by_topic[name] = [s for s in await self.toolbox.tools_for(name, careful=careful) if s.server not in skip]
        stable = [s for name in topics for s in by_topic[name]]
        limit = self.config.max_tools
        if limit < 1 or len(stable) <= limit:
            # Everything fits: the same order for every sentence. Ollama reuses the prompt it has
            # cached up to the first token that differs, and the tool schemas come right after the
            # system prompt: reordering them by topic re-read ~2700 tokens, 2.1-2.7 s instead of
            # ~0.3 s on qwen3.8:27b (WIRING §8b).
            return stable
        order = ([topic] if topic in topics else []) + [t for t in topics if t != topic]
        specs = [s for name in order for s in by_topic[name]]
        if first:
            specs = [s for s in specs if s.server in first] + [s for s in specs if s.server not in first]
        return self.fit(specs)

    async def offer(self, text: str, careful: bool = False, topic: str = "",
                    route: Any = None) -> tuple[list[ToolSpec], str, str]:
        """This sentence's tools, its system prompt and the note under it. Each server's adapter
        may say the sentence does not want its tools (a web search for small talk) or asks for them
        outright ("look it up"), and brings its paragraph for the rules: how to use its tools when
        they are offered, or that it is not answering when it is configured and they are not."""
        verdicts: dict[str, bool | None] = {}
        for server, adapter in self.toolbox.adapters.items():
            try:
                verdicts[server] = adapter.wanted(text, route)
            except Exception as exc:   # an adapter must never cost the sentence its answer
                log.warning("thinker: %s adapter could not read the sentence (%s)", server, exc)
                verdicts[server] = None
        skip = frozenset(s for s, v in verdicts.items() if v is False)
        first = frozenset(s for s, v in verdicts.items() if v is True)
        specs = await self.tools(careful, topic, skip=skip, first=first)
        offered = {s.server for s in specs}
        guides: list[str] = []
        notes: list[str] = []
        lookup = False
        for server, adapter in self.toolbox.adapters.items():
            if server in skip:
                continue
            if server in offered:
                lookup = lookup or bool(getattr(adapter, "looks_up_only", False))
                text_for = getattr(adapter, "guide", "")
                if server in first:
                    try:
                        note = adapter.nudge(text, route)
                    except Exception as exc:
                        log.warning("thinker: %s adapter could not write its note (%s)", server, exc)
                        note = ""
                    if note:
                        notes.append(note)
            else:
                text_for = getattr(adapter, "unavailable", "")
            if text_for and text_for not in guides:
                guides.append(text_for)
        acting = any(not getattr(self.toolbox.adapters.get(s.server), "looks_up_only", False) for s in specs)
        prompt = system_prompt(bool(specs), guides, acting=acting, lookup=lookup)
        if first:
            log.info("thinker: the sentence wants %s; %s", ", ".join(sorted(first)),
                     "offered first" if first & offered else "not answering")
        return specs, prompt, " ".join(notes)

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

    async def run(self, text: str, context: str = "", careful: bool = False, topic: str = "",
                  tools: bool = True, recent: list[str] | None = None, route: Any = None) -> Outcome:
        """One sentence, start to finish: her reply, its mood, and whatever tools it took to get
        there. Never raises: a failure is an Outcome with ok=False and something to say about it.
        `tools=False` offers none (the latency probe, which must not change anything). `recent`
        is the ledger's lines, oldest first: the first thing to go when the prompt is too long.
        `route` is the gate's reading, for the adapters that decide by it (Thinker.offer)."""
        self.calls += 1
        started = time.perf_counter()
        calls: list[ToolResult] = []
        try:
            outcome = await asyncio.wait_for(self._run(text, context, calls, careful, topic, tools, list(recent or []),
                                                       route), self.config.timeout_s)
        except asyncio.TimeoutError:
            outcome = Outcome("thought about it too long", "I tried, but my thinking took too long. Sorry.", False,
                              tuple(calls), "alert")
        except PromptTooLong as exc:
            log.warning("thinker: %s", exc)
            outcome = Outcome("had too much to think about", "That's more than I can hold in my head at once. Sorry.",
                              False, tuple(calls), "alert")
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
        log.info("thinker: %s -> %s -> [%s] %r in %.1fs", sentence(text), outcome.did, outcome.emotion, line(outcome.fact),
                 self.last_s)
        return outcome

    def fit_prompt(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], text: str, context: str,
                   recent: list[str], note: str = "") -> None:
        """Make this round's prompt fit `num_ctx`, leaving `num_predict` for the reply, in place:
        drop the oldest ledger lines (from `recent`, so later rounds go without them too), then
        shorten the tool results, oldest first, keeping each one's head. The system prompt, the
        tool schemas and the sentence are never touched; if they alone are too long, PromptTooLong.
        The log line has counts only: a tool result can carry a notification or a message."""
        budget = self.config.num_ctx - self.config.num_predict
        estimate = before = prompt_tokens(messages, tools)
        if estimate <= budget:
            return
        turns = len(recent)
        while estimate > budget and recent:
            recent.pop(0)
            messages[1]["content"] = user_message(text, context, recent, note)
            estimate = prompt_tokens(messages, tools)
        shortened = 0
        for message in messages:
            if estimate <= budget:
                break
            content = message.get("content") or ""
            if message.get("role") != "tool" or len(content) <= MIN_RESULT_CHARS:
                continue
            over = int((estimate - budget) * CHARS_PER_TOKEN) + 1
            keep = max(MIN_RESULT_CHARS, len(content) - over - len(CUT))
            if keep >= len(content):
                continue
            message["content"] = content[:keep] + CUT
            shortened += 1
            estimate = prompt_tokens(messages, tools)
        results = sum(1 for m in messages if m.get("role") == "tool")
        log.info("thinker: prompt ~%d tokens is over the %d that fit (num_ctx %d - num_predict %d); dropped %d of %d "
                 "ledger turns, shortened %d of %d tool results; now ~%d", before, budget, self.config.num_ctx,
                 self.config.num_predict, turns - len(recent), turns, shortened, results, estimate)
        if estimate > budget:
            fixed = prompt_tokens(messages[:1], tools)
            raise PromptTooLong(f"prompt does not fit: ~{estimate} tokens with nothing left to trim, {budget} fit "
                                f"(the system prompt and {len(tools)} tool schemas alone ~{fixed}); not sent, since "
                                "Ollama would silently drop its start")

    async def _run(self, text: str, context: str, calls: list[ToolResult], careful: bool, topic: str = "",
                   use_tools: bool = True, recent: list[str] | None = None, route: Any = None) -> Outcome:
        if use_tools:
            specs, prompt, note = await self.offer(text, careful, topic, route)
        else:
            specs, prompt, note = [], system_prompt(False), ""
        tools = [s.for_ollama() for s in specs]
        recent = recent if recent is not None else []
        messages: list[dict[str, Any]] = [{"role": "system", "content": prompt},
                                          {"role": "user", "content": user_message(text, context, recent, note)}]
        used: list[str] = []
        per_server: dict[str, int] = {}
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
            # Every round: the last round's tool results made the prompt longer.
            self.fit_prompt(messages, payload.get("tools") or [], text, context, recent, note)
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
                spec = self.toolbox.functions.get(name)
                limit = getattr(self.toolbox.adapters.get(spec.server), "max_calls", 0) if spec else 0
                if spec and limit and per_server.get(spec.server, 0) >= limit:
                    # Not made, and not counted as a call: the brain is told to answer with what it has.
                    log.info("thinker: %s.%s not called, %d calls is this server's limit for a sentence",
                             spec.server, spec.name, limit)
                    messages.append({"role": "tool", "tool_name": name, "content": OVER_LIMIT})
                    continue
                if spec:
                    per_server[spec.server] = per_server.get(spec.server, 0) + 1
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
