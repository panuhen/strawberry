#!/usr/bin/env python3
"""Moved: the notification doorway now lives in the package, on jeepney.

    strawberryd/strawberryd/doorways/notify_watch.py

This shim stays so an old systemd unit, a script, or muscle memory keeps working; it hands
over to the venv's Python, which has jeepney (the system Python does not).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

MODULE = "strawberryd.doorways.notify_watch"
ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    python = ROOT / "strawberryd" / ".venv" / "bin" / "python"
    if not python.is_file():
        print(f"{python} is missing; run: cd {ROOT}/strawberryd && uv sync --inexact --group gpu", file=sys.stderr)
        return 2
    os.execv(str(python), [str(python), "-m", MODULE, *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(main())
