"""The session bus, through jeepney: one small client both the doorways and the tray use.

jeepney is a protocol library, not a bus daemon. It hands us messages exactly as they went
over the wire, which is what we want here (a monitor connection and a StatusNotifierItem both
need raw messages), but it means three things are ours to do:

* **variants.** Every `v` parses to a `(signature, value)` pair, so `plain()` unwraps them.
* **replies.** A method call is matched to its reply by serial; `BusClient.call` keeps a future
  per serial and the read loop resolves it.
* **the socket dying.** `BusClient.run()` returns when the connection closes, so a watcher can
  exit non-zero and be restarted instead of going quietly deaf (WIRING.md §4).

Messages that are not replies to us — signals we subscribed to, or method calls addressed to
an object we export — go on `incoming` for a consumer task, so a handler that makes its own
calls cannot deadlock the reader.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from jeepney import DBusAddress, HeaderFields, Message, MessageType, new_method_call
from jeepney.io.asyncio import open_dbus_connection

log = logging.getLogger("strawberryd.bus")

DBUS = DBusAddress("/org/freedesktop/DBus", bus_name="org.freedesktop.DBus", interface="org.freedesktop.DBus")
SIGNATURE_CHARS = set("ybnqiuxtdsogavh(){}")


def plain(value: Any) -> Any:
    """Strip jeepney's variant pairs recursively: ('s', 'Spotify') -> 'Spotify'.

    A variant parses to (signature, value). Ordinary structs are tuples too, but their first
    member is a valid signature string only when the sender really did send a variant.
    """
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        signature, inner = value
        if signature and set(signature) <= SIGNATURE_CHARS:
            return plain(inner)
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def field(message: Message, name: str) -> Any:
    return message.header.fields.get(HeaderFields[name])


def is_call(message: Message, interface: str, member: str) -> bool:
    """True for a method call to `interface.member`, whoever it is addressed to."""
    return (message.header.message_type == MessageType.method_call
            and field(message, "interface") == interface
            and field(message, "member") == member)


class BusError(Exception):
    """A D-Bus error reply."""

    def __init__(self, message: Message) -> None:
        self.name = field(message, "error_name")
        self.data = message.body
        super().__init__(f"{self.name}: {self.data}")


class BusClient:
    """A session-bus connection with replies matched by serial and everything else queued."""

    def __init__(self, connection, queue_size: int = 256) -> None:
        self.connection = connection
        self.incoming: asyncio.Queue[Message] = asyncio.Queue(queue_size)
        self.pending: dict[int, asyncio.Future] = {}
        self.closed = asyncio.Event()
        self.error: BaseException | None = None
        self.dropped = 0

    @property
    def unique_name(self) -> str | None:
        return self.connection.unique_name

    async def send(self, message: Message, serial: int | None = None) -> None:
        await self.connection.send(message, serial=serial)

    async def call(self, message: Message, timeout: float = 5.0) -> tuple:
        """Send a method call, wait for the reply, return its body (raising BusError)."""
        loop = asyncio.get_running_loop()
        serial = next(self.connection.outgoing_serial)
        future: asyncio.Future = loop.create_future()
        self.pending[serial] = future
        try:
            await self.connection.send(message, serial=serial)
            reply = await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(serial, None)
        if reply.header.message_type is MessageType.error:
            raise BusError(reply)
        return reply.body

    async def call_method(self, address: DBusAddress, member: str, signature: str | None = None,
                          body: tuple = (), timeout: float = 5.0) -> tuple:
        return await self.call(new_method_call(address, member, signature, body), timeout=timeout)

    async def add_match(self, rule) -> None:
        await self.call_method(DBUS, "AddMatch", "s", (rule.serialise() if hasattr(rule, "serialise") else rule,))

    async def run(self) -> BaseException | None:
        """Read until the connection closes; returns the exception that ended it, if any."""
        try:
            while True:
                message = await self.connection.receive()
                serial = field(message, "reply_serial")
                future = self.pending.pop(serial, None) if serial is not None else None
                if future is not None:
                    if not future.done():
                        future.set_result(message)
                    continue
                try:
                    self.incoming.put_nowait(message)
                except asyncio.QueueFull:
                    self.dropped += 1
                    log.warning("bus queue full; dropped a %s (%d so far)", field(message, "member"), self.dropped)
        except (EOFError, ConnectionError, OSError, asyncio.IncompleteReadError) as exc:
            self.error = exc
        except asyncio.CancelledError:
            raise
        finally:
            self.closed.set()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(self.error or ConnectionError("bus connection closed"))
            self.pending.clear()
        return self.error

    async def serve(self, handle: Callable[[Message], Awaitable[None]]) -> None:
        """Hand every queued message to `handle`; one bad message must not end the loop."""
        while True:
            message = await self.incoming.get()
            try:
                await handle(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a odd peer must not take the watcher down
                log.warning("could not handle %s.%s: %s", field(message, "interface"), field(message, "member"), exc)

    async def close(self) -> None:
        try:
            await self.connection.close()
        except (OSError, RuntimeError):
            pass


async def open_session_bus(queue_size: int = 256) -> BusClient:
    """Connect to the session bus and say Hello (jeepney does the handshake)."""
    return BusClient(await open_dbus_connection("SESSION"), queue_size=queue_size)
