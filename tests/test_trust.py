"""The trust model (trust.py, WIRING.md §20, ADAPTERS.md): the private / foreign / egress flags, the
escalation after foreign text, the containment that cannot be switched off, reflexes and the approval tiers,
a whole-server tier and the destructive annotation, the journal's defaults, and tool offering at scale."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from strawberry_crab import logtext, trust
from strawberry_crab.actions import Actor, Outcome
from strawberry_crab.adapters.base import Adapter
from strawberry_crab.adapters.spotify import SPOTIFY, shape_listing
from strawberry_crab.adapters.web import WEB
from strawberry_crab.config import ActionsConfig, Config, ConfigError, ThinkerConfig, ToolsConfig, _validate, load
from strawberry_crab.confirm import card_line, generic_question
from strawberry_crab.server import create_app
from strawberry_crab.thinker import (AFTER_FOREIGN, FOREIGN_REPLY, PRIVATE_IN_CALL, WITHHELD, Thinker, names_server,
                                     schema_tokens)
from strawberry_crab.tools import Toolbox, ToolSpec
from tests.fake_spotify import TOOLS as SPOTIFY_TOOLS, FakeSpotify, make as spotify_box
from tests.test_adapter_web import SEARXNG_TOOLS, FakeSearxng, SnapshotQwen, public_dns  # noqa: F401 (a fixture)
from tests.test_approvals import AnnotatedTool, Hints
from tests.test_thinker import FakeQwen, ScriptedGate, plain_config, reading, voice_daemon
from tests.test_tools import FakeContent, FakeResult, FakeSession, FakeTool, make_connect

SECRET_NAME = "Tamarind Lullaby"           # a library name: private context
ARG_CANARY = "PAGE-SAYS-kitchen-7d1f"      # what a page could make the model put in an argument
RESULT_CANARY = "plover-result-91ac"


class Plain:
    """A plain MCP server's tools, answering every call with one line that says what it was."""

    def __init__(self, text: str = "") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.text = text

    async def handle(self, name: str, arguments: dict) -> FakeResult:
        self.calls.append((name, arguments))
        return FakeResult([FakeContent(self.text or f"{name} done {RESULT_CANARY}")])


def box(servers: dict[str, dict], sessions: dict[str, FakeSession], adapters: dict[str, Any] | None = None,
        risks: dict[str, str] | None = None) -> Toolbox:
    toolbox = Toolbox(ToolsConfig(servers=servers, preconnect=False), connect=make_connect(sessions), adapters=adapters)
    toolbox.risks = dict(risks or {})
    return toolbox


def world(lights_flags: list[str] | None = None, risks: dict[str, str] | None = None, spotify: bool = True):
    """The web server, a lights server that is none of the three (`flags = []`), and the Spotify fake."""
    searxng, lights = FakeSearxng(), Plain()
    servers = {"web": {"topic": "other", "command": "web"},
               "lights": {"topic": "system", "command": "lights", "flags": lights_flags if lights_flags is not None else []}}
    sessions = {"web": FakeSession(SEARXNG_TOOLS, searxng.handle),
                "lights": FakeSession([FakeTool("turn_on"), FakeTool("status")], lights.handle)}
    fake = FakeSpotify()
    if spotify:
        servers["spotify"] = {"topic": "music", "command": "spotify"}
        sessions["spotify"] = FakeSession(SPOTIFY_TOOLS + [FakeTool("search")], fake.handle)
    toolbox = box(servers, sessions, risks={"lights.status": "read", **(risks or {})})
    return toolbox, searxng, lights, fake, sessions


# ----------------------------------------------------------------------------- the flags


def test_the_flag_table():
    toolbox = box({"web": {"topic": "other", "command": "web"}, "spotify": {"topic": "music", "command": "s"},
                   "notes": {"topic": "notes", "command": "n"}, "lights": {"topic": "system", "command": "l", "flags": []},
                   "mine": {"topic": "music", "command": "m", "adapter": "spotify", "flags": ["foreign"]},
                   "web2": {"topic": "other", "command": "w", "adapter": "web", "flags": []},
                   "diary": {"topic": "notes", "command": "d", "flags": ["private"]}}, {})
    assert toolbox.flags("web") == {"foreign", "egress"}
    assert toolbox.flags("spotify") == {"private", "egress"}
    assert toolbox.flags("notes") == trust.UNKNOWN == {"private", "foreign", "egress"}    # nobody said anything
    assert toolbox.flags("lights") == frozenset() and toolbox.flags("diary") == {"private"}
    assert toolbox.flags("mine") == {"private", "egress", "foreign"}     # a config adds to an adapter's…
    assert toolbox.flags("web2") == {"foreign", "egress"}                # …and never takes one away
    assert toolbox.flags("not configured") == trust.UNKNOWN
    assert (toolbox.private("spotify"), toolbox.foreign("spotify"), toolbox.egress("spotify")) == (True, False, True)
    assert WEB.foreign and WEB.egress and not WEB.private and SPOTIFY.private and SPOTIFY.egress and not SPOTIFY.foreign
    assert toolbox.servers["notes"].stats()["flags"] == ["egress", "foreign", "private"]


def test_flags_and_offer_in_the_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[tools.servers.lights]\ntopic = "system"\ncommand = "lights"\nflags = []\noffer = "topic"\n'
                    '[thinker]\ntool_tokens = 2500\n')
    config = load(path)
    assert config.tools.servers["lights"]["flags"] == [] and config.thinker.tool_tokens == 2500
    for bad in ({"flags": ["secret"]}, {"flags": "private"}, {"offer": "sometimes"}):
        config = Config()
        config.tools.servers = {"x": {"topic": "other", "command": "x", **bad}}
        with pytest.raises(ConfigError):
            _validate(config)
    config = Config()
    config.thinker.tool_tokens = 9000
    with pytest.raises(ConfigError):
        _validate(config)


def test_a_server_claimed_by_its_tools_gets_the_adapters_flags():
    from strawberry_crab.tools import Server

    async def connect(stack, config):
        return FakeSession(SEARXNG_TOOLS, FakeSearxng().handle)

    server = Server("search-at-home", {"topic": "other", "command": "x", "flags": []}, connect, 5, 5)
    assert server.flags == frozenset()
    server._claim(["searxng_web_search"])
    assert server.flags == {"foreign", "egress"} and not server.unknown


# ----------------------------------------------------------------------------- escalation after foreign text


async def test_before_any_foreign_text_a_change_is_made_at_once():
    toolbox, _, lights, _, _ = world()
    qwen = FakeQwen([[("turn_on", {"room": "kitchen"})], "[happy] Lights on."])
    outcome = await Thinker(ThinkerConfig(), toolbox, "q", chat=qwen).run("turn on the lights")
    assert outcome.held is None and lights.calls == [("turn_on", {"room": "kitchen"})]


async def test_after_a_web_result_a_change_waits_for_a_yes_in_the_cores_words():
    toolbox, searxng, lights, _, _ = world()
    qwen = SnapshotQwen([[("searxng_web_search", {"query": "sunset helsinki"})],
                         [("status", {}), ("turn_on", {"room": ARG_CANARY})], "[neutral] unreachable"])
    outcome = await Thinker(ThinkerConfig(), toolbox, "q", chat=qwen).run("when is sunset, and turn the lights on")
    held = outcome.held
    assert held is not None and held.key == "lights.turn_on" and held.arguments == {"room": ARG_CANARY}
    assert outcome.fact == held.question == generic_question("turn_on")          # written by code
    assert held.prompt == card_line(generic_question("turn_on")) and ARG_CANARY not in held.prompt
    assert lights.calls == [("status", {})]            # a read goes through; the change waits for the user
    assert held.risk == "change"


async def test_an_adapters_own_question_is_not_used_after_foreign_text():
    class Lamp(Adapter):
        name = "lamp"

        async def ask(self, toolbox, server, name, arguments):
            return f"Turn on {arguments.get('room')}? Say yes.", arguments

    toolbox, _, lights, _, _ = world()
    toolbox.adapters["lights"] = toolbox.servers["lights"].adapter = Lamp()
    qwen = FakeQwen([[("searxng_web_search", {"query": "x"})], [("turn_on", {"room": ARG_CANARY})]])
    outcome = await Thinker(ThinkerConfig(), toolbox, "q", chat=qwen).run("look it up and turn the lamp on")
    assert outcome.held is not None and ARG_CANARY not in outcome.fact and ARG_CANARY not in outcome.held.prompt
    # Without foreign text the adapter words it, as before.
    qwen = FakeQwen([[("turn_on", {"room": "hall"})]])
    toolbox.risks["lights.turn_on"] = "sends"
    outcome = await Thinker(ThinkerConfig(), toolbox, "q", chat=qwen).run("turn the hall lamp on")
    assert outcome.fact == "Turn on hall? Say yes."


async def test_a_private_server_stays_refused_after_a_web_result_as_before():
    toolbox, _, _, fake, sessions = world()
    qwen = SnapshotQwen([[("searxng_web_search", {"query": "new order tour"})], [("play", {}), ("get_current_track", {})],
                         "[neutral] They tour."])
    await Thinker(ThinkerConfig(), toolbox, "q", chat=qwen).run("is this band touring")
    assert sessions["spotify"].calls == []
    assert [m["content"] for m in qwen.payloads[2]["messages"] if m["role"] == "tool"][-2:] == [AFTER_FOREIGN] * 2


async def test_an_unknown_server_is_foreign_to_itself():
    """A plain server nobody has said anything about is all three: its own result taints the conversation,
    so its next change asks first, and the private context leaves the prompt."""
    notes = Plain(f"note: {RESULT_CANARY}")
    toolbox = box({"notes": {"topic": "notes", "command": "notes"}},
                  {"notes": FakeSession([FakeTool("search_notes"), FakeTool("add_note")], notes.handle)})
    qwen = SnapshotQwen([[("search_notes", {"q": "milk"})], [("add_note", {"text": "buy milk"})]])
    outcome = await Thinker(ThinkerConfig(), toolbox, "q", chat=qwen).run(
        "find my shopping note and add milk", f"Today is Monday. Library: {SECRET_NAME}.",
        recent=[f'the user said "{SECRET_NAME}"'], public_context="Today is Monday.")
    assert outcome.held is not None and outcome.held.key == "notes.add_note" and notes.calls == [("search_notes", {"q": "milk"})]
    assert SECRET_NAME in qwen.payloads[0]["messages"][1]["content"]
    assert SECRET_NAME not in qwen.payloads[1]["messages"][1]["content"]
    # Declared as only private (its results are the user's own words), it is not foreign: no question.
    toolbox = box({"notes": {"topic": "notes", "command": "notes", "flags": ["private"]}},
                  {"notes": FakeSession([FakeTool("search_notes"), FakeTool("add_note")], Plain().handle)})
    qwen = FakeQwen([[("search_notes", {"q": "milk"})], [("add_note", {"text": "buy milk"})], "[happy] Added."])
    assert (await Thinker(ThinkerConfig(), toolbox, "q", chat=qwen).run("add milk")).held is None


async def test_the_daemon_asks_and_keeps_no_foreign_words(aiohttp_client):
    toolbox, _, lights, _, _ = world()
    text = "when is sunset, then turn the lights on"
    qwen = FakeQwen([[("searxng_web_search", {"query": "sunset"})], [("turn_on", {"room": ARG_CANARY})]])
    thinker = Thinker(ThinkerConfig(ack_after_s=30, still_on_it_s=60), toolbox, "q", chat=qwen)
    config = plain_config()
    daemon, sink = voice_daemon(config, toolbox, thinker, gate=ScriptedGate({text: reading(text, kind="request")}))
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await client.post("/event", json={"source": "voice", "title": text})
    approval = daemon.approvals.open
    assert approval is not None and approval.risk == "change" and ARG_CANARY not in approval.prompt
    assert ARG_CANARY not in str(approval.request)          # the card's line on the bus
    assert daemon.ledger.to_list()[-1]["reply"] == FOREIGN_REPLY
    daemon.approvals.answer(approval.approval_id, "yes", "typed")
    for _ in range(100):
        if lights.calls:
            break
        await asyncio.sleep(0.02)
    assert lights.calls == [("turn_on", {"room": ARG_CANARY})]     # only the user's yes made it
    await daemon.close()
    await toolbox.close()


# ----------------------------------------------------------------------------- containment


async def test_an_egress_call_carrying_a_private_phrase_is_refused_for_any_egress_server():
    """Not only the web: a foreign and egress server (recall-like) whose own result is in may not carry the
    ledger or the library out in its next call."""
    recall = Plain(f"a note {RESULT_CANARY}")
    toolbox = box({"recall": {"topic": "notes", "command": "recall", "flags": ["foreign", "egress"]}},
                  {"recall": FakeSession([FakeTool("search"), FakeTool("read_note")], recall.handle)},
                  risks={"recall": "read"})
    qwen = SnapshotQwen([[("search", {"q": "orbs"})], [("search", {"q": f"about {SECRET_NAME}"}), ("read_note", {"id": "7"})],
                         "[neutral] Found it."])
    await Thinker(ThinkerConfig(), toolbox, "q", chat=qwen).run(
        "what did we decide about the orbs", f"Today is Monday. Library: {SECRET_NAME}.", public_context="Today is Monday.")
    assert recall.calls == [("search", {"q": "orbs"}), ("read_note", {"id": "7"})]
    assert [m["content"] for m in qwen.payloads[2]["messages"] if m["role"] == "tool"][1] == PRIVATE_IN_CALL


async def test_control_characters_never_reach_a_prompt():
    dirty = "line one\x1b[31m red\x00‮gnp.exe​⁦x⁩\nline two\tok\x85end﻿"
    toolbox = box({"lights": {"topic": "system", "command": "l", "flags": []}},
                  {"l": FakeSession([FakeTool("status")], Plain(dirty).handle)})
    for shape in (False, True):
        result = await toolbox.call("lights", "status", shape=shape)
        assert not trust.CONTROL.search(result.text)
        assert "\n" in result.text and "\t" in result.text and "line two" in result.text


async def test_withheld_results_and_the_rounds_follow_any_foreign_server():
    toolbox, _, lights, _, _ = world()
    qwen = SnapshotQwen([[("status", {})], [("searxng_web_search", {"query": "x"})], "[neutral] Done."])
    await Thinker(ThinkerConfig(), toolbox, "q", chat=qwen).run("check the lights and look something up")
    tool_messages = [m for m in qwen.payloads[2]["messages"] if m["role"] == "tool"]
    assert tool_messages[0]["content"] == WITHHELD and tool_messages[1]["content"].startswith("1. ")


# ----------------------------------------------------------------------------- reflexes and the tiers


def skip_route(tool: str = "skip"):
    return reading("skip this", kind="request", topic="music", decision="act", tool=tool, tool_confidence=0.95,
                   has_argument=0.02)


async def test_a_reflex_whose_tool_waits_for_a_yes_goes_to_the_thinker(caplog):
    spotify, toolbox, actor = spotify_box()
    assert (await actor.act("skip this", skip_route())).ok and "next" in spotify.log
    spotify.log.clear()
    toolbox.risks = {"spotify.next": "sends"}
    with caplog.at_level(logging.INFO):
        assert await actor.act("skip this", skip_route()) is None
    assert "next" not in spotify.log and actor.deferred == 1 and "over to the thinker, which asks" in caplog.text
    toolbox.risks = {"spotify": "destructive"}                      # a whole server, reads too
    assert await actor.act("what's playing", skip_route("now_playing")) is None
    # A confirm list that names the reflex's tool does the same.
    spotify, toolbox, actor = spotify_box(server={"topic": "music", "command": "spotify", "confirm": ["pause"]})
    assert await actor.act("pause", skip_route("pause")) is None and "pause" not in spotify.log
    await toolbox.close()


async def test_the_thinker_then_asks_about_it(aiohttp_client):
    from tests.test_thinker import make

    spotify, toolbox, qwen, thinker = make([[("next", {})]])
    toolbox.risks = {"spotify.next": "sends"}
    outcome = await thinker.run("skip this")
    assert outcome.held is not None and outcome.held.key == "spotify.next" and outcome.held.risk == "sends"
    await toolbox.close()


async def test_a_reflex_that_calls_an_unlisted_tool_needing_a_yes_is_stopped_there(caplog):
    async def zap(toolbox, server):
        first = await toolbox.call(server, "get_current_track")
        second = await toolbox.call(server, "zap")
        return Outcome("zapped", "Zapped.", second.ok, (first, second))

    class Zapper(Adapter):
        name = "zapper"
        reflexes = {"skip": zap}

    fake = FakeSpotify()
    zapped: list[str] = []

    async def handle(name, arguments):
        if name == "zap":
            zapped.append(name)
            return FakeResult([FakeContent("{}")])
        return await fake.handle(name, arguments)

    toolbox = box({"music": {"topic": "music", "command": "m"}},
                  {"m": FakeSession(SPOTIFY_TOOLS + [FakeTool("zap")], handle)}, adapters={"music": Zapper()},
                  risks={"music.zap": "destructive"})
    actor = Actor(ActionsConfig(), toolbox)
    events = []
    with caplog.at_level(logging.WARNING):
        outcome = await actor.act("skip this", skip_route(), on_call=lambda *a: events.append(a))
    assert outcome is None and zapped == [] and actor.deferred == 1
    assert events[-1][0] == "completed" and events[-1][-1] == "refused"
    assert "music.zap, which waits for a yes" in caplog.text


# ----------------------------------------------------------------------------- the tiers


def tiered(risks: dict[str, str], adapter: Any = None) -> Toolbox:
    tools = [AnnotatedTool("shred", annotations=Hints(destructiveHint=True)), AnnotatedTool("echo"),
             AnnotatedTool("send")]
    toolbox = box({"echo": {"topic": "notes", "command": "e"}}, {"e": FakeSession(tools, Plain().handle)},
                  adapters={"echo": adapter} if adapter else {}, risks=risks)
    return toolbox


async def test_a_whole_server_entry_never_lowers_the_destructive_annotation():
    class Mail(Adapter):
        name = "mail"
        risks = {"send": "sends"}

    toolbox = tiered({"echo": "read"}, Mail())
    await toolbox.offered(["echo"])
    assert toolbox.risk("echo", "shred") == "destructive"        # the annotation stands
    assert toolbox.risk("echo", "send") == "sends"               # the adapter's own tier stands
    assert toolbox.risk("echo", "echo") == "read"                # the rest are lowered as written
    toolbox.risks = {"echo": "read", "echo.shred": "change"}     # a per-tool entry may lower it, explicitly
    assert toolbox.risk("echo", "shred") == "change"
    toolbox.risks = {"echo": "sends"}                            # a whole-server entry still raises
    assert toolbox.risk("echo", "echo") == "sends" and toolbox.risk("echo", "shred") == "destructive"
    toolbox.risks = {}
    assert toolbox.risk("echo", "shred") == "destructive" and toolbox.risk("echo", "echo") == "change"
    await toolbox.close()


# ----------------------------------------------------------------------------- the journal


async def test_private_foreign_and_unknown_servers_are_logged_as_counts_only(caplog):
    class Opted(Adapter):
        name = "opted"
        log_detail = True

    class OptedPrivate(Opted):
        private = True

    logtext.configure(True)          # even with the user's sentences in the journal
    try:
        servers = {name: {"topic": "other", "command": name} for name in ("plain", "opted", "mine")}
        sessions = {name: FakeSession([FakeTool("look")], Plain(f"{RESULT_CANARY} from {name}").handle) for name in servers}
        toolbox = box(servers, sessions, adapters={"opted": Opted(), "mine": OptedPrivate()})
        with caplog.at_level(logging.INFO, logger="strawberryd.tools"):
            for name in servers:
                await toolbox.call(name, "look", {"q": f"{ARG_CANARY} {name}"})
        lines = {name: next(r.getMessage() for r in caplog.records if f"tools: {name}.look" in r.getMessage())
                 for name in servers}
        assert RESULT_CANARY not in lines["plain"] and ARG_CANARY not in lines["plain"]
        assert "plain.look(q)" in lines["plain"] and "chars (not logged)" in lines["plain"]
        assert RESULT_CANARY not in lines["mine"] and ARG_CANARY not in lines["mine"]     # opted in, but private
        assert RESULT_CANARY in lines["opted"] and ARG_CANARY in lines["opted"]           # the one that opted in
        shown = toolbox.shown((await toolbox.call("plain", "look", {"q": ARG_CANARY})))
        assert shown["text"].startswith("<") and shown["arguments"] == ["q"]
    finally:
        logtext.configure(False)


async def test_spotify_says_counts_never_names(caplog):
    spotify, toolbox, actor = spotify_box()
    with caplog.at_level(logging.INFO, logger="strawberryd.tools"):
        await toolbox.call("spotify", "get_saved_tracks", {"limit": 50})
    line = next(r.getMessage() for r in caplog.records if "get_saved_tracks" in r.getMessage())
    assert "42 tracks" in line and "Feeling Good" not in line and '"limit"' not in line
    await toolbox.close()


# ----------------------------------------------------------------------------- shaping


def test_spotify_listings_read_one_line_per_hit_and_its_schemas_lose_device_id():
    text = ('{"tracks": [{"name": "Blue Monday", "uri": "spotify:track:1", "artists": ["New Order"], '
            '"album": "Substance"}, {"name": "Teardrop", "uri": "spotify:track:2", "artists": ["Massive Attack"]}], '
            '"total": 2}')
    assert shape_listing(text) == ("tracks: 2\n1. Blue Monday – New Order (Substance) · uri=spotify:track:1\n"
                                   "2. Teardrop – Massive Attack · uri=spotify:track:2\ntotal: 2")
    assert shape_listing('{"error": "x"}') == '{"error": "x"}' and shape_listing("not json") == "not json"
    assert SPOTIFY.shape_result("get_current_track", text, True) == text      # only the listings
    schema = {"type": "object", "properties": {"volume": {"type": "integer"}, "device_id": {"type": "string"}},
              "required": ["volume"]}
    assert "device_id" not in SPOTIFY.shape_tool(ToolSpec("s", "set_volume", "Set it.", schema)).schema["properties"]
    assert "device_id" in SPOTIFY.shape_tool(ToolSpec("s", "play", "Play.", schema)).schema["properties"]


async def test_the_thinker_reads_the_shape_and_the_code_reads_the_json():
    from tests.test_thinker import make

    spotify, toolbox, qwen, thinker = make([[("search", {"query": "jazz"})], "[happy] Found some."],
                                           tools=SPOTIFY_TOOLS + [FakeTool("search")])

    async def handle(name, arguments):
        if name == "search":
            return FakeResult([FakeContent('{"tracks": [{"name": "So What", "uri": "spotify:track:9", '
                                           '"artists": ["Miles Davis"]}]}')])
        return await spotify.handle(name, arguments)

    toolbox.servers["spotify"].connect = make_connect({"spotify": FakeSession(SPOTIFY_TOOLS + [FakeTool("search")], handle)})
    await thinker.run("find some jazz")
    assert [m["content"] for m in qwen.payloads[1]["messages"] if m["role"] == "tool"] == [
        "tracks: 1\n1. So What – Miles Davis · uri=spotify:track:9"]
    raw = await toolbox.call("spotify", "search", {"query": "jazz"})
    assert raw.text.startswith('{"tracks"')
    await toolbox.close()


# ----------------------------------------------------------------------------- offering at scale


def offering(max_tools: int = 30, tool_tokens: int = 0, offers: dict[str, str] | None = None):
    offers = offers or {}
    servers = {"spotify": {"topic": "music", "command": "s"}, "notes": {"topic": "notes", "command": "n", "flags": []},
               "web": {"topic": "other", "command": "w"}}
    for name, mode in offers.items():
        servers[name]["offer"] = mode
    sessions = {"s": FakeSession(SPOTIFY_TOOLS, FakeSpotify().handle),
                "n": FakeSession([FakeTool(f"note_{i}", "A note tool. " * 8) for i in range(8)], Plain().handle),
                "w": FakeSession(SEARXNG_TOOLS, FakeSearxng().handle)}
    toolbox = box(servers, sessions)
    return toolbox, Thinker(ThinkerConfig(max_tools=max_tools, tool_tokens=tool_tokens), toolbox, "q", chat=FakeQwen([]))


async def test_one_order_for_every_sentence_whatever_the_topic():
    toolbox, thinker = offering()
    keys = [s.key for s in await thinker.tools(topic="music")]
    assert keys == [s.key for s in await thinker.tools(topic="notes")] == [s.key for s in await thinker.tools(topic="")]
    assert [k.split(".")[0] for k in keys] == ["spotify"] * 9 + ["notes"] * 8 + ["web"] * 2   # music, notes, other


async def test_offer_modes():
    toolbox, thinker = offering(offers={"notes": "topic", "web": "asked"})
    servers = lambda specs: sorted({s.server for s in specs})   # noqa: E731
    assert servers(await thinker.tools(topic="music", text="play jazz")) == ["spotify"]
    assert servers(await thinker.tools(topic="notes", text="add a note")) == ["notes", "spotify"]
    # Asked for: by the adapter's own reading ("look it up"), or by the server's name or title.
    specs, prompt, note = await thinker.offer("look up the weather", topic="other")
    assert servers(specs) == ["spotify", "web"] and "search the web" in prompt.lower()
    specs, prompt, note = await thinker.offer("what is the capital of australia", topic="other")
    assert servers(specs) == ["spotify"] and WEB.unavailable not in prompt    # left out, not "not working"
    assert names_server("ask the web about it", "web") and names_server("open my Notes", "notes")
    assert not names_server("a webby thing", "web")
    await toolbox.close()


async def test_a_token_budget_keeps_the_likely_tools_and_their_order(caplog):
    toolbox, thinker = offering(tool_tokens=1)
    full = await offering()[1].tools(topic="music")
    sizes = {s.key: schema_tokens(s) for s in full}
    budget = sum(sizes[k] for k in sizes if k.startswith("spotify.")) + sizes["web.searxng_web_search"]
    thinker.config.tool_tokens = budget
    with caplog.at_level(logging.INFO, logger="strawberryd.thinker"):
        kept = await thinker.tools(topic="music")
    assert all(s.server != "notes" for s in kept) and sum(schema_tokens(s) for s in kept) <= budget
    assert [s.key for s in kept] == [s.key for s in full if s.key in {k.key for k in kept}]     # the same order
    assert "leaving out notes.note_0" in caplog.text and f"tool_tokens={budget}" in caplog.text
    # Asked for outright goes first: a notes sentence that names nothing keeps the gate's topic.
    kept = await thinker.tools(topic="notes")
    assert {s.server for s in kept} >= {"notes"}
    kept = await thinker.tools(topic="music", first=frozenset({"notes"}))
    assert "notes.note_0" in {s.key for s in kept} and len([s for s in kept if s.server == "spotify"]) < 9


async def test_the_default_budget_fits_todays_tools():
    toolbox, thinker = offering()
    specs = await thinker.tools(topic="music")
    assert sum(schema_tokens(s) for s in specs) < ThinkerConfig().tool_tokens


# ----------------------------------------------------------------------------- the builtin hook


async def test_a_builtin_server_is_offered_and_guarded_like_any_other():
    class Memory(Adapter):
        name = "memory"
        private = True

    session = FakeSession([FakeTool("remember"), FakeTool("recall")], Plain("kept").handle)

    async def open_session():
        return session

    toolbox = Toolbox(ToolsConfig(servers={}, preconnect=False))
    server = toolbox.add_builtin("memory", "notes", open_session, adapter=Memory())
    assert toolbox.flags("memory") == {"private"} and server.offer == "always"
    specs = await toolbox.offered(["memory"])
    assert [s.key for s in specs] == ["memory.remember", "memory.recall"]     # the session's own order
    assert (await toolbox.call("memory", "remember", {"what": "x"})).ok and session.calls == [("remember", {"what": "x"})]
    plain = toolbox.add_builtin("inbox", "other", open_session)
    assert plain.flags == trust.UNKNOWN
    await toolbox.close()
