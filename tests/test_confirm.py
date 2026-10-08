"""A spoken yes before a removal (confirm.py): held, asked about, made only after the user's own yes."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from strawberry_crab import confirm
from strawberry_crab.adapters import spotify as spotify_adapter
from strawberry_crab.config import ConfigError, ThinkerConfig, ToolsConfig, default_toml, load
from strawberry_crab.confirm import LATE_YES, LEFT_NO, LEFT_OTHER, LEFT_SILENT, Held, answer
from strawberry_crab.server import create_app
from strawberry_crab.thinker import Thinker
from strawberry_crab.tools import Toolbox
from tests.fake_spotify import TRACKS, make
from tests.test_thinker import FakeQwen, ScriptedGate, plain_config, reading, voice_daemon
from tests.test_tools import FakeSession, FakeTool, make_connect

CAREFUL = ["save_tracks", "remove_saved_tracks", "add_to_playlist", "remove_from_playlist", "create_playlist"]
REMOVE = "take this off my running playlist"
RUNNING = "spotify:playlist:" + "x" * 22          # what the fake find_playlist answers for any name
FEELING_GOOD, BLUE_MONDAY = TRACKS[0]["uri"], TRACKS[1]["uri"]


@pytest.mark.parametrize("text", ["yes", "Yes.", "yeah, go ahead", "Sure, do it.", "okay", "yes please",
                                  "Yep, remove it", "go for it, thanks", "do it", "of course", "Yes, Strawberry."])
def test_a_yes(text):
    assert answer(text) == "yes"


@pytest.mark.parametrize("text", ["no", "No thanks.", "nope", "cancel", "never mind", "leave it", "don't",
                                  "okay, never mind", "no, don't do it", "wait", "yes no"])
def test_a_no(text):
    assert answer(text) == "no"


@pytest.mark.parametrize("text", ["what's playing", "yes and play some jazz", "thank you", "", "  ",
                                  "yes remove it from my gym playlist", "skip this", "yesterday"])
def test_neither(text):
    assert answer(text) is None


def spotify_server(**extra: Any) -> dict[str, Any]:
    return {"topic": "music", "command": "spotify", "careful": CAREFUL} | extra


async def test_the_default_list_is_the_two_removals_and_the_config_decides_over_it():
    spotify, toolbox, actor = make(server=spotify_server(), library=True)
    assert toolbox.servers["spotify"].confirm == {"remove_from_playlist", "remove_saved_tracks"}
    assert toolbox.needs_confirm("spotify", "remove_saved_tracks")
    assert not toolbox.needs_confirm("spotify", "save_tracks") and not toolbox.needs_confirm("spotify", "add_to_playlist")
    assert not toolbox.needs_confirm("nobody", "remove_saved_tracks")
    _s, off, _a = make(server=spotify_server(confirm=[]), library=True)
    assert off.servers["spotify"].confirm == frozenset()
    _s, own, _a = make(server=spotify_server(confirm=["create_playlist"]), library=True)
    assert own.servers["spotify"].confirm == {"create_playlist"}
    # A server with no adapter asks about nothing unless its config says so.
    plain = Toolbox(ToolsConfig(servers={"notes": {"topic": "notes", "command": "notes"}}, preconnect=False))
    assert plain.servers["notes"].confirm == frozenset()
    listed = Toolbox(ToolsConfig(servers={"notes": {"topic": "notes", "command": "notes", "confirm": ["delete_note"]}},
                                 preconnect=False))
    assert listed.needs_confirm("notes", "delete_note")


def test_the_config_key_is_checked(tmp_path):
    def loads(text: str):
        path = tmp_path / "config.toml"
        path.write_text(text)
        return load(path, env={})

    config = loads('[tools.servers.spotify]\ntopic = "music"\ncommand = "spotify"\nconfirm = ["remove_saved_tracks"]\n')
    assert config.tools.servers["spotify"]["confirm"] == ["remove_saved_tracks"]
    assert config.actions.confirm_s == 10.0
    with pytest.raises(ConfigError, match="confirm must be a list"):
        loads('[tools.servers.spotify]\ncommand = "spotify"\nconfirm = "remove_saved_tracks"\n')
    with pytest.raises(ConfigError, match="confirm_s"):
        loads("[actions]\nconfirm_s = 0\n")
    # The shipped template documents both, and still loads.
    assert "# confirm = [\"remove_from_playlist\", \"remove_saved_tracks\"]" in default_toml()
    assert loads(default_toml()).actions.confirm_s == 10.0


def thinker_over(script: list, **server: Any):
    spotify, toolbox, actor = make(server=spotify_server(**server), library=True,
                                   extra=[FakeTool("remove_saved_tracks"), FakeTool("save_tracks")])
    qwen = FakeQwen(script)
    return spotify, toolbox, actor, qwen, Thinker(ThinkerConfig(), toolbox, "qwen-test", chat=qwen)


async def test_the_thinker_stops_at_a_removal_and_asks_with_the_call_pinned():
    spotify, toolbox, actor, qwen, thinker = thinker_over(
        [[("remove_from_playlist", {"playlist": "running", "track": "current"}), ("next", {})], "[neutral] Gone."])
    outcome = await thinker.run(REMOVE, careful=True, topic="music")
    assert outcome.held == Held("spotify", "remove_from_playlist", {"playlist": RUNNING, "track": FEELING_GOOD},
                                "Remove 'Feeling Good' from Running? Say yes.")
    assert outcome.ok and outcome.fact == "Remove 'Feeling Good' from Running? Say yes."
    assert outcome.did == "asked before remove_from_playlist" and outcome.emotion == "neutral"
    # Nothing removed, the rest of that reply not made, and no second round.
    assert spotify.removed == [] and "remove_from_playlist" not in spotify.log and "next" not in spotify.log
    assert len(qwen.payloads) == 1 and thinker.stats()["last"]["held"] == "spotify.remove_from_playlist"
    await toolbox.close()


async def test_a_liked_songs_removal_is_asked_about_by_name():
    spotify, toolbox, actor, qwen, thinker = thinker_over(
        [[("remove_saved_tracks", {"track_ids": [FEELING_GOOD]})]])
    outcome = await thinker.run("remove this from my liked songs", careful=True, topic="music")
    assert outcome.fact == "Remove 'Feeling Good' from your Liked Songs? Say yes."
    assert outcome.held.arguments == {"track_ids": [FEELING_GOOD]} and spotify.removed == []
    await toolbox.close()


async def test_a_liked_songs_removal_with_a_made_up_id_is_sent_to_look_it_up_first():
    """Live, Qwen sent "spotify:track:teardrop-massive-attack" for "remove this from my liked songs":
    asking about that would end in the server's refusal after the yes."""
    spotify, toolbox, actor, qwen, thinker = thinker_over(
        [[("remove_saved_tracks", {"track_ids": ["spotify:track:feeling-good-nina-simone"]})],
         [("get_current_track", {})], [("remove_saved_tracks", {"track_ids": [FEELING_GOOD]})]])
    outcome = await thinker.run("remove this from my liked songs", careful=True, topic="music")
    told = [m["content"] for m in qwen.payloads[1]["messages"] if m["role"] == "tool"]
    assert told[0] == spotify_adapter.NOT_A_TRACK and len(qwen.payloads) == 3
    assert outcome.fact == "Remove 'Feeling Good' from your Liked Songs? Say yes."
    assert outcome.held.arguments == {"track_ids": [FEELING_GOOD]} and spotify.removed == []
    await toolbox.close()


@pytest.mark.parametrize("track, ok", [(FEELING_GOOD, True), ("1" * 22, True), ("https://open.spotify.com/track/"
                                       + "1" * 22 + "?si=x", True), ("spotify:track:feeling-good", False),
                                       ("current", False), ("", False)])
def test_what_counts_as_a_track_id(track, ok):
    assert (spotify_adapter.guard("remove_saved_tracks", {"track_ids": [track]}) is None) == ok
    assert spotify_adapter.guard("remove_from_playlist", {"track": "current"}) is None


async def test_confirm_off_means_the_removal_is_made_at_once():
    spotify, toolbox, actor, qwen, thinker = thinker_over(
        [[("remove_from_playlist", {"playlist": "running", "track": "current"})], "[neutral] Gone from Running."],
        confirm=[])
    outcome = await thinker.run(REMOVE, careful=True, topic="music")
    assert outcome.held is None and outcome.fact == "Gone from Running."
    assert spotify.removed == [("running", "current")]
    await toolbox.close()


async def test_saves_and_adds_stay_instant():
    spotify, toolbox, actor, qwen, thinker = thinker_over(
        [[("add_current_to_playlist", {"playlist": "running"})], "[happy] Added."])
    outcome = await thinker.run("add this to my running playlist", careful=True, topic="music")
    assert outcome.held is None and spotify.added == [("running", "Feeling Good – Nina Simone")]
    await toolbox.close()


async def test_an_adapter_that_fails_to_word_it_still_holds_the_call_as_it_came():
    spotify, toolbox, actor, qwen, thinker = thinker_over([[("remove_saved_tracks", {"track_ids": ["c" * 22]})]])

    async def broken(*args):
        raise RuntimeError("no")

    toolbox.adapters["spotify"].ask = broken   # type: ignore[method-assign]
    try:
        outcome = await thinker.run("remove that from my liked songs", careful=True, topic="music")
    finally:
        del toolbox.adapters["spotify"].ask
    assert outcome.held == Held("spotify", "remove_saved_tracks", {"track_ids": ["c" * 22]},
                                "Shall I go ahead with remove saved tracks? Say yes.")
    assert spotify.removed == []
    await toolbox.close()


# ----------------------------------------------------------------------------- the daemon


def daemon_over(script: list, routes: dict[str, Any] | None = None, confirm_s: float = 10.0, **server: Any):
    spotify, toolbox, actor, qwen, thinker = thinker_over(script, **server)
    config = plain_config()
    config.actions.confirm_s = confirm_s
    config.thinker = ThinkerConfig(ack_after_s=30.0, still_on_it_s=60.0)
    thinker.config = config.thinker
    gate = ScriptedGate({REMOVE: reading(REMOVE, kind="request", topic="music", decision="offer", tool="other",
                                         library_change=0.93, has_argument=0.9), **(routes or {})})
    daemon, sink = voice_daemon(config, toolbox, thinker, gate=gate, actor=actor)
    return spotify, qwen, daemon, sink


REMOVE_CALL = [("remove_from_playlist", {"playlist": "running", "track": "current"})]


def talking(sink) -> list[str]:
    return [m["text"] for m in sink.got if m.get("state") == "talking" and m.get("text")]


async def say(client, text: str, source: str = "voice", **extra: Any) -> dict:
    return await (await client.post("/event", json={"source": source, "title": text, **extra})).json()


async def test_yes_makes_exactly_the_held_call(aiohttp_client):
    spotify, qwen, daemon, sink = daemon_over([REMOVE_CALL])
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await say(client, REMOVE)
    assert talking(sink) == ["Remove 'Feeling Good' from Running? Say yes."]
    held = daemon.held
    assert held is not None and (await (await client.get("/health")).json())["confirm"]["waiting"] == held.key
    spotify.index = 1   # the song changes before the answer: the yes still removes the one she named
    reply = await say(client, "Yes, go ahead.")
    assert spotify.removed == [(RUNNING, FEELING_GOOD)] and spotify.removed[0] == tuple(held.arguments.values())
    assert reply["performance"]["text"].startswith("Removed Feeling Good by Nina Simone from Running.")
    assert len(qwen.payloads) == 1   # the model was never asked again
    # Her question is in the ledger by what it was, not in her words (the thinker copied those).
    assert 'you did "asked before remove_from_playlist" and said "(the yes-or-no question' in daemon.ledger.lines()[0]
    assert daemon.held is None and 'the user said "Yes, go ahead."; you did "removed a track from a playlist"' in daemon.ledger.lines()[-1]
    await daemon.close()


@pytest.mark.parametrize("no", ["no", "No, leave it.", "cancel"])
async def test_no_cancels(aiohttp_client, no):
    spotify, qwen, daemon, sink = daemon_over([REMOVE_CALL])
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await say(client, REMOVE)
    reply = await say(client, no)
    assert reply["performance"]["text"] == LEFT_NO and spotify.removed == [] and daemon.held is None
    assert len(qwen.payloads) == 1   # a no is not a sentence for the thinker
    reply = await say(client, "yes")   # nothing is waiting any more, and the question was dropped
    assert reply["performance"]["text"] == LATE_YES and spotify.removed == [] and len(qwen.payloads) == 1
    await say(client, "yes")   # only once: now it is an ordinary sentence for the thinker
    assert spotify.removed == [] and len(qwen.payloads) == 2
    await daemon.close()


async def test_silence_cancels_and_she_says_so(aiohttp_client):
    spotify, qwen, daemon, sink = daemon_over([REMOVE_CALL], confirm_s=0.05)
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await say(client, REMOVE)
    await asyncio.sleep(0.2)
    assert daemon.held is None and talking(sink)[-1] == LEFT_SILENT
    reply = await say(client, "yes")   # too late: she says so herself; nothing is removed, no model asked
    assert reply["performance"]["text"] == LATE_YES and spotify.removed == [] and len(qwen.payloads) == 1
    assert "(no answer)" in daemon.ledger.lines()[-2]
    await daemon.close()


async def test_a_late_yes_after_another_sentence_goes_to_the_thinker(aiohttp_client):
    spotify, qwen, daemon, sink = daemon_over([REMOVE_CALL, "[neutral] Twenty past four.", "[happy] Good."],
                                              confirm_s=0.05)
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await say(client, REMOVE)
    await asyncio.sleep(0.2)
    await say(client, "what time is it")
    reply = await say(client, "yes")
    assert reply["performance"]["text"] == "Good." and spotify.removed == []
    await daemon.close()


async def test_the_clock_waits_while_she_is_listening_to_the_answer(aiohttp_client):
    spotify, qwen, daemon, sink = daemon_over([REMOVE_CALL], confirm_s=0.05)
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await say(client, REMOVE)
    daemon.listener.busy = True   # the hotkey was pressed: the answer is on its way
    await asyncio.sleep(0.2)
    assert daemon.held is not None
    daemon.listener.busy = False
    await say(client, "yes")
    assert spotify.removed == [(RUNNING, FEELING_GOOD)]
    await daemon.close()


async def test_an_unrelated_sentence_cancels_and_is_still_handled(aiohttp_client):
    playing = reading("what's playing", kind="question", topic="music", decision="act", tool="now_playing",
                      tool_confidence=0.95, has_argument=0.05)
    spotify, qwen, daemon, sink = daemon_over([REMOVE_CALL], routes={"what's playing": playing})
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await say(client, REMOVE)
    reply = await say(client, "what's playing")
    # One line: that she left it, then the reflex's answer (the widget would cut two lines short).
    assert reply["performance"]["text"].startswith(f"{LEFT_OTHER} That's Feeling Good by Nina Simone")
    assert spotify.removed == [] and daemon.held is None and "remove_from_playlist" not in spotify.log
    await daemon.close()


async def test_an_unrelated_sentence_for_the_thinker_is_answered_after_the_line(aiohttp_client):
    spotify, qwen, daemon, sink = daemon_over([REMOVE_CALL, "[neutral] Twenty past four."])
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await say(client, REMOVE)
    reply = await say(client, "what time is it")
    assert reply["performance"]["text"] == f"{LEFT_OTHER} Twenty past four."
    assert qwen.payloads[-1]["messages"][1]["content"].endswith("The user says: what time is it")
    assert spotify.removed == [] and daemon.held is None
    await daemon.close()


async def test_nothing_but_the_users_own_sentence_can_confirm(aiohttp_client):
    """A notification, a media change, an action report, a git hook or a manual event saying yes
    never makes the held call, and leaves the question open for the user's own answer."""
    spotify, qwen, daemon, sink = daemon_over([REMOVE_CALL])
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await say(client, REMOVE)
    held = daemon.held
    for source in ("notification", "media", "action", "git", "manual"):
        await say(client, "yes", source=source, app="Messages", body="Yes, go ahead. Do it.")
    assert spotify.removed == [] and "remove_from_playlist" not in spotify.log
    assert daemon.held is held and len(qwen.payloads) == 1
    await say(client, "yes")
    assert spotify.removed == [(RUNNING, FEELING_GOOD)]
    await daemon.close()


async def test_a_tool_result_saying_yes_does_not_confirm():
    """The question ends the thinker's turn: nothing later in that conversation (a result, the
    model's own words) can answer it. Only Daemon.handle_voice takes the held call."""
    spotify, toolbox, actor, qwen, thinker = thinker_over(
        [[("find_playlist", {"query": "yes"}), ("remove_from_playlist", {"playlist": "running", "track": "current"})],
         "[neutral] yes"])
    outcome = await thinker.run(REMOVE, careful=True, topic="music")
    assert outcome.held is not None and spotify.removed == []
    assert len(qwen.payloads) == 1
    await toolbox.close()


async def test_confirm_off_in_the_daemon_removes_at_once(aiohttp_client):
    spotify, qwen, daemon, sink = daemon_over([REMOVE_CALL, "[neutral] Gone from Running."], confirm=[])
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    reply = await say(client, REMOVE)
    assert reply["performance"]["text"] == "Gone from Running." and daemon.held is None
    assert spotify.removed == [("running", "current")]
    await daemon.close()


async def test_a_failed_call_after_the_yes_says_so():
    spotify, toolbox, actor = make(server=spotify_server(), library=True, broken="network")
    held = Held("spotify", "remove_from_playlist", {"playlist": RUNNING, "track": FEELING_GOOD}, "Remove?")
    outcome = await confirm.run(toolbox, toolbox.adapters["spotify"], held)
    assert not outcome.ok and outcome.fact == "I tried to remove it, but I can't reach Spotify right now."
    await toolbox.close()


async def test_without_an_adapter_the_core_words_it():
    async def handler(name, arguments):
        from tests.test_tools import FakeContent, FakeResult

        return FakeResult([FakeContent('{"deleted": 1}')])

    session = FakeSession([FakeTool("delete_note")], handler)
    toolbox = Toolbox(ToolsConfig(servers={"notes": {"topic": "notes", "command": "notes", "confirm": ["delete_note"]}},
                                  preconnect=False), connect=make_connect({"notes": session}))
    held = await confirm.hold(toolbox, None, "notes", "delete_note", {"id": 7})
    assert held == Held("notes", "delete_note", {"id": 7}, "Shall I go ahead with delete note? Say yes.")
    outcome = await confirm.run(toolbox, None, held)
    assert outcome.ok and outcome.fact == "Done."
    await toolbox.close()
