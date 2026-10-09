"""A tiny MCP server for tests/test_tools.py: spawned over stdio like the real ones."""

import asyncio
import json
import os
import sys

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("echo")


@server.tool()
def echo(text: str) -> str:
    """Return the text you were given."""
    return text


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@server.tool()
def long(n: int) -> str:
    """Return n characters."""
    return "x" * n


@server.tool()
def env(name: str) -> str:
    """The value of an environment variable in the server's process ("" when unset)."""
    return os.environ.get(name, "")


@server.tool()
def soft_error() -> str:
    """A failure reported the Spotify way: a normal result carrying an error key."""
    return json.dumps({"error": "error: invalid_grant, Refresh token expired"})


@server.tool()
def hard_error() -> str:
    """A failure raised properly."""
    raise ValueError("nope")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
async def slow(seconds: float) -> str:
    """Wait this many seconds, then say how long it waited. Use it when asked to wait or to test something slow."""
    await asyncio.sleep(seconds)
    return f"waited {seconds} seconds"


@server.tool()
async def slow_change(seconds: float) -> str:
    """Wait this many seconds as if changing something, then say it is done."""
    await asyncio.sleep(seconds)
    return "changed"


@server.tool(annotations=ToolAnnotations(destructive_hint=True))
def shred(name: str) -> str:
    """Delete a note for good (nothing is deleted: a test of the destructiveHint annotation)."""
    return json.dumps({"shredded": len(name)})


@server.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
def peek(name: str) -> str:
    """Read a note (a test of an annotation that claims to only read)."""
    return f"{len(name)} characters"


if __name__ == "__main__":
    if "--crash" in sys.argv:
        sys.exit(3)
    server.run("stdio")
