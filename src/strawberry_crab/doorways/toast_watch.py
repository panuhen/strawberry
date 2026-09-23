#!/usr/bin/env python3
"""Notification doorway on Windows: read other apps' toasts and tell strawberryd.

The counterpart of notify_watch.py (WIRING.md §4), with the same event to the daemon:

    a new toast in the notification centre
      -> POST /event {"source": "notification", "app": ..., "title": ..., "body": ..., "urgency": "normal"}

Windows keeps every app's toasts in its notification centre, and
`Windows.UI.Notifications.Management.UserNotificationListener` reads them (WinRT, the `winrt-*`
packages): the app's display name and logo, and the toast's ToastGeneric text elements. The first
text element is the title, the rest joined are the body. Windows only shows the toast; this only
reads.

Everything after the reading is notify_watch's too (notifications.py): [notifications] in the
config decides which apps are forwarded, which apps' bodies may leave the watcher, and the
coalescing window; repeats are dropped by content; the body is never logged, only its length.
The daemon runs the privacy checks (privacy.py) on what arrives, whichever system sent it.

    python -m strawberry_crab.doorways.toast_watch [--daemon http://127.0.0.1:8770] [--config FILE] [--log-level DEBUG]

Three things the listener insists on, found on Windows 11 (build 26200):

* **Access** is the user's, in Settings > Privacy & security > Notifications ("Let apps access
  your notifications"). An unpackaged Python process may use the listener: GetAccessStatus says
  Allowed where that switch is on, and RequestAccessAsync answers Allowed without a prompt. Where
  it says Denied the watcher logs where to turn it on and exits 3.
* **No change events without package identity.** Adding a NotificationChanged handler from an
  unpackaged process fails with 0x80070490 (Element not found), so the watcher polls
  GetNotificationsAsync(Toast) every POLL_S (one read: about 160 ms, 4 ms of CPU) and diffs by
  the toast's Id. What is already there when it starts is not news.
* **Toasts carry no urgency or category**, and an update to a toast is a new toast: urgency is
  "normal", category empty, replaces_id 0.

winrt is imported in `request_listener` and `read_logo` alone, so this module imports on any
system; the tests hand `Toasts` a fake listener, and tests/conftest.py makes the real one
unreachable.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..client import configure_logging, stop_on_signals
from ..config import NotificationsConfig, load
from ..paths import app_icons_dir
from ..smtc import app_key
from .notifications import Forwarder, clean

log = logging.getLogger("toast_watch")

POLL_S = 1.0            # how often the notification centre is read
FAILURES_TO_EXIT = 30   # reads in a row that failed (access turned off meanwhile): exit, be restarted
LOGO_SIZE = 64          # px asked of the app's logo; Windows picks the nearest it has
TOAST_BINDING = "ToastGeneric"   # KnownNotificationBindings.ToastGeneric
# UserNotificationListenerAccessStatus
ACCESS = {0: "unspecified", 1: "allowed", 2: "denied"}
WHERE_TO_ALLOW = "Settings > Privacy & security > Notifications, 'Let apps access your notifications'"


class ToastError(Exception):
    """The notification centre cannot be read (not Windows, winrt missing, access, COM)."""


@dataclass(frozen=True)
class Toast:
    """One toast as read from the listener. Its text is left out of repr: it may be private."""
    id: int
    app: str                                          # AppDisplayInfo.DisplayName, as the user sees it
    app_id: str                                       # AppInfo.AppUserModelId
    texts: tuple[str, ...] = field(default=(), repr=False)
    display_info: Any = field(default=None, repr=False, compare=False)   # for the logo


def parse_toast(toast: Toast, max_body_chars: int = 1000) -> dict[str, Any]:
    """The same flat dict notify_watch.parse_notify makes, from a toast.

    `desktop_entry` holds the app's short key from its AppUserModelId (smtc.app_key: `slack`,
    `msteams`), so ignore_apps, only_apps and body_apps match by display name or key, as they
    match by app name or desktop entry on Linux.
    """
    key = app_key(toast.app_id)
    title = toast.texts[0] if toast.texts else ""
    return {
        "app": clean(toast.app, 80) or key,
        "desktop_entry": key,
        "title": clean(title, 200),
        "body": clean("\n".join(toast.texts[1:]), max_body_chars),
        "urgency": "normal",
        "category": "",
        "replaces_id": 0,
        "app_icon": "",
    }


# ----------------------------------------------------------------------------- the listener


async def request_listener() -> tuple[Any, Any]:
    """Windows' listener and the `NotificationKinds.Toast` to read with. ToastError where there
    is none."""
    try:
        from winrt.windows.ui.notifications import NotificationKinds
        from winrt.windows.ui.notifications.management import UserNotificationListener
    except ImportError as exc:
        raise ToastError("reading notifications needs Windows and its winrt packages") from exc
    try:
        return UserNotificationListener.current, NotificationKinds.TOAST
    except Exception as exc:  # noqa: BLE001 - a COM error; the same sentence either way
        raise ToastError(f"no notification listener ({type(exc).__name__}: {exc})") from exc


async def read_logo(display_info: Any, size: int = LOGO_SIZE) -> bytes | None:
    """The app's logo as image bytes, or None (desktop apps usually have none here)."""
    from winrt.windows.foundation import Size
    from winrt.windows.storage.streams import Buffer, InputStreamOptions

    reference = display_info.get_logo(Size(size, size))
    if reference is None:
        return None
    stream = await reference.open_read_async()
    try:
        buffer = await stream.read_async(Buffer(stream.size), stream.size, InputStreamOptions.NONE)
        return bytes(buffer)
    finally:
        stream.close()


def read_toast(notification: Any) -> Toast:
    """A UserNotification as a Toast: its Id, its app, and its ToastGeneric text elements."""
    info = notification.app_info
    binding = notification.notification.visual.get_binding(TOAST_BINDING)
    texts = tuple(element.text for element in binding.get_text_elements()) if binding is not None else ()
    display = info.display_info
    return Toast(int(notification.id), display.display_name or "", info.app_user_model_id or "", texts, display)


class Toasts:
    """The notification centre: one listener, asked for access once, then read on each poll."""

    def __init__(self, listener: Any = None, kinds: Any = 1) -> None:
        self.listener = listener
        self.kinds = kinds

    async def open(self) -> None:
        if self.listener is None:
            self.listener, self.kinds = await request_listener()

    async def access(self) -> str:
        """'allowed', 'denied' or 'unspecified'. Asks (RequestAccessAsync) when not yet allowed;
        that may show the user a prompt once."""
        try:
            status = ACCESS.get(int(self.listener.get_access_status()), "unspecified")
            if status != "allowed":
                status = ACCESS.get(int(await self.listener.request_access_async()), "unspecified")
        except Exception as exc:  # noqa: BLE001
            raise ToastError(f"could not ask for access ({type(exc).__name__}: {exc})") from exc
        return status

    async def read(self) -> list[Toast]:
        try:
            notifications = await self.listener.get_notifications_async(self.kinds)
        except Exception as exc:  # noqa: BLE001 - access turned off, the service restarting
            raise ToastError(f"could not read the notifications ({type(exc).__name__}: {exc})") from exc
        toasts = []
        for notification in notifications:
            try:
                toasts.append(read_toast(notification))
            except Exception as exc:  # noqa: BLE001 - one toast whose app is gone; the rest still count
                log.debug("a toast could not be read (%s)", type(exc).__name__)
        return toasts


# ----------------------------------------------------------------------------- the doorway


def icon_name(app_id: str) -> str:
    return re.sub(r"[^\w.-]+", "_", app_key(app_id) or "app") + ".png"


class Watcher(Forwarder):
    """The polling reader; filtering, batching and posting are Forwarder's (notifications.py)."""

    def __init__(self, daemon: str, cfg: NotificationsConfig, toasts: Toasts | None = None,
                 icons_dir: Path | None = None) -> None:
        super().__init__(daemon, cfg, log)
        self.toasts = toasts or Toasts()
        self.icons_dir = icons_dir
        self.known: set[int] | None = None       # the Ids in the last read; None before the first
        self.icons: dict[str, str | None] = {}   # AppUserModelId -> the logo's path, once per app
        self.failures = 0

    async def poll_once(self) -> None:
        """Read the notification centre and offer every toast that was not there last time."""
        try:
            toasts = await self.toasts.read()
        except ToastError as exc:
            self.failures += 1
            if self.failures == 1:
                log.warning("%s", exc)
            return
        self.failures = 0
        ids = {toast.id for toast in toasts}
        if self.known is None:          # what is already there when we start is not news
            self.known = ids
            return
        fresh = [toast for toast in toasts if toast.id not in self.known]
        self.known = ids
        for toast in fresh:
            n = parse_toast(toast, self.cfg.max_body_chars)
            n["icon"] = await self.logo(toast)
            self.offer(n)

    def icon_for(self, n: dict[str, Any]) -> str | None:
        return n.get("icon")            # fetched in poll_once, where it can be awaited

    async def logo(self, toast: Toast) -> str | None:
        """The app's logo as a PNG in the cache dir, written once per app per run."""
        if toast.app_id in self.icons:
            return self.icons[toast.app_id]
        path = None
        try:
            data = await read_logo(toast.display_info) if toast.display_info is not None else None
            if data and data.startswith(b"\x89PNG"):
                directory = self.icons_dir or app_icons_dir()
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / icon_name(toast.app_id)
                target.write_bytes(data)
                path = str(target)
        except Exception as exc:  # noqa: BLE001 - no badge is fine; nothing else changes
            log.debug("no logo for %r (%s)", toast.app, type(exc).__name__)
        self.icons[toast.app_id] = path
        return path

    async def run(self, stopping: asyncio.Event) -> int:
        try:
            await self.toasts.open()
            access = await self.toasts.access()
        except ToastError as exc:
            log.error("%s; exiting", exc)
            return 3
        if access != "allowed":
            log.error("Windows says %s to reading notifications; turn it on in %s", access, WHERE_TO_ALLOW)
            return 3
        try:
            await self.poll_once()
            log.info("reading notifications -> %s (ignore %s, min urgency %s, bodies %s%s, coalesce %.1fs, "
                     "%d there now, polled every %.1fs)",
                     self.daemon.url, self.cfg.ignore_apps or "none", self.cfg.min_urgency, self.cfg.body,
                     f" {self.cfg.body_apps}" if self.cfg.body_apps else "", self.cfg.coalesce_s,
                     len(self.known or ()), POLL_S)
            while not stopping.is_set():
                try:
                    await asyncio.wait_for(stopping.wait(), POLL_S)
                except asyncio.TimeoutError:
                    pass
                if stopping.is_set():
                    break
                await self.poll_once()
                if self.failures >= FAILURES_TO_EXIT:
                    log.error("the notifications could not be read %d times in a row; exiting", self.failures)
                    return 3
            return 0
        finally:
            self.cancel_flush()


async def watch(daemon: str, cfg: NotificationsConfig) -> int:
    stopping = asyncio.Event()
    stop_on_signals(stopping)
    return await Watcher(daemon, cfg).run(stopping)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Windows notifications (toasts) -> strawberryd doorway")
    parser.add_argument("--daemon", default=None, help="default: the config's [daemon] host and port")
    parser.add_argument("--config", type=Path, default=None, help="settings file (default: %%APPDATA%%\\strawberry)")
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


if __name__ == "__main__":
    sys.exit(main())
