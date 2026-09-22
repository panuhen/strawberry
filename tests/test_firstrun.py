"""The first-run privacy note: logged while unseen, shown in her bubble once, then never again."""

import asyncio
import logging

import pytest

from strawberry_crab import firstrun, paths
from strawberry_crab.config import Config
from strawberry_crab.daemon import Daemon
from strawberry_crab.events import CannedReactor
from strawberry_crab.server import create_app


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
    await first.send_json({"type": "hello", "client": "strawberry-widget", "version": "dev"})
    note = await first.receive_json(timeout=2)
    assert note["state"] == "talking"
    assert "never the message" in note["text"] and "Settings file" in note["text"]
    assert not firstrun.pending() and paths.privacy_notice_marker().is_file()
    await first.close()

    again = await client.ws_connect("/ws")
    await again.send_json({"type": "hello", "client": "strawberry-widget", "version": "dev"})
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

    monkeypatch.setattr(server.web, "run_app", lambda *a, **k: None)
    config = Config()
    with caplog.at_level(logging.INFO, logger="strawberryd.http"):
        server.run(config)
    assert "privacy: notification bodies are off by default" in caplog.text

    firstrun.mark_shown()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="strawberryd.http"):
        server.run(config)
    assert "privacy:" not in caplog.text
