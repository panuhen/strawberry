"""`strawberry gestures status | on | off | arm | fetch`: the camera doorway by hand (WIRING.md §26).

    status   whether gestures are on, what is missing (MediaPipe, the model) and what the daemon says
    on, off  `[gestures] enabled` in config.toml (as the tray's row writes it), then the daemon re-reads it;
             the camera doorway follows the file by itself
    arm      command mode on for `[gestures] armed_s` (with `watch = "armed"`, the camera opens for it);
             bind it to a key as you would `strawberry listen`
    fetch    the recogniser's model into the data dir, its sha256 checked
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from . import gestures


def run(action: str, port: int, config_path: Path | None = None, say: Callable[[str], None] = print) -> int:
    from .client import DaemonClient
    from .config import ConfigError, default_path, load

    path = config_path or default_path()
    daemon = DaemonClient(f"http://127.0.0.1:{port}")
    if action == "fetch":
        try:
            gestures.fetch(say=say)
        except (gestures.FetchError, OSError) as exc:
            say(f"not fetched: {exc}")
            return 1
        return 0
    if action == "arm":
        if not daemon.post_sync("/command", {"command": "arm_gestures"}):
            say("strawberryd is not running")
            return 1
        say("armed")
        return 0
    if action in ("on", "off"):
        from . import configedit

        try:
            configedit.set_value(path, "gestures.enabled", action == "on")
        except (ConfigError, OSError) as exc:
            say(f"not written: {exc}")
            return 1
        say(f"gestures {action} ({path})")
        if not daemon.post_sync("/command", {"command": "reload_gestures"}):
            say("strawberryd is not running; it reads the setting when it starts")
        if action == "on":
            for line in missing():
                say(line)
        return 0
    try:
        loaded = load(path, env={})
    except ConfigError as exc:
        say(f"config: {exc}")
        return 1
    cfg = loaded.gestures
    from .inputs import input_file

    if loaded.input_error:
        say(f"{input_file(path)} is not used: {loaded.input_error}")
    elif loaded.input_path is not None:
        say(f"settings and maps: {loaded.input_path} (the Brain UI's Input tab writes it)")
    say(f"gestures: {'on' if cfg.enabled else 'off'} (watch {cfg.watch}, camera {cfg.camera or 'the first'})")
    say("map: " + ", ".join(f"{k} -> {v}" for k, v in cfg.map.items()))
    problems = missing()
    for line in problems:
        say(line)
    state = daemon.get_sync("/gesture")
    if state is None:
        say("daemon: not answering")
    else:
        wanted = [k for k, v in (state.get("wanted") or {}).items() if v]
        say(f"daemon: {'on' if state.get('enabled') else 'off'}, armed {state.get('armed_s', 0)}s, "
            f"bodies want {', '.join(wanted) or 'nothing'}")
    return 1 if cfg.enabled and problems else 0


def missing() -> list[str]:
    """What gestures still need, as lines that say how to get it."""
    lines = []
    deps = gestures.mediapipe_missing()
    if deps:
        lines.append(f"missing: {deps}")
    if not gestures.model_ready():
        lines.append(f"missing: the model ({gestures.model_file()}): strawberry gestures fetch")
    return lines
