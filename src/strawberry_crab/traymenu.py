"""The tray's menu and what its rows do, on every system (WIRING.md §14).

The menu is her right-click menu (widget/menu.gd), preferences and all, plus show/hide and a
status row. Clicks become daemon calls: `POST /command` broadcasts `{"command": ...,
"value": ...}` to the widgets, `GET /health` tells us what she is doing, and the check marks
are read back from the widget's own settings file so the two menus agree.

One row is not the widget's: "Message bodies" writes `[notifications] body` into config.toml
(configedit.py: comments kept, validated, backed up), then tells the daemon to re-read
[notifications] and restarts the notification doorway child, the two readers of that setting
(§4, §14).

`TrayCore` is all of that without an icon. The icon is the front end's: a StatusNotifierItem on
Linux (tray.py), a notification-area icon on Windows (wintray.py). Each builds its menu from
`menu_items()` and hands clicks to `TrayCore.clicked`.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any

from .client import DaemonClient
from .supervisor import NOTIFY_CHILDREN, Children
from . import configedit, paths

log = logging.getLogger("strawberryd.tray")

HEALTH_EVERY_S = 2.0      # how often the status row is refreshed
QUIET_S = 3600            # "Quiet for an hour"

# The same choices as her right-click menu (widget/menu.gd, widget/skin_palettes.gd). They are
# repeated here because the tray is Python and the menu is GDScript; a test compares the two.
VOLUMES = (0.25, 0.5, 0.75, 1.0)
SLEEP_MINUTES = (5.0, 1.0, 10.0, 30.0, 0.0)          # 0 = never
SKINS = (("strawberry", "Strawberry"), ("peach", "Peach"), ("blueberry", "Blueberry"),
         ("mint", "Mint"), ("lavender", "Lavender"))
# [notifications] body in config.toml (config.BODY_MODES). Not in her right-click menu, which holds
# only the widget's own preferences.
BODY_CHOICES = (("off", "Off"), ("react", "React"), ("glance", "Glance"))
BODY_OVERRIDES_LABEL = "Per-app overrides in config"

STATE_LABELS = {
    "idle": "Idle",
    "listening": "Listening",
    "thinking": "Thinking…",
    "talking": "Talking",
    "dancing": "Dancing",
}
NO_DAEMON = "strawberryd is not running"
EARS_LOADING_LABEL = "loading whisper"
SHOW_HIDE_ID = 2          # the row a plain click on the icon runs


# --- the menu, as data ------------------------------------------------------------

@dataclass
class MenuItem:
    id: int
    action: str
    label: str = ""
    enabled: bool = True
    toggle: str = ""              # "checkmark" | "radio" | "" (dbusmenu's toggle-type)
    checked: bool = False
    separator: bool = False
    visible: bool = True
    value: Any = None            # what the action carries: a volume, a skin id, minutes
    children: list["MenuItem"] = dataclass_field(default_factory=list)

    def properties(self) -> dict[str, tuple[str, Any]]:
        """The dbusmenu property map, values as variants."""
        if self.separator:
            return {"type": ("s", "separator")} | ({} if self.visible else {"visible": ("b", False)})
        props: dict[str, tuple[str, Any]] = {
            "label": ("s", self.label),
            "enabled": ("b", self.enabled),
            "visible": ("b", self.visible),
        }
        if self.toggle:
            props["toggle-type"] = ("s", self.toggle)
            props["toggle-state"] = ("i", 1 if self.checked else 0)
        if self.children:
            props["children-display"] = ("s", "submenu")
        return props


@dataclass
class TrayState:
    """What the menu shows: her state from the daemon, her preferences from the widget.

    The preferences are read back from the widget's own settings file (read_widget_prefs), so
    the tray's check marks and her right-click menu never drift apart for long.
    """

    state: str = "idle"
    daemon_ok: bool = False
    widget_shown: bool = True
    muted: bool = False
    quiet_until: float = 0.0
    volume: float = 1.0
    skin: str = "strawberry"
    top_hat: bool = False
    always_on_top: bool = True
    sleep_minutes: float = 5.0
    body_mode: str = "off"            # [notifications] body, read from config.toml (read_body_setting)
    body_overrides: bool = False      # the file has a body_apps table
    ears_loading: bool = False        # /health.voice.phase: whisper still loading (a first start downloads it)

    @property
    def quiet(self) -> bool:
        return self.quiet_until > time.time()

    def status_label(self) -> str:
        if not self.daemon_ok:
            return NO_DAEMON
        label = STATE_LABELS.get(self.state, STATE_LABELS["idle"])
        return f"{label} ({EARS_LOADING_LABEL})" if self.ears_loading else label

    def quiet_label(self) -> str:
        left = self.quiet_until - time.time()
        return "Quiet for an hour" if left <= 0 else f"Quiet ({math.ceil(left / 60)} min left)"


def menu_items(state: TrayState) -> list[MenuItem]:
    """Her right-click menu, in the top bar: the same preferences, plus show/hide and a status
    row (WIRING.md §13 for the crab's menu, §14 for this one). Ids are stable, so a host may
    cache them; the submenu rows take 20, 30, 40 and 50 upwards. "Per-app overrides in config"
    and its separator are always there, hidden when body_apps is empty, so no row moves."""
    volume = [MenuItem(20 + i, "volume", f"{int(v * 100)}%", toggle="radio",
                       checked=abs(state.volume - v) < 0.01, value=v) for i, v in enumerate(VOLUMES)]
    skins = [MenuItem(30 + i, "skin", name, toggle="radio", checked=state.skin == skin_id, value=skin_id)
             for i, (skin_id, name) in enumerate(SKINS)]
    sleep = [MenuItem(40 + i, "sleep_after", "Never" if m == 0 else f"{int(m)} minutes", toggle="radio",
                      checked=abs(state.sleep_minutes - m) < 0.01, value=m) for i, m in enumerate(SLEEP_MINUTES)]
    bodies = [MenuItem(51 + i, "body_mode", name, toggle="radio", checked=state.body_mode == mode, value=mode)
              for i, (mode, name) in enumerate(BODY_CHOICES)]
    bodies += [MenuItem(54, "separator", separator=True, visible=state.body_overrides),
               MenuItem(55, "note", BODY_OVERRIDES_LABEL, enabled=False, visible=state.body_overrides)]
    return [
        MenuItem(1, "status", state.status_label(), enabled=False),
        MenuItem(SHOW_HIDE_ID, "hide" if state.widget_shown else "show",
                 "Hide her" if state.widget_shown else "Show her"),
        MenuItem(3, "chat", "Chat with Strawberry…"),
        MenuItem(4, "separator", separator=True),
        MenuItem(5, "mute", "Mute her voice", toggle="checkmark", checked=state.muted),
        MenuItem(6, "quiet", state.quiet_label(), toggle="checkmark", checked=state.quiet),
        MenuItem(7, "submenu", "Voice volume", children=volume),
        MenuItem(8, "submenu", "Skin", children=skins),
        MenuItem(9, "submenu", "Sleep after inactivity", children=sleep),
        MenuItem(10, "sleep_now", "Sleep now"),
        MenuItem(11, "hat", "Top hat", toggle="checkmark", checked=state.top_hat),
        MenuItem(12, "on_top", "Always on top", toggle="checkmark", checked=state.always_on_top),
        MenuItem(50, "submenu", "Message bodies", children=bodies),
        MenuItem(13, "separator", separator=True),
        MenuItem(14, "settings_file", "Settings file…"),
        MenuItem(15, "voices_folder", "Voices folder…"),
        MenuItem(16, "reset_position", "Reset position"),
        MenuItem(17, "separator", separator=True),
        MenuItem(18, "restart", "Restart (applies the settings)"),
        MenuItem(19, "quit", "Quit"),
    ]


def flatten(items: list[MenuItem]) -> list[MenuItem]:
    """Every row, submenu children included: what a click or a property request is looked up in."""
    out: list[MenuItem] = []
    for item in items:
        out.append(item)
        out.extend(flatten(item.children))
    return out


def widget_prefs_path() -> Path:
    """Where the widget keeps its preferences: $XDG_CONFIG_HOME/strawberry/widget.cfg."""
    return paths.widget_prefs_file()


def read_widget_prefs(path: Path | None = None) -> dict[str, Any]:
    """Godot's ConfigFile: INI with quoted strings, true/false and numbers. Keys are unique
    across its sections, so a flat dict is enough for the menu's check marks.

    With no path: the XDG file, or the pre-step-4 one in Godot's user dir until the widget has
    copied it over (it does that on its first start with a display)."""
    if path is None:
        path = widget_prefs_path()
        if not path.exists():
            path = paths.legacy_widget_prefs_file()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("[", ";", "#")) or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        values[key.strip()] = _prefs_value(raw.strip())
    return values


def read_body_setting(path: Path) -> tuple[str, bool] | None:
    """(`[notifications] body`, whether `body_apps` is set) from config.toml, for the radio rows.

    No file: the defaults. A file that does not parse: None, and the menu keeps what it shows.
    tomllib alone, not config.load: this runs every couple of seconds and must not log.
    """
    from .config import BODY_MODES, NotificationsConfig

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return NotificationsConfig().body, False
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    section = data.get("notifications")
    section = section if isinstance(section, dict) else {}
    body = section.get("body")
    if body is None and isinstance(section.get("include_body"), bool):
        body = "react" if section["include_body"] else "off"      # the old key, as config.load reads it
    if body not in BODY_MODES:
        body = NotificationsConfig().body
    apps = section.get("body_apps")
    return body, isinstance(apps, dict) and bool(apps)


def _prefs_value(raw: str) -> Any:
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return raw[1:-1]
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return float(raw)
    except ValueError:
        return raw


def quiet_process_options() -> dict[str, Any]:
    """What a helper process the tray starts needs: on Windows no console window, since the tray
    itself may have none to share (pythonw)."""
    if paths.windows():
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


# --- the tray, without its icon -----------------------------------------------------

class TrayCore:
    """The menu's state and what a click does. A front end shows `items` and calls `clicked`;
    `announce` is where it hears that rows changed."""

    def __init__(self, daemon_url: str, children: Children | None = None, prefs_path: Path | None = None,
                 config_path: Path | None = None) -> None:
        self.daemon = DaemonClient(daemon_url)
        self.children = children
        self.state = TrayState()
        self.prefs_path = prefs_path     # None: widget_prefs_path(), with the legacy fallback
        self.config_path = config_path   # None: config.default_path(), as the daemon reads it
        self.config_seen: tuple[int, int] | None = None     # (mtime_ns, size) last read
        self.items = menu_items(self.state)
        self.revision = 1
        self.stopping = asyncio.Event()

    # --- what a click does ----------------------------------------------------

    async def clicked(self, item_id: int) -> None:
        item = next((i for i in flatten(self.items) if i.id == item_id), None)
        if item is None or not item.enabled or item.action in ("status", "separator", "submenu"):
            return
        log.info("menu: %s%s", item.action, f" {item.value}" if item.value is not None else "")
        await self.activate(item.action, item.value)

    async def activate(self, action: str, value: Any = None) -> None:
        if action in ("show", "hide"):
            if await self.command(action):
                self.state.widget_shown = action == "show"
        elif action in ("chat", "sleep_now", "reset_position"):
            await self.command(action)
        elif action == "mute":
            want = not self.state.muted
            if await self.command("mute", want):
                self.state.muted = want
        elif action == "quiet":
            seconds = 0 if self.state.quiet else QUIET_S
            if await self.command("quiet", seconds):
                self.state.quiet_until = time.time() + seconds if seconds else 0.0
        elif action == "volume":
            if await self.command("volume", float(value)):
                self.state.volume = float(value)
        elif action == "skin":
            if await self.command("skin", str(value)):
                self.state.skin = str(value)
        elif action == "sleep_after":
            if await self.command("sleep_after", float(value)):
                self.state.sleep_minutes = float(value)
        elif action == "hat":
            want = not self.state.top_hat
            if await self.command("hat", want):
                self.state.top_hat = want
        elif action == "on_top":
            want = not self.state.always_on_top
            if await self.command("on_top", want):
                self.state.always_on_top = want
        elif action == "body_mode":
            await self.set_body_mode(str(value))
        elif action == "settings_file":
            await self.open_settings_file()
        elif action == "voices_folder":
            folder = paths.voices_dir()
            folder.mkdir(parents=True, exist_ok=True)
            await self.open_path(folder)
        elif action == "restart":
            # Her menu calls this "Apply settings": everything comes back with the new config.
            if self.children:
                await self.children.restart()
            else:
                log.warning("--no-children: nothing of mine to restart")
        elif action == "quit":
            self.stopping.set()
        await self.publish()

    def show_or_hide(self) -> str:
        """What a plain click on the icon does: the show/hide row's action."""
        return "show" if not self.state.widget_shown else "hide"

    def config_file(self) -> Path:
        from .config import default_path

        return self.config_path or default_path()

    async def set_body_mode(self, mode: str) -> None:
        """Message bodies ▸ off | react | glance: into config.toml, then live (WIRING.md §4, §14).

        1. `[notifications] body = "<mode>"` through configedit: comments kept, validated, the old
           file backed up. Nothing else changes; body_apps overrides stay as they are.
        2. The daemon re-reads [notifications] (POST /command reload_notifications): it decides
           react against glance.
        3. The notify_watch child restarts (it reads the mode at its start): it decides whether a
           body leaves the watcher at all. It is stopped after the daemon has the new mode, so
           turning bodies on never forwards one the daemon would read the old way.
        Logs the mode name and nothing else from the file; says nothing.
        """
        from .config import BODY_MODES, ConfigError

        if mode not in BODY_MODES:
            return
        path = self.config_file()
        try:
            saved = await asyncio.to_thread(configedit.set_value, path, "notifications.body", mode)
        except (ConfigError, OSError) as exc:
            log.warning("message bodies: %s not written (%s)", mode, exc)
            return
        self.state.body_mode = mode
        self.config_seen = None            # read the file again on the next refresh
        log.info("message bodies: %s (written%s)", mode, ", backup kept" if saved else "")
        if not await self.command("reload_notifications"):
            log.warning("message bodies: the daemon is not answering; it reads %s when it starts", mode)
        if self.children is None:
            log.warning("message bodies: --no-children, so restart notify_watch yourself to apply %s", mode)
            return
        for name in NOTIFY_CHILDREN:
            if self.children.restart_child(name):
                log.info("message bodies: restarting %s", name)

    def read_config(self) -> None:
        """The body mode from config.toml, re-read only when the file has changed."""
        path = self.config_file()
        try:
            stat = path.stat()
            seen = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            seen = (0, -1)
        if seen == self.config_seen:
            return
        setting = read_body_setting(path)
        if setting is None:
            return                         # mid-edit or broken: keep what the menu shows, try again later
        self.config_seen = seen
        self.state.body_mode, self.state.body_overrides = setting

    async def open_settings_file(self) -> None:
        """The same as her menu's Settings file…: write the commented template first if it is
        missing, then hand the file to the desktop's editor."""
        from .config import default_path

        path = default_path()
        if not path.exists():
            process = await asyncio.create_subprocess_exec(sys.executable, "-m", "strawberry_crab.strawberryd", "--init-config",
                                                           stdout=asyncio.subprocess.DEVNULL, **quiet_process_options())
            await process.wait()
        await self.open_path(path)

    async def open_path(self, path: Path) -> None:
        """The desktop's own handler: xdg-open on Linux; on Windows the file's associated app, and
        Notepad for a file type nothing is associated with (config.toml, usually)."""
        if paths.windows():
            try:
                await asyncio.to_thread(os.startfile, str(path))
            except OSError:
                if path.is_dir():
                    log.warning("could not open %s", path)
                    return
                try:
                    await asyncio.create_subprocess_exec("notepad.exe", str(path))
                except OSError as exc:
                    log.warning("could not open %s (%s)", path, exc)
            return
        try:
            await asyncio.create_subprocess_exec("xdg-open", str(path),
                                                 stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.DEVNULL)
        except OSError as exc:
            log.warning("could not open %s (%s)", path, exc)

    async def command(self, name: str, value: Any = None) -> bool:
        payload: dict[str, Any] = {"command": name}
        if value is not None:
            payload["value"] = value
        return await self.daemon.post("/command", payload)

    # --- keeping the menu honest ----------------------------------------------

    async def refresh(self) -> bool:
        """Ask the daemon what she is doing and the widget what it was told. True on a change."""
        health = await self.daemon.get("/health")
        self.state.daemon_ok = bool(health)
        if health:
            self.state.state = str(health.get("state") or health.get("rest_state") or "idle")
            voice = health.get("voice")
            self.state.ears_loading = isinstance(voice, dict) and voice.get("phase") == "loading"
        self.read_prefs()
        self.read_config()
        return await self.publish()

    def read_prefs(self) -> None:
        """Her own menu writes the same preferences; read them back so the two agree."""
        prefs = read_widget_prefs(self.prefs_path)
        if not prefs:
            return
        self.state.muted = bool(prefs.get("muted", self.state.muted))
        self.state.quiet_until = float(prefs.get("quiet_until", self.state.quiet_until))
        self.state.volume = float(prefs.get("volume", self.state.volume))
        self.state.skin = str(prefs.get("skin", self.state.skin))
        self.state.top_hat = bool(prefs.get("top_hat", self.state.top_hat))
        self.state.always_on_top = bool(prefs.get("always_on_top", self.state.always_on_top))
        self.state.sleep_minutes = float(prefs.get("after_minutes", self.state.sleep_minutes))

    async def publish(self) -> bool:
        """Rebuild the rows; tell the front end only about what actually changed."""
        items = menu_items(self.state)
        updated = [(new.id, new.properties()) for old, new in zip(flatten(self.items), flatten(items))
                   if old.properties() != new.properties()]
        if not updated:
            return False
        was_status = self.items[0].label
        self.items = items
        self.revision += 1
        await self.announce(updated, was_status != self.items[0].label)
        return True

    async def announce(self, updated: list[tuple[int, dict]], status_changed: bool) -> None:
        """The rows in `updated` changed (and the status row's text, if `status_changed`)."""

    async def poll(self, errors: tuple[type[BaseException], ...] = (OSError, ConnectionError)) -> None:
        while not self.stopping.is_set():
            try:
                await self.refresh()
            except errors as exc:
                log.debug("status poll failed: %s", exc)
            await asyncio.sleep(HEALTH_EVERY_S)
