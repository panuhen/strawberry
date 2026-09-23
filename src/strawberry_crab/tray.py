"""The tray icon on Linux, and the process that owns her (WIRING.md §14).

`strawberryd --tray` puts a 🍓 in the top bar and is the one thing that starts at login: it
launches the daemon, the doorways and the widget as children, restarts one that dies, and
stops them all on Quit. `--no-children` runs just the icon against a daemon that is already
up, which is what development and the acceptance script use.

The icon is a **StatusNotifierItem**: we own a bus name of our own, export
`org.kde.StatusNotifierItem` on /StatusNotifierItem and a `com.canonical.dbusmenu` menu on
/MenuBar, and ask `org.kde.StatusNotifierWatcher` to show it. There is no tray library here;
jeepney gives us messages and we answer them, which is a page of dispatch and no GTK.

What is not D-Bus is shared with the Windows tray (wintray.py): the menu and what its rows do
are traymenu.py, the children and their restarts supervisor.py. Their names are re-exported
here, as they were before the split.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

from jeepney import DBusAddress, MessageFlag, MessageType, new_error, new_method_return, new_signal

from .bus import BusClient, BusError, field as header_field, open_session_bus
from .client import configure_logging, stop_on_signals
from .icons import pixmaps
from . import paths
from .supervisor import NOTIFY_CHILD, NOTIFY_CHILDREN, Child, Children, child_specs  # noqa: F401 - re-exported
from .traymenu import (  # noqa: F401 - re-exported: the menu is shared with wintray.py
    BODY_CHOICES, BODY_OVERRIDES_LABEL, HEALTH_EVERY_S, NO_DAEMON, QUIET_S, SKINS, SLEEP_MINUTES, STATE_LABELS,
    VOLUMES, MenuItem, TrayCore, TrayState, flatten, menu_items, read_body_setting, read_widget_prefs,
    widget_prefs_path,
)

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
ICON_THEME_SIZES = ("scalable", "64x64", "48x48", "32x32", "24x24", "22x22", "16x16")


# --- the menu on the bus ---------------------------------------------------------

def layout(items: list[MenuItem], revision: int = 1) -> tuple:
    """The GetLayout reply body: (revision, (id, properties, children as variants)).

    Two levels deep is all we have, but the shape is recursive because dbusmenu's is.
    """
    def node(item: MenuItem):
        return ("(ia{sv}av)", (item.id, item.properties(), [node(child) for child in item.children]))

    return (revision, (0, {"children-display": ("s", "submenu")}, [node(item) for item in items]))


def group_properties(items: list[MenuItem], ids: list[int] | None = None) -> list[tuple[int, dict]]:
    wanted = set(ids or [])
    return [(item.id, item.properties()) for item in flatten(items) if not wanted or item.id in wanted]


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


# --- the tray itself ---------------------------------------------------------------

class Tray(TrayCore):
    def __init__(self, daemon_url: str, children: Children | None = None, icons=None,
                 icon_name: str | None = None, prefs_path: Path | None = None,
                 config_path: Path | None = None) -> None:
        super().__init__(daemon_url, children, prefs_path=prefs_path, config_path=config_path)
        self.icons = icons if icons is not None else pixmaps()
        self.icon_name = themed_icon_name() if icon_name is None else icon_name
        self.client: BusClient | None = None
        self.bus_name = ""
        self.item = DBusAddress(ITEM_PATH, interface=SNI_IFACE)
        self.menu = DBusAddress(MENU_PATH, interface=MENU_IFACE)
        self.registered = False

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

    # --- keeping the menu honest ----------------------------------------------

    async def announce(self, updated: list[tuple[int, dict]], status_changed: bool) -> None:
        """Tell the host only about what actually changed."""
        if self.client is not None:
            await self.client.send(new_signal(self.menu, "ItemsPropertiesUpdated", "a(ia{sv})a(ias)", (updated, [])))
            await self.client.send(new_signal(self.menu, "LayoutUpdated", "ui", (self.revision, 0)))
            if status_changed:
                await self.client.send(new_signal(self.item, "NewToolTip"))

    async def poll(self) -> None:
        await super().poll((BusError, OSError, ConnectionError))

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
