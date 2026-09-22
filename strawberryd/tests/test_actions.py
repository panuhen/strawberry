"""The reflex tier (WIRING.md §8b): what fires, what is handed on, and what she says about it.

Which reflex runs — a configured server's adapter or MPRIS — is `tests/test_adapters.py`;
the Spotify adapter's own behaviour is `tests/test_adapter_spotify.py`.
"""

from __future__ import annotations

import pytest

from strawberryd.actions import Actor, Outcome, carries_argument
from strawberryd.brain import describe
from strawberryd.config import ActionsConfig, Config
from strawberryd.daemon import Daemon
from strawberryd.events import CannedReactor
from strawberryd.systemone import Route
from tests.fake_spotify import make


def route(text: str, tool: str, tool_confidence: float = 0.9, has_argument: float = 0.1, decision: str = "act",
          topic: str = "music", kind: str = "request") -> Route:
    return Route(text=text, kind=kind, topic=topic, confidence=0.9, is_urgent=0.2, is_about_her=0.1, decision=decision,
                 tool=tool, tool_confidence=tool_confidence, has_argument=has_argument)


@pytest.fixture(autouse=True)
def no_settle_wait(monkeypatch):
    """The reflexes wait for the player to catch up; not in tests."""
    import asyncio

    import strawberryd.actions as actions

    real_sleep = asyncio.sleep
    monkeypatch.setattr(actions.asyncio, "sleep", lambda s: real_sleep(0))


def test_outcome_becomes_an_action_event_asking_for_a_quip():
    ok = Outcome("skipped to the next track", "Skipped. Now Blue Monday by New Order.", True).event("skip this song")
    assert ok.source == "action" and ok.app == "skipped to the next track" and ok.category == ""
    text = describe(ok)
    assert text.startswith("source: action") and 'have just said: "Skipped. Now Blue Monday by New Order."' in text
    assert "asked: skip this song" in text and "at most 8 words" in text
    failed = Outcome("tried to pause", "I tried to pause, but Spotify said: no active device.", False).event("pause")
    assert failed.category == "failed" and failed.urgency == "critical"


def test_only_a_resume_sentence_can_carry_a_name():
    assert not carries_argument("skip this one", "skip")
    assert not carries_argument("ok play it please", "resume")
    assert carries_argument("play daft punk", "resume")
    assert carries_argument("play some classical music", "resume")


async def test_when_the_reflex_does_not_apply_she_answers_as_chat():
    spotify, toolbox, actor = make()
    assert await actor.act("play some jazz", route("play some jazz", "other")) is None
    assert await actor.act("play Nina Simone", route("play Nina Simone", "resume", has_argument=0.9)) is None
    assert await actor.act("skip maybe", route("skip maybe", "skip", tool_confidence=0.3)) is None
    # A name the embedding under-scored as an argument (live: "play daft punk" -> resume 0.71, argument 0.31).
    assert await actor.act("play daft punk", route("play daft punk", "resume", has_argument=0.31)) is None
    assert await actor.act("play some classical music", route("play some classical music", "resume", has_argument=0.37)) is None
    for bare in ("play", "ok play it", "play it please", "music back on please", "go on, play", "put it on again"):
        assert actor.reflex_for(route(bare, "resume")) is not None, bare
    assert await actor.act("skip", route("skip", "skip", decision="offer")) is None
    assert await actor.act("lock the screen", route("lock the screen", "", topic="system")) is None
    assert spotify.log == [] and actor.stats()["deferred"] == 6  # other, argument, low confidence, no reflex for system
    disabled = Actor(ActionsConfig(enabled=False), toolbox)
    assert await disabled.act("skip this song", route("skip this song", "skip")) is None
    await toolbox.close()


async def test_a_hung_tool_is_a_failure_not_a_stall():
    spotify, toolbox, actor = make(actions=ActionsConfig(timeout_s=0.05))

    async def hang(tb, server):
        import asyncio

        await asyncio.get_running_loop().create_future()

    actor.reflexes = {"spotify": {"skip": hang}}
    outcome = await actor.act("skip", route("skip", "skip"))
    assert not outcome.ok and outcome.fact == "I tried to skip, but spotify did not answer in time."
    await toolbox.close()


async def test_no_quip_after_a_paragraph_long_fact():
    from strawberryd.contract import Performance

    class Quipper:
        async def react(self, event, context=""):
            return Performance(state="talking", text="Frankly an upgrade.", emotion="happy")

    config = Config()
    config.brain.enabled = config.speech.enabled = config.voice.enabled = config.gate.enabled = False
    config.tools.enabled = config.thinker.enabled = config.actions.mpris = False
    daemon = Daemon(reactor=Quipper(), config=config)
    short = Outcome("skipped", "Skipped. Now Blue Monday by New Order.", True).event("skip")
    performance, _ = await daemon.report(short, True)
    assert performance.text == "Skipped. Now Blue Monday by New Order. Frankly an upgrade."
    long = Outcome("answered", "Led Zeppelin were a British rock band formed in London in 1968. " * 4, True).event("tell me")
    performance, _ = await daemon.report(long, True)
    assert performance.text == long.body and "upgrade" not in performance.text
