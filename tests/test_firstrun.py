"""The first-run privacy note: logged while unseen, shown in her bubble once, then never again."""

import asyncio
import logging

import pytest

from strawberry_crab import firstrun, paths
from strawberry_crab.config import Config
from strawberry_crab.daemon import Daemon
from strawberry_crab.events import CannedReactor
from strawberry_crab.server import create_app
from tests.bus import BUS_SECRET, trusted


@pytest.fixture
def unseen():
    paths.privacy_notice_marker().unlink()
    assert firstrun.pending()


@pytest.fixture
async def client(aiohttp_client):
    config = Config()
    for part in (config.brain, config.speech, config.voice, config.gate, config.tools, config.thinker):
        part.enabled = False
    return await aiohttp_client(create_app(Daemon(reactor=CannedReactor(), config=config)))


async def test_the_first_widget_hears_the_note_once(unseen, client):
    first = await client.ws_connect("/ws")
    await first.send_json(trusted({"type": "hello", "client": "strawberry-widget", "version": "dev"}))
    note = await first.receive_json(timeout=2)
    assert note["state"] == "talking"
    assert "never the message" in note["text"] and "Settings file" in note["text"]
    assert not firstrun.pending() and paths.privacy_notice_marker().is_file()
    await first.close()

    again = await client.ws_connect("/ws")
    await again.send_json(trusted({"type": "hello", "client": "strawberry-widget", "version": "dev"}))
    await again.send_json({"type": "ping"})
    assert await again.receive_json(timeout=2) == {"type": "pong"}   # no second note before it
    await again.close()


async def test_a_refused_widget_does_not_use_up_the_note(unseen, client):
    from strawberry_crab import __version__

    ws = await client.ws_connect("/ws")
    await ws.send_json({"type": "hello", "client": "strawberry-widget",
                        "version": f"{int(__version__.split('.')[0]) + 1}.0.0"})
    await ws.receive(timeout=2)
    await asyncio.sleep(0.05)
    assert ws.closed and firstrun.pending()


def test_the_log_line_says_bodies_are_off_and_where_to_change_them(unseen):
    line = firstrun.log_text(Config())
    assert "off by default" in line and "[notifications] body" in line
    assert str(paths.config_file()) in line


def test_the_note_follows_the_config_when_bodies_are_on():
    config = Config()
    config.notifications.body_apps = {"Slack": "glance"}
    assert "are read" in firstrun.log_text(config)
    assert "never the message" not in firstrun.bubble_text(config)


def test_the_daemon_logs_the_note_at_start_only_while_unseen(unseen, monkeypatch, caplog):
    from strawberry_crab import server

    async def serve(*args, **kwargs):
        return None

    monkeypatch.setattr(server, "serve", serve)
    config = Config()
    with caplog.at_level(logging.INFO, logger="strawberryd.http"):
        server.run(config)
    assert "privacy: notification bodies are off by default" in caplog.text

    firstrun.mark_shown()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="strawberryd.http"):
        server.run(config)
    assert "privacy:" not in caplog.text


ORBS = {"type": "hello", "client": "orbs", "version": "dev", "protocol": 2,
        "body": {"id": "orbs", "name": "Orbs"}, "capabilities": {"phases": ["run"]}}
CRAB = {"type": "hello", "client": "strawberry-widget", "version": "dev", "protocol": 2,
        "body": {"id": "crab", "name": "Strawberry"},
        "capabilities": {"phases": ["run"], "speech": {"bubble": True}}, "secret": BUS_SECRET}


def test_a_v2_body_shows_text_only_when_its_hello_says_so():
    from strawberry_crab.hub import body_from_hello

    assert body_from_hello({"type": "hello", "version": "dev"}).shows_text           # v1: always
    assert body_from_hello(CRAB).shows_text
    assert not body_from_hello(ORBS).shows_text
    assert not body_from_hello(ORBS | {"capabilities": {"speech": {"bubble": "yes"}}}).shows_text


async def test_a_body_that_shows_no_text_neither_gets_nor_uses_up_the_note(unseen, client):
    orbs = await client.ws_connect("/ws")
    await orbs.send_json(ORBS)
    assert (await orbs.receive_json(timeout=2))["type"] == "welcome"
    await asyncio.sleep(0.2)
    assert firstrun.pending()

    crab = await client.ws_connect("/ws")
    await crab.send_json(CRAB)
    assert (await crab.receive_json(timeout=2))["type"] == "welcome"
    note = await crab.receive_json(timeout=2)
    assert note["state"] == "talking" and "never the message" in note["text"]
    assert not firstrun.pending()

    # The note went to the crab alone: the orbs' next message is the answer to their ping.
    await orbs.send_json({"type": "ping"})
    assert await orbs.receive_json(timeout=2) == {"type": "pong"}
    await orbs.close()
    await crab.close()


async def test_a_text_body_without_the_secret_neither_gets_nor_uses_up_the_note(unseen, client):
    """It would get the note without its words (hub.shape), so it gets none: the next trusted body does."""
    stranger = await client.ws_connect("/ws")
    await stranger.send_json({k: v for k, v in CRAB.items() if k != "secret"})
    assert (await stranger.receive_json(timeout=2))["type"] == "welcome"
    await asyncio.sleep(0.2)
    await stranger.send_json({"type": "ping"})
    assert await stranger.receive_json(timeout=2) == {"type": "pong"}
    assert firstrun.pending()
    crab = await client.ws_connect("/ws")
    await crab.send_json(CRAB)
    assert (await crab.receive_json(timeout=2))["type"] == "welcome"
    assert "never the message" in (await crab.receive_json(timeout=2))["text"]
    await stranger.close()
    await crab.close()


async def test_the_note_is_not_marked_shown_when_it_reached_no_one(unseen):
    from strawberry_crab import server

    config = Config()
    for part in (config.brain, config.speech, config.voice, config.gate, config.tools, config.thinker):
        part.enabled = False
    daemon = Daemon(reactor=CannedReactor(), config=config)
    gone = object()     # a socket the hub no longer has (it closed before the note went out)
    daemon.privacy_note = "sending"
    await server._say_first_run_notice(daemon, gone)
    assert firstrun.pending() and daemon.privacy_note == ""


async def test_two_text_bodies_saying_hello_together_get_one_note(unseen, client):
    first = await client.ws_connect("/ws")
    second = await client.ws_connect("/ws")
    await first.send_json(CRAB)
    await second.send_json(CRAB)
    got = []
    for ws in (first, second):
        assert (await ws.receive_json(timeout=2))["type"] == "welcome"
        await asyncio.sleep(0.3)
        await ws.send_json({"type": "ping"})
        while (message := await ws.receive_json(timeout=2)) != {"type": "pong"}:
            got.append(message)
    assert len([m for m in got if "text" in m]) == 1 and not firstrun.pending()
    await first.close()
    await second.close()
