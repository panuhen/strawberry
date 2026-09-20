#!/usr/bin/env python3
"""Notification doorway: watch desktop notifications on D-Bus and tell strawberryd.

GNOME Shell is the notification daemon here, so there is no dunst script hook. Instead a
monitor connection (org.freedesktop.DBus.Monitoring.BecomeMonitor) sees every
org.freedesktop.Notifications.Notify call as it goes past. GNOME still shows the
notification; this only listens (WIRING.md §4).

    Notify(app_name, replaces_id, app_icon, summary, body, actions, hints, expire_timeout)
      -> POST /event {"source": "notification", "app": ..., "title": summary, "body": body, "urgency": ...}

What gets forwarded is decided by [notifications] in ~/.config/strawberry/config.toml:
ignored apps, an urgency floor, whether message bodies are included, and a short
coalescing window so twenty Slack pings become one "20 notifications" event.

    doorways/notify_watch.py [--daemon http://127.0.0.1:8770] [--log-level DEBUG]
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "strawberryd"))
from strawberryd.config import NotificationsConfig, load  # noqa: E402  (stdlib-only module)

log = logging.getLogger("notify_watch")

URGENCY_NAMES = {0: "low", 1: "normal", 2: "critical"}
URGENCY_RANK = {"low": 0, "normal": 1, "critical": 2}
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
MATCH_RULE = "type='method_call',interface='org.freedesktop.Notifications',member='Notify'"


# --- pure parts (unit-tested without a bus) --------------------------------------

def clean(text: str, limit: int) -> str:
    """Notification bodies may carry markup and entities; the model should see plain words."""
    text = html.unescape(TAG_RE.sub(" ", text or ""))
    text = SPACE_RE.sub(" ", text).strip()
    return text[:limit]


def parse_notify(args: tuple | list, max_body_chars: int = 200) -> dict[str, Any]:
    """Turn the Notify() argument tuple into a flat dict."""
    app_name, replaces_id, _icon, summary, body, _actions, hints, _timeout = args
    hints = dict(hints or {})
    urgency_raw = hints.get("urgency", 1)
    try:
        urgency = URGENCY_NAMES.get(int(urgency_raw), "normal")
    except (TypeError, ValueError):
        urgency = "normal"
    desktop_entry = str(hints.get("desktop-entry", "") or "")
    return {
        "app": clean(str(app_name or ""), 80) or desktop_entry,
        "desktop_entry": desktop_entry,
        "title": clean(str(summary or ""), 200),
        "body": clean(str(body or ""), max_body_chars),
        "urgency": urgency,
        "category": str(hints.get("category", "") or ""),
        "replaces_id": int(replaces_id or 0),
    }


class Deduper:
    """Drop repeats of the same notification inside a short window.

    On this desktop every Notify crosses the bus twice: the app sends it to a relay, and the
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


def to_event(n: dict[str, Any], cfg: NotificationsConfig) -> dict[str, str]:
    event = {"source": "notification", "app": n["app"], "title": n["title"], "urgency": n["urgency"]}
    if cfg.include_body and n["body"]:
        event["body"] = n["body"]
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
        return {"source": "notification", "app": distinct[0], "title": f"{len(batch)} notifications from {distinct[0]}",
                "body": " · ".join(items), "urgency": urgency}
    return {"source": "notification", "app": "several apps", "title": f"{len(batch)} notifications",
            "body": " · ".join(items), "urgency": urgency}


# --- the bus side ---------------------------------------------------------------

class Watcher:
    def __init__(self, daemon: str, cfg: NotificationsConfig) -> None:
        self.daemon = daemon.rstrip("/")
        self.cfg = cfg
        self.batch: list[dict[str, Any]] = []
        self.deduper = Deduper()
        self.flush_id = 0
        self.seen = 0
        self.forwarded = 0

    def start(self) -> None:
        import gi

        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gio, GLib

        self.GLib = GLib
        # A monitor connection can only listen, so it is a second, private connection.
        address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
        flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        self.conn = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
        self.conn.set_exit_on_close(False)
        self.conn.connect("closed", self._closed)
        self.conn.add_filter(self._filter)
        self.conn.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus.Monitoring", "BecomeMonitor",
            GLib.Variant("(asu)", ([MATCH_RULE], 0)), None, Gio.DBusCallFlags.NONE, 3000, None,
        )
        log.info("monitoring notifications -> %s (ignore %s, min urgency %s, bodies %s, coalesce %.1fs)",
                 self.daemon, self.cfg.ignore_apps or "none", self.cfg.min_urgency,
                 "included" if self.cfg.include_body else "dropped", self.cfg.coalesce_s)

    def _closed(self, _conn, remote_peer_vanished: bool, error) -> None:
        # A monitor that sends anything is dropped by the bus; if that ever happens we must
        # not sit here deaf. Exit non-zero so the launcher (or you) notices.
        log.error("bus closed the monitor connection (peer vanished=%s, %s); exiting", remote_peer_vanished, error)
        self.GLib.idle_add(lambda: sys.exit(3))

    def _filter(self, _conn, message, incoming: bool):
        from gi.repository import Gio

        try:
            if incoming and message.get_message_type() == Gio.DBusMessageType.METHOD_CALL \
                    and message.get_interface() == "org.freedesktop.Notifications" and message.get_member() == "Notify":
                body = message.get_body()
                if body is not None:
                    # Hand off to the main loop; filters run on the connection's worker thread.
                    self.GLib.idle_add(self.on_notify, body.unpack())
                # Consume it. Passing it on would make GDBusConnection answer a method call that
                # was never for us, and a monitor that sends gets disconnected by the bus.
                return None
        except Exception as exc:  # a bad message must never kill the listener
            log.warning("could not read a notification: %s", exc)
        return message

    def on_notify(self, args) -> bool:
        self.seen += 1
        n = parse_notify(args, self.cfg.max_body_chars)
        if self.deduper.seen(n):
            log.debug("relay copy of %r/%r ignored", n["app"], n["title"])
            return False
        reason = allowed(n, self.cfg)
        if reason:
            log.debug("dropped %r/%r: %s", n["app"], n["title"], reason)
            return False
        log.info("notification app=%r title=%r urgency=%s", n["app"], n["title"], n["urgency"])
        self.batch.append(n)
        if self.flush_id:
            self.GLib.source_remove(self.flush_id)
        self.flush_id = self.GLib.timeout_add(int(self.cfg.coalesce_s * 1000), self.flush)
        return False

    def flush(self) -> bool:
        self.flush_id = 0
        batch, self.batch = self.batch, []
        if batch:
            self.post("/event", summarise(batch, self.cfg))
        return False

    def post(self, path: str, payload: dict) -> None:
        request = urllib.request.Request(
            self.daemon + path, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                reply = json.loads(response.read() or b"{}")
            self.forwarded += 1
            log.info("%s %s -> %s widget(s)", path, payload, reply.get("sent", "?"))
        except urllib.error.HTTPError as exc:
            log.warning("%s rejected %s: %s", path, payload, exc.read().decode(errors="replace")[:200])
        except (urllib.error.URLError, TimeoutError) as exc:
            log.warning("strawberryd unreachable at %s (%s); will keep watching", self.daemon, exc)


def main() -> None:
    config = load()
    parser = argparse.ArgumentParser(description="desktop notifications -> strawberryd doorway")
    parser.add_argument("--daemon", default=f"http://{config.daemon.host}:{config.daemon.port}")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")

    from gi.repository import GLib

    watcher = Watcher(args.daemon, config.notifications)
    watcher.start()
    loop = GLib.MainLoop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, lambda *_: (loop.quit(), False)[1])
    loop.run()


if __name__ == "__main__":
    sys.exit(main())
