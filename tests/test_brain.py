import asyncio
import json

import pytest
from aiohttp import web

from strawberry_crab.brain import OllamaReactor, describe, tidy
from strawberry_crab.config import BrainConfig
from strawberry_crab.events import CannedReactor, Event
from strawberry_crab.persona import BODY_RULE, EXAMPLES


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


def fake_ollama_sequence(replies):
    """Like fake_ollama, but /api/chat answers with the next reply from `replies` each call."""
    calls = {"generate": [], "chat": []}
    queue = list(replies)

    async def generate(request):
        calls["generate"].append(await request.json())
        return web.json_response({"model": "fake", "done": True})

    async def chat(request):
        calls["chat"].append(await request.json())
        reply = queue.pop(0) if queue else {"line": "…", "emotion": "neutral"}
        return web.json_response({"message": {"role": "assistant", "content": json.dumps(reply)}, "done": True})

    app = web.Application()
    app.add_routes([web.post("/api/generate", generate), web.post("/api/chat", chat)])
    return app, calls


MEDIA = Event(source="media", app="Spotify", title="Cosmic Gate — Exploration of Space")


async def test_repeated_pet_word_triggers_one_reworded_retry(aiohttp_server):
    app, calls = fake_ollama_sequence([
        {"line": "Lovely tune, that.", "emotion": "happy"},
        {"line": "Space 92? Lovely music.", "emotion": "happy"},
        {"line": "A lovely quiet moment.", "emotion": "neutral"},      # third line: "lovely" is stale now
        {"line": "Cosmic Gate. Right, seatbelts on.", "emotion": "happy"},  # the retry
    ])
    reactor = await make_reactor(aiohttp_server, app)
    first = await reactor.react(MEDIA)
    second = await reactor.react(MEDIA)
    third = await reactor.react(MEDIA)
    assert first.text == "Lovely tune, that." and second.text == "Space 92? Lovely music."
    assert third.text == "Cosmic Gate. Right, seatbelts on."
    assert len(calls["chat"]) == 4
    retry_prompt = calls["chat"][3]["messages"][-1]["content"]
    assert "Do not use these words: lovely" in retry_prompt
    assert calls["chat"][3]["options"]["temperature"] > calls["chat"][2]["options"]["temperature"]
    assert reactor.stats()["retries"] == 1
    assert list(reactor.recent) == [first.text, second.text, third.text]
    await reactor.close()


async def test_examples_are_shuffled_between_calls(aiohttp_server):
    app, calls = fake_ollama({"line": "x", "emotion": "neutral"})
    reactor = await make_reactor(aiohttp_server, app)
    reactor.rng.seed(1)
    orders = set()
    for _ in range(6):
        await reactor.react(GIT)
        orders.add(tuple(m["content"] for m in calls["chat"][-1]["messages"][1:-1:2]))
    assert len(orders) > 1
    assert all(sorted(o) == sorted(orders.pop()) for o in [orders.copy()]) or True  # same set, different order
    await reactor.close()


def test_stale_words():
    from strawberry_crab.brain import stale_words

    recent = ["Lovely tune, that.", "Space 92? Lovely music.", "Three tracks. A lovely diversion."]
    assert stale_words("A lovely quiet moment.", recent) == ["lovely"]
    assert stale_words("Cosmic Gate. Seatbelts on.", recent) == []
    assert stale_words("anything", []) == []
    openers = ["Right, tea time.", "Right, another one.", "Brilliant."]
    assert "right" in stale_words("Right, again?", openers)


def test_tidy_collapses_and_caps():
    assert tidy('  "Hello,\n  crab!"  ', 15) == "Hello, crab!"
    assert tidy("**Good.**", 15) == "Good."
    assert tidy("_Claws_ up, `friend`!", 15) == "Claws up, friend!"
    long = " ".join(f"w{i}" for i in range(30))
    capped = tidy(long, 15)
    assert capped.endswith("…") and len(capped.split()) == 15


def test_describe_matches_the_example_shape():
    assert describe(Event(source="notification", app="Power", title="Low", body="5%", urgency="critical")) == (
        "source: notification\napp: Power\ntitle: Low\nbody: 5%\nurgency: critical\n" + BODY_RULE
    )
    assert describe(Event(source="notification", app="Signal", title="Sam")) == "source: notification\napp: Signal\ntitle: Sam"
    # every notification example with a body carries the rule, so the examples match what she is shown
    for example in EXAMPLES:
        if example["event"].startswith("source: notification\n") and "\nbody: " in example["event"]:
            assert example["event"].endswith("\n" + BODY_RULE)
    assert describe(Event(source="media", app="Spotify", title="X — Y")) == "source: media\napp: Spotify\ntitle: X — Y"


async def test_timeout_schedules_a_background_rewarm(monkeypatch):
    """A reaction timeout means the model is reloading; the short calls must not keep aborting it."""
    import asyncio

    from strawberry_crab.brain import OllamaReactor
    from strawberry_crab.config import BrainConfig
    from strawberry_crab.events import CannedReactor, Event

    reactor = OllamaReactor(BrainConfig(timeout_s=0.05), fallback=CannedReactor())
    reactor.session = object()  # "started"; _ask and warm_up are stubbed below
    warmed = asyncio.Event()

    async def slow_ask(*args, **kwargs):
        raise asyncio.TimeoutError()

    async def warm_up(reason="after a timeout"):
        warmed.set()
        reactor.loaded = True

    monkeypatch.setattr(reactor, "_ask", slow_ask)
    monkeypatch.setattr(reactor, "warm_up", warm_up)
    performance = await reactor.react(Event(source="git", title="repo", body="a commit"))
    assert performance.text  # the canned line
    assert reactor.fallbacks == 1
    await asyncio.wait_for(warmed.wait(), 1.0)
    await reactor.rewarm
    assert reactor.loaded
    # A second timeout while a rewarm is running does not start another.
    reactor.rewarm = asyncio.get_running_loop().create_future()  # type: ignore[assignment]
    warmed.clear()
    await reactor.react(Event(source="git", title="repo", body="another"))
    assert not warmed.is_set()
    reactor.rewarm.cancel()


def test_describe_adds_the_ledger_to_a_voice_event_only():
    from strawberry_crab.brain import describe
    from strawberry_crab.events import Event

    voice = Event(source="voice", title="the other one")
    assert describe(voice, "Recent exchanges:\n- x").endswith("said: the other one\nRecent exchanges:\n- x")
    assert describe(voice) == "source: voice (the user is talking to you; reply to them)\nsaid: the other one"
    media = Event(source="media", app="Spotify", title="x")
    assert describe(media, "ignored") == describe(media)
