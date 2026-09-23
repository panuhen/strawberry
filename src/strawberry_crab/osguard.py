"""The one place that says which operating systems she runs on.

The doorways, the tray and the service are Linux-only (D-Bus, MPRIS, PipeWire, systemd), while
the dependencies install anywhere. Each entry point calls `require_supported()` first, so a
Windows or macOS install stops with a plain sentence instead of a traceback from jeepney.
A port adds its platform here and picks its own doorways and tray.

STRAWBERRY_ALLOW_UNSUPPORTED=1 lets the entry points run anyway. It is for working on a port
(WINDOWS.md) and for the test suite on such a system, not for users: whatever has no backend on
that system yet fails in its own way.
"""

from __future__ import annotations

import os
import sys

SUPPORTED = ("linux",)
OVERRIDE_ENV = "STRAWBERRY_ALLOW_UNSUPPORTED"


def unsupported_message(platform: str | None = None) -> str | None:
    platform = sys.platform if platform is None else platform
    if platform.startswith(SUPPORTED):
        return None
    return (
        f"Strawberry runs on Linux only for now (this is {platform}). "
        "Windows and macOS need their own notification, media and tray code, which is not written yet."
    )


def overridden() -> bool:
    return os.environ.get(OVERRIDE_ENV, "").strip() == "1"


def require_supported() -> None:
    message = unsupported_message()
    if message and not overridden():
        print(message, file=sys.stderr)
        raise SystemExit(1)
