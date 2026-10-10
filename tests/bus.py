"""The bus secret as the tests present it (bussecret.py, PROTOCOL §1.4).

conftest.py writes BUS_SECRET into every test's throwaway state dir, where the daemon finds it as it
would its own; `tests/test_bussecret.py` checks the real making of one. A test body presents it the way
the widget does: in its hello.
"""

from __future__ import annotations

from typing import Any

BUS_SECRET = "test-bus-secret-0123456789abcdefghijklmnopq"     # 43 url-safe characters, like token_urlsafe(32)
V1_HELLO = {"type": "hello", "client": "test", "version": "dev"}


def trusted(hello: dict[str, Any] | None = None) -> dict[str, Any]:
    """A hello (a v1 one by default) that presents the bus secret."""
    return (hello or V1_HELLO) | {"secret": BUS_SECRET}


async def connect(client: Any, hello: dict[str, Any] | None = None) -> Any:
    """A websocket to the test daemon whose hello presented the secret: it may type, poke and cancel."""
    ws = await client.ws_connect("/ws")
    await ws.send_json(trusted(hello))
    return ws
