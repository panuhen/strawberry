"""What both notification doorways do with a notification once they have read it (WIRING.md §4).

notify_watch.py (Linux, a D-Bus monitor) and toast_watch.py (Windows, UserNotificationListener)
only read: each turns what its system hands over into the same flat dict,

    {"app", "desktop_entry", "title", "body", "urgency", "category", "replaces_id", "app_icon"}

and gives it to `Forwarder.offer`. From there on everything is shared, so [notifications] means
the same on both systems: repeats dropped by content, ignored apps, the urgency floor, which apps'
bodies may leave the watcher (`body` / `body_apps`), the coalescing window and its summary, the
log line (never the body, only its length) and the POST to the daemon, which runs the privacy
checks (privacy.py) on what arrives. Nothing here imports jeepney or winrt.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from typing import Any

from ..client import DaemonClient
from ..config import NotificationsConfig

URGENCY_RANK = {"low": 0, "normal": 1, "critical": 2}
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")

# How long a POST /event may take before the watcher stops waiting for the reply. Longer than the
# client's usual 2 s: a body whose privacy check timed out is checked again once the gate's model
# has loaded ([gate] retry_timeout_s, 15 s by default), and the reply only comes after that. The
# post runs in a thread, so the reader keeps reading meanwhile (WIRING.md §4).
POST_TIMEOUT_S = 30.0


def clean(text: str, limit: int) -> str:
    """Notification bodies may carry markup and entities; the model should see plain words."""
    text = html.unescape(TAG_RE.sub(" ", text or ""))
    text = SPACE_RE.sub(" ", text).strip()
    if len(text) <= limit:
        return text
    # Cut at the last word boundary that keeps most of the text, and say so with an ellipsis.
    cut = text[:limit - 1]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:.-") + "…"


class Deduper:
    """Drop repeats of the same notification inside a short window.

    On the Linux desktop every Notify crosses the bus twice: the app sends it to a relay, and the
    relay re-sends it to GNOME Shell with a new sender and serial. Content is the only thing
    the two copies share, so content is the key.
    """

    def __init__(self, window_s: float = 1.5) -> None:
        self.window_s = window_s
        self.recent: list[tuple[tuple, float]] = []

    def seen(self, n: dict[str, Any], now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        key = (n["app"], n["title"], n["body"], n["urgency"], n["replaces_id"])
        self.recent = [(k, t) for k, t in self.recent if now - t < self.window_s]
        if any(k == key for k, _ in self.recent):
            return True
        self.recent.append((key, now))
        return False


def allowed(n: dict[str, Any], cfg: NotificationsConfig) -> str | None:
    """None if the notification should be forwarded, otherwise the reason it is dropped."""
    app = (n["app"] or n["desktop_entry"]).lower()
    if app == "strawberry":
        return "own notification"
    if cfg.only_apps and app not in {a.lower() for a in cfg.only_apps}:
        return f"{n['app']!r} not in only_apps"
    if app in {a.lower() for a in cfg.ignore_apps}:
        return f"{n['app']!r} in ignore_apps"
    if URGENCY_RANK[n["urgency"]] < URGENCY_RANK.get(cfg.min_urgency, 0):
        return f"urgency {n['urgency']} below {cfg.min_urgency}"
    if n["replaces_id"] and cfg.ignore_replacements:
        return "replaces an earlier notification (progress/update)"
    return None


def forwards_body(n: dict[str, Any], cfg: NotificationsConfig) -> bool:
    return cfg.mode_for(n["app"], n.get("desktop_entry", "")) != "off"


def to_event(n: dict[str, Any], cfg: NotificationsConfig) -> dict[str, str]:
    event = {"source": "notification", "app": n["app"], "title": n["title"], "urgency": n["urgency"]}
    # Body mode "off" (the default): the body stays here and never crosses even localhost HTTP.
    # Otherwise it goes to the daemon, which drops it if it looks sensitive (WIRING.md §4).
    if forwards_body(n, cfg) and n["body"]:
        event["body"] = n["body"]
    if n.get("category"):
        event["category"] = n["category"]
    if n.get("icon"):
        event["icon"] = n["icon"]
    return event


def summarise(batch: list[dict[str, Any]], cfg: NotificationsConfig) -> dict[str, str]:
    """Several notifications inside the coalescing window become one event."""
    if len(batch) == 1:
        return to_event(batch[0], cfg)
    apps = [n["app"] for n in batch if n["app"]]
    distinct = list(dict.fromkeys(apps))
    urgency = max((n["urgency"] for n in batch), key=lambda u: URGENCY_RANK[u])
    items = []
    for n in batch[:5]:
        piece = n["title"] if len(distinct) == 1 else f"{n['app']}: {n['title']}" if n["app"] else n["title"]
        if piece:
            items.append(piece)
    if len(batch) > 5:
        items.append(f"and {len(batch) - 5} more")
    if len(distinct) == 1:
        event = {"source": "notification", "app": distinct[0], "title": f"{len(batch)} notifications from {distinct[0]}",
                 "body": " · ".join(items), "urgency": urgency}
        icon = next((n.get("icon") for n in batch if n.get("icon")), None)
        if icon:
            event["icon"] = icon
        return event
    return {"source": "notification", "app": "several apps", "title": f"{len(batch)} notifications",
            "body": " · ".join(items), "urgency": urgency}


class Forwarder:
    """A notification doorway's shared half: filter, log, batch, post.

    A subclass reads its system's notifications, turns each into the flat dict and calls
    `offer`; `icon_for` is its hook for the app's icon (a path on disk, or None).
    """

    def __init__(self, daemon: str, cfg: NotificationsConfig, log: logging.Logger) -> None:
        self.daemon = DaemonClient(daemon, timeout=POST_TIMEOUT_S)
        self.cfg = cfg
        self.log = log
        self.batch: list[dict[str, Any]] = []
        self.deduper = Deduper()
        self.flush_task: asyncio.Task | None = None
        self.seen = 0

    def icon_for(self, n: dict[str, Any]) -> str | None:
        return None

    def offer(self, n: dict[str, Any]) -> None:
        self.seen += 1
        if self.deduper.seen(n):
            self.log.debug("repeat of %r/%r ignored", n["app"], n["title"])
            return
        reason = allowed(n, self.cfg)
        if reason:
            self.log.debug("dropped %r/%r: %s", n["app"], n["title"], reason)
            return
        n["icon"] = self.icon_for(n)
        # Never the body in a log line, whatever the mode: its length is enough to debug with.
        self.log.info("notification app=%r title=%r urgency=%s body_len=%d (%s) icon=%s", n["app"], n["title"],
                      n["urgency"], len(n["body"]), "forwarded" if forwards_body(n, self.cfg) else "kept here",
                      n["icon"] or "-")
        self.batch.append(n)
        if self.flush_task and not self.flush_task.done():
            self.flush_task.cancel()
        self.flush_task = asyncio.ensure_future(self._flush_after(self.cfg.coalesce_s))

    async def _flush_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        batch, self.batch = self.batch, []
        if batch:
            await self.daemon.post("/event", summarise(batch, self.cfg))

    def cancel_flush(self) -> None:
        if self.flush_task and not self.flush_task.done():
            self.flush_task.cancel()
