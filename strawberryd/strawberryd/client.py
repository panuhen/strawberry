"""Talking to the daemon over HTTP: the doorways' `/event` posts and the tray's `/health`.

Short-lived calls only. The daemon owns the one websocket to the widget, so everything else
— a notification, a track change, a tray click — arrives as a plain POST (WIRING.md §0).
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("strawberryd.client")


def loggable(payload: dict) -> str:
    """What a log line may say about a payload: its source and app, its keys, the body's length.

    Never the body, and never the title: a notification's text stays out of the journal (§4).
    """
    parts = [f"{key}={payload[key]!r}" for key in ("source", "app", "command") if key in payload]
    parts.append("keys=" + ",".join(sorted(payload)))
    if "body" in payload:
        parts.append(f"body_len={len(str(payload['body']))}")
    return "{" + " ".join(parts) + "}"


class DaemonClient:
    def __init__(self, url: str, timeout: float = 2.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.forwarded = 0

    def post_sync(self, path: str, payload: dict) -> bool:
        """True when the daemon answered (even with a rejection); False when it was unreachable."""
        request = urllib.request.Request(
            self.url + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                reply = json.loads(response.read() or b"{}")
            self.forwarded += 1
            log.info("%s %s -> %s widget(s)", path, loggable(payload), reply.get("sent", "?"))
            return True
        except urllib.error.HTTPError as exc:
            log.warning("%s rejected %s: %s", path, loggable(payload), exc.read().decode(errors="replace")[:200])
            return True
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.warning("strawberryd unreachable at %s (%s)", self.url, exc)
            return False

    def get_sync(self, path: str) -> dict[str, Any] | None:
        """The parsed JSON, or None when the daemon is not answering."""
        try:
            with urllib.request.urlopen(self.url + path, timeout=self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            return None

    async def post(self, path: str, payload: dict) -> bool:
        """The same call off the event loop, so a slow daemon cannot stall the bus reader."""
        return await asyncio.to_thread(self.post_sync, path, payload)

    async def get(self, path: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_sync, path)


def stop_on_signals(stopping: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
