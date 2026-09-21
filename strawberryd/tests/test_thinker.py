"""The thinker (WIRING.md §8b) on a fake Ollama and the fake Spotify: tool rounds, bounds, cover."""

from __future__ import annotations

import asyncio
import json

from strawberryd.config import Config, GateConfig, ThinkerConfig, ToolsConfig
from strawberryd.contract import Performance
from strawberryd.daemon import Daemon
from strawberryd.events import CannedReactor
from strawberryd.server import create_app
from strawberryd.systemone import Gate, Route
from strawberryd.thinker import KNOWLEDGE, SYSTEM, Thinker, ThinkerError, tidy_sentence
from strawberryd.tools import Toolbox
from tests.test_actions import TOOLS, FakeSpotify
from tests.test_systemone import FakeEmbedder
from tests.test_tools import FakeContent, FakeResult, FakeSession, FakeTool, make_connect
from tests.test_voice import Sink


class FakeQwen:
    """Scripted replies: each item is a list of tool calls, or a final string."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.payloads: list[dict] = []

    async def __call__(self, payload: dict) -> dict:
        self.payloads.append(payload)
        if not self.script:
            return {"message": {"role": "assistant", "content": "I ran out of script."}}
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, str):
            return {"message": {"role": "assistant", "content": step}}
        calls = [{"function": {"name": name, "arguments": args}} for name, args in step]
        return {"message": {"role": "assistant", "content": "", "tool_calls": calls}}


def make(script: list, config: ThinkerConfig | None = None, tools: list[FakeTool] | None = None, handler=None):
    spotify = FakeSpotify()
    session = FakeSession(tools or TOOLS + [FakeTool("search"), FakeTool("save_tracks")], handler or spotify.handle)
    toolbox = Toolbox(ToolsConfig(servers={"spotify": {"topic": "music", "command": "spotify", "careful": ["save_tracks"]}},
                                  preconnect=False),
                      connect=make_connect({"spotify": session}))
    qwen = FakeQwen(script)
    thinker = Thinker(config or ThinkerConfig(), toolbox, "qwen-test", chat=qwen)
    return spotify, toolbox, qwen, thinker


def test_tidy_sentence():
    assert tidy_sentence("**Done.** Playing Feeling Good.\n\nAnything else?") == "Done. Playing Feeling Good."
    assert tidy_sentence("  spaced   out  ") == "spaced out"
    long = tidy_sentence("word " * 100, limit=40)
    assert long.endswith("…") and len(long) <= 42
    prose = "Led Zeppelin were a British rock band. They formed in London in 1968. Their fourth album sold very well indeed."
    assert tidy_sentence(prose, limit=80) == "Led Zeppelin were a British rock band. They formed in London in 1968."
    assert tidy_sentence('She said "yes." Then she left the room quietly.', limit=20) == 'She said "yes."'


async def test_tool_rounds_then_a_fact():
    async def handler(name, arguments):
        if name == "search":
            return FakeResult([FakeContent(json.dumps({"tracks": [{"name": "Feeling Good", "uri": "spotify:track:1"}]}))])
        if name == "play":
            return FakeResult([FakeContent(json.dumps({"success": True}))])
        return FakeResult([FakeContent("{}")])

    spotify, toolbox, qwen, thinker = make(
        [[("search", {"query": "Nina Simone Feeling Good"})], [("play", {"uri": "spotify:track:1"})], "Now playing Feeling Good by Nina Simone."],
        handler=handler,
    )
    outcome = await thinker.run("play feeling good by nina simone", "music")
    assert outcome.ok and outcome.fact == "Now playing Feeling Good by Nina Simone."
    assert outcome.did == "used search, play"
    assert [c.name for c in outcome.calls] == ["search", "play"]
    # The conversation Qwen saw: system, user, assistant(tool call), tool, assistant(tool call), tool.
    last = qwen.payloads[-1]
    roles = [m["role"] for m in last["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "tool"]
    assert last["messages"][0]["content"] == SYSTEM and last["messages"][3]["tool_name"] == "search"
    assert last["messages"][1]["content"] == "play feeling good by nina simone"  # no situation given: plain
    assert "Feeling Good" in last["messages"][3]["content"]
    assert last["think"] is False and last["keep_alive"] == "10m" and last["options"]["num_ctx"] == 8192
    assert {t["function"]["name"] for t in last["tools"]} >= {"search", "play", "next"}
    assert "save_tracks" not in {t["function"]["name"] for t in last["tools"]}  # careful, and nobody asked
    assert thinker.stats()["calls"] == 1 and thinker.stats()["last"]["fact"] == outcome.fact
    await toolbox.close()


async def test_max_rounds_forces_an_answer_without_tools():
    spotify, toolbox, qwen, thinker = make(
        [[("get_current_track", {})]] * 2 + ["Best I can tell, Feeling Good is playing."],
        config=ThinkerConfig(max_rounds=2),
    )
    outcome = await thinker.run("what is this", "music")
    assert len(qwen.payloads) == 3  # two tool rounds, then the forced final
    assert "tools" not in qwen.payloads[-1]
    assert qwen.payloads[-1]["messages"][-1]["content"].startswith("You can call no more tools")
    assert "Never claim an action" in qwen.payloads[-1]["messages"][-1]["content"]
    assert outcome.ok and outcome.fact == "Best I can tell, Feeling Good is playing."
    assert spotify.log == ["get_current_track", "get_current_track"]
    await toolbox.close()


async def test_unknown_tool_and_string_arguments_are_survivable():
    spotify, toolbox, qwen, thinker = make([[("teleport", '{"where": "moon"}')], [("set_volume", '{"volume": 30}')], "Volume is now 30."])
    outcome = await thinker.run("volume to thirty", "music")
    assert outcome.ok and spotify.volume == 30
    tool_messages = [m for m in qwen.payloads[-1]["messages"] if m["role"] == "tool"]
    assert "no tool named 'teleport'" in tool_messages[0]["content"]
    await toolbox.close()


async def test_failures_are_outcomes_not_exceptions():
    _, toolbox, _, down = make([ThinkerError("connection refused")])
    outcome = await down.run("play something", "music")
    assert not outcome.ok and "not answering" in outcome.fact and down.stats()["failures"] == 1

    _, toolbox2, _, empty = make([""])
    outcome = await empty.run("play something", "music")
    assert not outcome.ok and "got lost" in outcome.fact

    class Slow(FakeQwen):
        async def __call__(self, payload):
            await asyncio.sleep(60)

    _, toolbox3, _, slow = make([])
    slow.chat = Slow([])
    slow.config = ThinkerConfig(timeout_s=0.05)
    outcome = await slow.run("play something", "music")
    assert not outcome.ok and "too long" in outcome.fact
    for box in (toolbox, toolbox2, toolbox3):
        await box.close()


async def test_no_tools_for_the_topic_is_a_plain_failure():
    _, toolbox, qwen, thinker = make(["unused"])
    assert not thinker.can_handle("calendar") and thinker.can_handle("music")
    outcome = await thinker.run("what's on tomorrow", "calendar")
    assert not outcome.ok and "calendar tools" in outcome.fact and qwen.payloads == []
    await toolbox.close()


async def test_an_argument_request_goes_to_the_thinker_with_cover(aiohttp_client):
    """'put on some jazz': reflex declines (argument), she acks in the thinking pose, then reports."""
    config = Config()
    config.brain.enabled = config.speech.enabled = config.voice.enabled = False
    config.thinker = ThinkerConfig(still_on_it_s=0.02, acks=["On it."])
    spotify, toolbox, qwen, thinker = make(["a slow one", "Now playing Feeling Good by Nina Simone."])

    class SlowFirst(FakeQwen):
        async def __call__(self, payload):
            await asyncio.sleep(0.05)  # longer than still_on_it_s
            return await super().__call__(payload)

    thinker.chat = SlowFirst(["Now playing Feeling Good by Nina Simone.", "Queued."])
    thinker.config = config.thinker
    gate = Gate(GateConfig(query_prefix="", document_prefix=""), embedder=FakeEmbedder())
    daemon = Daemon(reactor=CannedReactor(), config=config, gate=gate, toolbox=toolbox, thinker=thinker)
    sink = Sink()
    daemon.hub.add(sink)  # type: ignore[arg-type]
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    response = await client.post("/event", json={"source": "voice", "title": "put on some jazz"})
    body = await response.json()
    route = gate.last_route
    assert route.decision == "act" and route.has_argument > 0.5, route.to_dict()
    assert daemon.actor.stats()["deferred"] == 1 and thinker.stats()["calls"] == 1
    texts = [m.get("text") for m in sink.got if m.get("state") in ("thinking", "talking")]
    assert texts == ["On it.", "Still on it.", "Now playing Feeling Good by Nina Simone."]
    states = [m["state"] for m in sink.got if "state" in m]
    assert states[:2] == ["thinking", "thinking"] and states[-1] == "talking"
    assert body["performance"]["text"] == "Now playing Feeling Good by Nina Simone."
    health = await (await client.get("/health")).json()
    assert health["thinker"]["last"]["asked"] == "put on some jazz"
    # The thinker was told what is playing, and "get_current_track" was how it learnt it.
    first_user = thinker.chat.payloads[0]["messages"][1]["content"]
    assert first_user.startswith("Situation: Now playing on Spotify: Feeling Good by Nina Simone (album: I Put a Spell on You).")
    daemon.vocabulary = ["Daft Punk", "New Order"]
    await client.post("/event", json={"source": "voice", "title": "put on some jazz"})
    assert "Names in the user's library: Daft Punk, New Order." in thinker.chat.payloads[-1]["messages"][1]["content"]
    assert first_user.endswith("The user says: put on some jazz")
    assert spotify.log[0] == "get_current_track"
    await daemon.close()


async def test_a_question_with_no_tools_is_answered_from_memory():
    _, toolbox, qwen, thinker = make(["Dwight D. Eisenhower was President of the United States in 1960."])
    outcome = await thinker.answer("who was the president of the united states in 1960", "Today is Monday.")
    assert outcome.ok and outcome.fact == "Dwight D. Eisenhower was President of the United States in 1960."
    assert outcome.did == "answered from memory"
    payload = qwen.payloads[-1]
    assert payload["messages"][0]["content"] == KNOWLEDGE and "tools" not in payload
    assert payload["messages"][1]["content"] == "Situation: Today is Monday.\n\nThe user asks: who was the president of the united states in 1960"
    assert thinker.stats()["last"]["topic"] == "knowledge"
    _, toolbox2, _, silent = make([""])
    assert not (await silent.answer("anything")).ok
    for box in (toolbox, toolbox2):
        await box.close()


async def test_daemon_sends_a_clear_question_without_tools_to_memory(aiohttp_client):
    config = Config()
    config.brain.enabled = config.speech.enabled = config.voice.enabled = False
    config.thinker = ThinkerConfig(acks=["Let me see."])
    _, toolbox, qwen, thinker = make(["Eisenhower, until January 1961."])
    thinker.config = config.thinker
    daemon = Daemon(reactor=CannedReactor(), config=config, toolbox=toolbox, thinker=thinker)
    sink = Sink()
    daemon.hub.add(sink)  # type: ignore[arg-type]
    await daemon.start()
    route = Route(text="who was president in 1960", kind="question", topic="other", confidence=0.95, is_urgent=0.1,
                  is_about_her=0.05, decision="act")
    outcome = await daemon.think("who was president in 1960", route, knowledge=True)
    assert outcome.ok and outcome.fact == "Eisenhower, until January 1961."
    assert [m.get("text") for m in sink.got] == ["Let me see."]
    assert qwen.payloads[-1]["messages"][1]["content"].startswith("Situation: Today is ")
    # A request (not a question) for a topic without tools still falls through to chat.
    assert not thinker.can_handle("other")
    await daemon.close()


async def test_careful_tools_appear_only_when_the_sentence_asks_for_a_library_change():
    _, toolbox, qwen, thinker = make(["Saved.", "Played."])
    await thinker.run("save this song", "music", careful=True)
    assert "save_tracks" in {t["function"]["name"] for t in qwen.payloads[-1]["tools"]}
    await thinker.run("play some jazz", "music", careful=False)
    assert "save_tracks" not in {t["function"]["name"] for t in qwen.payloads[-1]["tools"]}
    await toolbox.close()
