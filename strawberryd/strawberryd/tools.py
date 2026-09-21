"""MCP client: the servers she can act through, one or more per topic (WIRING.md §8b).

    [tools.servers.spotify]
    topic = "music"
    command = "spotify-mcp"

Each server runs as a child process over stdio, wrapped in the official Python MCP SDK. The
SDK's transport must be opened and closed from the same task, so every server gets its own
task that owns the connection and serves calls from a queue. Servers connect lazily (or in
the background at start), reconnect on the next use after a failure, and stay up for the
daemon's lifetime; a Spotify server is a small Python process and a cold connect is ~0.5 s.

Tool results come back as text, cut to `result_chars` before any model sees them: a playlist
listing is where the tokens would otherwise come from. Some servers report failures as a
normal text result with an `error` key rather than the MCP error flag, so `ok` reads both.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .config import ToolsConfig

log = logging.getLogger("strawberryd.tools")

# connect(stack, server_config) -> a session with list_tools() and call_tool(name, args), the
# SDK's ClientSession shape. Injected in tests.
Connector = Callable[[AsyncExitStack, dict[str, Any]], Awaitable[Any]]


class ToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolSpec:
    server: str
    name: str
    description: str
    schema: dict[str, Any]
    function: str = ""          # the name a model sees; differs from `name` only on a collision

    @property
    def key(self) -> str:
        return f"{self.server}.{self.name}"

    def for_ollama(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.function or self.name,
                "description": self.description,
                "parameters": self.schema or {"type": "object", "properties": {}},
            },
        }


@dataclass(frozen=True)
class ToolResult:
    server: str
    name: str
    ok: bool
    text: str
    ms: float
    truncated: bool = False
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"server": self.server, "name": self.name, "ok": self.ok, "text": self.text, "ms": round(self.ms, 1),
                "truncated": self.truncated, "arguments": self.arguments}


async def stdio_connect(stack: AsyncExitStack, server: dict[str, Any]) -> Any:
    """The real thing: spawn the server over stdio and initialise a ClientSession on it."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=server["command"], args=list(server.get("args", [])),
                                   env={**server.get("env", {})} or None, cwd=server.get("cwd") or None)
    read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session


def result_text(result: Any) -> str:
    parts = [getattr(c, "text", "") for c in getattr(result, "content", []) or []]
    text = "\n".join(p for p in parts if p).strip()
    if not text and getattr(result, "structured_content", None) is not None:
        text = json.dumps(result.structured_content, ensure_ascii=False)
    return text


def looks_like_error(text: str) -> bool:
    """Servers that return {"error": ...} as an ordinary result instead of is_error."""
    if not text.lstrip().startswith("{"):
        return False
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(data, dict) and "error" in data and len(data) <= 2


class Server:
    """One MCP server: a task owning the connection, a queue of calls into it."""

    def __init__(self, name: str, config: dict[str, Any], connect: Connector, connect_timeout_s: float,
                 call_timeout_s: float) -> None:
        self.name = name
        self.config = config
        self.topic: str = config.get("topic", "other")
        self.connect = connect
        self.connect_timeout_s = connect_timeout_s
        self.call_timeout_s = call_timeout_s
        self.task: asyncio.Task | None = None
        self.queue: asyncio.Queue[tuple[str, dict[str, Any], asyncio.Future] | None] = asyncio.Queue()
        self.ready = asyncio.Event()
        self.failed = ""
        self.tools: list[ToolSpec] = []
        self.calls = 0
        self.failures = 0
        self.last_ms: float | None = None
        self.connected_at: float | None = None

    @property
    def state(self) -> str:
        if self.ready.is_set():
            return "ready"
        if self.failed:
            return "failed"
        if self.task and not self.task.done():
            return "connecting"
        return "idle"

    async def ensure(self) -> None:
        """Connected and ready, or raises ToolError with the reason."""
        if self.ready.is_set():
            return
        if self.task is None or self.task.done():
            self.failed = ""
            self.task = asyncio.get_running_loop().create_task(self._run(), name=f"mcp:{self.name}")
        # Wake on ready, on the task dying (a server that cannot start), or on the timeout.
        waiter = asyncio.ensure_future(self.ready.wait())
        done, _pending = await asyncio.wait({waiter, self.task}, timeout=self.connect_timeout_s,
                                            return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        if not done:
            self.failed = self.failed or f"no answer in {self.connect_timeout_s:.0f}s"
            self._abandon()
        if not self.ready.is_set():
            raise ToolError(f"{self.name}: {self.failed or 'not connected'}")

    async def _run(self) -> None:
        started = time.perf_counter()
        try:
            async with AsyncExitStack() as stack:
                session = await self.connect(stack, self.config)
                listed = await session.list_tools()
                self.tools = [
                    ToolSpec(self.name, t.name, (t.description or "").strip(), dict(getattr(t, "input_schema", None) or {}))
                    for t in listed.tools
                ]
                self.connected_at = time.time()
                self.ready.set()
                log.info("tools: %s up in %.2fs with %d tools (%s)", self.name, time.perf_counter() - started,
                         len(self.tools), self.topic)
                while True:
                    item = await self.queue.get()
                    if item is None:
                        break
                    name, arguments, future = item
                    try:
                        future.set_result(await session.call_tool(name, arguments))
                    except Exception as exc:  # the SDK raises many shapes; the caller gets a ToolError
                        if not future.done():
                            future.set_exception(ToolError(f"{self.name}.{name}: {exc}"))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # ExceptionGroup from the SDK's task groups included
            self.failed = _describe(exc)
            log.warning("tools: %s failed: %s", self.name, self.failed)
        finally:
            self.ready.clear()
            self._drain(ToolError(f"{self.name}: connection closed"))

    def _drain(self, error: Exception) -> None:
        while not self.queue.empty():
            item = self.queue.get_nowait()
            if item and not item[2].done():
                item[2].set_exception(error)

    def _abandon(self) -> None:
        """Drop the connection; the task finishes cancelling on its own and the next use reconnects."""
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = None
        self.ready.clear()

    async def call(self, name: str, arguments: dict[str, Any] | None, result_chars: int) -> ToolResult:
        arguments = arguments or {}
        started = time.perf_counter()
        self.calls += 1
        try:
            await self.ensure()
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            await self.queue.put((name, arguments, future))
            try:
                raw = await asyncio.wait_for(future, self.call_timeout_s)
            except asyncio.TimeoutError:
                # A hung call would block every later one: drop the connection, reconnect next time.
                self._abandon()
                raise ToolError(f"{self.name}.{name}: no answer in {self.call_timeout_s:.0f}s")
        except ToolError as exc:
            self.failures += 1
            ms = (time.perf_counter() - started) * 1000
            self.last_ms = ms
            log.warning("tools: %s", exc)
            return ToolResult(self.name, name, False, str(exc), ms, arguments=arguments)
        ms = (time.perf_counter() - started) * 1000
        self.last_ms = ms
        text = result_text(raw)
        ok = not getattr(raw, "is_error", False) and not looks_like_error(text)
        truncated = len(text) > result_chars
        if truncated:
            text = text[:result_chars] + f"\n… [{len(text) - result_chars} more characters cut]"
        if not ok:
            self.failures += 1
        log.info("tools: %s.%s(%s) -> %s in %.0f ms: %s", self.name, name, json.dumps(arguments, ensure_ascii=False)[:120],
                 "ok" if ok else "error", ms, text[:160].replace("\n", " "))
        return ToolResult(self.name, name, ok, text, ms, truncated, arguments)

    async def close(self) -> None:
        if self.task and not self.task.done():
            if self.ready.is_set():
                await self.queue.put(None)
                try:
                    await asyncio.wait_for(self.task, 5.0)
                    return
                except (asyncio.TimeoutError, Exception):
                    pass
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass

    def stats(self) -> dict[str, Any]:
        return {"topic": self.topic, "state": self.state, "tools": len(self.tools), "calls": self.calls,
                "failures": self.failures, "last_ms": round(self.last_ms, 1) if self.last_ms is not None else None,
                "error": self.failed or None}


def _describe(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        inner = [_describe(e) for e in exc.exceptions]
        return "; ".join(dict.fromkeys(inner)) or exc.__class__.__name__
    text = str(exc).strip().split("\n")[0]
    return f"{exc.__class__.__name__}: {text}" if text else exc.__class__.__name__


class Toolbox:
    """All configured servers, looked up by topic. What the action path and Qwen see."""

    def __init__(self, config: ToolsConfig, connect: Connector | None = None) -> None:
        self.config = config
        connect = connect or stdio_connect
        self.servers: dict[str, Server] = {
            name: Server(name, server, connect, config.connect_timeout_s, config.call_timeout_s)
            for name, server in (config.servers.items() if config.enabled else ())
        }
        self.functions: dict[str, ToolSpec] = {}   # model-facing function name -> spec, per last tools_for

    def topics(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for server in self.servers.values():
            out.setdefault(server.topic, []).append(server.name)
        return out

    async def start(self) -> None:
        """Connect everything in the background so the first request doesn't pay for it."""
        if self.config.preconnect:
            for server in self.servers.values():
                asyncio.get_running_loop().create_task(self._preconnect(server))

    async def _preconnect(self, server: Server) -> None:
        try:
            await server.ensure()
        except ToolError as exc:
            log.warning("tools: %s (will retry on first use)", exc)

    async def close(self) -> None:
        await asyncio.gather(*(s.close() for s in self.servers.values()), return_exceptions=True)

    async def tools_for(self, topic: str) -> list[ToolSpec]:
        """The connected tools for a topic; servers that fail are skipped with a warning."""
        chosen = [s for s in self.servers.values() if s.topic == topic]
        results = await asyncio.gather(*(s.ensure() for s in chosen), return_exceptions=True)
        specs: list[ToolSpec] = []
        for server, outcome in zip(chosen, results):
            if isinstance(outcome, Exception):
                log.warning("tools: %s unavailable for %s: %s", server.name, topic, outcome)
                continue
            specs += server.tools
        names = [s.name for s in specs]
        resolved = [
            ToolSpec(s.server, s.name, s.description, s.schema, s.name if names.count(s.name) == 1 else f"{s.server}_{s.name}")
            for s in specs
        ]
        for spec in resolved:
            self.functions[spec.function] = spec
        return resolved

    async def call(self, server: str, name: str, arguments: dict[str, Any] | None = None,
                   result_chars: int | None = None) -> ToolResult:
        """`result_chars` overrides the configured cut for callers that parse the whole result
        themselves (the vocabulary reads a 50-track listing); a model never gets more than the config."""
        if server not in self.servers:
            return ToolResult(server, name, False, f"no server named {server!r} in [tools.servers]", 0.0, arguments=arguments or {})
        return await self.servers[server].call(name, arguments, result_chars or self.config.result_chars)

    async def call_function(self, function: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """By the model-facing name from the last `tools_for`."""
        spec = self.functions.get(function)
        if spec is None:
            return ToolResult("?", function, False, f"no tool named {function!r} was offered", 0.0, arguments=arguments or {})
        return await self.call(spec.server, spec.name, arguments)

    def stats(self) -> dict[str, Any]:
        return {name: server.stats() for name, server in self.servers.items()}
