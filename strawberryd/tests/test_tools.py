"""The MCP client (WIRING.md §8b): a fake session for the logic, a real stdio server for the transport."""

from __future__ import annotations

import asyncio
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from strawberryd.config import Config, ConfigError, ToolsConfig, _validate
from strawberryd.daemon import Daemon
from strawberryd.events import CannedReactor
from strawberryd.server import create_app
from strawberryd.tools import Toolbox, ToolSpec, clarify_error, looks_like_error, result_text

ECHO_SERVER = Path(__file__).with_name("mcp_echo_server.py")


# ----------------------------------------------------------------------------- fake session


@dataclass
class FakeTool:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeContent:
    text: str


@dataclass
class FakeResult:
    content: list[FakeContent]
    is_error: bool = False
    structured_content: Any = None


@dataclass
class FakeListed:
    tools: list[FakeTool]


class FakeSession:
    def __init__(self, tools: list[FakeTool], handler) -> None:
        self.tools = tools
        self.handler = handler
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self) -> FakeListed:
        return FakeListed(self.tools)

    async def call_tool(self, name: str, arguments: dict) -> FakeResult:
        self.calls.append((name, arguments))
        return await self.handler(name, arguments)


def make_connect(sessions: dict[str, FakeSession], connects: list[str] | None = None):
    async def connect(stack: AsyncExitStack, server: dict[str, Any]) -> FakeSession:
        if connects is not None:
            connects.append(server["command"])
        if server["command"] == "boom":
            raise RuntimeError("cannot start")
        if server["command"] == "hang":
            await asyncio.sleep(60)
        return sessions[server["command"]]

    return connect


async def echo_handler(name: str, arguments: dict) -> FakeResult:
    if name == "echo":
        return FakeResult([FakeContent(arguments["text"])])
    if name == "long":
        return FakeResult([FakeContent("x" * arguments["n"])])
    if name == "soft":
        return FakeResult([FakeContent('{"error": "token expired"}')])
    if name == "hard":
        return FakeResult([FakeContent("nope")], is_error=True)
    if name == "slow":
        await asyncio.sleep(60)
    if name == "structured":
        return FakeResult([], structured_content={"bpm": 128})
    raise KeyError(name)


def toolbox(servers: dict[str, dict[str, Any]], sessions: dict[str, FakeSession], **kw) -> Toolbox:
    config = ToolsConfig(servers=servers, preconnect=False, **kw)
    return Toolbox(config, connect=make_connect(sessions))


MUSIC = FakeSession([FakeTool("echo", "Echo.", {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}),
                     FakeTool("long"), FakeTool("soft"), FakeTool("hard"), FakeTool("slow"), FakeTool("structured")], echo_handler)


# ----------------------------------------------------------------------------- unit


def test_result_text_and_soft_errors():
    assert result_text(FakeResult([FakeContent("a"), FakeContent("b")])) == "a\nb"
    assert result_text(FakeResult([], structured_content={"x": 1})) == '{"x": 1}'
    assert looks_like_error('{"error": "bad"}')
    assert looks_like_error('{"error": "bad", "code": 401}')
    assert looks_like_error('{"error": "Permission denied. Check app scopes.", "status": 403, "details": "Restriction violated"}')
    assert not looks_like_error('{"error": "bad", "track": "x", "artist": "y"}')  # a record that happens to mention an error
    assert not looks_like_error("Skipped.")
    assert not looks_like_error("{not json")


def test_toolspec_for_ollama_uses_function_name_and_schema():
    spec = ToolSpec("spotify", "next", "Skip.", {"type": "object", "properties": {"device_id": {"type": "string"}}}, function="spotify_next")
    tool = spec.for_ollama()
    assert tool["function"]["name"] == "spotify_next"
    assert tool["function"]["parameters"]["properties"]["device_id"]["type"] == "string"
    assert ToolSpec("s", "n", "", {}).for_ollama()["function"]["parameters"] == {"type": "object", "properties": {}}
    assert spec.key == "spotify.next"


async def test_tools_for_connects_lazily_and_lists_the_topic():
    box = toolbox({"music": {"topic": "music", "command": "music"}}, {"music": MUSIC})
    assert box.topics() == {"music": ["music"]}
    assert box.servers["music"].state == "idle"
    specs = await box.tools_for("music")
    assert [s.name for s in specs][:2] == ["echo", "long"]
    assert box.servers["music"].state == "ready"
    assert await box.tools_for("calendar") == []
    result = await box.call("music", "echo", {"text": "hi"})
    assert result.ok and result.text == "hi" and result.arguments == {"text": "hi"}
    assert result.to_dict()["server"] == "music"
    by_function = await box.call_function("echo", {"text": "again"})
    assert by_function.ok and by_function.text == "again"
    assert not (await box.call_function("nothing", {})).ok
    await box.close()
    assert box.servers["music"].state == "idle"


async def test_results_are_truncated_and_errors_read_both_ways():
    box = toolbox({"music": {"topic": "music", "command": "music"}}, {"music": MUSIC}, result_chars=100)
    long = await box.call("music", "long", {"n": 500})
    assert long.ok and long.truncated and len(long.text) < 200 and "400 more characters cut" in long.text
    soft = await box.call("music", "soft")
    assert not soft.ok and "token expired" in soft.text
    hard = await box.call("music", "hard")
    assert not hard.ok
    structured = await box.call("music", "structured")
    assert structured.ok and structured.text == '{"bpm": 128}'
    assert box.servers["music"].stats()["failures"] == 2
    await box.close()


async def test_a_server_that_cannot_start_fails_softly_and_retries_next_time():
    connects: list[str] = []
    config = ToolsConfig(servers={"bad": {"topic": "music", "command": "boom"}}, preconnect=False)
    box = Toolbox(config, connect=make_connect({}, connects))
    assert await box.tools_for("music") == []
    assert box.servers["bad"].state == "failed" and "cannot start" in box.servers["bad"].failed
    result = await box.call("bad", "echo", {})
    assert not result.ok and "cannot start" in result.text
    assert connects == ["boom", "boom"]  # every use tries again; nothing is cached as broken
    unknown = await box.call("nope", "echo", {})
    assert not unknown.ok and "no server named" in unknown.text
    await box.close()


async def test_connect_and_call_timeouts_drop_the_connection():
    sessions = {"music": MUSIC, "hang": MUSIC}
    box = toolbox({"h": {"topic": "music", "command": "hang"}, "m": {"topic": "music", "command": "music"}}, sessions,
                  connect_timeout_s=0.05, call_timeout_s=0.05)
    specs = await box.tools_for("music")
    assert {s.server for s in specs} == {"m"}  # the hanging one is skipped, the other still serves
    assert box.servers["h"].state == "failed" and "no answer" in box.servers["h"].failed
    slow = await box.call("m", "slow")
    assert not slow.ok and "no answer" in slow.text
    assert box.servers["m"].state in ("idle", "failed")
    again = await box.call("m", "echo", {"text": "back"})  # reconnects
    assert again.ok and again.text == "back"
    await box.close()


async def test_colliding_tool_names_get_the_server_prefix():
    other = FakeSession([FakeTool("echo")], echo_handler)
    box = toolbox({"a": {"topic": "music", "command": "music"}, "b": {"topic": "music", "command": "other"}},
                  {"music": MUSIC, "other": other})
    specs = await box.tools_for("music")
    functions = {s.key: s.function for s in specs}
    assert functions["a.echo"] == "a_echo" and functions["b.echo"] == "b_echo" and functions["a.long"] == "long"
    result = await box.call_function("b_echo", {"text": "b"})
    assert result.server == "b"
    await box.close()


def test_config_validation():
    config = Config()
    config.tools.servers = {"x": {"topic": "music", "command": "x", "args": ["--a"], "env": {"K": "v"}}}
    _validate(config)
    for bad in [{"x": {"topic": "music"}}, {"x": {"command": "x", "args": [1]}}, {"x": {"command": "x", "env": {"K": 1}}},
                {"x": {"command": "x", "port": 1}}]:
        config.tools.servers = bad
        with pytest.raises(ConfigError):
            _validate(config)
    config.tools.servers = {}
    config.tools.result_chars = 10
    with pytest.raises(ConfigError):
        _validate(config)


async def test_daemon_exposes_tools_in_health(aiohttp_client):
    config = Config()
    config.brain.enabled = config.speech.enabled = config.voice.enabled = config.gate.enabled = config.thinker.enabled = False
    config.tools = ToolsConfig(servers={"music": {"topic": "music", "command": "music"}}, preconnect=False)
    daemon = Daemon(reactor=CannedReactor(), config=config, toolbox=Toolbox(config.tools, connect=make_connect({"music": MUSIC})))
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    health = await (await client.get("/health")).json()
    assert health["tools"]["music"] == {"topic": "music", "state": "idle", "tools": 0, "calls": 0, "failures": 0,
                                        "last_ms": None, "error": None, "adapter": None}
    await daemon.toolbox.tools_for("music")
    health = await (await client.get("/health")).json()
    assert health["tools"]["music"]["state"] == "ready" and health["tools"]["music"]["tools"] == 6
    await daemon.close()


# ----------------------------------------------------------------------------- real stdio


async def test_real_stdio_server_round_trip():
    """The SDK path for real: spawn tests/mcp_echo_server.py, list, call, truncate, both error shapes."""
    config = ToolsConfig(servers={"echo": {"topic": "test", "command": sys.executable, "args": [str(ECHO_SERVER)]}},
                         preconnect=False, result_chars=120)
    box = Toolbox(config)
    specs = await box.tools_for("test")
    assert {s.name for s in specs} == {"echo", "add", "long", "soft_error", "hard_error"}
    add = next(s for s in specs if s.name == "add")
    assert add.schema["properties"]["a"]["type"] == "integer" and add.description == "Add two integers."
    assert (await box.call("echo", "echo", {"text": "hello"})).text == "hello"
    result = await box.call("echo", "add", {"a": 2, "b": 3})
    assert result.ok and result.text.strip() in ("5", '{"result": 5}') or "5" in result.text
    long = await box.call("echo", "long", {"n": 1000})
    assert long.truncated and long.ok
    soft = await box.call("echo", "soft_error")
    assert not soft.ok and "Refresh token expired" in soft.text
    hard = await box.call("echo", "hard_error")
    assert not hard.ok
    assert box.servers["echo"].stats()["failures"] == 2
    await box.close()
    assert box.servers["echo"].state == "idle"


async def test_real_server_that_exits_is_reported():
    config = ToolsConfig(servers={"echo": {"topic": "test", "command": sys.executable, "args": [str(ECHO_SERVER), "--crash"]}},
                         preconnect=False, connect_timeout_s=5.0)
    box = Toolbox(config)
    assert await box.tools_for("test") == []
    assert box.servers["echo"].state == "failed" and box.servers["echo"].failed
    await box.close()


async def test_careful_tools_are_withheld_unless_asked():
    session = FakeSession([FakeTool("play"), FakeTool("save_tracks"), FakeTool("clear_favorites")], echo_handler)
    box = toolbox({"spotify": {"topic": "music", "command": "music", "careful": ["save_tracks", "clear_favorites"]}}, {"music": session})
    assert [s.name for s in await box.tools_for("music", careful=False)] == ["play"]
    assert [s.name for s in await box.tools_for("music")] == ["play", "save_tracks", "clear_favorites"]
    await box.close()


def test_clarify_error_asks_the_server_adapter_and_nobody_else():
    """The core knows no server's wording: without an adapter an error body is left alone."""

    class Clearer:
        name = "clearer"

        def clarify_error(self, text: str) -> str:
            return '{"error": "in plain words"}'

    class Broken:
        name = "broken"

        def clarify_error(self, text: str) -> str:
            raise RuntimeError("boom")

    muddled = '{"error": "Permission denied. Check app scopes.", "status": 403, "details": "Restriction violated"}'
    assert clarify_error(muddled) == muddled                      # no adapter, no rewriting
    assert clarify_error(muddled, Clearer()) == '{"error": "in plain words"}'
    assert looks_like_error(clarify_error(muddled, Clearer()))     # still an error, in plainer words
    assert clarify_error("Skipped.", Clearer()) == "Skipped."      # not an error body at all
    assert clarify_error(muddled, Broken()) == muddled             # an adapter never breaks a call
