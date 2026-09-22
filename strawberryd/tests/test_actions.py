"""The reflex tier (WIRING.md §8b) on a fake Spotify: gate -> reflex -> action event -> her line."""

from __future__ import annotations

import json

import pytest

from strawberryd.actions import Actor, Outcome, _track
from strawberryd.brain import describe
from strawberryd.config import ActionsConfig, Config, GateConfig, ToolsConfig
from strawberryd.daemon import Daemon
from strawberryd.events import CannedReactor, Event
from strawberryd.server import create_app
from strawberryd.systemone import Gate, Route
from strawberryd.tools import Toolbox
from tests.test_systemone import FakeEmbedder
from tests.test_tools import FakeContent, FakeResult, FakeSession, FakeTool, make_connect

TRACKS = [
    {"name": "Feeling Good", "artists": ["Nina Simone"], "album": "I Put a Spell on You"},
    {"name": "Blue Monday", "artists": ["New Order"], "album": "Power, Corruption & Lies"},
]


class FakeSpotify:
    """Enough of the Spotify MCP server for the reflexes: a queue position, a volume, pause."""

    def __init__(self, broken: bool = False) -> None:
        self.index = 0
        self.playing = True
        self.volume = 80
        self.broken = broken
        self.log: list[str] = []

    async def handle(self, name: str, arguments: dict) -> FakeResult:
        self.log.append(name)
        if self.broken == "no_device":
            return FakeResult([FakeContent(json.dumps({"error": "Resource not found.", "status": 404,
                                                       "details": "http status: 404, code: -1 - .../me/player/play:\n Player command failed: No active device found"}))])
        if self.broken == "restricted":
            return FakeResult([FakeContent(json.dumps({"error": "Permission denied. Check app scopes.", "status": 403,
                                                       "details": "http status: 403 ... Player command failed: Restriction violated, reason: UNKNOWN"}))])
        if self.broken:
            return FakeResult([FakeContent(json.dumps({"error": "error: no active device"}))])
        if name == "next":
            self.index = (self.index + 1) % len(TRACKS)
            return FakeResult([FakeContent(json.dumps({"success": True, "message": "Skipped to next track"}))])
        if name == "previous":
            self.index = (self.index - 1) % len(TRACKS)
            return FakeResult([FakeContent(json.dumps({"success": True}))])
        if name == "pause":
            self.playing = False
            return FakeResult([FakeContent(json.dumps({"success": True}))])
        if name == "play":
            self.playing = True
            return FakeResult([FakeContent(json.dumps({"success": True}))])
        if name == "get_current_track":
            return FakeResult([FakeContent(json.dumps({"playing": self.playing, "track": TRACKS[self.index]}))])
        if name == "get_devices":
            return FakeResult([FakeContent(json.dumps({"devices": [{"name": "x", "is_active": False, "volume": 100},
                                                                   {"name": "Panu-Ubuntu", "is_active": True, "volume": self.volume}]}))])
        if name == "set_volume":
            self.volume = arguments["volume"]
            return FakeResult([FakeContent(json.dumps({"success": True}))])
        if name == "get_favorites":
            return FakeResult([FakeContent(json.dumps({"favorites": [{"name": "Around the World", "artists": ["Daft Punk"]}]}))])
        if name == "get_saved_tracks":
            padding = [{"name": f"Filler {i}", "artists": ["New Order"], "album": "x" * 60} for i in range(40)]  # > result_chars
            return FakeResult([FakeContent(json.dumps({"tracks": padding + [{"name": "Feeling Good", "artists": ["Nina Simone"]},
                                                                            {"name": "Last", "artists": ["Erik Satie"]}]}))])
        if name == "get_playlists":
            return FakeResult([FakeContent(json.dumps({"playlists": [{"name": "Acid Techno"}, {"name": "🥲"}]}))])
        raise KeyError(name)


TOOLS = [FakeTool(n) for n in ("next", "previous", "pause", "play", "get_current_track", "get_devices", "set_volume",
                               "get_favorites", "get_saved_tracks", "get_playlists")]


def make(broken: bool = False, actions: ActionsConfig | None = None):
    spotify = FakeSpotify(broken)
    session = FakeSession(TOOLS, spotify.handle)
    tools = ToolsConfig(servers={"spotify": {"topic": "music", "command": "spotify"}}, preconnect=False)
    toolbox = Toolbox(tools, connect=make_connect({"spotify": session}))
    return spotify, toolbox, Actor(actions or ActionsConfig(), toolbox)


def route(text: str, tool: str, tool_confidence: float = 0.9, has_argument: float = 0.1, decision: str = "act",
          topic: str = "music", kind: str = "request") -> Route:
    return Route(text=text, kind=kind, topic=topic, confidence=0.9, is_urgent=0.2, is_about_her=0.1, decision=decision,
                 tool=tool, tool_confidence=tool_confidence, has_argument=has_argument)


@pytest.fixture(autouse=True)
def no_settle_wait(monkeypatch):
    """The reflexes wait 0.6 s for Spotify to catch up; not in tests."""
    import asyncio

    import strawberryd.actions as actions

    real_sleep = asyncio.sleep
    monkeypatch.setattr(actions.asyncio, "sleep", lambda s: real_sleep(0))


def test_track_shapes():
    assert _track({"playing": True, "track": TRACKS[0]}) == "Feeling Good by Nina Simone"
    assert _track({"track": {"name": "Solo", "artists": []}}) == "Solo by an unknown artist"
    assert _track({"playing": False}) == ""


def test_outcome_becomes_an_action_event_asking_for_a_quip():
    ok = Outcome("skipped to the next track", "Skipped. Now Blue Monday by New Order.", True).event("skip this song")
    assert ok.source == "action" and ok.app == "skipped to the next track" and ok.category == ""
    text = describe(ok)
    assert text.startswith("source: action") and 'have just said: "Skipped. Now Blue Monday by New Order."' in text
    assert "asked: skip this song" in text and "at most 8 words" in text
    failed = Outcome("tried to pause", "I tried to pause, but Spotify said: no active device.", False).event("pause")
    assert failed.category == "failed" and failed.urgency == "critical"


async def test_skip_reflex_calls_next_and_names_the_new_track():
    spotify, toolbox, actor = make()
    outcome = await actor.act("skip this song", route("skip this song", "skip"))
    assert outcome is not None
    assert spotify.log == ["next", "get_current_track"]
    assert outcome.did == "skipped to the next track" and outcome.fact == "Skipped. Now Blue Monday by New Order."
    assert actor.stats()["acted"] == 1 and actor.stats()["last"]["ok"] is True and actor.stats()["last"]["fact"] == outcome.fact
    assert actor.stats()["last"]["calls"][0]["name"] == "next"
    await toolbox.close()


async def test_every_spotify_reflex():
    spotify, toolbox, actor = make()
    pause = await actor.act("pause", route("pause", "pause"))
    assert pause.fact == "Paused." and spotify.playing is False
    resume = await actor.act("resume", route("resume", "resume"))
    assert spotify.playing is True and resume.fact == "Playing again: Feeling Good by Nina Simone."
    again = await actor.act("play it please", route("play it please", "resume"))
    assert again.ok and again.fact == "It's already playing: Feeling Good by Nina Simone." and spotify.log[-1] == "get_current_track"
    prev = await actor.act("previous", route("previous", "previous"))
    assert prev.did == "went back to the previous track" and prev.fact == "Back to Blue Monday by New Order."
    down = await actor.act("quieter", route("quieter", "volume_down"))
    assert spotify.volume == 65 and down.fact == "Volume down to 65."
    up = await actor.act("louder", route("louder", "volume_up"))
    assert spotify.volume == 80 and up.did == "turned the volume up" and up.fact == "Volume up to 80."
    now = await actor.act("what song is this", route("what song is this", "now_playing", kind="question"))
    assert now.did == "looked at the player" and now.fact == "That's Blue Monday by New Order, from Power, Corruption & Lies."
    spotify.playing = False
    paused = await actor.act("what song is this", route("what song is this", "now_playing", kind="question"))
    assert paused.fact.startswith("Paused on Blue Monday")
    await toolbox.close()


async def test_failures_become_a_failed_action_event():
    spotify, toolbox, actor = make(broken=True)
    outcome = await actor.act("skip this song", route("skip this song", "skip"))
    assert not outcome.ok and outcome.fact == "I tried to skip, but Spotify said: no active device."
    assert actor.stats()["failed"] == 1
    canned = await CannedReactor().react(outcome.event("skip this song"))
    assert canned.emotion == "alert" and not canned.text  # nothing to add to the fact
    await toolbox.close()
    _, toolbox2, restricted = make(broken="restricted")
    outcome = await restricted.act("skip", route("skip", "skip"))
    assert outcome.fact == "I tried to skip, but Spotify won't do that right now (already doing it, or the device refuses)."
    await toolbox2.close()
    _, toolbox3, no_device = make(broken="no_device")
    outcome = await no_device.act("skip", route("skip", "skip"))
    assert outcome.fact == "I tried to skip, but Spotify has no active device; open Spotify on the computer or phone first."
    await toolbox3.close()


async def test_when_the_reflex_does_not_apply_she_answers_as_chat():
    spotify, toolbox, actor = make()
    assert await actor.act("play some jazz", route("play some jazz", "other")) is None
    assert await actor.act("play Nina Simone", route("play Nina Simone", "resume", has_argument=0.9)) is None
    assert await actor.act("skip maybe", route("skip maybe", "skip", tool_confidence=0.3)) is None
    assert await actor.act("skip", route("skip", "skip", decision="offer")) is None
    assert await actor.act("lock the screen", route("lock the screen", "", topic="system")) is None
    assert spotify.log == [] and actor.stats()["deferred"] == 4  # other, argument, low confidence, no reflex for system
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


async def test_voice_to_skip_end_to_end_through_the_daemon(aiohttp_client):
    """A spoken 'skip this track' becomes a next() call and a line about the new track."""
    config = Config()
    config.brain.enabled = config.speech.enabled = config.voice.enabled = config.thinker.enabled = False
    spotify, toolbox, actor = make()
    gate = Gate(GateConfig(query_prefix="", document_prefix=""), embedder=FakeEmbedder())
    daemon = Daemon(reactor=CannedReactor(), config=config, gate=gate, toolbox=toolbox, actor=actor)
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    response = await client.post("/event", json={"source": "voice", "title": "skip this track"})
    body = await response.json()
    assert spotify.log == ["next", "get_current_track"], gate.last_route.to_dict()
    assert body["performance"]["text"] == "Skipped. Now Blue Monday by New Order."  # canned adds no quip
    assert body["performance"]["emotion"] == "happy" and body["performance"]["reaction"] == "nod"
    health = await (await client.get("/health")).json()
    assert health["actions"]["acted"] == 1 and health["actions"]["last"]["tool"] == "skip"
    assert health["gate"]["last_route"]["tool"] == "skip"
    # Small talk does not touch the tools.
    await client.post("/event", json={"source": "voice", "title": "how are you doing today"})
    assert spotify.log == ["next", "get_current_track"]
    # The track change she just caused is reported by the MPRIS doorway; that reaction is swallowed.
    performed = daemon.performed
    response = await client.post("/event", json={"source": "media", "app": "Spotify", "title": "New Order — Blue Monday"})
    assert (await response.json())["sent"] == 0 and daemon.performed == performed
    daemon.quiet_media_until = 0.0
    response = await client.post("/event", json={"source": "media", "app": "Spotify", "title": "Nina Simone — Feeling Good"})
    assert daemon.performed == performed + 1
    await daemon.close()


async def test_vocabulary_comes_from_the_library_in_order_of_likelihood():
    spotify, toolbox, actor = make()
    names = await actor.vocabulary()
    # now playing, playlists (no emoji), favourites, saved (a listing far longer than tools.result_chars)
    assert names == ["Nina Simone", "Acid Techno", "Daft Punk", "New Order", "Erik Satie"]
    assert await actor.situation("music") == "Now playing on Spotify: Feeling Good by Nina Simone (album: I Put a Spell on You)."
    await toolbox.close()


async def test_daemon_merges_config_vocabulary_with_the_library(aiohttp_client):
    config = Config()
    config.brain.enabled = config.speech.enabled = config.voice.enabled = config.gate.enabled = config.thinker.enabled = False
    config.voice.vocabulary = ["Kaelon", "Daft Punk"]
    spotify, toolbox, actor = make()
    daemon = Daemon(reactor=CannedReactor(), config=config, toolbox=toolbox, actor=actor)
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    assert daemon.vocabulary_task is None  # voice is off: no background refresh
    await daemon.refresh_vocabulary()
    assert daemon.vocabulary == ["Kaelon", "Daft Punk", "Nina Simone", "Acid Techno", "New Order", "Erik Satie"]
    assert daemon.hotwords() == "Kaelon, Daft Punk, Nina Simone, Acid Techno, New Order, Erik Satie"
    config.voice.max_hotwords = 2
    assert daemon.hotwords() == "Kaelon, Daft Punk"
    assert (await (await client.get("/health")).json())["voice"]["hotwords"] == 6
    await daemon.close()


async def test_no_quip_after_a_paragraph_long_fact():
    from strawberryd.contract import Performance

    class Quipper:
        async def react(self, event, context=""):
            return Performance(state="talking", text="Frankly an upgrade.", emotion="happy")

    config = Config()
    config.brain.enabled = config.speech.enabled = config.voice.enabled = config.gate.enabled = False
    config.tools.enabled = config.thinker.enabled = False
    daemon = Daemon(reactor=Quipper(), config=config)
    short = Outcome("skipped", "Skipped. Now Blue Monday by New Order.", True).event("skip")
    performance, _ = await daemon.report(short, True)
    assert performance.text == "Skipped. Now Blue Monday by New Order. Frankly an upgrade."
    long = Outcome("answered", "Led Zeppelin were a British rock band formed in London in 1968. " * 4, True).event("tell me")
    performance, _ = await daemon.report(long, True)
    assert performance.text == long.body and "upgrade" not in performance.text
