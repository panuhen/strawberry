"""The gate's shipped data (WIRING.md §8a, src/strawberry_crab/data/): the data set, the held-out set
and the head trained on them fit together and fit the gate's questions."""

from __future__ import annotations

import json
from collections import Counter

from strawberry_crab import gatehead
from strawberry_crab.config import GateConfig
from strawberry_crab.systemone import IS_SENSITIVE, ROUTING, _options

QUESTIONS = {q.name: [o.name for o in _options(q)] for q in (*ROUTING, IS_SENSITIVE)}


def test_the_data_set_answers_every_option_of_every_question():
    samples, _ = gatehead.read_dataset()
    counts = Counter((q, v) for s in samples for q, v in s.labels.items())
    for name, options in QUESTIONS.items():
        for option in options:
            assert counts[(name, option)] >= 40, (name, option, counts[(name, option)])
    for s in samples:
        assert set(s.labels) <= set(QUESTIONS), s.text
        assert all(v in QUESTIONS[q] for q, v in s.labels.items()), s.text
        if "music_tool" in s.labels:
            assert s.labels.get("topic") == "music", s.text      # the tool question is music's only


def test_the_held_out_set_is_big_enough_separate_and_partly_finnish():
    heldout = json.loads(gatehead.HELDOUT_SET.read_text(encoding="utf-8"))
    phrases, notifications = heldout["phrases"], heldout["notifications"]
    assert len(phrases) >= 150 and len(notifications) >= 30
    finnish = sum(p["lang"] == "fi" for p in phrases) / len(phrases)
    assert 0.1 <= finnish <= 0.3
    kinds = Counter(p["kind"] for p in phrases)
    assert all(kinds[k] >= 10 for k in ("request", "question", "chat", "other"))
    tools = Counter(p["tool"] for p in phrases if "tool" in p)
    assert set(tools) == set(QUESTIONS["music_tool"])
    assert {n["sensitive"] for n in notifications} == {True, False}
    # The cosine separation is checked when the set is built (scripts/gate_build.py); here, no text in both.
    samples, _ = gatehead.read_dataset()
    trained = {s.text.lower().strip() for s in samples}
    assert not [p["text"] for p in phrases if p["text"].lower().strip() in trained]


def test_the_shipped_head_fits_the_shipped_questions_and_the_default_embedder():
    head = gatehead.load(gatehead.SHIPPED)
    config = GateConfig()
    assert config.scorer == "head"
    assert head.mismatch("embeddinggemma", config.query_prefix, 768, QUESTIONS) == ""
    assert 0.0 <= head.offer <= head.act <= 1.0
    _, dataset = gatehead.read_dataset()
    assert head.dataset == dataset          # the shipped head was trained on the shipped data set
