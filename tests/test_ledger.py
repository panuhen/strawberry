"""The ledger (WIRING.md §8b): her short memory, and who gets to read it."""

from __future__ import annotations

from strawberry.config import Config, ThinkerConfig
from strawberry.contract import Performance
from strawberry.daemon import Daemon
from strawberry.ledger import Ledger
from strawberry.server import create_app
from tests.test_thinker import ScriptedGate, make as make_thinker


def test_ledger_rolls_by_count_and_age():
    now = [1000.0]
    ledger = Ledger(max_turns=3, max_age_s=60.0, clock=lambda: now[0])
    assert ledger.context() == ""
    ledger.record("skip this", "Skipped. Now X.", did="skipped to the next track")
    now[0] += 20
    ledger.record("how are you", "Splendid.")
    text = ledger.context()
    assert text.startswith("Recent exchanges (newest last):")
    assert '- 20 s ago the user said "skip this"; you did "skipped to the next track" and said "Skipped. Now X."' in text
    assert '- 0 s ago the user said "how are you"; you said "Splendid."' in text
    for i in range(3):
        ledger.record(f"more {i}", "ok")
    assert [t.said for t in ledger.recent()] == ["more 0", "more 1", "more 2"]  # count cap
    assert ledger.context(limit=1).count("\n") == 1
    now[0] += 61
    assert ledger.recent() == [] and ledger.context() == "" and ledger.to_list() == []  # age cap
    ledger.record("late", "yes")
    assert ledger.to_list()[0]["ago_s"] == 0.0 and "1 min ago" not in ledger.context()


async def test_every_turn_is_recorded_and_given_back_to_qwen(aiohttp_client):
    config = Config()
    config.brain.enabled = config.speech.enabled = config.voice.enabled = False
    config.thinker = ThinkerConfig(acks=["On it."])
    spotify, toolbox, qwen, thinker = make_thinker(["[happy] Nina Simone is on.", "[neutral] That one was hers too."])
    thinker.config = config.thinker
    gate = ScriptedGate({})
    daemon = Daemon(reactor=None, config=config, gate=gate, toolbox=toolbox, thinker=thinker)  # type: ignore[arg-type]
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await client.post("/event", json={"source": "voice", "title": "put on some Nina Simone"})
    await client.post("/event", json={"source": "voice", "title": "who was that"})
    context = qwen.payloads[-1]["messages"][1]["content"]
    assert '- 0 s ago the user said "put on some Nina Simone"' in context
    assert 'and said "Nina Simone is on."' in context
    health = await (await client.get("/health")).json()
    assert [t["said"] for t in health["ledger"]] == ["put on some Nina Simone", "who was that"]
    assert [t["reply"] for t in health["ledger"]] == ["Nina Simone is on.", "That one was hers too."]
    assert "offers" not in health  # the offer path is gone (§8b)
    await daemon.close()


async def test_gemma_gets_the_last_lines_when_the_thinker_is_off(aiohttp_client):
    seen: list[str] = []

    class Listening:
        async def react(self, event, context=""):
            seen.append(context)
            return Performance(state="talking", text="Splendid.", emotion="happy")

    config = Config()
    config.brain.enabled = config.speech.enabled = config.voice.enabled = False
    config.thinker.enabled = config.tools.enabled = False
    daemon = Daemon(reactor=Listening(), config=config, gate=ScriptedGate({}))  # type: ignore[arg-type]
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await client.post("/event", json={"source": "voice", "title": "how are you"})
    await client.post("/event", json={"source": "voice", "title": "and now"})
    assert seen[0] == ""
    assert '- 0 s ago the user said "how are you"; you said "Splendid."' in seen[1]
    assert [t["said"] for t in daemon.ledger.to_list()] == ["how are you", "and now"]
    await daemon.close()
