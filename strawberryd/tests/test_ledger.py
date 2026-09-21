"""The ledger and the offer (WIRING.md §8b): short memory, and a yes within ten seconds."""

from __future__ import annotations

from strawberryd.config import ActionsConfig, Config, ThinkerConfig
from strawberryd.daemon import Daemon
from strawberryd.events import CannedReactor
from strawberryd.ledger import Ledger
from strawberryd.server import create_app
from strawberryd.systemone import Route
from tests.test_thinker import make as make_thinker
from tests.test_voice import Sink


def route(text: str, **kw) -> Route:
    base = dict(text=text, kind="chat", topic="other", confidence=0.9, is_urgent=0.1, is_about_her=0.1, decision="chat")
    base.update(kw)
    return Route(**base)


class ScriptedGate:
    """Preset readings per sentence: these tests are about what the daemon does with a route."""

    ready = True
    calls = 0

    def __init__(self, routes: dict[str, Route]) -> None:
        self.routes = routes
        self.last_route: Route | None = None

    async def start(self) -> None: ...
    async def close(self) -> None: ...

    async def route(self, text: str) -> Route | None:
        self.last_route = self.routes.get(text) or route(text)
        return self.last_route

    def stats(self) -> dict:
        return {"scripted": True}


OFFER = route("put on some Nina Simone", kind="request", topic="music", confidence=0.45, decision="offer", tool="other",
              tool_confidence=0.8, has_argument=0.95)
ROUTES = {
    "put on some Nina Simone": OFFER,
    "yes please": route("yes please", kind="other", is_yes="yes", is_yes_confidence=0.9),
    "no thanks": route("no thanks", kind="other", is_yes="no", is_yes_confidence=0.9),
}


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


async def test_offer_then_yes_routes_the_pending_sentence(aiohttp_client):
    config = Config()
    config.brain.enabled = config.speech.enabled = config.voice.enabled = False
    config.thinker = ThinkerConfig(acks=["On it."])
    config.actions = ActionsConfig(offer_window_s=10.0)
    spotify, toolbox, qwen, thinker = make_thinker(["Now playing Feeling Good by Nina Simone."])
    thinker.config = config.thinker
    gate = ScriptedGate(ROUTES)
    daemon = Daemon(reactor=CannedReactor(), config=config, gate=gate, toolbox=toolbox, thinker=thinker)  # type: ignore[arg-type]
    sink = Sink()
    daemon.hub.add(sink)  # type: ignore[arg-type]
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    response = await client.post("/event", json={"source": "voice", "title": "put on some Nina Simone"})
    body = await response.json()
    assert gate.last_route.decision == "offer", gate.last_route.to_dict()
    assert body["performance"]["text"].endswith("Want me to do that?")
    assert daemon.offer is not None and thinker.calls == 0
    health = await (await client.get("/health")).json()
    assert health["offers"] == {"made": 1, "taken": 0, "open": True}
    # The yes.
    response = await client.post("/event", json={"source": "voice", "title": "yes please"})
    body = await response.json()
    assert gate.last_route.is_yes == "yes", gate.last_route.to_dict()
    assert thinker.calls == 1 and daemon.offer is None
    assert body["performance"]["text"] == "Now playing Feeling Good by Nina Simone."
    assert qwen.payloads[0]["messages"][1]["content"].endswith("The user says: put on some Nina Simone")
    health = await (await client.get("/health")).json()
    assert health["offers"] == {"made": 1, "taken": 1, "open": False}
    ledger = health["ledger"]
    assert [t["said"] for t in ledger] == ["put on some Nina Simone", "put on some Nina Simone"]
    assert ledger[-1]["did"] == "answered without tools"  # the scripted Qwen answered straight away
    await daemon.close()


async def test_offer_then_no_or_something_else(aiohttp_client):
    config = Config()
    config.brain.enabled = config.speech.enabled = config.voice.enabled = False
    spotify, toolbox, qwen, thinker = make_thinker(["unused"])
    gate = ScriptedGate(ROUTES)
    daemon = Daemon(reactor=CannedReactor(), config=config, gate=gate, toolbox=toolbox, thinker=thinker)  # type: ignore[arg-type]
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await client.post("/event", json={"source": "voice", "title": "put on some Nina Simone"})
    assert daemon.offer is not None
    body = await (await client.post("/event", json={"source": "voice", "title": "no thanks"})).json()
    assert body["performance"]["text"] == "Right, leaving it." and daemon.offer is None and thinker.calls == 0
    # An offer that expires, then an unrelated sentence: no action, the offer is gone.
    await client.post("/event", json={"source": "voice", "title": "put on some Nina Simone"})
    daemon.offer = (daemon.offer[0], daemon.offer[1], 0.0)
    await client.post("/event", json={"source": "voice", "title": "yes please"})
    assert daemon.offer is None and thinker.calls == 0
    # The ledger reaches Gemma's context for the next chat line and Qwen's situation.
    assert "put on some Nina Simone" in daemon.ledger.context()
    await daemon.close()
