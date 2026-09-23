"""Event producers that run beside the daemon, one process each (WIRING.md §4, §4b, §4c).

    strawberry-doorway mpris_watch      media players (MPRIS)                             Linux
    strawberry-doorway notify_watch     desktop notifications (D-Bus monitor)             Linux
    strawberry-doorway beat_watch       the player's audio stream -> tempo (PipeWire)     Linux
    strawberry-doorway smtc_watch       media players (System Media Transport Controls)   Windows

`python -m strawberry_crab.doorways.<name>` is the same thing; the tray uses that form so every
child runs on its own interpreter. The D-Bus watchers speak jeepney (pure Python), the Windows
one winrt, and all of them reach the daemon over plain HTTP: a doorway that trips must never
take the websocket down with it. `for_system()` says which ones this system runs.
"""

from __future__ import annotations

import importlib
import sys

from .. import osguard

# In the order the tray starts them.
DOORWAYS = ("mpris_watch", "notify_watch", "beat_watch")
# The Windows port so far (WINDOWS.md): media only; notifications and the beat come later.
WINDOWS_DOORWAYS = ("smtc_watch",)


def for_system(platform: str | None = None) -> tuple[str, ...]:
    """The doorways this system has: the Linux three, the Windows ones, or none elsewhere."""
    platform = platform or sys.platform
    if platform.startswith("linux"):
        return DOORWAYS
    if platform == "win32":
        return WINDOWS_DOORWAYS
    return ()


def main(argv: list[str] | None = None) -> int:
    """`strawberry-doorway <name> [args]`: run one doorway in this process."""
    osguard.require_supported()
    argv = list(sys.argv[1:] if argv is None else argv)
    known = (*DOORWAYS, *WINDOWS_DOORWAYS)
    if not argv or argv[0] in ("-h", "--help") or argv[0] not in known:
        stream = sys.stdout if argv and argv[0] in ("-h", "--help") else sys.stderr
        print(f"usage: strawberry-doorway {{{','.join(known)}}} [--daemon URL] [--help]", file=stream)
        return 0 if stream is sys.stdout else 2
    name, rest = argv[0], argv[1:]
    module = importlib.import_module(f"{__name__}.{name}")
    # Each doorway parses sys.argv itself (it is also a `python -m` entry point).
    sys.argv = [f"strawberry-doorway {name}", *rest]
    return module.main() or 0


if __name__ == "__main__":
    sys.exit(main())
