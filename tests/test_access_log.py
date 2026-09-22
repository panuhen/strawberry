"""The access log (server.QuietAccessLogger): the polling routes stay out of the journal, every
other request is logged at INFO, and no request body ever reaches a log line."""

from __future__ import annotations

import logging

import aiohttp
from aiohttp import web

from strawberry_crab.config import Config
from strawberry_crab.daemon import Daemon
from strawberry_crab.events import CannedReactor
from strawberry_crab.server import QuietAccessLogger, create_app


def quiet_daemon() -> Daemon:
    config = Config()
    config.brain.enabled = config.speech.enabled = config.voice.enabled = config.gate.enabled = False
    config.tools.enabled = config.thinker.enabled = config.actions.mpris = False
    return Daemon(reactor=CannedReactor(), config=config)


async def test_polling_is_not_logged_and_the_rest_is_without_bodies(caplog):
    # The runner server.serve builds: web.AppRunner(..., access_log_class=QuietAccessLogger).
    runner = web.AppRunner(create_app(quiet_daemon()), access_log_class=QuietAccessLogger)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    base = f"http://127.0.0.1:{port}"
    secret = "Your verification code is 482913"
    tempo = {"bpm": 124.0, "period_s": 0.48, "confidence": 0.8, "next_beat": 1.0, "evenness": 0.9,
             "low_ratio": 0.5, "density": 4.0, "loudness_db": -20.0}
    caplog.set_level(logging.INFO, logger="aiohttp.access")
    try:
        async with aiohttp.ClientSession() as session:
            for _ in range(3):
                assert (await session.get(base + "/health")).status == 200
            assert (await session.post(base + "/tempo", json=tempo)).status == 200
            assert (await session.post(base + "/tempo", json={"bpm": "fast"})).status == 400   # a failing poll
            assert (await session.post(base + "/event", json={"source": "notification", "app": "Bank",
                                                               "title": "Bank", "body": secret})).status == 200
            assert (await session.get(base + "/config")).status == 200
    finally:
        await runner.cleanup()
    lines = [r.getMessage() for r in caplog.records if r.name == "aiohttp.access"]
    assert all(r.levelno == logging.INFO for r in caplog.records if r.name == "aiohttp.access")
    assert not any("GET /health" in line for line in lines), lines
    tempo_lines = [line for line in lines if "POST /tempo" in line]
    assert len(tempo_lines) == 1 and '" 400 ' in tempo_lines[0]   # only the one that failed
    assert any("POST /event" in line for line in lines) and any("GET /config" in line for line in lines)
    assert len(lines) == 3
    everything = "\n".join(r.getMessage() for r in caplog.records)
    assert "482913" not in everything and "verification" not in everything   # no body, in any logger
