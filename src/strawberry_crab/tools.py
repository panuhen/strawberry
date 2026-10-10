"""MCP client: the servers she can act through, one or more per topic (WIRING.md §8b).

    [tools.servers.notes]
    topic = "notes"
    command = "my-notes-mcp"

The servers you list are the servers; none is configured by default (ADAPTERS.md). A server
whose name — or whose `adapter` key — matches one of `strawberry/adapters/` also gets that
adapter, which is where everything server-specific lives: the reflexes, the situation line,
the recogniser's vocabulary, and the rewording of that server's confusing errors. The client
below knows none of it.

Each server runs as a child process over stdio, wrapped in the official Python MCP SDK. The
SDK's transport must be opened and closed from the same task, so every server gets its own
task that owns the connection and serves calls from a queue. Servers connect lazily (or in
the background at start), reconnect on the next use after a failure, and stay up for the
daemon's lifetime; a Spotify server is a small Python process and a cold connect is ~0.5 s.

Tool results come back as text, cut to `result_chars` before any model sees them: a playlist
listing is where the tokens would otherwise come from. Some servers report failures as a
normal text result with an `error` key rather than the MCP error flag, so `ok` reads both.
Control characters are taken out of every result, and a server's trust flags (trust.py: private,
foreign, egress) decide what the journal may say of its calls: a private, foreign or unknown
server's results and arguments are logged as counts and sizes only.

A server that runs inside the daemon (memory and messages, from stages 5 and 6 of brain step 6) is
added with `Toolbox.add_builtin`: the same Server, with a session object in place of a process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .config import RISKS, ToolsConfig
from . import logtext, trust

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
    urls: tuple[str, ...] = ()          # the result's own URLs (Adapter.result_urls), read before the cut

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


def _field(item: Any, *names: str) -> Any:
    """The first of these fields an MCP object has, as an attribute or a dict key. The SDK has
    spelt them both ways: `isError`/`structuredContent` in 1.x (and on the wire), `is_error`/
    `structured_content` in 2.x; a dict is the result as it came over the wire."""
    for name in names:
        value = item.get(name) if isinstance(item, dict) else getattr(item, name, None)
        if value is not None:
            return value
    return None


def result_text(result: Any) -> str:
    parts = [_field(c, "text") or "" for c in _field(result, "content") or []]
    text = "\n".join(p for p in parts if isinstance(p, str) and p).strip()
    structured = _field(result, "structured_content", "structuredContent")
    if not text and structured is not None:
        text = json.dumps(structured, ensure_ascii=False)
    return text


def flagged_error(result: Any) -> bool:
    """The MCP error flag, however the SDK (or a dict) spells it."""
    return bool(_field(result, "is_error", "isError"))


def looks_like_error(text: str) -> bool:
    """Servers that return {"error": ...} as an ordinary result instead of is_error."""
    if not text.lstrip().startswith("{"):
        return False
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict) or "error" not in data:
        return False
    return set(data) <= {"error", "status", "details", "code", "message"}


def clarify_error(text: str, adapter: Any | None = None, is_error: bool = False) -> str:
    """Ask the server's adapter to rewrite a confusing refusal before any model reads it.

    Some servers label every refusal the same way (Spotify's says "Permission denied. Check app
    scopes." for a 403 that is really "already playing"), and a model reading that would report a
    permissions problem to the user. The core knows no server's wording, so the rewriting lives in
    that server's adapter; a server without one is left exactly as it answered. A refusal is a
    result with the MCP error flag, or an ordinary one with an `error` body (`looks_like_error`).
    """
    if adapter is None or not (is_error or looks_like_error(text)):
        return text
    try:
        clarified = adapter.clarify_error(text)
    except Exception as exc:  # an adapter must never break a tool call
        log.warning("tools: %s could not clarify an error (%s)", getattr(adapter, "name", adapter), exc)
        return text
    return clarified if isinstance(clarified, str) and clarified else text


class Server:
    """One MCP server: a task owning the connection, a queue of calls into it."""

    def __init__(self, name: str, config: dict[str, Any], connect: Connector, connect_timeout_s: float,
                 call_timeout_s: float, adapter: Any | None = None) -> None:
        self.name = name
        self.config = config
        self.topic: str = config.get("topic", "other")
        # This server's adapter (strawberry/adapters/), or None for a plain server of tools.
        self.adapter = adapter
        self.on_adapter: Callable[[str, Any], None] | None = None   # told when a tool list claims one
        # Tools with consequences (saving, removing, changing playlists): withheld from the thinker
        # unless the sentence asks for such a change (systemone.WANTS_LIBRARY_CHANGE).
        self.careful: frozenset[str] = frozenset(config.get("careful", []))
        # Tools she asks about before calling (confirm.py): the config's `confirm` when it has one
        # (`confirm = []` asks about none), else the adapter's own default.
        self.confirm: frozenset[str] = frozenset(config["confirm"] if "confirm" in config
                                                 else getattr(adapter, "confirm", ()) or ())
        # private / foreign / egress (trust.py): the adapter's, plus what the config adds; all three for a
        # server nobody has said anything about.
        self.flags: frozenset[str] = trust.flags_for(adapter, config)
        self.unknown = adapter is None and "flags" not in config   # all three by default, not by anyone's word
        # When the thinker is offered its tools: always, only for a sentence of its topic, or only when a
        # sentence asks for it (Thinker.tools). The config's `offer`, else the adapter's, else always.
        self.offer: str = config.get("offer") or getattr(adapter, "offer", "") or "always"
        self.connect = connect
        self.connect_timeout_s = connect_timeout_s
        self.call_timeout_s = call_timeout_s
        self.task: asyncio.Task | None = None
        self.queue: asyncio.Queue[tuple[str, dict[str, Any], asyncio.Future] | None] = asyncio.Queue()
        self.ready = asyncio.Event()
        self.failed = ""
        self.tools: list[ToolSpec] = []
        # Tools the server marks as only reading (MCP's readOnlyHint): a cancel stops waiting for one,
        # where a call that may change something is let finish first (runs.py, Thinker._call).
        self.read_only: frozenset[str] = frozenset()
        # Tools the server marks destructiveHint: their approval tier is raised to `destructive` (risk).
        self.destructive: frozenset[str] = frozenset()
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
                self._claim([t.name for t in listed.tools])
                self.read_only = frozenset(t.name for t in listed.tools if _read_only(t))
                self.destructive = frozenset(t.name for t in listed.tools if _destructive(t))
                self.tools = self._offer([
                    ToolSpec(self.name, t.name, (t.description or "").strip(), dict(getattr(t, "input_schema", None) or {}))
                    for t in listed.tools
                ])
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

    def _claim(self, names: list[str]) -> None:
        """A server that lists web tools gets the web adapter, with its guards, whatever it is called
        in the config: guards that depended on the name would be off under any other one."""
        from .adapters import adapter_for_tools   # local: the adapters import this module

        claimed = adapter_for_tools(names)
        if claimed is None or claimed is self.adapter or getattr(self.adapter, "foreign", False):
            return
        log.warning("tools: %s lists %s tools; using the %s adapter for it (name it [tools.servers.%s] or set "
                    "adapter = \"%s\" to say so)", self.name, claimed.name, claimed.name, claimed.name, claimed.name)
        self.adapter = claimed
        self.flags = trust.flags_for(claimed, self.config)
        self.unknown = False
        if self.on_adapter is not None:
            self.on_adapter(self.name, claimed)

    def _offer(self, specs: list[ToolSpec]) -> list[ToolSpec]:
        """The tools as the brain sees them: the adapter's `tools` only, when it names some, each in
        its `shape_tool` form. A call by name still reaches a tool left out (`strawberry tool`)."""
        if self.adapter is None:
            return specs
        wanted = tuple(getattr(self.adapter, "tools", ()) or ())
        if wanted:
            specs = [s for s in specs if s.name in wanted]
        shaped: list[ToolSpec] = []
        for spec in specs:
            try:
                shaped.append(self.adapter.shape_tool(spec))
            except Exception as exc:  # an adapter must never cost the server its tools
                log.warning("tools: %s could not shape %s (%s)", self.name, spec.name, exc)
                shaped.append(spec)
        return shaped

    def _adapted(self, method: str, default: Any, *args: Any) -> Any:
        """An adapter hook that must never break a call: its answer, or `default` on any failure."""
        hook = getattr(self.adapter, method, None) if self.adapter is not None else None
        if hook is None:
            return default
        try:
            return hook(*args)
        except Exception as exc:
            log.warning("tools: %s: %s.%s failed (%s)", self.name, getattr(self.adapter, "name", "?"), method, exc)
            return default

    def risk(self, name: str, overrides: dict[str, str] | None = None) -> str:
        """The approval tier of one of this server's tools (approvals.py): read | change | sends |
        destructive. An `[approvals] risk` entry for the tool ("server.tool") decides, taken as written:
        the one way to lower a tool below what its adapter or its server says. Otherwise the adapter's own
        tier (`Adapter.risk`), else `change`; a tool the server marks destructiveHint is raised to
        `destructive`; and a whole-server entry ("server") then raises or lowers it, but never below the
        adapter's own tier or the annotation (it says how to treat the server's ordinary tools, not that
        its deletions are harmless). An annotation never lowers a tier (readOnlyHint is not believed here:
        a server cannot talk its way out of a yes)."""
        overrides = overrides or {}
        own = overrides.get(f"{self.name}.{name}")
        if own in RISKS:
            return own
        declared = self._adapted("risk", None, name)
        declared = declared if declared in RISKS else None
        floor = "destructive" if name in self.destructive else "read"
        if declared is not None and RISKS.index(declared) > RISKS.index(floor):
            floor = declared
        whole = overrides.get(self.name)
        if whole in RISKS:
            return whole if RISKS.index(whole) >= RISKS.index(floor) else floor
        tier = declared or "change"
        return tier if RISKS.index(tier) >= RISKS.index(floor) else floor

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

    def logs_detail(self) -> bool:
        """May the journal carry what this server's calls say (the first 160 characters of a result, the
        arguments as logtext shows them)? Only for a server whose adapter opts in (`log_detail`) and
        that is neither private nor foreign; every other one is logged as counts and sizes."""
        return (self.adapter is not None and getattr(self.adapter, "log_detail", False) is True
                and not self.flags & {"private", "foreign"})

    async def call(self, name: str, arguments: dict[str, Any] | None, result_chars: int,
                   shape: bool = False) -> ToolResult:
        """One call. `shape`: the result as a model reads it (the adapter's `shape_result`, one line per
        hit); without it, as the server wrote it, for the code that parses it (a reflex, the vocabulary,
        an adapter's `ask` and `done`). Either way cut to `result_chars`, without control characters."""
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
            if not self.logs_detail():
                # This server's results stay out of the journal; an SDK error can quote its arguments.
                log.warning("tools: %s.%s failed (%s; not logged)", self.name, name, type(exc).__name__)
            else:
                log.warning("tools: %s", exc)
            return ToolResult(self.name, name, False, trust.clean(str(exc)), ms, arguments=arguments)
        ms = (time.perf_counter() - started) * 1000
        self.last_ms = ms
        is_error = flagged_error(raw)
        text = clarify_error(result_text(raw), self.adapter, is_error)
        ok = not is_error and not looks_like_error(text)
        urls = self._adapted("result_urls", [], name, text, ok)
        urls = tuple(u for u in urls if isinstance(u, str)) if isinstance(urls, (list, tuple)) else ()
        if shape:
            text = self._adapted("shape_result", text, name, text, ok)
            if not isinstance(text, str):
                text = result_text(raw)
        text = trust.clean(text)
        # The adapter may keep a result out of the journal (a web search's results, which can
        # quote the query) and say what it was instead: the count, the size, of the whole result.
        summary = self._adapted("log_result", None, name, text, ok)
        truncated = len(text) > result_chars
        if truncated:
            text = text[:result_chars] + f"\n… [{len(text) - result_chars} more characters cut]"
        if not ok:
            self.failures += 1
        detail = self.logs_detail()
        if not isinstance(summary, str):
            summary = text[:160].replace("\n", " ") if detail else f"{len(text)} chars (not logged)"
        log.info("tools: %s.%s(%s) -> %s in %.0f ms: %s", self.name, name,
                 logtext.arguments(arguments) if detail else logtext.names(arguments), "ok" if ok else "error", ms,
                 summary)
        return ToolResult(self.name, name, ok, text, ms, truncated, arguments, urls)

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
                "error": self.failed or None, "adapter": self.adapter.name if self.adapter else None,
                "confirm": sorted(self.confirm), "flags": sorted(self.flags), "offer": self.offer}


def _read_only(tool: Any) -> bool:
    annotations = _field(tool, "annotations")
    return annotations is not None and _field(annotations, "read_only_hint", "readOnlyHint") is True


def _destructive(tool: Any) -> bool:
    """MCP's destructiveHint, only when the server says it: its default of true for any tool that is not
    read-only would make every unannotated tool wait for a yes."""
    annotations = _field(tool, "annotations")
    return annotations is not None and _field(annotations, "destructive_hint", "destructiveHint") is True


def _describe(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        inner = [_describe(e) for e in exc.exceptions]
        return "; ".join(dict.fromkeys(inner)) or exc.__class__.__name__
    text = str(exc).strip().split("\n")[0]
    return f"{exc.__class__.__name__}: {text}" if text else exc.__class__.__name__


class Toolbox:
    """All configured servers, looked up by topic. What the action path and Qwen see."""

    def __init__(self, config: ToolsConfig, connect: Connector | None = None,
                 adapters: dict[str, Any] | None = None) -> None:
        self.config = config
        connect = connect or stdio_connect
        if adapters is None:
            from .adapters import load  # local: the adapters import the core, never the other way round

            adapters = load(config.servers) if config.enabled else {}
        #: server name -> its adapter, for the configured servers that matched one
        self.adapters: dict[str, Any] = adapters
        self.servers: dict[str, Server] = {
            name: Server(name, server, connect, config.connect_timeout_s, config.call_timeout_s, adapters.get(name))
            for name, server in (config.servers.items() if config.enabled else ())
        }
        self.functions: dict[str, ToolSpec] = {}   # model-facing function name -> spec, per last tools_for
        self.risks: dict[str, str] = {}             # [approvals] risk, set by the daemon (Server.risk)
        for server in self.servers.values():
            server.on_adapter = self.adapters.__setitem__

    def common_tools(self, server: str) -> tuple[str, ...]:
        """The tools this server's adapter keeps first when the brain's context is tight (§8b)."""
        adapter = self.adapters.get(server)
        return tuple(getattr(adapter, "common_tools", ()) or ()) if adapter else ()

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

    async def tools_for(self, topic: str, careful: bool = True) -> list[ToolSpec]:
        """The connected tools for a topic; servers that fail are skipped with a warning.
        `careful=False` leaves out each server's careful tools (see Server.careful)."""
        chosen = [s for s in self.servers.values() if s.topic == topic]
        results = await asyncio.gather(*(s.ensure() for s in chosen), return_exceptions=True)
        specs: list[ToolSpec] = []
        for server, outcome in zip(chosen, results):
            if isinstance(outcome, Exception):
                log.warning("tools: %s unavailable for %s: %s", server.name, topic, outcome)
                continue
            specs += [t for t in server.tools if careful or t.name not in server.careful]
        names = [s.name for s in specs]
        resolved = [
            ToolSpec(s.server, s.name, s.description, s.schema, s.name if names.count(s.name) == 1 else f"{s.server}_{s.name}")
            for s in specs
        ]
        for spec in resolved:
            self.functions[spec.function] = spec
        return resolved

    async def offered(self, servers: list[str] | set[str] | frozenset[str], careful: bool = True) -> list[ToolSpec]:
        """The thinker's tools for one sentence: these servers' tools in one stable order (by topic, then the
        server's name, then the server's own order), so the schemas after the system prompt read the same from
        sentence to sentence and Ollama's prompt cache holds; names that collide anywhere among them are
        prefixed with their server. A server that does not connect is left out with a warning."""
        chosen = sorted((self.servers[n] for n in servers if n in self.servers), key=lambda s: (s.topic, s.name))
        results = await asyncio.gather(*(s.ensure() for s in chosen), return_exceptions=True)
        specs: list[ToolSpec] = []
        for server, outcome in zip(chosen, results):
            if isinstance(outcome, Exception):
                log.warning("tools: %s unavailable: %s", server.name, outcome)
                continue
            specs += [t for t in server.tools if careful or t.name not in server.careful]
        names = [s.name for s in specs]
        resolved = [
            ToolSpec(s.server, s.name, s.description, s.schema, s.name if names.count(s.name) == 1 else f"{s.server}_{s.name}")
            for s in specs
        ]
        for spec in resolved:
            self.functions[spec.function] = spec
        return resolved

    async def call(self, server: str, name: str, arguments: dict[str, Any] | None = None,
                   result_chars: int | None = None, shape: bool = False) -> ToolResult:
        """`result_chars` overrides the configured cut for callers that parse the whole result
        themselves (the vocabulary reads a 50-track listing); a model never gets more than the config.
        `shape`: as a model reads it (Server.call)."""
        if server not in self.servers:
            return ToolResult(server, name, False, f"no server named {server!r} in [tools.servers]", 0.0, arguments=arguments or {})
        return await self.servers[server].call(name, arguments, result_chars or self.config.result_chars, shape=shape)

    def label(self, server: str, name: str) -> str:
        """The tool as the widget's step chip names it ("Spotify: next", "searching the web…"): the
        adapter's own words for it, else the server's title and the tool's name. Never an argument."""
        adapter = self.adapters.get(server)
        own = (getattr(adapter, "labels", None) or {}).get(name) if adapter is not None else None
        if isinstance(own, str) and own:
            return own
        title = (getattr(adapter, "title", "") if adapter is not None else "") or server.replace("_", " ").capitalize()
        return f"{title}: {name.replace('_', ' ')}"

    def reads(self, server: str, name: str) -> bool:
        """Does this tool only read? A server that only looks things up (web search), a tool its adapter
        lists in `reads`, or one the server marks read-only. Anything else may change something."""
        adapter = self.adapters.get(server)
        if adapter is not None and (getattr(adapter, "looks_up_only", False) or name in (getattr(adapter, "reads", ()) or ())):
            return True
        found = self.servers.get(server)
        return found is not None and name in found.read_only

    def needs_confirm(self, server: str, name: str) -> bool:
        """Is this tool on its server's `confirm` list: a spoken yes first (confirm.py)?"""
        found = self.servers.get(server)
        return found is not None and name in found.confirm

    def risk(self, server: str, name: str) -> str:
        """The tool's approval tier (Server.risk, with `[approvals] risk`); `change` for an unknown server."""
        found = self.servers.get(server)
        return found.risk(name, self.risks) if found is not None else "change"

    def needs_approval(self, server: str, name: str, foreign: bool = False) -> bool:
        """Does this call wait for a yes (approvals.needed): on its server's `confirm` list, or `sends` or
        `destructive`, or, with `foreign` (strangers' text is in the conversation), anything but `read`?"""
        from .approvals import needed   # local: approvals imports confirm, which imports this module

        return needed(self.needs_confirm(server, name), self.risk(server, name), foreign=foreign)

    def flags(self, server: str) -> frozenset[str]:
        """The server's trust flags (trust.py); all three for a server that is not configured."""
        found = self.servers.get(server)
        return found.flags if found is not None else trust.UNKNOWN

    def private(self, server: str) -> bool:
        return "private" in self.flags(server)

    def shown(self, call: ToolResult) -> dict[str, Any]:
        """A call as /health keeps it (`thinker.last`, `actions.last`): for a private, foreign or unknown server
        its result as its size and its arguments by name only, the journal's rule (Server.logs_detail);
        for the rest the first 200 characters, as before."""
        out = call.to_dict()
        found = self.servers.get(call.server)
        detail = found is not None and found.logs_detail()
        if detail:
            out["text"] = call.text[:200]
        else:
            source = " from outside" if self.foreign(call.server) else ""
            out["text"] = f"<{len(call.text)} chars{source}>"
            out["arguments"] = sorted(call.arguments)
        return out

    def foreign(self, server: str) -> bool:
        return "foreign" in self.flags(server)

    def egress(self, server: str) -> bool:
        return "egress" in self.flags(server)

    def add_builtin(self, name: str, topic: str, open_session: Callable[[], Awaitable[Any]],
                    adapter: Any | None = None, config: dict[str, Any] | None = None) -> Server:
        """A server that runs inside the daemon (the hook for brain step 6's memory and messages): the same
        Server, offered, guarded and logged like any other, whose session is `await open_session()` (an
        object with list_tools() and call_tool(name, arguments)) instead of a process. Its flags come from
        its adapter, as for any server; without one it is unknown, so all three."""

        async def connect(stack: AsyncExitStack, _config: dict[str, Any]) -> Any:
            return await open_session()

        server = Server(name, {"topic": topic, **(config or {})}, connect, self.config.connect_timeout_s,
                        self.config.call_timeout_s, adapter)
        server.on_adapter = self.adapters.__setitem__
        self.servers[name] = server
        if adapter is not None:
            self.adapters[name] = adapter
        return server

    async def call_function(self, function: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """By the model-facing name from the last `tools_for`: the thinker's calls, so the result is
        shaped for a model (Server.call `shape`)."""
        spec = self.functions.get(function)
        if spec is None:
            return ToolResult("?", function, False, f"no tool named {function!r} was offered", 0.0, arguments=arguments or {})
        return await self.call(spec.server, spec.name, arguments, shape=True)

    def stats(self) -> dict[str, Any]:
        return {name: server.stats() for name, server in self.servers.items()}
