"""The one place that says which operating systems she runs on.

Linux (D-Bus, MPRIS, PipeWire, systemd) and Windows 10 2004 or later (WinRT, WASAPI, the
notification area, the Startup folder) have their own doorways and tray; the dependencies
install anywhere. Each entry point calls `require_supported()` first, so a macOS install stops
with a plain sentence instead of a traceback from whatever backend it lacks. A port adds its
platform here and picks its own doorways and tray.

STRAWBERRY_ALLOW_UNSUPPORTED=1 lets the entry points run anyway. It is for working on a port
and for the test suite on such a system, not for users: whatever has no backend on that system
fails in its own way.
"""

from __future__ import annotations

import os
import sys

SUPPORTED = ("linux", "win32")
OVERRIDE_ENV = "STRAWBERRY_ALLOW_UNSUPPORTED"


def unsupported_message(platform: str | None = None) -> str | None:
    platform = sys.platform if platform is None else platform
    if platform.startswith(SUPPORTED):
        return None
    return (
        f"Strawberry runs on Linux and Windows only for now (this is {platform}). "
        "Other systems need their own notification, media and tray code, which is not written yet."
    )


def overridden() -> bool:
    return os.environ.get(OVERRIDE_ENV, "").strip() == "1"


def require_supported() -> None:
    message = unsupported_message()
    if message and not overridden():
        print(message, file=sys.stderr)
        raise SystemExit(1)
