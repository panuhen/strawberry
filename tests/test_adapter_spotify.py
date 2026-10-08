"""The Spotify adapter (ADAPTERS.md): its reflexes, its situation, its vocabulary, its errors."""

from __future__ import annotations

import json
from typing import Any

import pytest

from strawberry_crab.adapters.spotify import (SPOTIFY, _failed, _track, clarify_error, playlist_request, says_like,
                                              says_play_liked, several)
from strawberry_crab.config import Config
from strawberry_crab.daemon import Daemon
from strawberry_crab.events import CannedReactor
from strawberry_crab.server import create_app
from strawberry_crab.tools import ToolResult, looks_like_error
from tests.fake_spotify import NEW_ERRORS, TRACKS, fake_gate, make
from tests.test_actions import route


@pytest.fixture(autouse=True)
def no_settle_wait(monkeypatch):
    """The reflexes wait 0.6 s for Spotify to catch up; not in tests."""
    import asyncio

    import strawberry_crab.actions as actions

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
    # now playing, playlists (no emoji), saved (a listing far longer than tools.result_chars)
    assert names == ["Nina Simone", "Acid Techno", "New Order", "Erik Satie"]
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


# ----------------------------------------------------------------------------- the by-name library tools


LIKES = ["I like this", "i like this song", "I really like this one!", "like this track", "Like this.", "save this song",
         "save this track please", "Save it.", "keep this one", "save this to my liked songs",
         "add this to my liked songs", "add it to liked songs", "Strawberry, I like this", "ok save this song for me",
         # Favourites are Liked Songs.
         "add this to my favourites", "add this song to my favorites please", "save this to my favourites",
         "put it in my favourites", "favourite this track", "Favorite this.",
         # The wrapping that leaves the request the same ("for later" made Qwen skip the call).
         "hey could you save this song to my favourites for later", "could you add this one to my favourites",
         "can you like this please", "please save this, thanks"]
NOT_LIKES = ["I like this better than the last one", "I don't like this", "do you like this song", "I love this song",
             "save this to my gym playlist", "add this to my running playlist", "like this but faster",
             "play something like this", "I like it when you dance", "save my place", "what do you like", "like",
             "add this to my favourites playlist", "play my favourites", "what are my favourites",
             "would you like this song", "can you save this to my gym playlist for later"]
PLAY_LIKED = ["play my favourites", "Play my favorites, please", "shuffle my favourites", "put on my liked songs",
              "play some of my favourite songs", "Strawberry, play my saved tracks", "play the liked songs now",
              "can you play my favourites", "Could you please play my favorites, thanks"]
NOT_PLAY_LIKED = ["play my favourites playlist", "play my favourite songs from last month", "play some jazz",
                  "add this to my favourites", "what are my favourites", "play my running playlist", "play",
                  "can you play my favourites playlist", "would you play my favourites"]


@pytest.mark.parametrize("text", LIKES)
def test_a_like_is_known_by_its_words(text):
    assert says_like(text)


@pytest.mark.parametrize("text", NOT_LIKES)
def test_anything_more_is_not_a_like(text):
    assert not says_like(text)


@pytest.mark.parametrize("text", PLAY_LIKED)
def test_play_my_favourites_is_known_by_its_words(text):
    assert says_play_liked(text) and SPOTIFY.said_reflex(text, None) == "play_liked"


@pytest.mark.parametrize("text", NOT_PLAY_LIKED)
def test_anything_more_is_not_play_my_favourites(text):
    assert not says_play_liked(text)


async def test_add_this_to_my_favourites_likes_the_track():
    """The favourites are the Liked Songs: the same reflex as "I like this", never a claim without a call."""
    spotify, toolbox, actor = make(library=True)
    outcome = await actor.act("add this to my favourites", route("add this to my favourites", "other"))
    assert outcome.ok and outcome.fact == "Liked: Feeling Good by Nina Simone."
    assert spotify.log == ["like_current"] and actor.stats()["last"]["tool"] == "like"
    await toolbox.close()


async def test_play_my_favourites_plays_the_liked_songs_shuffled():
    spotify, toolbox, actor = make(library=True)
    outcome = await actor.act("play my favourites", route("play my favourites", "other", decision="chat"))
    assert outcome.ok and outcome.did == "played the liked songs"
    assert outcome.fact == "Playing your Liked Songs, shuffled, starting with Around the World by Daft Punk."
    assert spotify.log == ["play_liked"] and spotify.played_liked == [{"shuffle": True}]
    assert actor.stats()["last"]["tool"] == "play_liked"
    await toolbox.close()


async def test_play_my_favourites_failures_in_her_words():
    spotify, toolbox, actor = make(broken="device", library=True)
    outcome = await actor.act("play my favourites", route("play my favourites", "other"))
    assert not outcome.ok and outcome.fact == ("I tried to play your Liked Songs, but Spotify has no active device; "
                                               "open Spotify on the computer or phone first.")
    await toolbox.close()


async def test_an_older_server_without_play_liked_leaves_play_my_favourites_to_the_thinker():
    spotify, toolbox, actor = make()   # no library tools
    assert await actor.act("play my favourites", route("play my favourites", "other", decision="chat")) is None
    assert "play_liked" not in spotify.log
    await toolbox.close()


async def test_i_like_this_likes_the_track_even_when_the_gate_reads_chat():
    """The gate has no option for liking and reads "I like this" as chat: the adapter knows the
    sentence by its words, and the server's like_current saves the track to Liked Songs, once."""
    spotify, toolbox, actor = make(library=True)
    chat = route("I like this", "other", decision="chat", kind="chat")
    outcome = await actor.act("I like this", chat)
    assert outcome.ok and outcome.did == "liked the track" and outcome.fact == "Liked: Feeling Good by Nina Simone."
    assert spotify.log == ["like_current"] and spotify.liked == ["Feeling Good – Nina Simone"]
    assert actor.stats()["last"]["tool"] == "like" and actor.stats()["last"]["server"] == "spotify"
    again = await actor.act("save this song", route("save this song", "other"))
    assert again.ok and again.fact == "Already in your Liked Songs: Feeling Good by Nina Simone."
    assert spotify.liked == ["Feeling Good – Nina Simone"]
    # Not aimed at her, or about something else: not a like.
    assert await actor.act("I like this", route("I like this", "other", kind="other", decision="chat")) is None
    assert await actor.act("I like this", route("I like this", "other", topic="other", decision="chat")) is None
    assert spotify.log == ["like_current", "like_current"]
    await toolbox.close()


async def test_an_older_server_without_like_current_leaves_it_to_the_thinker():
    spotify, toolbox, actor = make()
    assert await actor.act("I like this", route("I like this", "other", decision="chat", kind="chat")) is None
    assert await actor.act("save this song", route("save this song", "other")) is None   # deferred to the thinker
    assert "like_current" not in spotify.log and actor.stats()["deferred"] == 1
    await toolbox.close()


@pytest.mark.parametrize("broken, fact", [
    ("network", "I tried to like it, but I can't reach Spotify right now."),
    ("auth", "I tried to like it, but Spotify needs signing in again."),
    ("device", "I tried to like it, but Spotify has no active device; open Spotify on the computer or phone first."),
    ("rate_limited", "I tried to like it, but Spotify is limiting requests right now. Try again in 30 seconds."),
    ("nothing", "I tried to like it, but nothing is playing right now."),
    ("premium", "I tried to like it, but that needs Spotify Premium."),
    ("several", "I tried to like it, but several playlists match: Gym, Gym Mix or Old Gym. Which one?"),
    # Was "…but Spotify said: Could not reach Spotify. Check the internet connection and try again.."
    ("old_network", "I tried to like it, but I can't reach Spotify right now."),
])
async def test_failures_in_her_own_words_by_their_code(broken, fact):
    spotify, toolbox, actor = make(broken=broken, library=True)
    outcome = await actor.act("I like this", route("I like this", "other", decision="chat", kind="chat"))
    assert not outcome.ok and outcome.fact == fact
    assert ".." not in outcome.fact and "Spotify said" not in outcome.fact
    await toolbox.close()


def failed(body: dict | str):
    text = body if isinstance(body, str) else json.dumps(body)
    return _failed("skip", ToolResult("spotify", "next", False, text, 1.0))


def test_the_old_error_shapes_still_read_right():
    """A server from before the codes: Spotify's own details, a 403, or one sentence."""
    old_network = {"error": "Could not reach Spotify. Check the internet connection and try again."}
    assert failed(old_network).fact == "I tried to skip, but I can't reach Spotify right now."
    assert failed({"error": "Spotify sign-in failed. Run spotify-mcp --login in a terminal to sign in again."}).fact == (
        "I tried to skip, but Spotify needs signing in again.")
    assert failed({"error": "Permission denied. Check app scopes.", "status": 403, "details": "Forbidden"}).fact == (
        "I tried to skip, but Spotify says that's not allowed for this app.")
    assert failed({"error": "Spotify returned an unexpected error."}).fact == (
        "I tried to skip, but Spotify returned an unexpected error.")
    assert failed({"error": "Token expired."}).fact == "I tried to skip, but Spotify said: token expired."
    assert failed("upstream exploded\nsecond line").fact == "I tried to skip, but Spotify said: upstream exploded."
    assert failed("").fact == "I tried to skip, but Spotify said: no answer."


def test_a_code_wins_over_the_details_and_the_rest_say_the_servers_sentence():
    assert failed({"error": "Spotify refused that command.", "code": "restricted", "status": 403,
                   "details": "Restriction violated"}).fact == (
        "I tried to skip, but Spotify won't do that right now (already doing it, or the device refuses).")
    assert failed({"error": "Spotify is having trouble right now. Try again in a minute.", "code": "unavailable",
                   "status": 502}).fact == ("I tried to skip, but Spotify is having trouble right now. Try again in a "
                                            "minute.")
    assert failed({"error": "No playlist of yours matches 'gim'. Closest: Gym.", "code": "not_found"}).fact == (
        "I tried to skip, but no playlist of yours matches 'gim'. Closest: Gym.")
    assert failed({"error": "You already have a playlist called 'Gym'. Use it, or pass force=true to make another.",
                   "code": "bad_request"}).fact == "I tried to skip, but you already have a playlist called 'Gym'."


def test_several_playlists_become_a_question_naming_at_most_three():
    assert several("Several playlists match 'gym': Gym, Gym Mix (id 0123456789abcdefABCDEF), Old Gym, Gym 2 and 3 "
                   "more. Which one?") == ["Gym", "Gym Mix", "Old Gym", "Gym 2"]
    assert several("Several playlists match 'run': Running, Run Club. Which one?") == ["Running", "Run Club"]
    assert several("No playlist matches 'run'.") == []
    assert failed({"error": "Several playlists match 'run': Running, Run Club. Which one?", "code": "bad_request"}).fact == (
        "I tried to skip, but several playlists match: Running or Run Club. Which one?")
    body = json.dumps(NEW_ERRORS["several"])
    clarified = json.loads(clarify_error(body))
    assert clarified["error"].startswith("Several of the user's playlists match that name: Gym, Gym Mix or Old Gym.")
    assert "Ask the user which one" in clarified["error"] and "Gym 2" not in clarified["error"]
    assert clarified["code"] == "bad_request" and looks_like_error(json.dumps(clarified))


def test_clarify_reads_the_code():
    network = json.loads(clarify_error(json.dumps(NEW_ERRORS["network"])))
    assert "cannot be reached" in network["error"] and "Do not retry" in network["error"]
    auth = json.loads(clarify_error(json.dumps(NEW_ERRORS["auth"])))
    assert "signing in again" in auth["error"] and "--login" not in auth["error"]
    device = json.loads(clarify_error(json.dumps(NEW_ERRORS["device"])))
    assert "no active device" in device["error"] and "Do not retry" in device["error"]
    restricted = {"error": "Spotify refused that command.", "code": "restricted", "status": 403}
    assert "Not a permissions problem" in clarify_error(json.dumps(restricted))
    for kept in ("rate_limited", "nothing", "premium"):
        assert clarify_error(json.dumps(NEW_ERRORS[kept])) == json.dumps(NEW_ERRORS[kept])


def test_the_new_tools_are_common_and_not_careful():
    assert {"like_current", "add_current_to_playlist", "find_playlist", "play_liked"} <= set(SPOTIFY.common_tools)
    assert len(SPOTIFY.common_tools) == 14 and len(set(SPOTIFY.common_tools)) == 14
    assert SPOTIFY.common_tools.index("like_current") < SPOTIFY.common_tools.index("add_to_queue")
    assert SPOTIFY.guide_for(["like_current", "play_liked", "play"]) == SPOTIFY.guide
    assert "add_current_to_playlist" in SPOTIFY.guide and "call play_liked" in SPOTIFY.guide
    # A server without play_liked gets the paragraph without it; an older one without the by-name tools, none.
    assert "play_liked" not in SPOTIFY.guide_for(["like_current", "play"])
    assert "add this to my favourites', call like_current" in SPOTIFY.guide_for(["like_current", "play"])
    assert SPOTIFY.guide_for(["play", "next"]) == ""


def test_the_local_favourites_tools_are_gone():
    """The server's local favourites list was retired: nothing here names its tools any more."""
    import inspect

    import strawberry_crab.adapters.spotify as module

    source = inspect.getsource(module)
    for name in ("favorite_current", "get_favorites", "remove_favorite", "play_favorites", "clear_favorites"):
        assert name not in source and name not in CAREFUL


CAREFUL = ["save_tracks", "remove_saved_tracks", "add_to_playlist", "remove_from_playlist", "create_playlist"]


def thinker_over(script: list, broken: bool | str = False, confirm: list[str] | None = None):
    from strawberry_crab.config import ThinkerConfig
    from strawberry_crab.thinker import Thinker
    from tests.test_thinker import FakeQwen

    server: dict[str, Any] = {"topic": "music", "command": "spotify", "careful": CAREFUL}
    if confirm is not None:
        server["confirm"] = confirm
    spotify, toolbox, actor = make(broken=broken, server=server, library=True)
    qwen = FakeQwen(script)
    return spotify, toolbox, qwen, Thinker(ThinkerConfig(), toolbox, "qwen-test", chat=qwen)


@pytest.mark.parametrize("text, asks, name", [
    ("add this to my gym playlist", "add the playing track to their playlist", "gym"),
    ("put this song on my running playlist please", "add the playing track to their playlist", "running"),
    ("add it to the playlist called road trip please", "add the playing track to their playlist", "road trip"),
    ("play my running playlist", "play their playlist", "running"),
    ("put on my schranz playlist", "play their playlist", "schranz"),
    ("shuffle my deep focus playlist", "play their playlist", "deep focus"),
    ("take this off my gym playlist", "take the playing track off their playlist", "gym"),
    ("remove this song from my chill playlist", "take the playing track off their playlist", "chill"),
])
def test_a_playlist_sentence_gets_a_line_under_it(text, asks, name):
    line = playlist_request(text)
    assert line.startswith(f"They asked to {asks} '{name}':") and "only if a tool did it" in line
    assert SPOTIFY.wanted(text, None) is True and SPOTIFY.nudge(text, None) == line


@pytest.mark.parametrize("text, asks", [
    ("I think you should add this one to my favourites", "save the playing track to their favourites"),
    ("can you put this song in my favorites for me", "save the playing track to their favourites"),
    ("can you play my favourites", "play their favourites"),
    ("shuffle some of my liked songs while I work", "play their favourites"),
])
def test_a_favourites_sentence_gets_a_line_naming_the_liked_songs(text, asks):
    line = playlist_request(text)
    assert line.startswith(f"They asked to {asks}, which are their Liked Songs:") and "tool" in line
    assert SPOTIFY.wanted(text, None) is True and SPOTIFY.nudge(text, None) == line


def test_a_favourites_playlist_is_a_playlist():
    assert playlist_request("add this to my favourites playlist").startswith(
        "They asked to add the playing track to their playlist 'favourites'")
    assert playlist_request("play my favourites playlist").startswith("They asked to play their playlist 'favourites'")


@pytest.mark.parametrize("text", ["play some jazz", "what are my favourites", "what's on my gym playlist",
                                  "I like this", "how are you today", "play the playlist"])
def test_other_sentences_get_no_line(text):
    assert playlist_request(text) == "" and SPOTIFY.wanted(text, None) is None


def test_no_line_for_a_removal_that_is_not_offered():
    from tests.test_thinker import reading

    asked = reading("take this off my gym playlist", library_change=0.9)
    not_asked = reading("take this off my gym playlist", library_change=0.2)
    assert playlist_request("take this off my gym playlist", asked) and not playlist_request(
        "take this off my gym playlist", not_asked)


async def test_the_line_goes_under_the_sentence_and_the_prompt_stays_the_same():
    spotify, toolbox, qwen, thinker = thinker_over(["[neutral] Fine.", "[neutral] Fine."])
    await thinker.run("play my running playlist", topic="music")
    await thinker.run("play some jazz", topic="music")
    first, second = qwen.payloads
    assert first["messages"][0] == second["messages"][0] and first["tools"] == second["tools"]
    assert "They asked to play their playlist 'running'" in first["messages"][1]["content"]
    assert "They asked" not in second["messages"][1]["content"]
    await toolbox.close()


async def test_add_this_to_my_playlist_goes_by_name_through_the_thinker():
    spotify, toolbox, qwen, thinker = thinker_over([[("add_current_to_playlist", {"playlist": "gym"})],
                                                    "[happy] Added to Gym."])
    outcome = await thinker.run("add this to my gym playlist", careful=True, topic="music")
    assert outcome.ok and spotify.added == [("gym", "Feeling Good – Nina Simone")]
    system = qwen.payloads[0]["messages"][0]["content"]
    assert "call add_current_to_playlist with playlist X" in system
    await toolbox.close()


async def test_the_easy_to_undo_tools_are_offered_with_any_sentence_and_the_rest_only_when_asked():
    spotify, toolbox, qwen, thinker = thinker_over(["[neutral] Fine.", "[neutral] Fine."])
    await thinker.run("play my running playlist", topic="music")
    offered = {t["function"]["name"] for t in qwen.payloads[0]["tools"]}
    assert {"like_current", "add_current_to_playlist", "find_playlist", "play", "play_liked"} <= offered
    assert offered.isdisjoint({"remove_from_playlist", "create_playlist"})
    await thinker.run("take this off my running playlist", careful=True, topic="music")
    offered = {t["function"]["name"] for t in qwen.payloads[1]["tools"]}
    assert {"remove_from_playlist", "create_playlist", "like_current"} <= offered
    await toolbox.close()


async def test_a_favourites_sentence_through_the_thinker_gets_its_line_and_calls_like_current():
    spotify, toolbox, qwen, thinker = thinker_over([[("like_current", {})], "[happy] Liked it."])
    outcome = await thinker.run("I think you should add this one to my favourites", topic="music")
    assert outcome.ok and spotify.liked == ["Feeling Good – Nina Simone"]
    system, user = qwen.payloads[0]["messages"][0]["content"], qwen.payloads[0]["messages"][1]["content"]
    assert "the user's favourites are their Liked Songs" in system and "call play_liked" in system
    assert "which are their Liked Songs" in user
    await toolbox.close()


async def test_take_this_off_my_playlist_removes_the_current_track_only_when_asked():
    spotify, toolbox, qwen, thinker = thinker_over([[("remove_from_playlist", {"playlist": "running",
                                                                               "track": "current"})],
                                                    "[neutral] Gone from Running."], confirm=[])
    # `confirm = []`: no spoken yes first (test_confirm.py has the default, which asks).
    outcome = await thinker.run("take this off my running playlist", careful=True, topic="music")
    assert outcome.ok and spotify.removed == [("running", "current")]
    # Not asked to change the library: the same call is never made.
    spotify2, toolbox2, qwen2, thinker2 = thinker_over([[("remove_from_playlist", {"playlist": "running",
                                                                                   "track": "current"})],
                                                        "[neutral] Done."])
    await thinker2.run("play my running playlist", topic="music")
    assert spotify2.removed == [] and "remove_from_playlist" not in spotify2.log
    await toolbox.close()
    await toolbox2.close()


async def test_several_matching_playlists_reach_the_model_as_a_question_to_ask():
    spotify, toolbox, qwen, thinker = thinker_over([[("add_current_to_playlist", {"playlist": "gym"})],
                                                    "[neutral] Which one: Gym, Gym Mix or Old Gym?"], broken="several")
    outcome = await thinker.run("add this to my gym playlist", careful=True, topic="music")
    told = [m["content"] for m in qwen.payloads[1]["messages"] if m["role"] == "tool"][0]
    assert "Ask the user which one they mean" in told and "Gym 2" not in told
    assert outcome.fact.startswith("Which one")
    await toolbox.close()


async def test_i_like_this_through_the_daemon_is_a_reflex_on_the_liked_songs(aiohttp_client):
    from tests.test_thinker import ScriptedGate, reading

    config = Config()
    config.brain.enabled = config.speech.enabled = config.voice.enabled = config.thinker.enabled = False
    spotify, toolbox, actor = make(library=True)
    gate = ScriptedGate({"I like this": reading("I like this", kind="chat", topic="music", decision="chat",
                                                tool="other", library_change=0.35)})
    daemon = Daemon(reactor=CannedReactor(), config=config, gate=gate, toolbox=toolbox, actor=actor)
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    body = await (await client.post("/event", json={"source": "voice", "title": "I like this"})).json()
    assert spotify.log == ["like_current"] and body["performance"]["text"] == "Liked: Feeling Good by Nina Simone."
    health = await (await client.get("/health")).json()
    assert health["actions"]["last"]["tool"] == "like"
    await daemon.close()
