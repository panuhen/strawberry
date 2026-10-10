""""You can talk again" (runs.is_talk_again, Daemon._talk_again, WIRING.md §26): a whole sentence of the user's own
ends the quiet that shh, the tray's Quiet row or her own menu put her in. A sentence that only has the words in it
does nothing, and neither does any text that is not the user's sentence (a notification, the timeline)."""

from __future__ import annotations

import asyncio

import pytest

from strawberry_crab import paths, persona
from strawberry_crab.daemon import Daemon
from strawberry_crab.events import Event
from strawberry_crab.runs import is_stop, is_talk_again
from strawberry_crab.server import create_app
from tests.bus import add_trusted
from tests.test_gestures import gesture_daemon
from tests.test_runs import JAZZ, SlowQwen, kinds, say, settle, thinker_daemon, until
from tests.test_voice import Sink

LINES = persona.shipped().lines["talk_again"]
QUIET_0 = {"command": "quiet", "value": 0}


@pytest.mark.parametrize("text, talk", [
    ("you can talk again", True), ("You can talk again!", True), ("Okay, you can speak again, Strawberry.", True),
    ("unmute", True), ("Unmute, please.", True), ("talk to me", True), ("you can talk now", True),
    ("hey, talk to me again", True), ("you’re fine, you can talk", False),
    ("I want to talk to me mum about it", False), ("don't talk to me", False), ("talk to me about the weather", False),
    ("unmute the music", False), ("can you talk again", False), ("stop", False), ("please", False), ("", False)])
def test_a_whole_sentence_lets_her_talk_again(text, talk):
    assert is_talk_again(text) is talk


def test_stop_reads_as_before():
    assert is_stop("never mind, Strawberry") and not is_stop("you can talk again") and not is_stop("stop the music")


def test_the_fallback_line_is_the_shipped_one():
    assert Daemon.TALK_AGAIN == LINES[0]


async def quiet_daemon(aiohttp_client):
    daemon, _ = gesture_daemon(shh_minutes=10.0)
    client = await aiohttp_client(create_app(daemon))
    sink = add_trusted(daemon.hub, Sink())
    assert (await daemon.gestures.gesture({"name": "shh", "phase": "done"}))["action"] == "quiet"
    await asyncio.sleep(0.05)
    assert daemon.quiet_now() and {"command": "quiet", "value": 600} in sink.got
    return client, daemon, sink


async def test_you_can_talk_again_ends_the_shh_and_she_says_so(aiohttp_client):
    client, daemon, sink = await quiet_daemon(aiohttp_client)
    performance, sent = await daemon.handle_voice(Event(source="voice", title="Okay, you can talk again.", spoken=True))
    assert QUIET_0 in sink.got and daemon.gestures.quiet_left() == 0 and not daemon.quiet_now()
    assert performance.text in LINES and sent == 1
    turn = daemon.ledger.turns[-1]
    assert turn.did == "her voice back on (quiet ended)" and not turn.foreign
    # Typed the same way (the type box): the user's own sentence too.
    daemon.gestures.quiet_set(300)
    reply = await say(client, "unmute")
    assert reply["performance"]["text"] in LINES and daemon.gestures.quiet_left() == 0
    await daemon.close()


async def test_a_sentence_with_the_words_in_it_or_text_that_is_not_hers_does_nothing(aiohttp_client):
    client, daemon, sink = await quiet_daemon(aiohttp_client)
    await daemon.handle_voice(Event(source="voice", title="I want to talk to me mum about it", spoken=True))
    await daemon.handle_event(Event(source="notification", app="Chat", title="you can talk again",
                                    body="you can talk again"))
    await daemon.handle_event(Event(source="media", app="Player", title="Talk to Me", body="unmute"))
    await settle()
    assert QUIET_0 not in sink.got and daemon.gestures.quiet_left() > 0
    assert all(m.get("text") not in LINES for m in sink.got)
    await daemon.close()


async def test_while_she_is_not_quiet_the_sentence_is_an_ordinary_one(aiohttp_client):
    daemon, _ = gesture_daemon()
    sink = add_trusted(daemon.hub, Sink())
    assert not daemon.quiet_now()
    performance, _ = await daemon.handle_voice(Event(source="voice", title="talk to me", spoken=True))
    assert QUIET_0 not in sink.got and performance.text not in LINES
    await daemon.close()


async def test_her_own_menus_quiet_counts_too(aiohttp_client):
    daemon, _ = gesture_daemon()
    sink = add_trusted(daemon.hub, Sink())
    prefs = paths.widget_prefs_file()
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text("[audio]\n\nquiet_until=4102444800.0\n", encoding="utf-8")     # far ahead
    assert daemon.quiet_now()
    performance, _ = await daemon.handle_voice(Event(source="voice", title="you can speak again", spoken=True))
    assert QUIET_0 in sink.got and performance.text in LINES
    prefs.write_text("[audio]\n\nquiet_until=1.0\n", encoding="utf-8")
    assert not daemon.quiet_now()
    prefs.write_text("[audio]\n\nquiet_until=\"soon\"\n", encoding="utf-8")
    assert not daemon.quiet_now()
    await daemon.close()


async def test_it_neither_stops_nor_supersedes_the_run_she_is_on(aiohttp_client):
    client, daemon, spotify, qwen, sink, v1 = await thinker_daemon(aiohttp_client, [])
    daemon.thinker.chat = SlowQwen(["[happy] here is your jazz"], seconds=0.5)
    daemon.gestures.quiet_set(600)
    pending = asyncio.ensure_future(say(client, JAZZ))
    await until(lambda: "thinking" in kinds(sink))
    talk = await asyncio.wait_for(say(client, "you can talk again"), 3.0)
    first = await asyncio.wait_for(pending, 1.0)
    assert first["performance"]["text"] == "here is your jazz"                  # not superseded
    assert talk["performance"]["text"] in LINES and QUIET_0 in v1.got
    assert not [m for m in sink.got if m.get("type") == "run.cancelled"]
    await daemon.close()
