"""A tiny MCP server for tests/test_tools.py: spawned over stdio like the real ones."""

import json
import sys

from mcp.server.mcpserver import MCPServer

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
def soft_error() -> str:
    """A failure reported the Spotify way: a normal result carrying an error key."""
    return json.dumps({"error": "error: invalid_grant, Refresh token expired"})


@server.tool()
def hard_error() -> str:
    """A failure raised properly."""
    raise ValueError("nope")


if __name__ == "__main__":
    if "--crash" in sys.argv:
        sys.exit(3)
    server.run("stdio")
