"""The tray icon, and the process that owns her (WIRING.md §14).

`strawberryd --tray` puts a 🍓 in the top bar and is the one thing that starts at login: it
launches the daemon, the doorways and the widget as children, restarts one that dies, and
stops them all on Quit. `--no-children` runs just the icon against a daemon that is already
up, which is what development and the acceptance script use.

The icon is a **StatusNotifierItem**: we own a bus name of our own, export
`org.kde.StatusNotifierItem` on /StatusNotifierItem and a `com.canonical.dbusmenu` menu on
/MenuBar, and ask `org.kde.StatusNotifierWatcher` to show it. There is no tray library here;
jeepney gives us messages and we answer them, which is a page of dispatch and no GTK.

The menu is her right-click menu (widget/menu.gd), preferences and all, plus show/hide and a
status row. Clicks become daemon calls: `POST /command` broadcasts `{"command": ...,
"value": ...}` to the widgets, `GET /health` tells us what she is doing, and the check marks
are read back from the widget's own settings file so the two menus agree.

One row is not the widget's: "Message bodies" writes `[notifications] body` into config.toml
(configedit.py: comments kept, validated, backed up), then tells the daemon to re-read
[notifications] and restarts the notify_watch child, the two readers of that setting (§4, §14).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
import time
import tomllib
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Callable

from jeepney import DBusAddress, MessageFlag, MessageType, new_error, new_method_return, new_signal

from .bus import BusClient, BusError, field as header_field, open_session_bus
from .client import DaemonClient, configure_logging, stop_on_signals
from . import doorways as doorway_modules
from .icons import pixmaps
from . import configedit, paths
from . import widgetbin

log = logging.getLogger("strawberryd.tray")

SNI_IFACE = "org.kde.StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
INTROSPECT_IFACE = "org.freedesktop.DBus.Introspectable"
PEER_IFACE = "org.freedesktop.DBus.Peer"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
DBUS = DBusAddress("/org/freedesktop/DBus", bus_name="org.freedesktop.DBus", interface="org.freedesktop.DBus")
DO_NOT_QUEUE = 4          # RequestName flag: fail rather than wait behind another owner
HEALTH_EVERY_S = 2.0      # how often the status row is refreshed
QUIET_S = 3600            # "Quiet for an hour"
ICON_THEME_SIZES = ("scalable", "64x64", "48x48", "32x32", "24x24", "22x22", "16x16")

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
NOTIFY_CHILD = "notify_watch"     # the doorway that reads [notifications] body at its start
NOTIFY_CHILDREN = (NOTIFY_CHILD, "toast_watch")   # ... on Linux, and on Windows


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

    @property
    def quiet(self) -> bool:
        return self.quiet_until > time.time()

    def status_label(self) -> str:
        if not self.daemon_ok:
            return NO_DAEMON
        return STATE_LABELS.get(self.state, STATE_LABELS["idle"])

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
        MenuItem(2, "hide" if state.widget_shown else "show", "Hide her" if state.widget_shown else "Show her"),
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


def layout(items: list[MenuItem], revision: int = 1) -> tuple:
    """The GetLayout reply body: (revision, (id, properties, children as variants)).

    Two levels deep is all we have, but the shape is recursive because dbusmenu's is.
    """
    def node(item: MenuItem):
        return ("(ia{sv}av)", (item.id, item.properties(), [node(child) for child in item.children]))

    return (revision, (0, {"children-display": ("s", "submenu")}, [node(item) for item in items]))


def flatten(items: list[MenuItem]) -> list[MenuItem]:
    """Every row, submenu children included: what a click or a property request is looked up in."""
    out: list[MenuItem] = []
    for item in items:
        out.append(item)
        out.extend(flatten(item.children))
    return out


def group_properties(items: list[MenuItem], ids: list[int] | None = None) -> list[tuple[int, dict]]:
    wanted = set(ids or [])
    return [(item.id, item.properties()) for item in flatten(items) if not wanted or item.id in wanted]


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


# --- the item's properties --------------------------------------------------------

def themed_icon_name(name: str = "strawberry", roots: list[Path] | None = None) -> str:
    """The name, but only if it really is in an icon theme; otherwise "".

    GNOME's AppIndicator extension prefers IconName over IconPixmap and, when the name is in
    no theme, draws a placeholder: with `IconName = "strawberry"` the panel showed "…" where
    the berry should be, and with it empty the pixmaps came through (measured 2026-09-22). So
    an unresolvable name is worse than no name at all, and we only claim one we can prove.
    """
    roots = roots if roots is not None else [paths.xdg_data_home() / "icons", Path("/usr/share/icons")]
    for root in roots:
        for size in ICON_THEME_SIZES:
            for extension in (".svg", ".png"):
                if (root / "hicolor" / size / "apps" / f"{name}{extension}").is_file():
                    return name
    return ""


def sni_properties(state: TrayState, icons: list[tuple[int, int, bytes]],
                   icon_name: str = "") -> dict[str, tuple[str, Any]]:
    """org.kde.StatusNotifierItem, as the spec names them.

    IconPixmap is `a(iiay)`: width, height and the pixels as ARGB32 in network byte order,
    smallest first. That is what actually shows; IconName is only filled in when the icon has
    been installed into a theme (see themed_icon_name).
    """
    return {
        "Category": ("s", "ApplicationStatus"),
        "Id": ("s", "strawberry"),
        "Title": ("s", "Strawberry"),
        "Status": ("s", "Active"),
        "WindowId": ("i", 0),
        "IconName": ("s", icon_name),
        "IconPixmap": ("a(iiay)", icons),
        "OverlayIconName": ("s", ""),
        "OverlayIconPixmap": ("a(iiay)", []),
        "AttentionIconName": ("s", ""),
        "AttentionIconPixmap": ("a(iiay)", []),
        "AttentionMovieName": ("s", ""),
        "ToolTip": ("(sa(iiay)ss)", ("", [], "Strawberry", state.status_label())),
        "ItemIsMenu": ("b", True),
        "Menu": ("o", MENU_PATH),
    }


MENU_PROPERTIES = {
    "Version": ("u", 3),
    "TextDirection": ("s", "ltr"),
    "Status": ("s", "normal"),
    "IconThemePath": ("as", []),
}

ITEM_XML = f"""<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="{SNI_IFACE}">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
  <interface name="{PROPS_IFACE}">
    <method name="Get"><arg name="interface" type="s" direction="in"/><arg name="name" type="s" direction="in"/><arg name="value" type="v" direction="out"/></method>
    <method name="GetAll"><arg name="interface" type="s" direction="in"/><arg name="properties" type="a{{sv}}" direction="out"/></method>
  </interface>
  <interface name="{INTROSPECT_IFACE}">
    <method name="Introspect"><arg name="xml" type="s" direction="out"/></method>
  </interface>
</node>
"""

MENU_XML = f"""<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="{MENU_IFACE}">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{{sv}}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{{sv}})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/><arg name="name" type="s" direction="in"/><arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/><arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/><arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg name="events" type="a(isvu)" direction="in"/><arg name="idErrors" type="ai" direction="out"/>
    </method>
    <method name="AboutToShow"><arg name="id" type="i" direction="in"/><arg name="needUpdate" type="b" direction="out"/></method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/><arg name="idErrors" type="ai" direction="out"/>
    </method>
    <signal name="LayoutUpdated"><arg name="revision" type="u"/><arg name="parent" type="i"/></signal>
    <signal name="ItemsPropertiesUpdated">
      <arg name="updatedProps" type="a(ia{{sv}})"/><arg name="removedProps" type="a(ias)"/>
    </signal>
  </interface>
  <interface name="{INTROSPECT_IFACE}">
    <method name="Introspect"><arg name="xml" type="s" direction="out"/></method>
  </interface>
</node>
"""


# --- the children -----------------------------------------------------------------

@dataclass
class Child:
    name: str
    argv: list[str]
    env: dict[str, str] = dataclass_field(default_factory=dict)   # on top of the tray's own environment
    process: asyncio.subprocess.Process | None = None
    restarts: int = 0
    task: asyncio.Task | None = None
    asked_to_restart: bool = False     # restart_child: the next exit is ours, not a crash

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process and self.process.returncode is None else None


def child_specs(port: int, config: Path | None, widget: bool = True,
                resolve_widget: Callable[[], widgetbin.Widget] | None = None,
                doorways: tuple[str, ...] | None = None) -> list[Child]:
    """The daemon, this system's doorways (`doorways.for_system()`: the three on Linux) and the
    widget, in the order they should come up.

    Everything Python runs on this same interpreter as a module of the package (`python -m
    strawberry_crab.strawberryd`, `python -m strawberry_crab.doorways.<name>`), so an installed tray never
    reaches back into a checkout. The widget is the exported binary when it is installed, run
    directly on the X11 backend; else developer mode, `python -m strawberry_crab widget` with the
    checkout's Godot project (it imports the project first when needed); else no widget child,
    and the log says how to get one (widgetbin.resolve). STRAWBERRY_CLI tells the widget's menu
    which `strawberry` to run for "Settings file…" and "Apply settings".
    """
    python = sys.executable
    url = f"http://127.0.0.1:{port}"
    daemon = [python, "-m", "strawberry_crab.strawberryd", "--port", str(port)]
    if config:
        daemon += ["--config", str(config)]
    children = [Child("daemon", daemon)]
    for module in (doorway_modules.for_system() if doorways is None else doorways):
        argv = [python, "-m", f"strawberry_crab.doorways.{module}", "--daemon", url]
        if config and module in NOTIFY_CHILDREN:
            argv += ["--config", str(config)]      # the file "Message bodies" writes, not the XDG one
        children.append(Child(module, argv))
    if not widget:
        return children
    try:
        found = (resolve_widget or widgetbin.resolve)()
    except widgetbin.WidgetMissing as exc:
        log.warning("no widget: %s", exc)
        return children
    env = {"STRAWBERRYD_PORT": str(port)}
    strawberry = widgetbin.strawberry_cli()
    if strawberry:
        env["STRAWBERRY_CLI"] = strawberry
    if found.kind == "binary":
        children.append(Child("widget", found.argv(port), env))
    else:
        children.append(Child("widget", [python, "-m", "strawberry_crab", "widget"], env))
    return children


class Children:
    """Start each child, restart the ones that exit, stop them all on the way out.

    Backoff doubles from 1 s to 30 s and resets once a child has stayed up for a while, so a
    doorway that cannot reach its bus does not spin, and a crash after an hour restarts at once.
    """

    FIRST_BACKOFF_S = 1.0
    MAX_BACKOFF_S = 30.0
    SETTLED_S = 30.0
    TERM_GRACE_S = 5.0

    def __init__(self, children: list[Child], state_path: Path | None = None) -> None:
        self.children = children
        self.state_path = state_path
        self.stopping = False

    def start(self) -> None:
        for child in self.children:
            child.task = asyncio.ensure_future(self._supervise(child))

    async def _supervise(self, child: Child) -> None:
        backoff = self.FIRST_BACKOFF_S
        while not self.stopping:
            started = time.monotonic()
            try:
                child.process = await asyncio.create_subprocess_exec(
                    *child.argv, env=self._env(child), start_new_session=False)
            except OSError as exc:
                log.error("%s will not start (%s)", child.name, exc)
                return
            log.info("%s started (pid %d)", child.name, child.process.pid)
            self.write_state()
            code = await child.process.wait()
            if self.stopping:
                return
            if child.asked_to_restart:
                child.asked_to_restart = False
                log.info("%s stopped to pick up a setting; starting it again", child.name)
                continue
            lived = time.monotonic() - started
            child.restarts += 1
            log.warning("%s exited with %s after %.0f s; restarting in %.0f s", child.name, code, lived, backoff)
            self.write_state()
            await asyncio.sleep(backoff)
            backoff = self.FIRST_BACKOFF_S if lived > self.SETTLED_S else min(backoff * 2, self.MAX_BACKOFF_S)

    def _env(self, child: Child | None = None) -> dict[str, str]:
        # The widget launcher must not start a daemon or doorways of its own: we own those.
        return {**os.environ, "STRAWBERRY_TRAY": "1", **(child.env if child else {})}

    async def restart(self) -> None:
        """Ask every child to go; the supervisors bring them back."""
        for child in self.children:
            if child.process and child.process.returncode is None:
                child.process.terminate()

    def restart_child(self, name: str) -> bool:
        """Stop one child so its supervisor starts it again at once (no backoff, not counted as a
        restart). False when it is not running: it reads its settings when it next starts anyway."""
        for child in self.children:
            if child.name == name and child.process and child.process.returncode is None:
                child.asked_to_restart = True
                child.process.terminate()
                return True
        return False

    async def stop(self) -> None:
        self.stopping = True
        for child in self.children:
            if child.process and child.process.returncode is None:
                child.process.terminate()
        for child in self.children:
            if child.process is None:
                continue
            try:
                await asyncio.wait_for(child.process.wait(), self.TERM_GRACE_S)
            except asyncio.TimeoutError:
                log.warning("%s did not stop; killing it", child.name)
                child.process.kill()
        for child in self.children:
            if child.task and not child.task.done():
                child.task.cancel()
        self.clear_state()

    def report(self) -> list[dict[str, Any]]:
        return [{"name": c.name, "pid": c.pid, "restarts": c.restarts,
                 "running": c.pid is not None, "command": c.argv} for c in self.children]

    def write_state(self) -> None:
        """`bin/strawberry status` reads this; nothing else depends on it."""
        if self.state_path is None:
            return
        payload = {"pid": os.getpid(), "updated": time.time(), "children": self.report()}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, indent=2))
            temporary.replace(self.state_path)
        except OSError as exc:
            log.debug("could not write %s (%s)", self.state_path, exc)

    def clear_state(self) -> None:
        if self.state_path is not None:
            self.state_path.unlink(missing_ok=True)



# --- the tray itself ---------------------------------------------------------------

class Tray:
    def __init__(self, daemon_url: str, children: Children | None = None, icons=None,
                 icon_name: str | None = None, prefs_path: Path | None = None,
                 config_path: Path | None = None) -> None:
        self.daemon = DaemonClient(daemon_url)
        self.children = children
        self.state = TrayState()
        self.icons = icons if icons is not None else pixmaps()
        self.icon_name = themed_icon_name() if icon_name is None else icon_name
        self.prefs_path = prefs_path     # None: widget_prefs_path(), with the legacy fallback
        self.config_path = config_path   # None: config.default_path(), as the daemon reads it
        self.config_seen: tuple[int, int] | None = None     # (mtime_ns, size) last read
        self.items = menu_items(self.state)
        self.revision = 1
        self.client: BusClient | None = None
        self.bus_name = ""
        self.item = DBusAddress(ITEM_PATH, interface=SNI_IFACE)
        self.menu = DBusAddress(MENU_PATH, interface=MENU_IFACE)
        self.registered = False
        self.stopping = asyncio.Event()

    # --- registration ---------------------------------------------------------

    async def register(self) -> None:
        """Own a name of our own, then ask the watcher to show us.

        The name is the one the spec suggests, `org.kde.StatusNotifierItem-<pid>-<n>`; the
        watcher takes either a bus name or an object path, and a bus name is what old hosts
        understand. Without a watcher (GNOME without the AppIndicator extension) we keep
        running: the item is there the moment one appears, and her right-click menu covers
        everything meanwhile.
        """
        self.bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        reply = await self.client.call_method(DBUS, "RequestName", "su", (self.bus_name, DO_NOT_QUEUE))
        if reply[0] != 1:  # 1 = primary owner
            raise RuntimeError(f"another process owns {self.bus_name} (RequestName returned {reply[0]})")
        await self.client.add_match(
            "type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',"
            f"member='NameOwnerChanged',arg0='{WATCHER_NAME}'")
        await self.register_with_watcher()

    async def register_with_watcher(self) -> bool:
        watcher = DBusAddress(WATCHER_PATH, bus_name=WATCHER_NAME, interface=WATCHER_NAME)
        try:
            await self.client.call_method(watcher, "RegisterStatusNotifierItem", "s", (self.bus_name,), timeout=3.0)
        except (BusError, asyncio.TimeoutError, ConnectionError) as exc:
            self.registered = False
            log.warning("no StatusNotifierWatcher (%s). On GNOME that is the AppIndicator extension: "
                        "`gnome-extensions enable ubuntu-appindicators@ubuntu.com`. She runs without it; "
                        "her right-click menu has the same items.", exc)
            return False
        self.registered = True
        log.info("registered %s with %s", self.bus_name, WATCHER_NAME)
        return True

    # --- serving the bus ------------------------------------------------------

    async def handle(self, message) -> None:
        if message.header.message_type is not MessageType.method_call:
            if header_field(message, "member") == "NameOwnerChanged":
                name, _old, new = message.body
                if name == WATCHER_NAME and new:
                    log.info("%s came back; registering again", WATCHER_NAME)
                    await self.register_with_watcher()
            return
        interface = header_field(message, "interface")
        member = header_field(message, "member")
        path = header_field(message, "path")
        try:
            reply = await self.answer(message, interface, member, path)
        except Exception as exc:  # noqa: BLE001 - an error reply beats a hung caller
            log.warning("%s.%s failed: %r", interface, member, exc)
            reply = new_error(message, "org.freedesktop.DBus.Error.Failed", "s", (str(exc),))
        if reply is not None and not (message.header.flags & MessageFlag.no_reply_expected):
            await self.client.send(reply)

    async def answer(self, message, interface: str, member: str, path: str):
        if interface == PEER_IFACE:
            if member == "Ping":
                return new_method_return(message)
            if member == "GetMachineId":
                return new_method_return(message, "s", (machine_id(),))
        if interface == INTROSPECT_IFACE and member == "Introspect":
            return new_method_return(message, "s", (MENU_XML if path == MENU_PATH else ITEM_XML,))
        if interface == PROPS_IFACE:
            return self.properties(message, path)
        if interface == SNI_IFACE:
            return await self.item_method(message, member)
        if interface == MENU_IFACE:
            return await self.menu_method(message, member)
        return new_error(message, "org.freedesktop.DBus.Error.UnknownInterface", "s", (f"no {interface} here",))

    def properties(self, message, path: str):
        member = header_field(message, "member")
        table = MENU_PROPERTIES if path == MENU_PATH else sni_properties(self.state, self.icons, self.icon_name)
        if member == "GetAll":
            return new_method_return(message, "a{sv}", (table,))
        if member == "Get":
            _iface, name = message.body
            if name not in table:
                return new_error(message, "org.freedesktop.DBus.Error.UnknownProperty", "s", (name,))
            return new_method_return(message, "v", (table[name],))
        if member == "Set":
            return new_error(message, "org.freedesktop.DBus.Error.PropertyReadOnly", "s", ("read-only",))
        return new_error(message, "org.freedesktop.DBus.Error.UnknownMethod", "s", (str(member),))

    async def item_method(self, message, member: str):
        if member in ("Activate", "SecondaryActivate"):
            # A host that does not open the menu itself: the plain click shows or hides her.
            await self.activate("show" if not self.state.widget_shown else "hide")
            return new_method_return(message)
        if member in ("ContextMenu", "Scroll"):
            return new_method_return(message)
        return new_error(message, "org.freedesktop.DBus.Error.UnknownMethod", "s", (str(member),))

    async def menu_method(self, message, member: str):
        if member == "GetLayout":
            _parent, _depth, _names = message.body
            return new_method_return(message, "u(ia{sv}av)", layout(self.items, self.revision))
        if member == "GetGroupProperties":
            ids, _names = message.body
            return new_method_return(message, "a(ia{sv})", (group_properties(self.items, list(ids)),))
        if member == "GetProperty":
            item_id, name = message.body
            for item in self.items:
                if item.id == item_id and name in item.properties():
                    return new_method_return(message, "v", (item.properties()[name],))
            return new_error(message, "org.freedesktop.DBus.Error.InvalidArgs", "s", (f"no property {name}",))
        if member == "Event":
            item_id, event_id, _data, _timestamp = message.body
            if event_id == "clicked":
                await self.clicked(int(item_id))
            return new_method_return(message)
        if member == "EventGroup":
            for item_id, event_id, _data, _timestamp in message.body[0]:
                if event_id == "clicked":
                    await self.clicked(int(item_id))
            return new_method_return(message, "ai", ([],))
        if member == "AboutToShow":
            changed = await self.refresh()
            return new_method_return(message, "b", (changed,))
        if member == "AboutToShowGroup":
            changed = await self.refresh()
            return new_method_return(message, "aiai", ([0] if changed else [], []))
        return new_error(message, "org.freedesktop.DBus.Error.UnknownMethod", "s", (str(member),))

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
                                                           stdout=asyncio.subprocess.DEVNULL)
            await process.wait()
        await self.open_path(path)

    async def open_path(self, path: Path) -> None:
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
        """Rebuild the rows; tell the host only about what actually changed."""
        items = menu_items(self.state)
        updated = [(new.id, new.properties()) for old, new in zip(flatten(self.items), flatten(items))
                   if old.properties() != new.properties()]
        if not updated:
            return False
        was_status = self.items[0].label
        self.items = items
        self.revision += 1
        if self.client is not None:
            await self.client.send(new_signal(self.menu, "ItemsPropertiesUpdated", "a(ia{sv})a(ias)", (updated, [])))
            await self.client.send(new_signal(self.menu, "LayoutUpdated", "ui", (self.revision, 0)))
            if was_status != self.items[0].label:
                await self.client.send(new_signal(self.item, "NewToolTip"))
        return True

    async def poll(self) -> None:
        while not self.stopping.is_set():
            try:
                await self.refresh()
            except (BusError, OSError, ConnectionError) as exc:
                log.debug("status poll failed: %s", exc)
            await asyncio.sleep(HEALTH_EVERY_S)

    # --- the loop -------------------------------------------------------------

    async def run(self) -> int:
        self.client = await open_session_bus()
        self.item = DBusAddress(ITEM_PATH, bus_name=self.client.unique_name, interface=SNI_IFACE)
        self.menu = DBusAddress(MENU_PATH, bus_name=self.client.unique_name, interface=MENU_IFACE)
        reader = asyncio.ensure_future(self.client.run())
        try:
            await self.refresh()      # ask before showing: the first tooltip should be true
            await self.register()
            if self.children:
                self.children.start()
            server = asyncio.ensure_future(self.client.serve(self.handle))
            poller = asyncio.ensure_future(self.poll())
            stop = asyncio.ensure_future(self.stopping.wait())
            await asyncio.wait([reader, server, poller, stop], return_when=asyncio.FIRST_COMPLETED)
            for task in (server, poller, stop):
                task.cancel()
            if reader.done():
                log.error("the session bus closed the connection (%s); exiting", self.client.error)
                return 3
            return 0
        finally:
            reader.cancel()
            if self.children:
                await self.children.stop()
            await self.client.close()


def machine_id() -> str:
    for path in ("/var/lib/dbus/machine-id", "/etc/machine-id"):
        try:
            return Path(path).read_text().strip()
        except OSError:
            continue
    return "0" * 32


async def run_tray(port: int, children: bool = True, widget: bool = True, config: Path | None = None) -> int:
    tray = Tray(f"http://127.0.0.1:{port}", config_path=config)
    if children:
        tray.children = Children(child_specs(port, config, widget=widget),
                                 state_path=paths.tray_state_file())
    stop_on_signals(tray.stopping)
    return await tray.run()


def main(argv: list[str] | None = None) -> int:
    from .config import load

    parser = argparse.ArgumentParser(prog="strawberryd --tray", description="the Strawberry tray icon")
    parser.add_argument("--port", type=int, default=None, help="the daemon's port (default: the config's)")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--no-children", action="store_true",
                        help="just the icon: do not start the daemon, the doorways or the widget")
    parser.add_argument("--no-widget", action="store_true", help="start the daemon and doorways but not the widget")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    configure_logging(args.log_level)
    port = args.port or load(args.config).daemon.port
    try:
        return asyncio.run(run_tray(port, children=not args.no_children, widget=not args.no_widget,
                                    config=args.config))
    except KeyboardInterrupt:
        return 0
    except (ConnectionError, OSError, RuntimeError) as exc:
        log.error("the tray cannot reach the session bus (%s)", exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())
