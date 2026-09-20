import asyncio
import json

import pytest
from aiohttp import web

from strawberryd.brain import OllamaReactor, describe, tidy
from strawberryd.config import BrainConfig
from strawberryd.events import CannedReactor, Event


def fake_ollama(reply=None, *, delay=0.0, raw=None, status=200):
    """A stand-in Ollama: /api/generate loads, /api/chat answers with `reply` (or `raw`)."""
    calls = {"generate": [], "chat": []}

    async def generate(request):
        calls["generate"].append(await request.json())
        return web.json_response({"model": "fake", "done": True})

    async def chat(request):
        body = await request.json()
        calls["chat"].append(body)
        if delay:
            await asyncio.sleep(delay)
        if status != 200:
            return web.Response(status=status, text="nope")
        content = raw if raw is not None else json.dumps(reply)
        return web.json_response({"message": {"role": "assistant", "content": content}, "done": True})

    app = web.Application()
    app.add_routes([web.post("/api/generate", generate), web.post("/api/chat", chat)])
    return app, calls


async def make_reactor(aiohttp_server, app, **overrides):
    server = await aiohttp_server(app)
    brain = BrainConfig(ollama_url=str(server.make_url("")).rstrip("/"), timeout_s=overrides.pop("timeout_s", 1.0), **overrides)
    reactor = OllamaReactor(brain, fallback=CannedReactor())
    await reactor.start()
    return reactor


GIT = Event(source="git", app="post-commit", title="strawberry", body="Fix reconnect")


async def test_warm_up_loads_the_model_with_keep_alive(aiohttp_server):
    app, calls = fake_ollama({"line": "x", "emotion": "neutral"})
    reactor = await make_reactor(aiohttp_server, app, keep_alive=-1)
    # Ollama wants a number of seconds (-1 = forever) or a duration string; "-1" as a string is a 400.
    assert calls["generate"] == [{"model": "gemma3:1b", "keep_alive": -1}]
    assert reactor.loaded
    await reactor.close()


async def test_model_line_becomes_a_talking_performance(aiohttp_server):
    app, calls = fake_ollama({"line": "A fix! Reconnects all round.", "emotion": "happy"})
    reactor = await make_reactor(aiohttp_server, app)
    performance = await reactor.react(GIT)
    assert performance.state == "talking"
    assert performance.text == "A fix! Reconnects all round."
    assert performance.emotion == "happy"
    assert performance.anim == "notify_perk"
    assert reactor.stats()["fallbacks"] == 0
    assert reactor.stats()["last_latency_s"] is not None
    await reactor.close()


async def test_prompt_carries_persona_examples_schema_and_no_thinking(aiohttp_server):
    app, calls = fake_ollama({"line": "x", "emotion": "neutral"})
    reactor = await make_reactor(aiohttp_server, app)
    await reactor.react(GIT)
    body = calls["chat"][0]
    assert body["think"] is False
    assert body["format"]["properties"]["emotion"]["enum"] == ["neutral", "happy", "alert", "angry"]
    messages = body["messages"]
    assert messages[0]["role"] == "system" and "Strawberry" in messages[0]["content"]
    assert len(messages) == 1 + 2 * len(reactor.brain.examples) + 1
    assert messages[-1] == {"role": "user", "content": describe(GIT)}
    await reactor.close()


async def test_slow_model_falls_back_to_canned_line(aiohttp_server):
    app, _ = fake_ollama({"line": "too late", "emotion": "happy"}, delay=0.6)
    reactor = await make_reactor(aiohttp_server, app, timeout_s=0.2)
    performance = await reactor.react(GIT)
    assert performance.text == "Commit in strawberry: Fix reconnect"
    assert reactor.stats()["fallbacks"] == 1
    await reactor.close()


@pytest.mark.parametrize("raw", ["not json at all", '{"line": 5, "emotion": "happy"}', '{"line": "x", "emotion": "smug"}', '{"line": "   ", "emotion": "happy"}'])
async def test_nonsense_falls_back_to_canned_line(aiohttp_server, raw):
    app, _ = fake_ollama(raw=raw)
    reactor = await make_reactor(aiohttp_server, app)
    performance = await reactor.react(GIT)
    assert performance.text == "Commit in strawberry: Fix reconnect"
    assert reactor.stats()["fallbacks"] == 1
    await reactor.close()


async def test_http_error_falls_back(aiohttp_server):
    app, _ = fake_ollama(status=500)
    reactor = await make_reactor(aiohttp_server, app)
    performance = await reactor.react(GIT)
    assert performance.text.startswith("Commit in strawberry")
    await reactor.close()


async def test_unreachable_ollama_still_reacts():
    brain = BrainConfig(ollama_url="http://127.0.0.1:9", timeout_s=0.3)
    reactor = OllamaReactor(brain, fallback=CannedReactor())
    await reactor.start()  # warm-up fails, logged, not raised
    assert not reactor.loaded
    performance = await reactor.react(GIT)
    assert performance.text == "Commit in strawberry: Fix reconnect"
    await reactor.close()


def test_tidy_collapses_and_caps():
    assert tidy('  "Hello,\n  crab!"  ', 15) == "Hello, crab!"
    assert tidy("**Good.**", 15) == "Good."
    assert tidy("_Claws_ up, `friend`!", 15) == "Claws up, friend!"
    long = " ".join(f"w{i}" for i in range(30))
    capped = tidy(long, 15)
    assert capped.endswith("…") and len(capped.split()) == 15


def test_describe_matches_the_example_shape():
    assert describe(Event(source="notification", app="Power", title="Low", body="5%", urgency="critical")) == (
        "source: notification\napp: Power\ntitle: Low\nbody: 5%\nurgency: critical"
    )
    assert describe(Event(source="media", app="Spotify", title="X — Y")) == "source: media\napp: Spotify\ntitle: X — Y"
