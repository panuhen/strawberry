"""Where Strawberry keeps things on disk: the XDG base directories, in one place (WIRING.md §15).

    config   $XDG_CONFIG_HOME/strawberry/   (~/.config/strawberry/)        config.toml
    data     $XDG_DATA_HOME/strawberry/     (~/.local/share/strawberry/)   voices/, later the widget binary
    state    $XDG_STATE_HOME/strawberry/    (~/.local/state/strawberry/)   tray.json, pidfiles, logs, token caches

An unset, empty or relative XDG variable falls back to the default, as the spec says. Nothing
here creates a directory; the caller that writes does that.

The one thing that is not XDG is the source checkout: until PACKAGING.md step 4 ships the
widget as a binary, `strawberry widget` runs the Godot project in `widget/` next to this
package's source, and an installed package without a checkout has no widget.
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path

APP = "strawberry"


def _xdg(variable: str, default: str) -> Path:
    value = os.environ.get(variable, "")
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / default


def xdg_config_home() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config")


def xdg_data_home() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share")


def xdg_state_home() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state")


def config_dir() -> Path:
    return xdg_config_home() / APP


def config_file() -> Path:
    return config_dir() / "config.toml"


def data_dir() -> Path:
    return xdg_data_home() / APP


def voices_dir() -> Path:
    """Piper voices (`<name>.onnx` + `.onnx.json`), unless `[speech] voices_dir` says otherwise."""
    return data_dir() / "voices"


def state_dir() -> Path:
    return xdg_state_home() / APP


def tray_state_file() -> Path:
    """What the tray writes about its children; `strawberry status` reads it."""
    return state_dir() / "tray.json"


def systemd_user_dir() -> Path:
    return xdg_config_home() / "systemd" / "user"


def autostart_file() -> Path:
    return xdg_config_home() / "autostart" / "strawberry.desktop"


def git_hooks_dir() -> Path:
    """The global hooks directory `strawberry git-hooks install` points core.hooksPath at."""
    return xdg_config_home() / "git" / "hooks"


def godot_user_dir() -> Path:
    """The widget's `user://` today (Godot's own choice under XDG data; step 4 moves it)."""
    return xdg_data_home() / "godot" / "app_userdata" / "Strawberry"


def package_file(*parts: str) -> Path:
    """A file shipped inside the package (tray icons), as a real path on disk.

    Wheels install unzipped, so the resource is a file already; `as_file` would only matter
    for a zipped import, which this package does not support (the icons are read by path).
    """
    return Path(str(resources.files(__package__ or APP).joinpath(*parts)))


def icons_dir() -> Path:
    return package_file("assets", "icons")


def checkout_root() -> Path | None:
    """The source checkout this package runs from, or None when it is installed on its own.

    `src/strawberry/paths.py` -> the repo root, recognised by the widget project in it.
    """
    root = Path(__file__).resolve().parents[2]
    if (root / "widget" / "project.godot").is_file() and (root / "pyproject.toml").is_file():
        return root
    return None


def widget_project() -> Path | None:
    root = checkout_root()
    return root / "widget" if root is not None else None
