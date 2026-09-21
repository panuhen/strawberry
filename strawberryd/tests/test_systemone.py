"""The local System One (WIRING.md §8a) on a fake embedder: the maths, the primitives, the gate."""

from __future__ import annotations

import hashlib
import math

import pytest

from strawberryd.config import Config, ConfigError, GateConfig, _validate
from strawberryd.daemon import Daemon
from strawberryd.events import CannedReactor, Event
from strawberryd.server import create_app
from strawberryd.systemone import (
    IS_ABOUT_HER, KIND, ROUTING, TOPIC, Choice, Gate, GateError, Noul, Option, Score, SystemOne, confidence, decide,
    softmax,
)

DIMS = 64


def bag_embed(text: str) -> list[float]:
    """A bag-of-words hash embedding: texts sharing words land near each other."""
    vector = [0.0] * DIMS
    for word in text.lower().replace(":", " ").split():
        if word in ("|", "task:", "query:", "title:", "text:", "none", "classification"):
            continue
        digest = hashlib.sha256(word.encode()).digest()
        for i in range(0, 8, 2):
            vector[digest[i] % DIMS] += 1.0 if digest[i + 1] % 2 else -1.0
    if not any(vector):
        vector[0] = 1.0
    return vector


class FakeEmbedder:
    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.texts: list[str] = []
        self.fail = fail

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.texts += texts
        if self.fail:
            raise GateError("ollama is down")
        return [bag_embed(t) for t in texts]


FRUIT = Choice("fruit", (
    Option("apple", "apples", ("a red apple", "green apple pie", "apple juice")),
    Option("banana", "bananas", ("a ripe banana", "banana bread", "banana split")),
    Option("other", "neither"),
))


def test_confidence_is_typesafes_peakedness():
    assert confidence([1 / 3, 1 / 3, 1 / 3]) == pytest.approx(0.0)
    assert confidence([1.0, 0.0, 0.0]) == pytest.approx(1.0)
    assert confidence([0.8, 0.1, 0.1]) == pytest.approx((3 * 0.8 - 1) / 2)
    assert confidence([0.5, 0.5]) == pytest.approx(0.0)
    assert confidence([0.6, 0.1, 0.1, 0.1, 0.1]) == pytest.approx((5 * 0.6 - 1) / 4)


def test_softmax_sums_to_one_and_temperature_sharpens():
    soft = softmax([0.9, 0.8, 0.1], 1.0)
    sharp = softmax([0.9, 0.8, 0.1], 0.05)
    assert sum(soft) == pytest.approx(1.0) and sum(sharp) == pytest.approx(1.0)
    assert sharp[0] > soft[0]


async def test_choice_picks_the_option_whose_examples_match():
    one = SystemOne(FakeEmbedder())
    answers = await one.ask("banana bread for breakfast", FRUIT)
    answer = answers["fruit"]
    assert answer.type == "choice" and answer.choice == "banana"
    assert sum(answer.probabilities.values()) == pytest.approx(1.0)
    assert answer.probabilities["banana"] > answer.probabilities["apple"] > 0.0
    assert 0.0 <= answer.confidence <= 1.0
    assert answer.to_dict()["choice"] == "banana"


async def test_examples_are_embedded_once_with_the_document_prefix():
    embedder = FakeEmbedder()
    one = SystemOne(embedder, query_prefix="Q: ", document_prefix="D: ")
    await one.prepare(FRUIT)
    assert embedder.calls == 1
    assert embedder.texts[0] == "D: a red apple" and one.examples == 7
    await one.ask("apple juice", FRUIT)
    await one.ask("banana split", FRUIT)
    assert embedder.calls == 3  # one call per sentence, none for the examples again
    assert embedder.texts[-1] == "Q: banana split"


async def test_score_is_the_expected_level_with_a_legend():
    rubric = Score("severity", (
        Option("fine", "nothing wrong", ("everything works fine",)),
        Option("workaround", "broken but a workaround exists", ("broken but there is a workaround",)),
        Option("blocked", "cannot work at all", ("completely blocked cannot work",)),
    ))
    one = SystemOne(FakeEmbedder())
    answer = (await one.ask("it is broken but I found a workaround", rubric))["severity"]
    assert answer.type == "score"
    assert answer.legend == {1: "fine", 2: "workaround", 3: "blocked"}
    assert 1.0 <= answer.score <= 3.0
    assert answer.probabilities["workaround"] == max(answer.probabilities.values())
    assert answer.score == pytest.approx(sum(level * answer.probabilities[name] for level, name in answer.legend.items()))


async def test_noul_is_a_probability_of_yes():
    noul = Noul("hungry", yes=Option("yes", "", ("I am hungry", "starving hungry")), no=Option("no", "", ("I am full", "ate plenty")))
    one = SystemOne(FakeEmbedder())
    answer = (await one.ask("so hungry right now", noul))["hungry"]
    assert answer.type == "noul"
    assert answer.score == pytest.approx(answer.probabilities["yes"])
    assert answer.score > 0.5


def test_decide_thresholds_per_consequence():
    assert decide("request", 0.95, act=0.6, offer=0.3) == "act"
    assert decide("question", 0.6, act=0.6, offer=0.3) == "act"
    assert decide("request", 0.45, act=0.6, offer=0.3) == "offer"
    assert decide("request", 0.1, act=0.6, offer=0.3) == "chat"
    assert decide("chat", 1.0, act=0.6, offer=0.3) == "chat"   # never acts, however sure
    assert decide("other", 1.0, act=0.6, offer=0.3) == "chat"


def test_shipped_routing_questions_are_well_formed():
    names = [q.name for q in ROUTING]
    assert names == ["kind", "topic", "is_urgent", "is_about_her"]
    assert [o.name for o in KIND.options] == ["request", "question", "chat", "other"]
    assert [o.name for o in TOPIC.options] == ["music", "calendar", "notes", "system", "other"]
    for question in (KIND, TOPIC):
        for option in question.options:
            assert len(option.examples) >= 4, f"{question.name}.{option.name} needs examples"
    assert IS_ABOUT_HER.yes.examples and IS_ABOUT_HER.no.examples


def test_config_examples_extend_an_option():
    kind = KIND.with_examples({"kind.request": ["put the kettle on"], "topic.music": ["ignored here"]})
    request = kind.options[0]
    assert request.examples[-1] == "put the kettle on"
    assert kind.options[1] is KIND.options[1]


def test_config_validation_of_gate_examples():
    config = Config()
    config.gate.examples = {"kind.request": ["fine"]}
    _validate(config)
    config.gate.examples = {"mood.happy": ["x"]}
    with pytest.raises(ConfigError):
        _validate(config)
    config.gate.examples = {"kind.request": "not a list"}  # type: ignore[dict-item]
    with pytest.raises(ConfigError):
        _validate(config)
    config.gate.examples = {}
    config.gate.offer = 0.9
    with pytest.raises(ConfigError):
        _validate(config)


async def test_gate_routes_and_logs_a_request():
    gate = Gate(GateConfig(query_prefix="", document_prefix=""), embedder=FakeEmbedder())
    await gate.start()
    assert gate.ready
    route = await gate.route("skip this track")
    assert route is not None
    assert route.kind == "request" and route.topic == "music"
    assert route.decision in ("act", "offer")
    assert set(route.answers) == {"kind", "topic", "is_urgent", "is_about_her"}
    stats = gate.stats()
    assert stats["calls"] == 1 and stats["ready"] is True
    assert stats["last_route"]["kind"] == "request" and "answers" not in stats["last_route"]


async def test_gate_failure_means_chat_not_a_crash():
    gate = Gate(GateConfig(), embedder=FakeEmbedder(fail=True))
    await gate.start()
    assert not gate.ready and "ollama is down" in gate.disabled_reason
    assert await gate.route("skip this track") is None
    assert gate.stats()["ready"] is False


async def test_gate_disabled_in_config_routes_nothing():
    gate = Gate(GateConfig(enabled=False), ollama_url="http://127.0.0.1:1")
    await gate.start()
    assert not gate.ready
    assert await gate.route("anything") is None
    assert gate.stats()["model"] is None and gate.stats()["disabled_reason"] == "disabled in config"


async def test_voice_events_pass_through_the_gate(aiohttp_client):
    config = Config()
    config.brain.enabled = False
    config.speech.enabled = False
    config.voice.enabled = False
    gate = Gate(GateConfig(query_prefix="", document_prefix=""), embedder=FakeEmbedder())
    daemon = Daemon(reactor=CannedReactor(), config=config, gate=gate)
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await daemon.handle_event(Event(source="voice", title="pause the music please"))
    await daemon.handle_event(Event(source="media", title="Nina Simone - Feeling Good"))
    assert gate.calls == 1  # only spoken sentences are routed
    health = await (await client.get("/health")).json()
    assert health["gate"]["last_route"]["text"] == "pause the music please"
    assert health["gate"]["last_route"]["kind"] == "request"
