"""The Spotify adapter (ADAPTERS.md): its reflexes, its situation, its vocabulary, its errors."""

from __future__ import annotations

import pytest

from strawberryd.adapters.spotify import SPOTIFY, _track, clarify_error
from strawberryd.config import Config
from strawberryd.daemon import Daemon
from strawberryd.events import CannedReactor
from strawberryd.server import create_app
from strawberryd.tools import looks_like_error
from tests.fake_spotify import TRACKS, fake_gate, make
from tests.test_actions import route


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


async def test_skip_reflex_calls_next_and_names_the_new_track():
    spotify, toolbox, actor = make()
    outcome = await actor.act("skip this song", route("skip this song", "skip"))
    assert outcome is not None
    assert spotify.log == ["next", "get_current_track"]
    assert outcome.did == "skipped to the next track" and outcome.fact == "Skipped. Now Blue Monday by New Order."
    assert actor.stats()["acted"] == 1 and actor.stats()["last"]["ok"] is True and actor.stats()["last"]["fact"] == outcome.fact
    assert actor.stats()["last"]["server"] == "spotify" and actor.stats()["last"]["calls"][0]["name"] == "next"
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


def test_403s_are_clarified_but_stay_errors():
    """Spotify's server labels several different refusals "Permission denied. Check app scopes."."""
    restricted = '{"error": "Permission denied. Check app scopes.", "status": 403, "details": "http 403: Player command failed: Restriction violated, reason: UNKNOWN"}'
    text = clarify_error(restricted)
    assert "Not a permissions problem" in text and "already playing" in text and looks_like_error(text)
    forbidden = '{"error": "Permission denied. Check app scopes.", "status": 403, "details": "http 403: Forbidden, reason: None"}'
    assert "forbids this for the app" in clarify_error(forbidden)
    no_device = '{"error": "Resource not found.", "status": 404, "details": "http status: 404, code: -1 - https://api.spotify.com/v1/me/player/play:\\n Player command failed: No active device found"}'
    assert "no active device" in clarify_error(no_device) and "Do not retry" in clarify_error(no_device) and looks_like_error(clarify_error(no_device))
    assert clarify_error("Skipped.") == "Skipped." and clarify_error('{"error": "token expired"}') == '{"error": "token expired"}'
    assert SPOTIFY.clarify_error(restricted) == text


async def test_a_clarified_error_reaches_the_model_through_the_toolbox():
    spotify, toolbox, actor = make(broken="restricted")
    result = await toolbox.call("spotify", "next")
    assert not result.ok and "Not a permissions problem" in result.text
    await toolbox.close()


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
    config.voice.vocabulary = ["Lighthouse", "Daft Punk"]
    spotify, toolbox, actor = make()
    daemon = Daemon(reactor=CannedReactor(), config=config, toolbox=toolbox, actor=actor)
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    assert daemon.vocabulary_task is None  # voice is off: no background refresh
    await daemon.refresh_vocabulary()
    assert daemon.vocabulary == ["Lighthouse", "Daft Punk", "Nina Simone", "Acid Techno", "New Order", "Erik Satie"]
    assert daemon.hotwords() == "Lighthouse, Daft Punk, Nina Simone, Acid Techno, New Order, Erik Satie"
    config.voice.max_hotwords = 2
    assert daemon.hotwords() == "Lighthouse, Daft Punk"
    assert (await (await client.get("/health")).json())["voice"]["hotwords"] == 6
    await daemon.close()


async def test_voice_to_skip_end_to_end_through_the_daemon(aiohttp_client):
    """A spoken 'skip this track' becomes a next() call and a line about the new track."""
    config = Config()
    config.brain.enabled = config.speech.enabled = config.voice.enabled = config.thinker.enabled = False
    spotify, toolbox, actor = make()
    gate = fake_gate(toolbox)
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
    assert health["tools"]["spotify"]["adapter"] == "spotify"
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
