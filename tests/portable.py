"""Helpers that let a test mean the same thing on Linux and on Windows."""

from __future__ import annotations

import sys
from pathlib import Path

NAMES = ("CONFIG", "DATA", "STATE", "CACHE")


def point_dirs(monkeypatch, base: Path, names: tuple[str, ...] = NAMES) -> None:
    """Throwaway dirs: the XDG ones as base/config, base/data, ..., and the Windows known folders
    that paths.py reads there instead (base/appdata for the config, base/localappdata for data
    and state)."""
    for name in names:
        monkeypatch.setenv(f"XDG_{name}_HOME", str(base / name.lower()))
    monkeypatch.setenv("APPDATA", str(base / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(base / "localappdata"))


def program(path: Path, body: bytes = b"#!/bin/sh\nexit 0\n") -> Path:
    """A file that counts as a program here (widgetbin.is_runnable): executable on Linux; on
    Windows an .exe also starts with the "MZ" of a PE image. Not meant to be run."""
    if sys.platform == "win32" and path.suffix.lower() == ".exe":
        body = b"MZ" + body
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(0o755)
    return path


def sh_script(path: Path, text: str) -> Path:
    """An executable sh script with LF line ends (Git's sh on Windows chokes on CRLF)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, newline="\n")
    path.chmod(0o755)
    return path
