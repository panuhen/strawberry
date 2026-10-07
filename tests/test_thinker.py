"""The thinker (WIRING.md §8b) on a fake Ollama and the fake Spotify: tool rounds, voice, bounds."""

from __future__ import annotations

import asyncio
import copy
import json

from strawberry_crab.config import ActionsConfig, Config, GateConfig, ThinkerConfig, ToolsConfig
from strawberry_crab.contract import Performance
from strawberry_crab.daemon import Daemon
from strawberry_crab.events import CannedReactor
from strawberry_crab.server import create_app
from strawberry_crab.systemone import Gate, Route
from strawberry_crab.thinker import (CUT, MIN_RESULT_CHARS, NO_TOOLS, NOT_OFFERED, TOOLS_GUIDE, VOICE, Thinker, ThinkerError,
                                     prompt_tokens, split_emotion, system_prompt, tidy_sentence)
from strawberry_crab.tools import Toolbox
from tests.fake_spotify import TOOLS, FakeSpotify, fake_gate
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


class ScriptedGate:
    """Preset readings per sentence: for tests about what the daemon does with a route."""

    ready = True
    calls = 0

    def __init__(self, routes: dict[str, Route]) -> None:
        self.routes = routes
        self.last_route: Route | None = None

    async def start(self) -> None: ...
    async def close(self) -> None: ...

    async def route(self, text: str) -> Route | None:
        self.last_route = self.routes.get(text) or reading(text)
        return self.last_route

    def stats(self) -> dict:
        return {"scripted": True}


def reading(text: str, **kw) -> Route:
    """A gate reading, chat by default; keyword arguments override any field."""
    base = dict(text=text, kind="chat", topic="other", confidence=0.9, is_urgent=0.1, is_about_her=0.1, decision="chat")
    base.update(kw)
    return Route(**base)


def make(script: list, config: ThinkerConfig | None = None, tools: list[FakeTool] | None = None, handler=None):
    spotify = FakeSpotify()
    session = FakeSession(tools or TOOLS + [FakeTool("search"), FakeTool("save_tracks")], handler or spotify.handle)
    toolbox = Toolbox(ToolsConfig(servers={"spotify": {"topic": "music", "command": "spotify", "careful": ["save_tracks"]}},
                                  preconnect=False),
                      connect=make_connect({"spotify": session}))
    qwen = FakeQwen(script)
    thinker = Thinker(config or ThinkerConfig(), toolbox, "qwen-test", chat=qwen)
    return spotify, toolbox, qwen, thinker


def bare(script: list):
    """A thinker with no servers at all: she has to answer from what she knows."""
    toolbox = Toolbox(ToolsConfig(servers={}, preconnect=False))
    qwen = FakeQwen(script)
    return toolbox, qwen, Thinker(ThinkerConfig(), toolbox, "qwen-test", chat=qwen)


def test_tidy_sentence():
    assert tidy_sentence("**Done.** Playing Feeling Good.\n\nAnything else?") == "Done. Playing Feeling Good."
    assert tidy_sentence("  spaced   out  ") == "spaced out"
    long = tidy_sentence("word " * 100, limit=40)
    assert long.endswith("…") and len(long) <= 42
    prose = "Led Zeppelin were a British rock band. They formed in London in 1968. Their fourth album sold very well indeed."
    assert tidy_sentence(prose, limit=80) == "Led Zeppelin were a British rock band. They formed in London in 1968."
    assert tidy_sentence('She said "yes." Then she left the room quietly.', limit=20) == 'She said "yes."'


def test_split_emotion_off_the_front_of_a_reply():
    assert split_emotion("[happy] Skipped. Blue Monday next.") == ("happy", "Skipped. Blue Monday next.")
    assert split_emotion("[ALERT]: Spotify is sulking.") == ("alert", "Spotify is sulking.")
    assert split_emotion("(angry) Three tests down.") == ("angry", "Three tests down.")
    assert split_emotion("Nothing tagged here.") == ("neutral", "Nothing tagged here.")
    assert split_emotion("[cheerful] not one of the four") == ("neutral", "[cheerful] not one of the four")
    assert split_emotion("[happy]") == ("neutral", "[happy]")   # a tag and nothing else keeps the words
    assert split_emotion("", default="alert") == ("alert", "")


async def test_tool_rounds_then_her_line():
    async def handler(name, arguments):
        if name == "search":
            return FakeResult([FakeContent(json.dumps({"tracks": [{"name": "Feeling Good", "uri": "spotify:track:1"}]}))])
        if name == "play":
            return FakeResult([FakeContent(json.dumps({"success": True}))])
        return FakeResult([FakeContent("{}")])

    spotify, toolbox, qwen, thinker = make(
        [[("search", {"query": "Nina Simone Feeling Good"})], [("play", {"uri": "spotify:track:1"})],
         "[happy] Nina Simone it is. Feeling Good is on."],
        handler=handler,
    )
    outcome = await thinker.run("play feeling good by nina simone")
    assert outcome.ok and outcome.fact == "Nina Simone it is. Feeling Good is on."
    assert outcome.emotion == "happy" and outcome.did == "used search, play"
    assert [c.name for c in outcome.calls] == ["search", "play"]
    # The conversation Qwen saw: system, user, assistant(tool call), tool, assistant(tool call), tool.
    last = qwen.payloads[-1]
    roles = [m["role"] for m in last["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "tool"]
    assert last["messages"][0]["content"].startswith(VOICE) and TOOLS_GUIDE in last["messages"][0]["content"]
    assert last["messages"][3]["tool_name"] == "search"
    assert last["messages"][1]["content"] == "play feeling good by nina simone"  # no situation given: plain
    assert "Feeling Good" in last["messages"][3]["content"]
    assert last["think"] is False and last["keep_alive"] == "30m" and last["options"]["num_ctx"] == 8192
    assert {t["function"]["name"] for t in last["tools"]} >= {"search", "play", "next"}
    assert "save_tracks" not in {t["function"]["name"] for t in last["tools"]}  # careful, and nobody asked
    assert thinker.stats()["calls"] == 1 and thinker.stats()["last"]["said"] == outcome.fact
    assert thinker.stats()["last"]["emotion"] == "happy"
    await toolbox.close()


async def test_max_rounds_forces_an_answer_without_tools():
    spotify, toolbox, qwen, thinker = make(
        [[("get_current_track", {})]] * 2 + ["[neutral] Best I can tell, Feeling Good is playing."],
        config=ThinkerConfig(max_rounds=2),
    )
    outcome = await thinker.run("what is this")
    assert len(qwen.payloads) == 3  # two tool rounds, then the forced final
    assert "tools" not in qwen.payloads[-1]
    assert qwen.payloads[-1]["messages"][-1]["content"].startswith("You can call no more tools")
    assert "Never claim an action" in qwen.payloads[-1]["messages"][-1]["content"]
    assert outcome.ok and outcome.fact == "Best I can tell, Feeling Good is playing."
    assert spotify.log == ["get_current_track", "get_current_track"]
    await toolbox.close()


async def test_unknown_tool_and_string_arguments_are_survivable():
    spotify, toolbox, qwen, thinker = make([[("teleport", '{"where": "moon"}')], [("set_volume", '{"volume": 30}')],
                                            "[neutral] Volume is thirty now."])
    outcome = await thinker.run("volume to thirty")
    assert outcome.ok and spotify.volume == 30
    tool_messages = [m for m in qwen.payloads[-1]["messages"] if m["role"] == "tool"]
    # A tool that was not offered for this sentence is never called, from any server: refused in place.
    assert tool_messages[0]["content"] == NOT_OFFERED
    assert [c.name for c in outcome.calls] == ["set_volume"] and outcome.emotion == "neutral"
    await toolbox.close()


async def test_failures_are_outcomes_not_exceptions():
    _, toolbox, _, down = make([ThinkerError("connection refused")])
    outcome = await down.run("play something")
    assert not outcome.ok and "not answering" in outcome.fact and outcome.emotion == "alert"
    assert down.stats()["failures"] == 1

    _, toolbox2, _, empty = make([""])
    outcome = await empty.run("play something")
    assert not outcome.ok and "got lost" in outcome.fact and outcome.emotion == "alert"

    class Slow(FakeQwen):
        async def __call__(self, payload):
            await asyncio.sleep(60)

    _, toolbox3, _, slow = make([])
    slow.chat = Slow([])
    slow.config = ThinkerConfig(timeout_s=0.05)
    outcome = await slow.run("play something")
    assert not outcome.ok and "too long" in outcome.fact
    for box in (toolbox, toolbox2, toolbox3):
        await box.close()


async def test_with_no_servers_she_answers_from_what_she_knows():
    toolbox, qwen, thinker = bare(["[neutral] Dwight D. Eisenhower was president in 1960."])
    outcome = await thinker.run("who was the president of the united states in 1960", "Today is Monday.")
    assert outcome.ok and outcome.fact == "Dwight D. Eisenhower was president in 1960."
    assert outcome.did == "answered without tools" and outcome.emotion == "neutral"
    payload = qwen.payloads[-1]
    assert "tools" not in payload and NO_TOOLS in payload["messages"][0]["content"]
    assert payload["messages"][1]["content"] == (
        "Situation: Today is Monday.\n\nThe user says: who was the president of the united states in 1960")
    assert len(qwen.payloads) == 1  # no tools means no rounds to spend
    await toolbox.close()


async def test_careful_tools_appear_only_when_the_sentence_asks_for_a_library_change():
    _, toolbox, qwen, thinker = make(["[happy] Saved.", "[happy] Played."])
    await thinker.run("save this song", careful=True)
    assert "save_tracks" in {t["function"]["name"] for t in qwen.payloads[-1]["tools"]}
    await thinker.run("play some jazz", careful=False)
    assert "save_tracks" not in {t["function"]["name"] for t in qwen.payloads[-1]["tools"]}
    await toolbox.close()


def two_servers(max_tools: int = 30):
    """A music server with an adapter (ten tools) and a plain notes server (eight)."""
    spotify = FakeSpotify()
    notes = FakeSession([FakeTool(f"note_{i}") for i in range(8)], spotify.handle)
    toolbox = Toolbox(ToolsConfig(servers={"spotify": {"topic": "music", "command": "spotify"},
                                           "notes": {"topic": "notes", "command": "notes"}}, preconnect=False),
                      connect=make_connect({"spotify": FakeSession(TOOLS, spotify.handle), "notes": notes}))
    return toolbox, Thinker(ThinkerConfig(max_tools=max_tools), toolbox, "qwen-test", chat=FakeQwen([]))


async def test_every_tool_is_offered_while_they_fit_in_one_order_for_every_sentence():
    """The same order whatever the gate's topic: Ollama reuses its cached prompt up to the first
    token that differs, and a reordered tool list cost 2.1-2.7 s of re-reading (WIRING §8b)."""
    toolbox, thinker = two_servers()
    specs = await thinker.tools(topic="music")
    assert len(specs) == 18
    assert [s.server for s in specs[:10]] == ["spotify"] * 10   # music before notes: the topics' order
    assert [s.server for s in specs[10:]] == ["notes"] * 8
    assert [s.key for s in await thinker.tools(topic="notes")] == [s.key for s in specs]
    assert [s.key for s in await thinker.tools(topic="")] == [s.key for s in specs]
    await toolbox.close()


async def test_over_max_tools_the_topic_and_the_common_tools_survive(caplog):
    toolbox, thinker = two_servers(max_tools=6)
    with caplog.at_level("INFO", logger="strawberryd.thinker"):
        specs = await thinker.tools(topic="music")
    # The music server's own common tools, in the adapter's order; the notes server is out.
    assert [s.name for s in specs] == ["play", "pause", "next", "previous", "get_current_track", "get_playlists"]
    assert "18 tools is more than max_tools=6" in caplog.text
    assert "spotify.get_devices" in caplog.text and "notes.note_0" in caplog.text
    # A sentence the gate read as notes keeps the notes tools instead.
    assert [s.name for s in await thinker.tools(topic="notes")] == [f"note_{i}" for i in range(6)]
    # No topic (the gate is off, or it read "other") keeps the alphabetical order it had before.
    assert [s.server for s in await thinker.tools(topic="")] == ["spotify"] * 6
    await toolbox.close()


async def test_the_gates_topic_reaches_the_thinker(aiohttp_client):
    config = plain_config()
    spotify, toolbox, qwen, thinker = make(["[neutral] Nothing much."])
    gate = ScriptedGate({"what is playing": reading("what is playing", kind="question", topic="music", decision="act",
                                                    tool="other")})
    daemon, sink = voice_daemon(config, toolbox, thinker, gate=gate)  # type: ignore[arg-type]
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    seen: list[str] = []
    real = thinker.tools

    async def watched(careful=False, topic="", **kw):
        seen.append(topic)
        return await real(careful, topic, **kw)

    thinker.tools = watched  # type: ignore[assignment]
    await client.post("/event", json={"source": "voice", "title": "what is playing"})
    assert seen == ["music"]
    await daemon.close()


def voice_daemon(config: Config, toolbox, thinker, gate=None, actor=None):
    daemon = Daemon(reactor=CannedReactor(), config=config, gate=gate, toolbox=toolbox, thinker=thinker, actor=actor)
    sink = Sink()
    daemon.hub.add(sink)  # type: ignore[arg-type]
    return daemon, sink


def plain_config() -> Config:
    config = Config()
    config.brain.enabled = config.speech.enabled = config.voice.enabled = False
    return config


async def test_a_quick_qwen_reply_gets_the_pose_but_no_spoken_ack(aiohttp_client):
    """A warm round is ~2 s: she thinks visibly and answers; "On it." before "what's up?" read odd."""
    config = plain_config()
    config.thinker = ThinkerConfig(ack_after_s=5.0, still_on_it_s=8.0, acks=["On it."])
    spotify, toolbox, qwen, thinker = make([])
    thinker.chat = FakeQwen(["[neutral] Just some Mozart drifting through."])
    thinker.config = config.thinker
    gate = fake_gate(toolbox)
    daemon, sink = voice_daemon(config, toolbox, thinker, gate=gate)
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await client.post("/event", json={"source": "voice", "title": "what is up"})
    texts = [m.get("text") for m in sink.got if m.get("state") in ("thinking", "talking")]
    assert texts == [None, "Just some Mozart drifting through."]
    await daemon.close()


async def test_an_argument_request_goes_to_qwen_with_cover(aiohttp_client):
    """'put on some jazz': no reflex for it; the thinking pose at once, the spoken ack and "still on
    it" only because Qwen is slow here, then her line."""
    config = plain_config()
    config.thinker = ThinkerConfig(ack_after_s=0.02, still_on_it_s=0.08, acks=["On it."])
    spotify, toolbox, qwen, thinker = make([])

    class SlowFirst(FakeQwen):
        async def __call__(self, payload):
            await asyncio.sleep(0.4)  # longer than still_on_it_s
            return await super().__call__(payload)

    thinker.chat = SlowFirst(["[happy] Jazz it is. Feeling Good is on.", "[neutral] Queued."])
    thinker.config = config.thinker
    gate = fake_gate(toolbox)
    daemon, sink = voice_daemon(config, toolbox, thinker, gate=gate)
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    response = await client.post("/event", json={"source": "voice", "title": "put on some jazz"})
    body = await response.json()
    route = gate.last_route
    assert route.has_argument > 0.5, route.to_dict()
    assert daemon.actor.stats()["deferred"] == 1 and thinker.stats()["calls"] == 1
    texts = [m.get("text") for m in sink.got if m.get("state") in ("thinking", "talking")]
    assert texts == [None, "On it.", "Still on it.", "Jazz it is. Feeling Good is on."]
    states = [m["state"] for m in sink.got if "state" in m]
    assert states[:3] == ["thinking"] * 3 and states[-1] == "talking"
    assert body["performance"]["text"] == "Jazz it is. Feeling Good is on."
    assert body["performance"]["emotion"] == "happy" and body["performance"]["reaction"] == "nod"
    health = await (await client.get("/health")).json()
    assert health["thinker"]["last"]["asked"] == "put on some jazz"
    assert health["ledger"][-1]["said"] == "put on some jazz"
    assert health["ledger"][-1]["reply"] == "Jazz it is. Feeling Good is on."
    # The situation she was given: the date, the player, then the sentence.
    first_user = thinker.chat.payloads[0]["messages"][1]["content"]
    assert first_user.startswith("Situation: Today is ")
    assert "Now playing on Spotify: Feeling Good by Nina Simone (album: I Put a Spell on You)." in first_user
    assert first_user.endswith("The user says: put on some jazz")
    daemon.vocabulary = ["Daft Punk", "New Order"]
    await client.post("/event", json={"source": "voice", "title": "put on some jazz"})
    later = thinker.chat.payloads[-1]["messages"][1]["content"]
    assert "Names in the user's library: Daft Punk, New Order." in later
    assert "Recent exchanges (newest last):" in later  # the ledger reaches Qwen
    assert spotify.log[0] == "get_current_track"
    await daemon.close()


async def test_small_talk_is_her_line_from_qwen_not_a_canned_echo(aiohttp_client):
    config = plain_config()
    config.thinker = ThinkerConfig(acks=["Let me see."])
    spotify, toolbox, qwen, thinker = make(["[happy] Splendid, thanks. Still judging your typing."])
    thinker.config = config.thinker
    gate = ScriptedGate({"how are you doing today": reading("how are you doing today", is_about_her=0.9)})
    daemon, sink = voice_daemon(config, toolbox, thinker, gate=gate)  # type: ignore[arg-type]
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    body = await (await client.post("/event", json={"source": "voice", "title": "how are you doing today"})).json()
    assert body["performance"]["text"] == "Splendid, thanks. Still judging your typing."
    assert body["performance"]["emotion"] == "happy"
    assert "You said:" not in body["performance"]["text"]   # the canned reactor never speaks for her
    assert thinker.stats()["calls"] == 1 and daemon.actor.stats()["acted"] == 0
    assert [t["said"] for t in daemon.ledger.to_list()] == ["how are you doing today"]
    await daemon.close()


async def test_a_bare_reflex_never_reaches_qwen(aiohttp_client):
    config = plain_config()
    spotify, toolbox, qwen, thinker = make(["[happy] unused"])
    gate = fake_gate(toolbox)
    daemon, sink = voice_daemon(config, toolbox, thinker, gate=gate)
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    body = await (await client.post("/event", json={"source": "voice", "title": "skip this track"})).json()
    assert spotify.log == ["next", "get_current_track"]
    assert body["performance"]["text"] == "Skipped. Now Blue Monday by New Order."
    assert thinker.stats()["calls"] == 0 and qwen.payloads == []
    assert daemon.ledger.to_list()[-1]["did"] == "skipped to the next track"
    await daemon.close()


async def test_the_careful_tools_are_gated_by_the_gates_library_change(aiohttp_client):
    config = plain_config()
    spotify, toolbox, qwen, thinker = make(["[happy] Saved it.", "[neutral] Playing."])
    gate = ScriptedGate({
        "save this song": reading("save this song", kind="request", topic="music", decision="act", tool="other",
                                  library_change=0.93, has_argument=0.2),
        "play some jazz": reading("play some jazz", kind="request", topic="music", decision="act", tool="other",
                                  library_change=0.04, has_argument=0.9),
    })
    daemon, sink = voice_daemon(config, toolbox, thinker, gate=gate)  # type: ignore[arg-type]
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await client.post("/event", json={"source": "voice", "title": "save this song"})
    assert "save_tracks" in {t["function"]["name"] for t in qwen.payloads[-1]["tools"]}
    await client.post("/event", json={"source": "voice", "title": "play some jazz"})
    assert "save_tracks" not in {t["function"]["name"] for t in qwen.payloads[-1]["tools"]}
    await daemon.close()


async def test_with_the_thinker_off_gemma_still_answers(aiohttp_client):
    """scripts/check_config.toml and any machine without Qwen: the old chat path is the fallback."""
    config = plain_config()
    config.thinker.enabled = False
    spotify, toolbox, qwen, thinker = make(["[happy] never asked"])
    thinker.config = config.thinker
    gate = fake_gate(toolbox)
    daemon, sink = voice_daemon(config, toolbox, thinker, gate=gate)
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    body = await (await client.post("/event", json={"source": "voice", "title": "how are you doing today"})).json()
    assert body["performance"]["text"] == "You said: how are you doing today"  # the canned reactor
    assert qwen.payloads == [] and thinker.stats()["calls"] == 0
    assert daemon.ledger.to_list()[-1]["reply"] == "You said: how are you doing today"
    await daemon.close()


# The token guard: Ollama drops the oldest tokens of a prompt over num_ctx (the system prompt),
# so the thinker trims the ledger, then the tool results, and never the system prompt.

class Snapshots(FakeQwen):
    """FakeQwen keeping a copy of each payload: the thinker's message list grows in place."""

    async def __call__(self, payload: dict) -> dict:
        reply = await super().__call__(payload)
        self.payloads[-1] = copy.deepcopy(payload)
        return reply


def ledger_lines(n: int, width: int = 300) -> list[str]:
    return [f"- {n - i} min ago the user said \"turn {i}\"; you said \"{'x' * width}\"" for i in range(n)]


def fits(payload: dict, config: ThinkerConfig) -> bool:
    return prompt_tokens(payload["messages"], payload.get("tools") or []) <= config.num_ctx - config.num_predict


async def test_the_oldest_ledger_turns_go_first_and_the_system_prompt_stays(caplog):
    recent = ledger_lines(6)
    toolbox, qwen, thinker = bare([])
    thinker.chat = roomy = Snapshots(["[neutral] Fine."])
    await thinker.run("how are you", "Today is Monday.", recent=recent)
    full = prompt_tokens(roomy.payloads[0]["messages"], [])
    per_turn = len(recent[0]) / 3
    # Room for everything but about two and a half turns.
    config = ThinkerConfig(num_ctx=full + 300 - int(per_turn * 2.5), num_predict=300)
    thinker.config = config
    thinker.chat = qwen = Snapshots(["[neutral] Fine."])
    with caplog.at_level("INFO", logger="strawberryd.thinker"):
        outcome = await thinker.run("how are you", "Today is Monday.", recent=recent)
    assert outcome.ok
    messages = qwen.payloads[0]["messages"]
    assert messages[0]["content"] == system_prompt(False)                   # untouched
    user = messages[1]["content"]
    assert user.startswith("Situation: Today is Monday.\nRecent exchanges (newest last):")
    assert user.endswith("The user says: how are you")
    assert [line in user for line in recent] == [False, False, False, True, True, True]  # oldest first
    assert fits(qwen.payloads[0], config)
    assert recent == ledger_lines(6)                                        # the caller's list is not changed
    logged = " ".join(r.getMessage() for r in caplog.records if "prompt ~" in r.getMessage())
    assert "dropped 3 of 6 ledger turns, shortened 0 of 0 tool results" in logged
    assert "xxxx" not in logged and "turn 0" not in logged                  # counts, never content
    await toolbox.close()


async def test_tool_results_are_shortened_after_the_ledger_keeping_their_head(caplog):
    result = "HEAD " + "y" * 1800 + " TAIL"

    async def handler(name, arguments):
        return FakeResult([FakeContent(result)])

    script = [[("search", {"query": "jazz"})], "[happy] Jazz it is."]
    recent = ledger_lines(2, width=40)
    spotify, toolbox, roomy, thinker = make(list(script), tools=[FakeTool("search")], handler=handler)
    thinker.chat = roomy = Snapshots(list(script))
    await thinker.run("play some jazz", recent=recent)
    first, second = (prompt_tokens(p["messages"], p.get("tools") or []) for p in roomy.payloads)
    assert second > first + 500                                             # the result made round two long
    config = ThinkerConfig(num_ctx=first + 300 + 250, num_predict=300)      # round one fits whole, round two not
    thinker.config = config
    thinker.chat = qwen = Snapshots(list(script))
    with caplog.at_level("INFO", logger="strawberryd.thinker"):
        outcome = await thinker.run("play some jazz", recent=recent)
    assert outcome.ok and outcome.fact == "Jazz it is."
    one, two = qwen.payloads
    assert one["messages"] == roomy.payloads[0]["messages"]                 # nothing trimmed in round one
    assert "Recent exchanges" in one["messages"][1]["content"]
    assert "Recent exchanges" not in two["messages"][1]["content"]          # the ledger went first
    tool = next(m for m in two["messages"] if m["role"] == "tool")
    assert tool["content"].startswith("HEAD ") and tool["content"].endswith(CUT) and "TAIL" not in tool["content"]
    assert MIN_RESULT_CHARS < len(tool["content"]) < len(result)
    assert two["messages"][0]["content"] == system_prompt(True) and two["tools"] == one["tools"]
    assert fits(one, config) and fits(two, config)
    logged = [r.getMessage() for r in caplog.records if "prompt ~" in r.getMessage()]
    assert len(logged) == 1 and "dropped 2 of 2 ledger turns, shortened 1 of 1 tool results" in logged[0]
    assert "HEAD" not in logged[0]
    await toolbox.close()


async def test_a_prompt_that_cannot_fit_is_not_sent(caplog):
    toolbox, qwen, thinker = bare(["[neutral] never sent"])
    base = prompt_tokens([{"role": "system", "content": system_prompt(False)},
                          {"role": "user", "content": "how are you"}], [])
    thinker.config = ThinkerConfig(num_ctx=base + 300 - 50, num_predict=300)
    with caplog.at_level("INFO", logger="strawberryd.thinker"):
        outcome = await thinker.run("how are you", recent=ledger_lines(3))
    assert not outcome.ok and outcome.emotion == "alert" and outcome.did == "had too much to think about"
    assert "more than I can hold" in outcome.fact
    assert qwen.payloads == []                                              # Ollama never saw a clipped prompt
    assert thinker.stats()["failures"] == 1
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "dropped 3 of 3 ledger turns" in logged and "prompt does not fit" in logged
    assert "xxxx" not in logged
    await toolbox.close()
