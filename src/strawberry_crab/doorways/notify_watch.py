#!/usr/bin/env python3
"""Notification doorway: watch desktop notifications on D-Bus and tell strawberryd.

GNOME Shell is the notification daemon here, so there is no dunst script hook. Instead a
monitor connection (org.freedesktop.DBus.Monitoring.BecomeMonitor) sees every
org.freedesktop.Notifications.Notify call as it goes past. GNOME still shows the
notification; this only listens (WIRING.md §4).

    Notify(app_name, replaces_id, app_icon, summary, body, actions, hints, expire_timeout)
      -> POST /event {"source": "notification", "app": ..., "title": summary, "body": body, "urgency": ...}

What gets forwarded is decided by [notifications] in ~/.config/strawberry/config.toml:
ignored apps, an urgency floor, which apps' message bodies may leave the watcher, and a short
coalescing window so twenty Slack pings become one "20 notifications" event.

    python -m strawberry_crab.doorways.notify_watch [--daemon http://127.0.0.1:8770] [--config FILE] [--log-level DEBUG]

Two rules for the monitor connection, both learned the hard way:

* it is a **second, private connection** and it may only listen. A monitor that sends a
  message is disconnected by the bus, so the daemon is reached over HTTP, never from here.
* every Notify crosses the bus **twice** on this desktop (the app sends it to a relay, the
  relay re-sends it to the shell with a new sender and serial), so repeats are dropped by
  content, which is all the two copies share.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from jeepney import DBusAddress, MatchRule, new_method_call

from ..bus import is_call, open_session_bus, plain
from ..config import NotificationsConfig, load
from ..paths import xdg_data_home
from ..client import DaemonClient, configure_logging, stop_on_signals

log = logging.getLogger("notify_watch")

URGENCY_NAMES = {0: "low", 1: "normal", 2: "critical"}
URGENCY_RANK = {"low": 0, "normal": 1, "critical": 2}
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
NOTIFICATIONS_IFACE = "org.freedesktop.Notifications"
MATCH_RULE = MatchRule(type="method_call", interface=NOTIFICATIONS_IFACE, member="Notify")
DBUS = DBusAddress("/org/freedesktop/DBus", bus_name="org.freedesktop.DBus",
                   interface="org.freedesktop.DBus.Monitoring")


# --- pure parts (unit-tested without a bus) --------------------------------------

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


def parse_notify(args: tuple | list, max_body_chars: int = 1000) -> dict[str, Any]:
    """Turn the Notify() argument tuple into a flat dict."""
    app_name, replaces_id, app_icon, summary, body, _actions, hints, _timeout = args
    hints = {key: plain(value) for key, value in dict(hints or {}).items()}
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
        "app_icon": str(app_icon or ""),
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


# --- app icons ------------------------------------------------------------------

ICON_EXTS = (".svg", ".png")
ICON_SIZES = ("scalable", "512x512", "256x256", "128x128", "96x96", "64x64", "48x48")


def icon_search_dirs() -> list[Path]:
    dirs: list[Path] = []
    for base in (xdg_data_home() / "icons", Path("/usr/share/icons"), Path("/var/lib/snapd/desktop/icons")):
        for theme in ("hicolor", "Yaru", "Adwaita"):
            for size in ICON_SIZES:
                dirs.append(base / theme / size / "apps")
        dirs.append(base)
    dirs.append(Path("/usr/share/pixmaps"))
    return dirs


def resolve_icon(app_icon: str, desktop_entry: str, app_name: str, search_dirs: list[Path] | None = None,
                 desktop_icon: Any = None) -> str | None:
    """Best-effort path to the notifying app's icon, or None.

    app_icon may already be a path (notify-send -i /x.png). Otherwise try the name the
    .desktop file declares, then the desktop-entry id, then the app name, across the
    usual icon directories. Without Gtk we do the theme walk by hand.
    """
    if app_icon:
        raw = app_icon[7:] if app_icon.startswith("file://") else app_icon
        if raw.startswith("/") and Path(raw).is_file():
            return raw
    declared = desktop_icon(desktop_entry) if desktop_icon and desktop_entry else None
    if declared and declared.startswith("/"):
        return declared if Path(declared).is_file() else None  # snaps declare a full path in Icon=
    names: list[str] = []
    for candidate in (app_icon, declared, desktop_entry, app_name.lower(), app_name.lower().replace(" ", "-")):
        if candidate and "/" not in candidate and candidate not in names:
            names.append(candidate)
    for directory in search_dirs if search_dirs is not None else icon_search_dirs():
        if not directory.is_dir():
            continue
        for name in names:
            for ext in ICON_EXTS:
                path = directory / f"{name}{ext}"
                if path.is_file():
                    return str(path)
    return None


def desktop_dirs() -> list[Path]:
    """Where .desktop files live, in XDG order (GLib's search path, without GLib)."""
    home_data = xdg_data_home()
    raw = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    dirs = [home_data] + [Path(part) for part in raw.split(":") if part]
    dirs.append(Path("/var/lib/snapd/desktop"))
    return [d / "applications" for d in dirs]


def desktop_icon_name(desktop_entry: str, dirs: list[Path] | None = None) -> str | None:
    """The Icon= of an app's .desktop file, read by hand (PyGObject is not in the venv)."""
    if not desktop_entry:
        return None
    name = desktop_entry if desktop_entry.endswith(".desktop") else f"{desktop_entry}.desktop"
    for directory in dirs if dirs is not None else desktop_dirs():
        path = directory / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        in_entry = False
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("["):
                if in_entry:
                    break              # a later group's Icon= belongs to an action, not the app
                in_entry = line == "[Desktop Entry]"
            elif in_entry and line.startswith("Icon="):
                return line[5:].strip() or None
    return None


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


# --- the bus side ---------------------------------------------------------------

class Watcher:
    def __init__(self, daemon: str, cfg: NotificationsConfig) -> None:
        self.daemon = DaemonClient(daemon)
        self.cfg = cfg
        self.batch: list[dict[str, Any]] = []
        self.deduper = Deduper()
        self.flush_task: asyncio.Task | None = None
        self.seen = 0

    async def run(self, stopping: asyncio.Event) -> int:
        """Become a monitor, then read until told to stop or the bus hangs up."""
        client = await open_session_bus()
        reader = asyncio.ensure_future(client.run())
        try:
            # BecomeMonitor is a plain method call (`asu`: the rules, and a flags word that
            # must be 0). After the reply this connection may only listen: a monitor that
            # sends anything is disconnected by the bus, which is why the daemon is reached
            # over HTTP and why this connection is ours alone.
            await client.call(new_method_call(DBUS, "BecomeMonitor", "asu", ([MATCH_RULE.serialise()], 0)))
            log.info("monitoring notifications -> %s (ignore %s, min urgency %s, bodies %s%s, coalesce %.1fs)",
                     self.daemon.url, self.cfg.ignore_apps or "none", self.cfg.min_urgency, self.cfg.body,
                     f" {self.cfg.body_apps}" if self.cfg.body_apps else "", self.cfg.coalesce_s)
            consumer = asyncio.ensure_future(client.serve(self.handle))
            stop = asyncio.ensure_future(stopping.wait())
            await asyncio.wait([reader, consumer, stop], return_when=asyncio.FIRST_COMPLETED)
            for task in (consumer, stop):
                task.cancel()
            if reader.done():
                # The bus drops a monitor that misbehaves, and takes the socket with it at
                # logout. Exiting non-zero lets the tray (or systemd) start us again.
                log.error("the bus closed the monitor connection (%s); exiting", client.error)
                return 3
            return 0
        finally:
            if self.flush_task and not self.flush_task.done():
                self.flush_task.cancel()
            reader.cancel()
            await client.close()

    async def handle(self, message) -> None:
        if is_call(message, NOTIFICATIONS_IFACE, "Notify"):
            self.on_notify(message.body)

    def on_notify(self, args) -> None:
        self.seen += 1
        n = parse_notify(args, self.cfg.max_body_chars)
        if self.deduper.seen(n):
            log.debug("relay copy of %r/%r ignored", n["app"], n["title"])
            return
        reason = allowed(n, self.cfg)
        if reason:
            log.debug("dropped %r/%r: %s", n["app"], n["title"], reason)
            return
        n["icon"] = resolve_icon(n["app_icon"], n["desktop_entry"], n["app"], desktop_icon=desktop_icon_name)
        # Never the body in a log line, whatever the mode: its length is enough to debug with.
        log.info("notification app=%r title=%r urgency=%s body_len=%d (%s) icon=%s", n["app"], n["title"],
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


async def watch(daemon: str, cfg: NotificationsConfig) -> int:
    stopping = asyncio.Event()
    stop_on_signals(stopping)
    return await Watcher(daemon, cfg).run(stopping)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="desktop notifications -> strawberryd doorway")
    parser.add_argument("--daemon", default=None, help="default: the config's [daemon] host and port")
    parser.add_argument("--config", type=Path, default=None, help="settings file (default: the XDG one)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    configure_logging(args.log_level)
    # Read once, at start: the tray restarts this doorway when it changes [notifications] (§14).
    config = load(args.config)
    daemon = args.daemon or f"http://{config.daemon.host}:{config.daemon.port}"
    try:
        return asyncio.run(watch(daemon, config.notifications))
    except KeyboardInterrupt:
        return 0
    except (ConnectionError, OSError, RuntimeError) as exc:
        log.error("no session bus (%s)", exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())
