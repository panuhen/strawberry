"""The one place that says which operating systems she runs on.

The doorways, the tray and the service are Linux-only (D-Bus, MPRIS, PipeWire, systemd), while
the dependencies install anywhere. Each entry point calls `require_supported()` first, so a
Windows or macOS install stops with a plain sentence instead of a traceback from jeepney.
A port adds its platform here and picks its own doorways and tray.
"""

from __future__ import annotations

import sys

SUPPORTED = ("linux",)


def unsupported_message(platform: str | None = None) -> str | None:
    platform = sys.platform if platform is None else platform
    if platform.startswith(SUPPORTED):
        return None
    return (
        f"Strawberry runs on Linux only for now (this is {platform}). "
        "Windows and macOS need their own notification, media and tray code, which is not written yet."
    )


def require_supported() -> None:
    message = unsupported_message()
    if message:
        print(message, file=sys.stderr)
        raise SystemExit(1)
