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

The routing questions for spoken sentences (`KIND`, `TOPIC`, `IS_URGENT`, `IS_ABOUT_HER`) and
the `Gate` that combines them into chat / offer / act live at the bottom of this file. The
examples are the whole model: add a phrase she misreads to the right option and it is fixed.
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
    )),
    Option("question", "wants a fact looked up or read out", (
        "what song is this", "who sings this", "what's on my calendar tomorrow", "when is my next meeting",
        "how long until my next appointment", "what day is it", "what time is it in Tokyo",
        "how many unread messages do I have", "what did I note down yesterday", "is it going to rain",
        "is the battery charging", "is the wifi still connected", "how much disk space is left",
    )),
    Option("chat", "small talk, feelings, banter, talking to her for its own sake", (
        "hello there", "good morning strawberry", "how was your night", "how are you doing today",
        "you're adorable", "you look lovely today", "I had a rough day", "I'm so tired today",
        "what do you think of this music", "do you like techno", "thanks strawberry", "good night",
        "you're a menace", "I love this song",
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
    Option("other", "none of those", (
        "how are you today", "good morning", "you're adorable", "I'm tired", "what time is it in Tokyo",
        "is it going to rain",
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

ROUTING: tuple[Question, ...] = (KIND, TOPIC, IS_URGENT, IS_ABOUT_HER)
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
            "ms": round(self.ms, 1),
            "answers": {k: v.to_dict() for k, v in self.answers.items()},
        }


def decide(kind: str, kind_confidence: float, act: float, offer: float) -> str:
    """Thresholds per consequence (WIRING §8a): act on a clear request, offer on a likely one,
    otherwise she just talks. `chat` and `other` never act, however confident."""
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

    def __init__(self, config: GateConfig, ollama_url: str = "", embedder: Embedder | None = None) -> None:
        self.config = config
        self.embedder = embedder
        self.owned: OllamaEmbedder | None = None
        if embedder is None and config.enabled:
            self.owned = OllamaEmbedder(config.model, ollama_url, config.timeout_s)
            self.embedder = self.owned
        self.systemone = SystemOne(self.embedder, config.temperature, config.neighbours, config.query_prefix,
                                   config.document_prefix) if self.embedder else None
        self.questions: tuple[Question, ...] = tuple(
            q.with_examples(config.examples) if isinstance(q, Choice) else q for q in ROUTING
        )
        self.ready = False
        self.calls = 0
        self.failures = 0
        self.last_route: Route | None = None
        self.last_ms: float | None = None
        self.disabled_reason = "" if config.enabled else "disabled in config"

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
            await self.systemone.prepare(*self.questions)
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
            return None
        ms = (time.perf_counter() - started) * 1000
        kind = answers["kind"]
        topic = answers["topic"]
        route = Route(
            text=text,
            kind=kind.choice or "other",
            topic=topic.choice if topic.confidence >= self.config.topic_min else "other",
            confidence=kind.confidence,
            is_urgent=answers["is_urgent"].score or 0.0,
            is_about_her=answers["is_about_her"].score or 0.0,
            decision=decide(kind.choice or "other", kind.confidence, self.config.act, self.config.offer),
            answers=answers,
            ms=ms,
        )
        self.last_route = route
        self.last_ms = ms
        log.info("gate: %r -> %s/%s conf %.2f -> %s (%.0f ms) %s", text, route.kind, route.topic, route.confidence,
                 route.decision, ms, {k: round(v, 2) for k, v in kind.probabilities.items()})
        return route

    def stats(self) -> dict[str, Any]:
        return {
            "model": self.config.model if self.config.enabled else None,
            "ready": self.ready,
            "calls": self.calls,
            "failures": self.failures,
            "last_ms": round(self.last_ms, 1) if self.last_ms is not None else None,
            "last_route": {k: v for k, v in self.last_route.to_dict().items() if k != "answers"} if self.last_route else None,
            "disabled_reason": self.disabled_reason or None,
        }
