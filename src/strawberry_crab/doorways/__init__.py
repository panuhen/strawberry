"""Event producers that run beside the daemon, one process each (WIRING.md §4, §4b, §4c).

    strawberry-doorway mpris_watch      media players (MPRIS)
    strawberry-doorway notify_watch     desktop notifications (D-Bus monitor)
    strawberry-doorway beat_watch       the player's audio stream -> tempo (PipeWire + numpy)

`python -m strawberry_crab.doorways.<name>` is the same thing; the tray uses that form so every
child runs on its own interpreter. The D-Bus watchers speak jeepney (pure Python) and all
three reach the daemon over plain HTTP: a doorway that trips must never take the websocket
down with it.
"""

from __future__ import annotations

import importlib
import sys

# In the order the tray starts them.
DOORWAYS = ("mpris_watch", "notify_watch", "beat_watch")


def main(argv: list[str] | None = None) -> int:
    """`strawberry-doorway <name> [args]`: run one doorway in this process."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help") or argv[0] not in DOORWAYS:
        stream = sys.stdout if argv and argv[0] in ("-h", "--help") else sys.stderr
        print(f"usage: strawberry-doorway {{{','.join(DOORWAYS)}}} [--daemon URL] [--help]", file=stream)
        return 0 if stream is sys.stdout else 2
    name, rest = argv[0], argv[1:]
    module = importlib.import_module(f"{__name__}.{name}")
    # Each doorway parses sys.argv itself (it is also a `python -m` entry point).
    sys.argv = [f"strawberry-doorway {name}", *rest]
    return module.main() or 0


if __name__ == "__main__":
    sys.exit(main())
