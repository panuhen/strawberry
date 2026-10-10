"""Hand gestures and touch, as the user tunes them (WIRING.md §17, §25, §26): `input.toml`, the recent ring, the
live view and practice mode.

    ~/.config/strawberry/input.toml   [gestures] and [touch], written by the Brain UI's Input tab (or by hand)
       │
       ├─ config.load ─ overlay(): its sections replace config.toml's, whole; `[gestures] enabled` stays in
       │                config.toml (the tray's Hand gestures row, `strawberry gestures on|off`)
       └─ InputStore (the daemon) ─ stat once a second, parsed again when it changed; a file that does not check
                                    out keeps the settings she has, is logged once and shown in the Input tab

The file is checked by the config loader's own functions (config._apply, _validate_touch, _validate_gestures),
so the Input tab, the daemon, the gesture watcher and `strawberry gestures status` read it the same way.

`Recent` is the last twenty gestures and touch actions with what each did or why it did nothing (names, times
and outcomes only), `Live` hands the Input tab's live view what the watcher posts (a hand's few numbers and
gesture names, never a frame) while the tab is open, and `InputDesk.practice` (runtime only, never saved) lets
gestures and touches be recognised and shown while they do nothing.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import time
import tomllib
from collections import deque
from pathlib import Path
from typing import Any

from . import paths
from .config import (ConfigError, GesturesConfig, TouchConfig, _apply, _touch_table, _validate_gestures,
                     _validate_touch, default_path)

log = logging.getLogger("strawberryd.input")

FILE_NAME = "input.toml"
SECTIONS = ("gestures", "touch")
MAX_TEXT = 20_000
# The [gestures] keys input.toml holds, in the order it is written: everything but `enabled`, which is the
# tray's switch and stays in config.toml.
GESTURE_KEYS = ("camera", "watch", "fps", "idle_fps", "hold_ms", "arming", "armed_s", "approvals", "zone",
                "min_size", "hand_hz", "map")
TOUCH_KEYS = ("target_s", "cooldown_s", "actions")
HEADER = """\
# Hand gestures and touch (WIRING.md §25, §26). Written by the Brain UI's Input tab (`strawberry ui`); edit it
# by hand if you like: she reads it again within a second. A section here replaces the same section of
# config.toml whole (keys left out take their defaults), except `[gestures] enabled`, which stays in
# config.toml: the tray's Hand gestures row and `strawberry gestures on|off` turn the camera on and off.
"""


def input_file(config_path: Path | None = None) -> Path:
    """input.toml, beside the config file (`~/.config/strawberry/input.toml`)."""
    return (config_path or default_path()).with_name(FILE_NAME)


# --- reading -------------------------------------------------------------------------------


def parse(text: str, base: Any) -> tuple[GesturesConfig, TouchConfig, frozenset[str]]:
    """input.toml's text as the settings it gives: [gestures] (its `enabled` False here: the caller keeps
    config.toml's), [touch], and which of the two the file has. `base` is the Config the rest comes from (its
    tools and approvals decide a touch action's tier). ConfigError says what does not check out, as the loader
    says it for config.toml."""
    if len(text) > MAX_TEXT:
        raise ConfigError(f"{FILE_NAME} is too long (at most {MAX_TEXT} characters)")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{FILE_NAME} does not parse: {exc}") from None
    unknown = sorted(set(data) - set(SECTIONS))
    if unknown:
        raise ConfigError(f"{FILE_NAME} holds [gestures] and [touch] only; not {', '.join(f'[{u}]' for u in unknown)}")
    gestures, touch = GesturesConfig(), TouchConfig()
    for section, target, keys in (("gestures", gestures, GESTURE_KEYS), ("touch", touch, TOUCH_KEYS)):
        values = data.get(section)
        if values is None:
            continue
        if not isinstance(values, dict):
            raise ConfigError(f"[{section}] must be a table")
        if section == "gestures" and "enabled" in values:
            raise ConfigError("gestures.enabled stays in config.toml (the tray's Hand gestures row, `strawberry "
                              f"gestures on|off`); take it out of {FILE_NAME}")
        if section == "touch":
            values = _touch_table(values)
        extra = sorted(set(values) - set(keys))
        if extra:
            raise ConfigError(f"{section}: unknown keys {extra}")
        _apply(section, target, values)
    probe = dataclasses.replace(base, gestures=gestures, touch=touch)
    _validate_touch(probe)
    _validate_gestures(gestures)
    return gestures, probe.touch, frozenset(s for s in SECTIONS if s in data)


def overlay(config: Any, config_path: Path | None) -> None:
    """config.load's last step: input.toml beside the config file, when there, replaces [gestures] (but its
    `enabled`) and [touch]. One that does not check out changes nothing; `config.input_error` says why (the
    log says only that it is not used: a map key can name an entity and a gesture)."""
    path = input_file(config_path)
    config.input_path, config.input_error = None, ""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except (OSError, UnicodeDecodeError) as exc:
        config.input_error = f"{FILE_NAME} could not be read ({type(exc).__name__})"
        log.warning("input: %s could not be read (%s); [gestures] and [touch] from config.toml", path,
                    type(exc).__name__)
        return
    try:
        gestures, touch, present = parse(text, config)
    except ConfigError as exc:
        config.input_error = str(exc)
        log.warning("input: %s does not check out and is not used (strawberry gestures status, or the Brain UI's "
                    "Input tab, says why); [gestures] and [touch] from config.toml", path)
        return
    config.input_path = path
    if "gestures" in present:
        gestures.enabled = config.gestures.enabled
        config.gestures = gestures
    if "touch" in present:
        config.touch = touch


# --- writing -------------------------------------------------------------------------------


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(float(value)) if isinstance(value, float) else repr(value)
    return json.dumps(str(value), ensure_ascii=False)      # a JSON string is a TOML basic string


def _key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key, ensure_ascii=False)


def render(gestures: GesturesConfig, touch: TouchConfig) -> str:
    """The two sections as input.toml says them: every key written out, the map after its section."""
    lines = [HEADER, "[gestures]"]
    lines += [f"{key} = {_value(getattr(gestures, key))}" for key in GESTURE_KEYS if key != "map"]
    lines += ["", "[gestures.map]"]
    lines += [f"{_key(key)} = {_value(action)}" for key, action in gestures.map.items()]
    lines += ["", "[touch]", f"target_s = {_value(touch.target_s)}", f"cooldown_s = {_value(touch.cooldown_s)}"]
    for entity, kinds in touch.actions.items():
        lines += [f"{_key(entity)}.{_key(kind)} = {_value(action)}" for kind, action in kinds.items()]
    return "\n".join(lines) + "\n"


def from_view(data: Any, base: Any) -> tuple[str, GesturesConfig, TouchConfig]:
    """The Input tab's settings ({"gestures": {...}, "touch": {...}}) as input.toml's text, checked by `parse`
    (the loader's functions). ConfigError when they do not check out."""
    if not isinstance(data, dict) or set(data) - set(SECTIONS):
        raise ConfigError("settings must be {\"gestures\": {...}, \"touch\": {...}}")
    gestures_in, touch_in = data.get("gestures") or {}, data.get("touch") or {}
    if not isinstance(gestures_in, dict) or not isinstance(touch_in, dict):
        raise ConfigError("gestures and touch must be objects")
    extra = sorted((set(gestures_in) - set(GESTURE_KEYS)) | {f"touch.{k}" for k in set(touch_in) - set(TOUCH_KEYS)})
    if extra:
        raise ConfigError(f"unknown settings {extra}")
    gestures, touch = GesturesConfig(), TouchConfig()
    _apply("gestures", gestures, gestures_in)
    actions = touch_in.get("actions", {})
    if not isinstance(actions, dict) or not all(isinstance(v, dict) for v in actions.values()):
        raise ConfigError("touch.actions must be {entity: {kind: action}}")
    _apply("touch", touch, {k: v for k, v in touch_in.items() if k != "actions"})
    touch.actions = {str(e): dict(kinds) for e, kinds in actions.items()}
    # The text is what is checked, so what is saved is exactly what was checked (and what she reads back).
    for key, action in gestures.map.items():
        if not isinstance(key, str) or not isinstance(action, str):
            raise ConfigError("gestures.map must be {gesture: action}")
    for entity, kinds in touch.actions.items():
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in kinds.items()):
            raise ConfigError(f"touch.{entity} must be {{kind: action}}")
    text = render(gestures, touch)
    checked_gestures, checked_touch, _ = parse(text, base)
    return text, checked_gestures, checked_touch


def save(text: str, path: Path) -> Path | None:
    """`text` as input.toml (atomic), the file it replaces kept as input.toml.bak. Returns the backup's path."""
    backup = None
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        paths.write_atomic(backup, path.read_text(encoding="utf-8"))
    paths.write_atomic(path, text)
    os.utime(path)   # a new mtime even within the file system's timestamp granularity
    return backup


# --- the daemon's side -----------------------------------------------------------------------


class Recent:
    """The last `keep` gestures and touch actions and what came of each, in memory, for the Input tab: names,
    times and outcomes only (never a hand, a label or a sentence). The same thing again within COALESCE_S is
    one row with a count, so a drag or a stream of unmapped touches does not push the rest out."""

    COALESCE_S = 2.0

    def __init__(self, keep: int = 20, clock=time.time, publish=None) -> None:
        self.items: deque[dict[str, Any]] = deque(maxlen=keep)
        self.clock = clock
        self.publish = publish
        self.next_id = 1

    def add(self, source: str, name: str, outcome: str, action: str = "", why: str = "") -> dict[str, Any]:
        """`outcome`: did, running, failed, ignored, practice. `why` is a fixed reason (not_mapped, cooldown,
        not_armed, …)."""
        now = self.clock()
        last = self.items[-1] if self.items else None
        if (last is not None and now - last["at"] < self.COALESCE_S and outcome != "running"
                and (last["source"], last["name"], last["outcome"], last["action"], last["why"])
                == (source, name, outcome, action, why)):
            last["count"] += 1
            last["at"] = round(now, 3)
            self._tell(last)
            return last
        entry = {"id": self.next_id, "at": round(now, 3), "source": source, "name": name, "outcome": outcome,
                 "action": action, "why": why, "count": 1}
        self.next_id += 1
        self.items.append(entry)
        self._tell(entry)
        return entry

    def finish(self, entry: dict[str, Any], outcome: str, why: str = "") -> None:
        """A running action's end: did or failed (and why)."""
        entry["outcome"], entry["why"] = outcome, why
        self._tell(entry)

    def view(self) -> list[dict[str, Any]]:
        return [dict(e) for e in reversed(self.items)]

    def _tell(self, entry: dict[str, Any]) -> None:
        if self.publish is not None:
            self.publish("recent", dict(entry))


class Live:
    """What the Input tab's live view gets while it is open: `hand` (the watcher's numbers), `gesture` (a name,
    phase and progress, and what was done) and `recent` rows. While a view is open the watcher is asked for the
    hand (GestureDesk.state's `wanted`). Nothing here is kept."""

    MAX_VIEWERS = 4
    QUEUE = 64

    def __init__(self) -> None:
        self.viewers: set[asyncio.Queue] = set()

    @property
    def watching(self) -> bool:
        return bool(self.viewers)

    def subscribe(self) -> asyncio.Queue | None:
        if len(self.viewers) >= self.MAX_VIEWERS:
            return None
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.QUEUE)
        self.viewers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.viewers.discard(queue)

    def publish(self, kind: str, data: dict[str, Any]) -> None:
        for queue in list(self.viewers):
            try:
                queue.put_nowait((kind, data))
            except asyncio.QueueFull:     # a slow page: it misses a frame of the hand, nothing else waits
                pass


WATCHER_HEADER = "X-Strawberry-Watcher"
_WATCHER = re.compile(r"^camera=(open|closed); fps=(\d{1,3}(?:\.\d{1,2})?); cpu=(\d{1,4}(?:\.\d)?)$")


def parse_watcher(value: str | None) -> dict[str, Any] | None:
    """The watcher's own report on each of its requests: `camera=open; fps=4.0; cpu=5.1` (the frames it read a
    second and its share of one CPU core, %). None when absent or not exactly that."""
    if not value or len(value) > 64:
        return None
    match = _WATCHER.match(value.strip())
    if match is None:
        return None
    return {"camera": match.group(1) == "open", "fps": float(match.group(2)), "cpu": float(match.group(3))}


class InputStore:
    """input.toml in the daemon: looked at once a second (`run`) and right after the Input tab saves it; parsed
    again when its mtime or size changed. A file that checks out is used at once (the gesture map and tuning,
    the touch map and times; `enabled` is kept). One that does not keeps the settings she has; `status()` says
    why, for the Input tab. A file that goes away gives config.toml's sections back."""

    EVERY_S = 1.0

    def __init__(self, daemon: Any) -> None:
        self.daemon = daemon
        config = daemon.config
        self.path = input_file(config.path)
        self.seen = self._stat()
        self.error = getattr(config, "input_error", "")
        self.in_use = getattr(config, "input_path", None) is not None
        self.task: asyncio.Task | None = None

    def _stat(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.get_running_loop().create_task(self.run())

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.EVERY_S)
            try:
                self.check()
            except Exception as exc:   # noqa: BLE001 - a stat or a parse that failed oddly: say it, keep going
                log.warning("input: checking %s failed (%s)", FILE_NAME, type(exc).__name__)

    def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            self.task = None

    def check(self, force: bool = False) -> bool:
        """Read input.toml again if it changed (or `force`). True when the settings in use changed."""
        seen = self._stat()
        if seen == self.seen and not force:
            return False
        self.seen = seen
        if seen is None:
            if not self.in_use and not self.error:
                return False
            self.error = ""
            if self.in_use:
                self.in_use = False
                fallback = self._config_toml()
                if fallback is not None:
                    self.apply(fallback.gestures, fallback.touch)
                log.info("input: %s is gone; [gestures] and [touch] from config.toml", FILE_NAME)
                return True
            return False
        try:
            text = self.path.read_text(encoding="utf-8")
            gestures, touch, present = parse(text, self.daemon.config)
        except (ConfigError, OSError, UnicodeDecodeError) as exc:
            new = str(exc) if isinstance(exc, ConfigError) else f"{FILE_NAME} could not be read ({type(exc).__name__})"
            if new != self.error:
                log.warning("input: %s does not check out; keeping the settings she has (the Brain UI's Input tab "
                            "says why)", FILE_NAME)
            self.error = new
            return False
        if present != frozenset(("gestures", "touch")):
            fallback = self._config_toml()
            if fallback is not None:
                gestures = gestures if "gestures" in present else fallback.gestures
                touch = touch if "touch" in present else fallback.touch
            else:
                gestures = gestures if "gestures" in present else self.daemon.config.gestures
                touch = touch if "touch" in present else self.daemon.config.touch
        self.error, self.in_use = "", True
        self.apply(gestures, touch)
        log.info("input: %s applied (%d gesture mapping(s), %d touch mapping(s))", FILE_NAME, len(gestures.map),
                 sum(len(kinds) for kinds in touch.actions.values()))
        return True

    def _config_toml(self) -> Any:
        from .config import load

        try:
            return load(self.daemon.config.path or default_path(), env={}, inputs=False)
        except ConfigError:
            log.warning("input: config.toml does not load; keeping the [gestures] and [touch] she has")
            return None

    def apply(self, gestures: GesturesConfig, touch: TouchConfig) -> None:
        daemon = self.daemon
        gestures = dataclasses.replace(gestures, enabled=daemon.config.gestures.enabled)
        daemon.config.gestures = gestures
        daemon.config.touch = touch
        daemon.targets.ttl_s = touch.target_s

    def text(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    def status(self) -> dict[str, Any]:
        return {"path": str(self.path), "exists": self.seen is not None, "in_use": self.in_use,
                "source": FILE_NAME if self.in_use else "config.toml", "error": self.error or None}


class InputDesk:
    """The daemon's input bits for the Input tab (`Daemon.input`): the file, the recent ring, the live view,
    what the watcher last said about itself, and practice mode."""

    def __init__(self, daemon: Any) -> None:
        self.live = Live()
        self.recent = Recent(publish=self.live.publish)
        self.store = InputStore(daemon)
        self.practice = False             # runtime only: gestures and touches are shown and do nothing
        self.watcher: dict[str, Any] | None = None
        self.watcher_at = -1e9

    def heard(self, header: str | None) -> None:
        """A request from the gesture watcher: what it said about itself (the camera, its frame rate, its CPU)."""
        report = parse_watcher(header)
        if report is not None:
            self.watcher, self.watcher_at = report, time.monotonic()

    def watcher_view(self) -> dict[str, Any]:
        age = time.monotonic() - self.watcher_at
        if self.watcher is None or age > 5.0:
            return {"heard": False, "age_s": None if self.watcher is None else round(age, 1)}
        return {"heard": True, "age_s": round(age, 1)} | self.watcher
