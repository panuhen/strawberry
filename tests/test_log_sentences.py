"""`[daemon] log_sentences` (WIRING.md §15, logtext.py): off by default, the user's sentences are
never written to the journal, only their length; on, they are, as before."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from strawberry_crab import logtext
from strawberry_crab.actions import Actor
from strawberry_crab.config import Config, ConfigError, VoiceConfig, default_toml, load
from strawberry_crab.daemon import Daemon
from strawberry_crab.events import CannedReactor, Event
from strawberry_crab.server import create_app
from strawberry_crab.systemone import Gate
from strawberry_crab.voice import Listener
from tests.bus import connect
from tests.test_outcomes import FakeMpris
from tests.test_systemone import FakeEmbedder
from tests.test_thinker import make, plain_config, reading
from tests.test_tools import FakeContent, FakeResult
from tests.test_voice import Sink, fake_recording

CANARY = "wombat-canary-3c9d"


@pytest.fixture(autouse=True)
def reset_the_switch():
    yield
    logtext.configure(False)


def test_off_by_default_in_the_template_and_config(tmp_path, aiohttp_client):
    assert Config().daemon.log_sentences is False
    assert "log_sentences = false" in default_toml()
    assert Config().to_dict()["daemon"]["log_sentences"] is False     # what GET /config shows
    path = tmp_path / "config.toml"
    path.write_text("[daemon]\nlog_sentences = true\n")
    assert load(path, env={}).daemon.log_sentences is True
    path.write_text('[daemon]\nlog_sentences = "yes"\n')
    with pytest.raises(ConfigError, match="daemon.log_sentences must be bool"):
        load(path, env={})


async def test_get_config_shows_it(aiohttp_client):
    config = plain_config()
    config.daemon.log_sentences = True
    client = await aiohttp_client(create_app(Daemon(reactor=CannedReactor(), config=config)))
    assert (await (await client.get("/config")).json())["daemon"]["log_sentences"] is True


def test_the_helpers():
    logtext.configure(False)
    assert logtext.sentence("skip this song") == "<sentence, 14 chars>"
    assert json.loads(logtext.arguments({"query": "daft punk", "volume": 60, "shuffle": True, "uris": ["a"]})) == \
        {"query": "<9 chars>", "volume": 60, "shuffle": True, "uris": "<list>"}
    with logtext.hearing("Play Daft Punk"):
        assert logtext.line("You said: play daft punk") == "You said: <sentence, 14 chars>"
    assert logtext.line("You said: play daft punk") == "You said: play daft punk"   # outside the handling
    logtext.configure(True)
    assert logtext.sentence("skip this song") == "'skip this song'"
    assert logtext.arguments({"query": "daft punk"}) == '{"query": "daft punk"}'
    with logtext.hearing("play daft punk"):
        assert logtext.line("You said: play daft punk") == "You said: play daft punk"


class Reading(Gate):
    """The real gate (it logs as it always does), with the reading scripted for some sentences."""

    def __init__(self, config, scripted) -> None:
        super().__init__(config, embedder=FakeEmbedder())
        self.scripted = scripted

    async def route(self, text):
        real = await super().route(text)
        return self.scripted.get(text, real)


def canary_daemon(config: Config, spoken: str):
    """The thinker on a scripted Qwen that searches with the canary, the Spotify fake behind it,
    the reflexes on a fake MPRIS player, and whisper hearing `spoken`."""

    async def handler(name, arguments):
        return FakeResult([FakeContent(json.dumps({"tracks": [{"name": "Some Track"}]}))])

    config.gate.query_prefix = config.gate.document_prefix = ""
    config.voice = VoiceConfig(enabled=True, hotwords=False)
    spotify, toolbox, qwen, thinker = make(
        ["[neutral] Hi.", [("search", {"query": f"{CANARY} songs"})], "[neutral] Found a few.",
         "[neutral] Done.", "[neutral] Not sure."], handler=handler)
    skip, other = f"skip this song {CANARY}", f"turn it {CANARY} please"
    gate = Reading(config.gate, {
        skip: reading(skip, kind="request", topic="music", decision="act", tool="skip", tool_confidence=0.95,
                      has_argument=0.02),
        other: reading(other, kind="request", topic="music", decision="act", tool="other", tool_confidence=0.9),
    })
    listener = Listener(config.voice, transcriber_factory=lambda cfg: (lambda audio, hotwords="": spoken),
                        recorder=lambda *args, **kwargs: fake_recording(), source_picker=lambda preferred: "alsa_input.test")
    actor = Actor(config.actions, toolbox, reflexes={}, mpris=FakeMpris())
    daemon = Daemon(reactor=CannedReactor(), config=config, gate=gate, toolbox=toolbox, thinker=thinker,
                    actor=actor, listener=listener)
    daemon.hub.add(Sink())  # type: ignore[arg-type]
    return daemon, toolbox, qwen, (skip, other)


async def turns(daemon: Daemon, n: int) -> None:
    for _ in range(200):
        if len(daemon.ledger.to_list()) >= n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{len(daemon.ledger.to_list())} turns, wanted {n}")


@pytest.mark.parametrize("log_sentences", [False, True])
async def test_no_sentence_in_any_log_record(aiohttp_client, caplog, log_sentences):
    """The canary, like the notification body's (test_privacy.py): typed into the widget, posted as a
    voice event, heard by whisper; through the real gate, the reflex tier, the thinker and a tool call
    with the canary in its arguments, every logger at DEBUG. Off, it is in no log record; on, it is."""
    config = plain_config()
    config.daemon.log_sentences = log_sentences
    daemon, toolbox, qwen, (skip, other) = canary_daemon(config, spoken=f"what is {CANARY}")
    client = await aiohttp_client(create_app(daemon))
    with caplog.at_level(logging.DEBUG):
        await daemon.listener.loaded()
        ws = await connect(client)
        await ws.send_str(json.dumps({"type": "heard", "text": f"hello there {CANARY}"}))      # typed
        await turns(daemon, 1)
        await client.post("/event", json={"source": "voice", "title": f"find me {CANARY} songs"})  # a tool call
        await client.post("/event", json={"source": "voice", "title": skip})                     # a reflex
        await client.post("/event", json={"source": "voice", "title": other})                    # not a bare one
        assert (await client.post("/listen")).status == 200                                      # spoken
        await daemon.listen_task
        await ws.close()
        await daemon.close()
        await toolbox.close()
    assert len(daemon.ledger.to_list()) == 5 and daemon.actor.acted == 1 and daemon.actor.deferred >= 1
    assert any(m["role"] == "tool" for payload in qwen.payloads for m in payload["messages"])   # the search ran
    messages = [r.getMessage() for r in caplog.records]
    leaked = [m for m in messages if CANARY in m]
    if log_sentences:
        assert leaked and any("widget typed: 'hello there" in m for m in messages)
        assert any("voice: heard 'what is" in m for m in messages)
    else:
        assert leaked == [], leaked
        for where in ("widget typed: <sentence, 30 chars>", "voice: heard <sentence, 26 chars>", "gate: <sentence,",
                      "actions: <sentence,", "thinker: <sentence,", "spotify.search(query)"):
            assert any(where in m for m in messages), where


async def test_the_canned_echo_of_a_sentence_is_not_logged(caplog):
    """Gemma off (or timing out): the canned line for a voice event is "You said: …"; her line is
    logged, the sentence in it is not."""
    config = plain_config()
    config.thinker.enabled = config.gate.enabled = config.tools.enabled = False
    config.actions.mpris = False
    daemon = Daemon(reactor=CannedReactor(), config=config)
    daemon.hub.add(Sink())  # type: ignore[arg-type]
    with caplog.at_level(logging.DEBUG):
        performance, _ = await daemon.handle_event(Event(source="voice", title=f"hi {CANARY}"))
    assert performance.text == f"You said: hi {CANARY}"                  # the widget still gets it
    messages = [r.getMessage() for r in caplog.records]
    assert not [m for m in messages if CANARY in m]
    assert any("You said: <sentence, 21 chars>" in m for m in messages)
