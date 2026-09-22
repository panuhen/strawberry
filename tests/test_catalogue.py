"""A request for particular music with no music server configured (WIRING.md §8b): MPRIS has only
the player's buttons, so "play daft punk" gets her fixed line that it needs a music add-on, and no
model is asked to pretend it played something. The buttons themselves keep working."""

from __future__ import annotations

from strawberry_crab.actions import NO_CATALOGUE, Actor, Outcome
from strawberry_crab.config import ActionsConfig, ToolsConfig
from strawberry_crab.server import create_app
from strawberry_crab.thinker import NO_TOOLS, TOOLS_GUIDE, system_prompt
from strawberry_crab.tools import Toolbox
from tests.fake_spotify import make as make_spotify
from tests.test_thinker import ScriptedGate, bare, make, plain_config, reading, voice_daemon


def music(text: str, tool: str = "other", catalogue: float = 0.85, kind: str = "request", **kw):
    return reading(text, kind=kind, topic="music", decision="act", tool=tool, tool_confidence=kw.pop("tool_confidence", 0.8),
                   has_argument=kw.pop("has_argument", 0.9), catalogue=catalogue, **kw)


class FakeMpris:
    """Just enough of mpris.Mpris for the Actor: a skip and a now_playing that say what they did."""

    def __init__(self) -> None:
        self.pressed: list[str] = []

    def reflexes(self):
        async def skip(_toolbox, _server):
            self.pressed.append("skip")
            return Outcome("skipped to the next track", "Skipped. Now Teardrop by Massive Attack.", True)

        async def now_playing(_toolbox, _server):
            self.pressed.append("now_playing")
            return Outcome("looked at the player", "That's Teardrop by Massive Attack.", True)

        return {"skip": skip, "now_playing": now_playing}

    async def situation(self) -> str:
        return ""


def no_servers() -> Toolbox:
    return Toolbox(ToolsConfig(servers={}, preconnect=False))


async def test_the_actor_knows_when_a_request_needs_a_catalogue_nobody_has():
    toolbox = no_servers()
    actor = Actor(ActionsConfig(), toolbox, mpris=FakeMpris())
    assert not actor.has_catalogue()
    for text in ("play daft punk", "play some acid techno", "queue one more time"):
        assert actor.needs_catalogue(music(text)), text
    assert actor.needs_catalogue(music("put on my liked songs", tool="resume", has_argument=0.4))
    # Not a catalogue request: a low score, a fact about music, small talk, another topic.
    assert not actor.needs_catalogue(music("recommend me something", catalogue=0.14))
    assert not actor.needs_catalogue(music("tell me about daft punk", kind="question", catalogue=0.02))
    assert not actor.needs_catalogue(music("I love this song", kind="chat", catalogue=0.9))
    assert not actor.needs_catalogue(reading("play the news", kind="request", topic="other", catalogue=0.9))
    # A bare "play" is the resume button however the embedding scored it (live: 0.61).
    assert not actor.needs_catalogue(music("play", tool="resume", catalogue=0.61, has_argument=0.33))
    assert not actor.needs_catalogue(music("keep playing", tool="resume", catalogue=0.61))
    await toolbox.close()


async def test_a_music_server_is_a_catalogue():
    _, toolbox, actor = make_spotify()
    assert actor.has_catalogue() and not actor.needs_catalogue(music("play daft punk"))
    await toolbox.close()
    # A server for another topic cannot find music.
    notes = Toolbox(ToolsConfig(servers={"notes": {"topic": "notes", "command": "notes"}}, preconnect=False))
    assert Actor(ActionsConfig(), notes).needs_catalogue(music("play daft punk"))
    await notes.close()


async def test_no_catalogue_is_her_fixed_line_and_no_model_is_asked(aiohttp_client):
    config = plain_config()
    toolbox, qwen, thinker = bare(["[happy] Queued again."])   # what Qwen said live, 5 runs out of 5
    gate = ScriptedGate({t: music(t) for t in ("play daft punk", "queue one more time")})
    actor = Actor(config.actions, toolbox, mpris=FakeMpris())
    daemon, sink = voice_daemon(config, toolbox, thinker, gate=gate, actor=actor)  # type: ignore[arg-type]
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    for text in ("play daft punk", "queue one more time"):
        body = await (await client.post("/event", json={"source": "voice", "title": text})).json()
        said = body["performance"]["text"]
        assert said in NO_CATALOGUE, said
        assert "add-on" in said and "Spotify" in said and ".md" not in said   # spoken: no file names
        assert body["performance"]["emotion"] == "neutral"
    assert qwen.payloads == [] and thinker.stats()["calls"] == 0
    assert [m.get("state") for m in sink.got] == ["talking", "talking"]   # no thinking pose, no ack
    assert daemon.ledger.to_list()[-1]["did"] == "needs a music add-on"
    assert daemon.quiet_media_until == 0.0   # she changed nothing; the next track change is reported
    await daemon.close()


async def test_with_the_thinker_off_gemma_is_not_asked_either(aiohttp_client):
    config = plain_config()
    config.thinker.enabled = False
    toolbox, qwen, thinker = bare([])
    gate = ScriptedGate({"play some acid techno": music("play some acid techno")})
    daemon, sink = voice_daemon(config, toolbox, thinker, gate=gate, actor=Actor(config.actions, toolbox))  # type: ignore[arg-type]
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    body = await (await client.post("/event", json={"source": "voice", "title": "play some acid techno"})).json()
    assert body["performance"]["text"] in NO_CATALOGUE   # not the canned reactor's "You said: …"
    await daemon.close()


async def test_the_player_buttons_still_work_with_no_server(aiohttp_client):
    config = plain_config()
    toolbox, qwen, thinker = bare(["[neutral] unused"])
    mpris = FakeMpris()
    gate = ScriptedGate({
        "play the next song": music("play the next song", tool="skip", tool_confidence=0.91, has_argument=0.08,
                                    catalogue=0.01),
        "what song is this": music("what song is this", kind="question", tool="now_playing", tool_confidence=0.96,
                                   has_argument=0.01, catalogue=0.02),
    })
    daemon, sink = voice_daemon(config, toolbox, thinker, gate=gate, actor=Actor(config.actions, toolbox, mpris=mpris))  # type: ignore[arg-type]
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    body = await (await client.post("/event", json={"source": "voice", "title": "play the next song"})).json()
    assert body["performance"]["text"].startswith("Skipped. Now Teardrop by Massive Attack.")
    body = await (await client.post("/event", json={"source": "voice", "title": "what song is this"})).json()
    assert body["performance"]["text"].startswith("That's Teardrop by Massive Attack.")
    assert mpris.pressed == ["skip", "now_playing"] and qwen.payloads == []
    await daemon.close()


async def test_with_a_music_server_the_request_goes_to_qwen_and_its_tools(aiohttp_client):
    config = plain_config()
    spotify, toolbox, qwen, thinker = make([[("search", {"q": "daft punk"})], "[happy] Daft Punk it is."])
    gate = ScriptedGate({"play daft punk": music("play daft punk")})
    daemon, sink = voice_daemon(config, toolbox, thinker, gate=gate)  # type: ignore[arg-type]
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    body = await (await client.post("/event", json={"source": "voice", "title": "play daft punk"})).json()
    assert body["performance"]["text"] == "Daft Punk it is."
    assert thinker.stats()["calls"] == 1 and qwen.payloads[0]["tools"]
    await daemon.close()


def test_the_no_tools_prompt_says_music_needs_an_add_on():
    """The fallback when the guard misses a sentence ("save this song" reads as chat): the prompt
    itself tells Qwen that choosing music needs a music add-on and never to claim it acted."""
    prompt = system_prompt(False)
    assert NO_TOOLS in prompt and TOOLS_GUIDE not in prompt
    assert "music add-on" in NO_TOOLS and "Never say you did something you did not do" in NO_TOOLS
    assert "add-on" not in TOOLS_GUIDE   # with tools she uses them; no talk of add-ons
