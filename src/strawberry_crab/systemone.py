"""A local "System One": fast structured decisions about text, no generation (WIRING.md §8a).

The shape is TypeSafe AI's (docs.typesafe.ai): a piece of *state* and a handful of typed,
atomic *questions* answered in one go, each with probabilities and a confidence that code
can threshold. Theirs is a hosted model; ours is `embeddinggemma` in Ollama plus labelled
examples, which scored 16/16 on the routing bake-off at 16 ms a sentence.

    Choice  options, each a one-line description and a few examples -> choice, probabilities, confidence
    Score   an ordered rubric of situations                          -> score (expected level), probabilities
    Noul    a yes/no                                                 -> probability of yes

How: every option's examples are embedded once (as "documents"). The state is embedded once
per `ask` (as a "query"); an option's score is the mean cosine similarity of its two nearest
examples, and a softmax at a low temperature turns the scores into probabilities. Confidence
is how peaked that distribution is, `(n·p_max − 1)/(n − 1)`: 0 for a coin toss, 1 for a spike.
Everything is pure Python; no numpy in the daemon.

Measured on scripts/gate_phrases.json (34 sentences, 2026-09-21): mean-pooled centroids 29/34,
nearest examples 32/34, nearest examples with embeddinggemma's task prefixes 34/34. One Ollama
embedding call costs ~165 ms whatever its size, so a sentence is routed in ~170 ms.

The routing questions for spoken sentences (`KIND`, `TOPIC`, `IS_URGENT`, `IS_ABOUT_HER`, and the
chained `MUSIC_TOOL` / `HAS_ARGUMENT` / `WANTS_LIBRARY_CHANGE`) live at the bottom of this file,
with the `Gate` that runs them. The daemon uses the reading for two things now: firing a bare
reflex, and deciding whether Qwen gets the careful tools. The examples are the whole model: add a
phrase she misreads to the right option and it is fixed.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import aiohttp

from .config import GateConfig

log = logging.getLogger("strawberryd.gate")

Embedder = Callable[[list[str]], Awaitable[list[list[float]]]]


class GateError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- questions


@dataclass(frozen=True)
class Option:
    name: str
    description: str = ""
    examples: tuple[str, ...] = ()

    def texts(self) -> tuple[str, ...]:
        """What gets embedded for this option: its examples, or the description alone."""
        return self.examples or (self.description,)


@dataclass(frozen=True)
class Choice:
    name: str
    options: tuple[Option, ...]

    def with_examples(self, extra: dict[str, list[str]]) -> Choice:
        """Extra examples from the config, keyed "<question>.<option>"."""
        options = []
        for option in self.options:
            more = extra.get(f"{self.name}.{option.name}", [])
            options.append(Option(option.name, option.description, option.examples + tuple(more)) if more else option)
        return Choice(self.name, tuple(options))


@dataclass(frozen=True)
class Score:
    """An ordered rubric. Levels are situations ("broken but a workaround exists"), 1-based."""

    name: str
    levels: tuple[Option, ...]


@dataclass(frozen=True)
class Noul:
    name: str
    yes: Option
    no: Option


Question = Choice | Score | Noul


@dataclass(frozen=True)
class Answer:
    question: str
    type: str                                  # choice | score | noul
    probabilities: dict[str, float]
    confidence: float
    choice: str | None = None                  # Choice: the winning option
    score: float | None = None                 # Score: Σ level·p; Noul: p(yes)
    legend: dict[int, str] | None = None       # Score: level -> situation

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": self.type,
            "probabilities": {k: round(v, 4) for k, v in self.probabilities.items()},
            "confidence": round(self.confidence, 4),
        }
        if self.choice is not None:
            out["choice"] = self.choice
        if self.score is not None:
            out["score"] = round(self.score, 4)
        if self.legend is not None:
            out["legend"] = self.legend
        return out


# ----------------------------------------------------------------------------- maths


def normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(math.sumprod(vector, vector))
    if norm == 0.0:
        raise GateError("zero embedding")
    return [x / norm for x in vector]


def softmax(scores: list[float], temperature: float) -> list[float]:
    logits = [s / temperature for s in scores]
    top = max(logits)
    weights = [math.exp(l - top) for l in logits]
    total = sum(weights)
    return [w / total for w in weights]


def confidence(probabilities: list[float]) -> float:
    """TypeSafe's collapse: how peaked the distribution is, not its entropy."""
    n = len(probabilities)
    if n < 2:
        return 1.0
    return max(0.0, (n * max(probabilities) - 1.0) / (n - 1))


# ----------------------------------------------------------------------------- the model


class OllamaEmbedder:
    def __init__(self, model: str, url: str, timeout_s: float) -> None:
        self.model = model
        self.url = url
        self.timeout_s = timeout_s
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(base_url=self.url)

    async def close(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        if not self.session:
            raise GateError("embedder not started")
        try:
            async with self.session.post(
                "/api/embed",
                json={"model": self.model, "input": texts, "keep_alive": -1},
                timeout=aiohttp.ClientTimeout(total=self.timeout_s),
            ) as response:
                if response.status != 200:
                    raise GateError(f"HTTP {response.status}: {(await response.text())[:200]}")
                data = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise GateError(str(exc) or type(exc).__name__) from exc
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise GateError("embedding response did not match the input")
        return vectors


class SystemOne:
    """Answers typed questions about a piece of text from embedding similarity.

    `query_prefix` and `document_prefix` are the model's prompt conventions: embeddinggemma
    wants "task: classification | query: " on the text being classified and "title: none |
    text: " on the examples, and it reads noticeably better with them. Leave both empty for a
    model without such conventions.
    """

    def __init__(self, embedder: Embedder, temperature: float = 0.05, neighbours: int = 2,
                 query_prefix: str = "", document_prefix: str = "") -> None:
        self.embed = embedder
        self.temperature = temperature
        self.neighbours = max(1, neighbours)
        self.query_prefix = query_prefix
        self.document_prefix = document_prefix
        self.vectors: dict[tuple[str, ...], list[list[float]]] = {}

    async def prepare(self, *questions: Question) -> None:
        """Embed every option's examples once. Cheap to call again: only new texts are embedded."""
        needed = [o.texts() for q in questions for o in _options(q) if o.texts() not in self.vectors]
        if not needed:
            return
        flat = [self.document_prefix + t for texts in needed for t in texts]
        vectors = [normalise(v) for v in await self.embed(flat)]
        i = 0
        for texts in needed:
            self.vectors[texts] = vectors[i:i + len(texts)]
            i += len(texts)

    @property
    def examples(self) -> int:
        return sum(len(v) for v in self.vectors.values())

    async def ask(self, state: str, *questions: Question) -> dict[str, Answer]:
        await self.prepare(*questions)
        vector = normalise((await self.embed([self.query_prefix + state]))[0])
        return {q.name: self._answer(vector, q) for q in questions}

    def _score(self, vector: list[float], option: Option) -> float:
        """Mean similarity of the option's nearest examples: one odd example cannot carry it."""
        nearest = sorted((math.sumprod(vector, v) for v in self.vectors[option.texts()]), reverse=True)[: self.neighbours]
        return sum(nearest) / len(nearest)

    def _answer(self, vector: list[float], question: Question) -> Answer:
        options = _options(question)
        similarities = [self._score(vector, o) for o in options]
        probabilities = softmax(similarities, self.temperature)
        by_name = {o.name: p for o, p in zip(options, probabilities)}
        conf = confidence(probabilities)
        if isinstance(question, Choice):
            best = max(by_name, key=by_name.__getitem__)
            return Answer(question.name, "choice", by_name, conf, choice=best)
        if isinstance(question, Score):
            expected = sum((i + 1) * p for i, p in enumerate(probabilities))
            legend = {i + 1: o.name for i, o in enumerate(options)}
            return Answer(question.name, "score", by_name, conf, score=expected, legend=legend)
        return Answer(question.name, "noul", by_name, conf, score=by_name["yes"])


def _options(question: Question) -> tuple[Option, ...]:
    if isinstance(question, Choice):
        return question.options
    if isinstance(question, Score):
        return question.levels
    yes = Option("yes", question.yes.description, question.yes.examples)
    no = Option("no", question.no.description, question.no.examples)
    return (yes, no)


# ----------------------------------------------------------------------------- routing


KIND = Choice("kind", (
    Option("request", "asks her to do something on the computer", (
        "play the next song", "stop the music", "skip this track", "pause it", "put on some jazz",
        "turn the volume down a bit", "make it louder", "set a timer for ten minutes",
        "remind me to call mum at five", "add this to my notes", "write down: buy milk",
        "open my email", "mute the music", "play something calmer", "queue up some Daft Punk",
        "recommend me some music", "suggest something to listen to", "play something I might like",
    )),
    Option("question", "wants a fact looked up or read out", (
        "what song is this", "who sings this", "what's on my calendar tomorrow", "when is my next meeting",
        "how long until my next appointment", "what day is it", "what time is it in Tokyo",
        "how many unread messages do I have", "what did I note down yesterday", "is it going to rain",
        "is the battery charging", "is the wifi still connected", "how much disk space is left",
        "who was the president of the United States in 1960", "what's the capital of Australia",
        "how far away is the moon", "how many grams are in an ounce", "when did the Berlin Wall fall",
        "what does RSVP stand for", "who wrote War and Peace", "how do you spell necessary",
        "what can you tell me about this artist", "tell me more about this band", "who are the members of this band",
        "tell me about the history of Led Zeppelin", "what else did this artist make", "when was this band formed",
        "tell me something about this song", "what genre is this",
    )),
    Option("chat", "small talk, feelings, banter, talking to her for its own sake", (
        "hello there", "good morning strawberry", "how was your night", "how are you doing today",
        "you're adorable", "you look lovely today", "I had a rough day", "I'm so tired today",
        "what do you think of this music", "do you like techno", "thanks strawberry", "good night",
        "you're a menace", "I love this song", "that one's a banger", "this track is great", "what a tune",
    )),
    Option("other", "not aimed at her: a fragment, noise, someone else in the room", (
        "um", "hello hello testing", "is this thing on", "testing testing one two", "one two three",
        "yeah yeah okay", "hang on a second", "no not you", "what was I saying",
    )),
))

TOPIC = Choice("topic", (
    Option("music", "the music that is playing, the player, the volume", (
        "skip this song", "who sings this", "pause the music", "play some jazz", "turn it down",
        "what album is this from", "put this on repeat", "I love this track", "turn the music off",
        "put something on", "play something for the evening", "what year is this from",
        "tell me about this artist", "who are the members of this band", "what else did they make",
        "recommend me some music", "what should I listen to",
    )),
    Option("calendar", "meetings, appointments, reminders, the time and the date", (
        "what's on my calendar tomorrow", "when is my next meeting", "remind me at five",
        "set a timer for ten minutes", "what day is it", "am I free on Friday",
    )),
    Option("notes", "notes, lists, things to write down or look up later", (
        "add this to my notes", "write down: buy milk", "what did I note yesterday",
        "put that on my shopping list", "find my note about the garden",
    )),
    Option("system", "the computer itself: windows, screen, files, power", (
        "lock the screen", "how much disk space is left", "open my email", "turn off the monitor",
        "what's using all the memory", "take a screenshot", "is the battery charging",
        "is the wifi connected",
    )),
    Option("other", "none of those: general knowledge, the world, small talk", (
        "how are you today", "good morning", "you're adorable", "I'm tired", "what time is it in Tokyo",
        "is it going to rain", "who was the president of the United States in 1960", "what's the capital of Australia",
        "how far away is the moon", "when did the Berlin Wall fall", "who wrote War and Peace",
    )),
))

IS_URGENT = Noul(
    "is_urgent",
    yes=Option("yes", "wants it done right now", ("quick, pause it", "stop stop stop", "now please", "hurry up",
                                                    "mute it now", "quickly, what's the time")),
    no=Option("no", "no rush", ("play something calmer when you get a chance", "how are you today",
                                  "what's on my calendar tomorrow", "you look lovely", "remind me at five")),
)

IS_ABOUT_HER = Noul(
    "is_about_her",
    yes=Option("yes", "about Strawberry herself: how she is, what she thinks, what she can do", (
        "how are you doing today", "you're adorable", "do you like techno", "what can you do",
        "are you listening", "what do you think of this song", "you look lovely today",
    )),
    no=Option("no", "about the music, the calendar, the computer, or the user", (
        "skip this track", "what's on my calendar", "I'm so tired today", "who sings this",
        "add this to my notes", "lock the screen",
    )),
)

# The second level (§8a, chained): which tool, and does the sentence carry an argument the
# embedding cannot extract. Asked on the same vector, so they cost nothing extra.
MUSIC_TOOL = Choice("music_tool", (
    Option("skip", "go to the next track", ("skip this song", "next song please", "skip", "play the next one",
                                            "not this one, next", "change the track", "can you skip this")),
    Option("previous", "go back to the previous track", ("play the previous one again", "go back a track",
                                                          "previous song", "put the last one back on")),
    Option("pause", "stop or pause the music", ("pause the music", "stop the music", "pause it", "turn this off",
                                                 "quiet please", "mute the music", "silence")),
    Option("resume", "start the music again", ("resume", "play", "unpause", "start the music again", "carry on",
                                                "music back on please", "play it", "ok play it", "play it please",
                                                "go on, play", "put it on", "press play")),
    Option("volume_down", "make it quieter", ("turn the volume down a bit", "quieter", "turn it down", "too loud",
                                               "bring the volume down")),
    Option("volume_up", "make it louder", ("turn it up", "louder", "make it louder", "volume up a bit",
                                            "I can barely hear it")),
    Option("now_playing", "say what is playing", ("what song is this", "who sings this", "what's playing",
                                                   "what are we listening to", "who is this by", "what album is this from")),
    Option("other", "something else about music: a specific song, artist or playlist, the queue, shuffle, facts", (
        "play some jazz", "put on some Nina Simone", "queue up Blue Monday",
        "shuffle this album", "put this on repeat", "what year did this come out",
        "tell me about this artist", "what can you tell me about the band", "who are the members of this band",
        "tell me more about the band members and the history", "what else did they make", "recommend me some music",
        "play something similar", "play something else", "play something by this band",
        "queue up something by Nina Simone", "put something on for cooking", "play the live version",
        "play some acid techno", "play some classical music", "no, play another classical song", "put on some techno",
        "play daft punk", "play some daft punk", "play led zeppelin",
    )),
))

HAS_ARGUMENT = Noul(
    "has_argument",
    yes=Option("yes", "names a specific song, artist, playlist, amount, time or text", (
        "play some Nina Simone", "queue up Blue Monday", "volume to thirty", "set a timer for ten minutes",
        "remind me to call mum at five", "play my running playlist", "write down: buy milk", "put on some jazz",
        "play some acid techno", "play some classical music", "play another classical song", "put on some techno",
        "play daft punk", "play some daft punk", "play led zeppelin",
    )),
    no=Option("no", "a plain command with nothing to fill in", (
        "skip this", "pause", "next song", "what song is this", "turn it down a bit", "louder", "resume",
        "who sings this", "stop the music", "lock the screen", "play it please", "skip please", "next one please",
        "pause please", "can you play it", "music on please",
    )),
)

WANTS_LIBRARY_CHANGE = Noul(
    "wants_library_change",
    yes=Option("yes", "asks to save, like, favourite, remove or add something to a playlist", (
        "save this song", "like this one", "add this to my favourites", "put this on my running playlist",
        "remove this from my liked songs", "favourite this track", "add it to the queue and save it",
    )),
    no=Option("no", "playing, skipping, pausing, volume, asking about the music", (
        "play some Nina Simone", "skip this", "what song is this", "turn it down", "queue up Blue Monday",
        "play my running playlist", "pause", "who sings this",
    )),
)

# Not a routing question: asked of a notification body before any model reads it (WIRING.md §4,
# strawberryd/privacy.py). Same embedder, its own call; scripts/sensitive_check.py is its contract.
IS_SENSITIVE = Noul(
    "is_sensitive",
    yes=Option("yes", "a code, a sign-in, a bank or card alert, an account security notice", (
        "Your verification code is 482913", "Use this code to sign in. Do not share it with anyone.",
        "is your Google verification code", "Your one-time passcode expires in 10 minutes",
        "Your password was changed. If this wasn't you, secure your account now.",
        "We received a request to reset your password", "New sign-in to your account from Chrome on Windows",
        "A new device just logged in to your account", "Was this you? Confirm this login attempt",
        "Approve the sign-in request on your phone", "Card payment of 45.20 EUR at the supermarket approved",
        "You spent 129.00 with your card ending in 4421", "Incoming transfer to your account received",
        "Your account balance is below your limit", "Suspicious activity detected on your account",
        "Your account has been locked after too many failed attempts",
        "Two-factor authentication was turned off for your account", "Security alert: unusual login attempt blocked",
        "Your recovery email address was changed", "Tap this link to log in to your account",
        "Direct debit collected from your current account", "Your card was declined",
    )),
    no=Option("no", "ordinary chat, work, builds, calendar, updates, music, the computer", (
        "are we still on for lunch tomorrow?", "can you review my PR when you get a sec", "haha that's brilliant",
        "Build passed on main", "3 tests failed in CI", "PR merged into main", "the deploy is done, looks good",
        "Meeting with the design team at 14:00", "Standup starts in 10 minutes",
        "Reminder: dentist appointment tomorrow morning", "17 updates available", "Download complete",
        "Now playing a new song", "Battery low, 10% remaining", "happy birthday!!", "Your order has been shipped",
        "New comment on your document", "shared a file with you", "running late, be there in 15",
        "Your meeting is starting now", "did you see the match last night", "Backup finished successfully",
    )),
)

TOOL_QUESTIONS: dict[str, Choice] = {"music": MUSIC_TOOL}
ROUTING: tuple[Question, ...] = (KIND, TOPIC, IS_URGENT, IS_ABOUT_HER, HAS_ARGUMENT, WANTS_LIBRARY_CHANGE,
                                 *TOOL_QUESTIONS.values())
ACTIONABLE = ("request", "question")


@dataclass(frozen=True)
class Route:
    text: str
    kind: str
    topic: str
    confidence: float          # of the kind
    is_urgent: float           # p(yes)
    is_about_her: float        # p(yes)
    decision: str              # chat | offer | act
    tool: str = ""             # the topic's tool question, when there is one: "skip", "now_playing", "other"…
    tool_confidence: float = 0.0
    has_argument: float = 0.0  # p(yes): something to fill in that needs the thinker
    library_change: float = 0.0  # p(yes): asks to save/like/remove/add to a playlist (careful tools)
    answers: dict[str, Answer] = field(default_factory=dict, compare=False)
    ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "kind": self.kind,
            "topic": self.topic,
            "confidence": round(self.confidence, 4),
            "is_urgent": round(self.is_urgent, 4),
            "is_about_her": round(self.is_about_her, 4),
            "decision": self.decision,
            "tool": self.tool,
            "tool_confidence": round(self.tool_confidence, 4),
            "has_argument": round(self.has_argument, 4),
            "library_change": round(self.library_change, 4),
            "ms": round(self.ms, 1),
            "answers": {k: v.to_dict() for k, v in self.answers.items()},
        }


def decide(kind: str, kind_confidence: float, act: float, offer: float) -> str:
    """Thresholds per consequence (WIRING §8a): `act` on a clear request or question, `chat` on a
    sentence that is plainly neither, `offer` in between. `chat` and `other` never act, however
    confident. Only `act` reaches a reflex; everything that is not a reflex goes to Qwen anyway,
    so the middle band is a label in the journal now, not a branch."""
    if kind not in ACTIONABLE:
        return "chat"
    if kind_confidence >= act:
        return "act"
    if kind_confidence >= offer:
        return "offer"
    return "chat"


class Gate:
    """Routes a spoken sentence: what it is, what it is about, how sure we are."""

    WARM_UP_S = 120.0

    def __init__(self, config: GateConfig, ollama_url: str = "", embedder: Embedder | None = None,
                 examples: dict[str, list[str]] | None = None) -> None:
        self.config = config
        self.embedder = embedder
        self.owned: OllamaEmbedder | None = None
        if embedder is None and config.enabled:
            self.owned = OllamaEmbedder(config.model, ollama_url, config.timeout_s)
            self.embedder = self.owned
        self.systemone = SystemOne(self.embedder, config.temperature, config.neighbours, config.query_prefix,
                                   config.document_prefix) if self.embedder else None
        # Extra phrases: the loaded adapters' first (a Spotify server brings "save this song"),
        # then the config's, which are the user's own corrections and go last.
        extra: dict[str, list[str]] = {key: list(phrases) for key, phrases in (examples or {}).items()}
        for key, phrases in config.examples.items():
            extra[key] = extra.get(key, []) + list(phrases)
        self.extra_examples = extra
        self.questions: tuple[Question, ...] = tuple(
            q.with_examples(extra) if isinstance(q, Choice) else q for q in ROUTING
        )
        self.ready = False
        self.calls = 0
        self.sensitive_calls = 0
        self.failures = 0
        self.last_route: Route | None = None
        self.last_ms: float | None = None
        self.disabled_reason = "" if config.enabled else "disabled in config"
        self.rewarm: asyncio.Task | None = None

    async def start(self) -> None:
        if not self.config.enabled or not self.systemone:
            return
        if self.owned:
            await self.owned.start()
        started = time.perf_counter()
        try:
            # The first call loads the model and embeds every example at once: the per-sentence
            # timeout would cut it off. Same allowance the brain's warm-up gets.
            if self.owned:
                self.owned.timeout_s = self.WARM_UP_S
            await self.systemone.prepare(*self.questions, IS_SENSITIVE)
        except GateError as exc:
            self.disabled_reason = f"could not embed the examples: {exc}"
            log.warning("gate: %s; routing every sentence to chat", self.disabled_reason)
            return
        finally:
            if self.owned:
                self.owned.timeout_s = self.config.timeout_s
        self.ready = True
        log.info("gate: %s ready in %.1fs (%d examples)", self.config.model, time.perf_counter() - started,
                 self.systemone.examples)

    async def close(self) -> None:
        if self.owned:
            await self.owned.close()

    async def route(self, text: str) -> Route | None:
        """None when the gate is off or failing; the caller then treats the sentence as chat."""
        if not self.ready or not self.systemone:
            return None
        self.calls += 1
        started = time.perf_counter()
        try:
            answers = await self.systemone.ask(text, *self.questions)
        except GateError as exc:
            self.failures += 1
            log.warning("gate: %s; treating %r as chat", exc, text)
            if "Timeout" in str(exc):
                self.schedule_rewarm()
            return None
        ms = (time.perf_counter() - started) * 1000
        kind = answers["kind"]
        topic = answers["topic"]
        topic_name = topic.choice if topic.confidence >= self.config.topic_min else "other"
        tool_q = TOOL_QUESTIONS.get(topic_name or "")
        tool = answers[tool_q.name] if tool_q and tool_q.name in answers else None
        route = Route(
            text=text,
            kind=kind.choice or "other",
            topic=topic_name or "other",
            confidence=kind.confidence,
            is_urgent=answers["is_urgent"].score or 0.0,
            is_about_her=answers["is_about_her"].score or 0.0,
            decision=decide(kind.choice or "other", kind.confidence, self.config.act, self.config.offer),
            tool=(tool.choice or "") if tool else "",
            tool_confidence=tool.confidence if tool else 0.0,
            has_argument=answers["has_argument"].score or 0.0,
            library_change=answers["wants_library_change"].score or 0.0,
            answers=answers,
            ms=ms,
        )
        self.last_route = route
        self.last_ms = ms
        log.info("gate: %r -> %s/%s conf %.2f -> %s%s arg %.2f (%.0f ms) %s", text, route.kind, route.topic,
                 route.confidence, route.decision, f" tool {route.tool} {route.tool_confidence:.2f}" if route.tool else "",
                 route.has_argument, ms, {k: round(v, 2) for k, v in kind.probabilities.items()})
        return route

    async def sensitive(self, text: str) -> tuple[float, float] | None:
        """p(yes) of IS_SENSITIVE for a notification's text, and the milliseconds it took; None when
        the gate is off or failing (the caller then treats the text as sensitive). Never logs the text."""
        if not self.ready or not self.systemone:
            return None
        self.sensitive_calls += 1
        started = time.perf_counter()
        try:
            answers = await self.systemone.ask(text, IS_SENSITIVE)
        except GateError as exc:
            self.failures += 1
            log.warning("gate: %s; the notification body counts as sensitive", exc)
            if "Timeout" in str(exc):
                self.schedule_rewarm()
            return None
        ms = (time.perf_counter() - started) * 1000
        p = answers[IS_SENSITIVE.name].score or 0.0
        log.debug("gate: is_sensitive %.2f (%.0f ms, %d chars)", p, ms, len(text))
        return p, ms

    def schedule_rewarm(self) -> None:
        """After a timeout the embedding model is most likely reloading after being evicted; a
        short call hanging up aborts that load (Ollama), so reload it once with the long allowance."""
        if self.rewarm and not self.rewarm.done():
            return

        async def warm() -> None:
            if not self.owned or not self.systemone:
                return
            self.owned.timeout_s = self.WARM_UP_S
            try:
                await self.embed_one("warm up")
                log.info("gate: %s reloaded", self.config.model)
            except GateError as exc:
                log.warning("gate: reload failed (%s)", exc)
            finally:
                self.owned.timeout_s = self.config.timeout_s

        self.rewarm = asyncio.get_running_loop().create_task(warm())

    async def embed_one(self, text: str) -> None:
        assert self.embedder
        await self.embedder([text])

    def stats(self) -> dict[str, Any]:
        return {
            "model": self.config.model if self.config.enabled else None,
            "ready": self.ready,
            "calls": self.calls,
            "sensitive_calls": self.sensitive_calls,
            "failures": self.failures,
            "last_ms": round(self.last_ms, 1) if self.last_ms is not None else None,
            "last_route": {k: v for k, v in self.last_route.to_dict().items() if k != "answers"} if self.last_route else None,
            "disabled_reason": self.disabled_reason or None,
        }
