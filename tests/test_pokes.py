"""The widget's "Talk when poked" lines (pokes.py, server._poked; WIRING.md §13 Touch, PROTOCOL.md §6)."""

import asyncio
import json
import logging

import pytest

from strawberry_crab import pokes
from strawberry_crab.daemon import Daemon
from strawberry_crab.pokes import Pokes
from strawberry_crab.server import create_app


@pytest.fixture
def daemon():
    from strawberry_crab.config import Config
    from strawberry_crab.events import CannedReactor

    config = Config()
    config.brain.enabled = False
    config.speech.enabled = False
    config.voice.enabled = False
    config.gate.enabled = False
    config.tools.enabled = False
    config.thinker.enabled = False
    return Daemon(reactor=CannedReactor(), config=config)


@pytest.fixture
async def client(aiohttp_client, daemon):
    return await aiohttp_client(create_app(daemon))


def test_parse_takes_known_zones_and_levels_only():
    assert pokes.parse({"zone": "shell", "level": 1}) == ("shell", 1)
    assert pokes.parse({"zone": "near", "level": 2}) == ("near", 2)
    for bad in ({"zone": "tail", "level": 1}, {"zone": "shell", "level": 3}, {"zone": "shell", "level": True},
                {"zone": "shell", "level": "1"}, {"zone": "shell"}, {}):
        assert pokes.parse(bad) is None


def test_lines_take_turns_and_annoyed_has_its_own():
    p = Pokes(clock=lambda: 0.0)
    said = [p.line("belly", 1)[0] for _ in range(4)]
    assert said[:3] == list(pokes.LINES["belly"]) and said[3] == said[0]    # in turn, none twice running
    text, emotion = p.line("eye", 2)
    assert text in pokes.LINES["annoyed"] and emotion == "angry"
    assert p.line("shell", 1)[1] == "happy"
    for zone in pokes.ZONES:
        assert pokes.LINES[zone] and zone in pokes.EMOTIONS


def test_cooldown_and_busy_decline():
    now = [100.0]
    p = Pokes(clock=lambda: now[0], cooldown_s=20.0)
    assert p.allowed(busy=False)
    p.line("shell", 1)
    now[0] += 5.0
    assert not p.allowed(busy=False)       # said one lately
    now[0] += 20.0
    assert not p.allowed(busy=True)        # busy wins over the cooldown
    assert p.allowed(busy=False)
    assert p.said == 1 and p.declined == 2


async def test_a_poke_gets_a_line_then_the_cooldown_holds(client, daemon, caplog):
    caplog.set_level(logging.INFO)
    ws = await client.ws_connect("/ws")
    await ws.send_json({"type": "poked", "zone": "belly", "level": 1})
    reply = await ws.receive_json(timeout=2)
    assert reply["state"] == "talking" and reply["text"] in pokes.LINES["belly"] and reply["emotion"] == "happy"
    assert "anim" not in reply and "reaction" not in reply      # the widget has already reacted
    await ws.send_json({"type": "poked", "zone": "eye", "level": 2})
    await ws.send_json({"type": "ping"})
    assert await ws.receive_json(timeout=2) == {"type": "pong"}  # no second line within the cooldown
    assert daemon.pokes.said == 1 and daemon.pokes.declined == 1
    assert daemon.ledger.to_list() == []                         # not a sentence; nothing remembered
    await ws.close()


async def test_no_line_while_she_is_busy(client, daemon):
    ws = await client.ws_connect("/ws")
    run = daemon.runs.start("typed")                             # a run going on: the step chip shows
    await ws.send_json({"type": "poked", "zone": "shell", "level": 1})
    await ws.send_json({"type": "ping"})
    assert await ws.receive_json(timeout=2) == {"type": "pong"}
    daemon.runs.finish(run)
    assert daemon.runs.busy() is None
    await client.post("/perform", json={"state": "thinking"})    # a pipeline state: not now either
    assert (await ws.receive_json(timeout=2))["state"] == "thinking"
    await ws.send_json({"type": "poked", "zone": "shell", "level": 1})
    await ws.send_json({"type": "ping"})
    assert await ws.receive_json(timeout=2) == {"type": "pong"}
    assert daemon.pokes.said == 0 and daemon.pokes.declined == 2
    await ws.close()


async def test_unknown_pokes_are_ignored(client, daemon):
    ws = await client.ws_connect("/ws")
    for bad in ({"type": "poked"}, {"type": "poked", "zone": "tail", "level": 1},
                {"type": "poked", "zone": "shell", "level": 9}):
        await ws.send_str(json.dumps(bad))
    await ws.send_json({"type": "ping"})
    assert await ws.receive_json(timeout=2) == {"type": "pong"}
    assert daemon.pokes.said == 0
    await asyncio.sleep(0)
    await ws.close()
