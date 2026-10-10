"""A body for the tests, as the orbs would be (PROTOCOL Part 1c): a v2 hello that names its entities and says
it sends `touch` and `target`, then those messages, over a real websocket to the test daemon.

    orbs = await FakeBody.join(client)                  # trusted, the music and calendar orbs
    await orbs.touch("music", "flick")
    await orbs.target("music")
    got = await orbs.drain()                            # every frame it was sent since, as dicts
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from tests.bus import BUS_SECRET

ORBS = [{"id": "music", "kind": "orb", "label": "the music orb"},
        {"id": "calendar", "kind": "orb", "label": "the calendar orb"}]


def hello(body_id: str = "orbs-test", entities: list | None = None, sends: dict | None = None,
          phases: list | None = None, secret: str | None = BUS_SECRET) -> dict[str, Any]:
    message: dict[str, Any] = {
        "type": "hello", "client": "orbs", "version": "dev", "protocol": 2,
        "body": {"id": body_id, "name": "Orbs"},
        "capabilities": {"phases": phases if phases is not None else ["touch", "target"],
                         "entities": entities if entities is not None else ORBS,
                         "sends": sends if sends is not None else {"touch": True, "target": True}}}
    if secret is not None:
        message["secret"] = secret
    return message


class FakeBody:
    def __init__(self, ws: Any, welcome: dict[str, Any], extra: list[dict[str, Any]]) -> None:
        self.ws = ws
        self.welcome = welcome
        self.greeted = extra        # what came after welcome, before the ping was answered (input.refused)
        self.early: list[dict[str, Any]] = []      # what `settle` set aside, for `drain`

    @classmethod
    async def join(cls, client: Any, **kwargs: Any) -> "FakeBody":
        """Connect, say hello (kwargs: `hello`'s), and wait until the daemon has read it."""
        ws = await client.ws_connect("/ws")
        await ws.send_json(hello(**kwargs))
        await ws.send_json({"type": "ping"})
        welcome: dict[str, Any] = {}
        extra = []
        while True:
            message = await ws.receive_json(timeout=5)
            if message == {"type": "pong"}:
                break
            if message.get("type") == "welcome":
                welcome = message
            else:
                extra.append(message)
        return cls(ws, welcome, extra)

    async def touch(self, entity: str, kind: str, **fields: Any) -> None:
        await self.ws.send_json({"type": "touch", "entity": entity, "kind": kind} | fields)

    async def target(self, entity: str | None, via: str = "pointer", **fields: Any) -> None:
        await self.ws.send_json({"type": "target", "entity": entity, "via": via} | fields)

    async def send(self, message: dict[str, Any]) -> None:
        await self.ws.send_str(json.dumps(message))

    async def settle(self) -> None:
        """Until the daemon has handled everything sent so far (a ping is answered in order)."""
        await self.ws.send_json({"type": "ping"})
        while True:
            message = await self.ws.receive_json(timeout=5)
            if message == {"type": "pong"}:
                return
            self.early.append(message)

    async def drain(self, seconds: float = 0.3) -> list[dict[str, Any]]:
        """Every frame that arrives within `seconds` of the last one (and what `settle` set aside)."""
        out, self.early = list(self.early), []
        while True:
            try:
                message = await self.ws.receive(timeout=seconds)
            except asyncio.TimeoutError:
                return out
            out.append(json.loads(message.data))

    async def close(self) -> None:
        await self.ws.close()
