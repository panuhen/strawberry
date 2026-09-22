"""The adapter registry and the routing above it (ADAPTERS.md, WIRING.md §8b).

Who answers a bare music command: a configured server whose adapter has a reflex for it, else
MPRIS. With no adapter the core says nothing in any server's words.
"""

from __future__ import annotations

import pytest

from strawberry_crab.actions import Actor
from strawberry_crab.adapters import REGISTRY, Adapter, adapter_for, gate_examples, load
from strawberry_crab.adapters.spotify import SPOTIFY
from strawberry_crab.config import ActionsConfig, Config, GateConfig, ToolsConfig
from strawberry_crab.daemon import Daemon
from strawberry_crab.events import CannedReactor
from strawberry_crab.mpris import Mpris
from strawberry_crab.tools import Toolbox
from tests.fake_spotify import make
from tests.test_actions import route
from tests.test_mpris import FakeBus, FakePlayer
from tests.test_tools import FakeSession, FakeTool, make_connect

RESTRICTED = ('{"error": "Permission denied. Check app scopes.", "status": 403, '
              '"details": "http 403: Player command failed: Restriction violated, reason: UNKNOWN"}')


@pytest.fixture(autouse=True)
def no_settle_wait(monkeypatch):
    import asyncio

    import strawberry_crab.actions as actions

    real_sleep = asyncio.sleep
    monkeypatch.setattr(actions.asyncio, "sleep", lambda s: real_sleep(0))


# ----------------------------------------------------------------------------- the registry


def test_an_adapter_matches_by_server_name_or_by_the_adapter_key():
    assert adapter_for("spotify", {"topic": "music", "command": "x"}) is SPOTIFY
    assert adapter_for("Spotify", {"command": "x"}) is SPOTIFY            # the name as written, any case
    assert adapter_for("music-at-home", {"command": "x", "adapter": "spotify"}) is SPOTIFY
    assert adapter_for("notes", {"command": "x"}) is None                 # a plain server of tools
    assert adapter_for("notes", {"command": "x", "adapter": "nothing-like-this"}) is None
    # An explicit key decides alone: it can also say "this is not the Spotify one".
    assert adapter_for("spotify", {"command": "x", "adapter": "elsewhere"}) is None
    assert all(a.name and a.server_names for a in REGISTRY)


def test_load_and_the_gate_phrases_it_brings():
    adapters = load({"spotify": {"topic": "music", "command": "x"},
                     "tunes": {"topic": "music", "command": "y", "adapter": "spotify"},
                     "notes": {"topic": "notes", "command": "z"}})
    assert adapters == {"spotify": SPOTIFY, "tunes": SPOTIFY}
    examples = gate_examples(adapters)
    assert examples["kind.request"] == ["save this song", "like this track", "add this to my favourites",
                                        "put this on my running playlist"]   # once, not twice for two servers
    assert "save this song" in examples["music_tool.other"]
    assert gate_examples({}) == {}
    assert load({}) == {}


def test_the_base_adapter_adds_nothing():
    plain = Adapter()
    assert plain.reflexes == {} and plain.common_tools == () and plain.flat_gate_examples() == {}
    assert plain.clarify_error(RESTRICTED) == RESTRICTED


async def test_a_server_without_an_adapter_is_left_in_its_own_words():
    """No adapter, no Spotify wording anywhere: the error body reaches the model as it came."""
    session = FakeSession([FakeTool("next")], lambda name, args: _restricted())
    box = Toolbox(ToolsConfig(servers={"tunes": {"topic": "music", "command": "tunes"}}, preconnect=False),
                  connect=make_connect({"tunes": session}))
    assert box.adapters == {} and box.servers["tunes"].adapter is None
    result = await box.call("tunes", "next")
    assert not result.ok and "Permission denied. Check app scopes." in result.text
    assert "Not a permissions problem" not in result.text
    assert box.servers["tunes"].stats()["adapter"] is None and box.common_tools("tunes") == ()
    await box.close()


async def _restricted():
    from tests.test_tools import FakeContent, FakeResult

    return FakeResult([FakeContent(RESTRICTED)])


async def test_the_adapter_key_gives_any_server_the_spotify_wording():
    spotify, toolbox, actor = make(broken="restricted", name="music-at-home",
                                   server={"topic": "music", "command": "spotify", "adapter": "spotify"})
    assert toolbox.adapters == {"music-at-home": SPOTIFY}
    assert toolbox.common_tools("music-at-home")[0] == "search"
    outcome = await actor.act("skip this song", route("skip this song", "skip"))
    assert outcome.fact.startswith("I tried to skip, but Spotify won't do that right now")
    assert actor.stats()["last"]["server"] == "music-at-home"
    await toolbox.close()


# ----------------------------------------------------------------------------- who answers


def mpris_actor(servers: dict | None = None, sessions: dict | None = None, **players):
    """An Actor with a fake MPRIS bus, and whatever servers a test wants beside it."""
    bus = FakeBus(FakePlayer("spotify", **players))
    box = Toolbox(ToolsConfig(servers=servers or {}, preconnect=False),
                  connect=make_connect(sessions or {}))
    return bus, box, Actor(ActionsConfig(), box, mpris=Mpris(bus, settle_s=0.0))


async def test_with_no_server_the_bare_music_commands_go_over_mpris():
    bus, box, actor = mpris_actor()
    found = actor.reflex_for(route("skip this song", "skip"))
    assert found is not None and found[0] == "mpris"
    outcome = await actor.act("skip this song", route("skip this song", "skip"))
    assert outcome.ok and outcome.fact == "Skipped. Now Blue Monday by New Order."
    assert actor.stats()["last"]["server"] == "mpris" and bus.log[-2] == "Next"
    now = await actor.act("what song is this", route("what song is this", "now_playing", kind="question"))
    assert now.fact.startswith("That's Blue Monday by New Order")
    pause = await actor.act("pause", route("pause", "pause"))
    assert pause.fact == "Paused."
    assert (await actor.act("what song is this", route("what song is this", "now_playing"))).fact.startswith("Paused on")
    # Only the music tools, and only when the gate is sure: everything else is still the thinker's.
    assert actor.reflex_for(route("lock the screen", "skip", topic="system")) is None
    assert actor.reflex_for(route("play daft punk", "resume")) is None
    assert actor.reflex_for(route("skip maybe", "skip", tool_confidence=0.3)) is None
    await box.close()


async def test_a_configured_adapter_beats_mpris():
    """Spotify's own reflexes can name the next track before the desktop metadata catches up."""
    from tests.fake_spotify import FakeSpotify, TOOLS

    spotify = FakeSpotify()
    bus, box, actor = mpris_actor(servers={"spotify": {"topic": "music", "command": "spotify"}},
                                  sessions={"spotify": FakeSession(TOOLS, spotify.handle)})
    found = actor.reflex_for(route("skip this song", "skip"))
    assert found is not None and found[0] == "spotify"
    outcome = await actor.act("skip this song", route("skip this song", "skip"))
    assert outcome.fact == "Skipped. Now Blue Monday by New Order."
    assert spotify.log == ["next", "get_current_track"]
    assert "Next" not in bus.log   # the desktop player was not touched
    await box.close()


async def test_a_server_with_no_reflex_for_that_tool_falls_through_to_mpris():
    """A music server that is not an adapter still leaves the bare commands to the player."""
    session = FakeSession([FakeTool("search")], lambda name, args: _restricted())
    bus, box, actor = mpris_actor(servers={"tunes": {"topic": "music", "command": "tunes"}},
                                  sessions={"tunes": session})
    assert actor.reflexes == {}
    outcome = await actor.act("pause", route("pause", "pause"))
    assert outcome.ok and outcome.fact == "Paused." and actor.stats()["last"]["server"] == "mpris"
    await box.close()


async def test_mpris_says_what_is_playing_when_no_server_can():
    bus, box, actor = mpris_actor()
    assert await actor.situation() == ("Now playing on Spotify: Feeling Good by Nina Simone "
                                       "(album: I Put a Spell on You).")
    assert await actor.situation("calendar") == ""   # not this topic's business
    assert await actor.vocabulary() == []            # no server, no library names
    await box.close()


async def test_mpris_can_be_turned_off_and_the_adapter_phrases_reach_the_gate():
    config = Config()
    config.brain.enabled = config.speech.enabled = config.voice.enabled = False
    config.gate.enabled = config.thinker.enabled = False
    config.tools.servers = {"spotify": {"topic": "music", "command": "spotify"}}
    daemon = Daemon(reactor=CannedReactor(), config=config)
    assert daemon.mpris is not None and daemon.actor.reflexes["spotify"]["skip"] is SPOTIFY.reflexes["skip"]
    assert "save this song" in daemon.gate.extra_examples["kind.request"]
    await daemon.close()

    config.actions.mpris = False
    config.tools.servers = {}
    quiet = Daemon(reactor=CannedReactor(), config=config)
    assert quiet.mpris is None and quiet.actor.mpris is None and quiet.actor.reflexes == {}
    assert quiet.gate.extra_examples == {}   # no server, no Spotify phrases in her head
    assert quiet.actor.reflex_for(route("skip this song", "skip")) is None
    await quiet.close()


def test_the_gate_keeps_the_config_examples_after_the_adapters():
    from strawberry_crab.systemone import Gate

    gate = Gate(GateConfig(enabled=False, examples={"kind.request": ["put the kettle on"]}),
                examples={"kind.request": ["save this song"]})
    assert gate.extra_examples["kind.request"] == ["save this song", "put the kettle on"]
    request = next(o for o in gate.questions[0].options if o.name == "request")
    assert request.examples[-2:] == ("save this song", "put the kettle on")
