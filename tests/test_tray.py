"""The tray: its menu, its D-Bus replies and its children — all without a session bus.

Every reply is serialised and parsed back, which is the part a hand-written D-Bus server gets
wrong: a signature that does not match the data is a runtime error on somebody else's desktop.
"""

import asyncio
import sys
import zlib
from pathlib import Path

import pytest
from jeepney import DBusAddress, HeaderFields, Parser, new_method_call

from strawberry_crab import doorways, icons, tray
from strawberry_crab.paths import checkout_root
from strawberry_crab.tray import Child, Children, Tray, TrayState, layout, menu_items

LINUX = doorways.DOORWAYS    # the Linux children, whichever system runs the tests

ITEM = DBusAddress(tray.ITEM_PATH, bus_name="org.kde.StatusNotifierItem-1-1", interface=tray.SNI_IFACE)
MENU = DBusAddress(tray.MENU_PATH, bus_name="org.kde.StatusNotifierItem-1-1", interface=tray.MENU_IFACE)
SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]     # a child that stays up
PROPS = DBusAddress(tray.ITEM_PATH, bus_name="org.kde.StatusNotifierItem-1-1", interface=tray.PROPS_IFACE)


def roundtrip(message):
    """Serialise a message and parse it back: proves the body matches its signature."""
    parser = Parser()
    parser.add_data(message.serialise(serial=11))
    return parser.get_next_message()


class FakeDaemon:
    def __init__(self, up=True, health=None):
        self.up = up
        self.health = health if health is not None else {"ok": True, "state": "talking", "widgets": 1}
        self.posts = []

    async def post(self, path, payload):
        self.posts.append((path, payload))
        return self.up

    async def get(self, path):
        return self.health if self.up else None


def tray_for(**state) -> Tray:
    item = Tray("http://127.0.0.1:1", icons=[(2, 2, b"\x00" * 16)], icon_name="")
    item.daemon = FakeDaemon()
    for key, value in state.items():
        setattr(item.state, key, value)
    item.items = menu_items(item.state)
    return item


# --- the menu ---------------------------------------------------------------------

def test_the_rows_are_the_crabs_menu_plus_show_hide_and_a_status_line():
    rows = menu_items(TrayState(daemon_ok=True))
    assert [(r.id, r.action) for r in rows] == [
        (1, "status"), (2, "hide"), (3, "chat"), (4, "separator"), (5, "mute"), (6, "quiet"),
        (7, "submenu"), (8, "submenu"), (9, "submenu"), (10, "sleep_now"), (11, "hat"), (12, "on_top"),
        (50, "submenu"), (13, "separator"), (14, "settings_file"), (15, "voices_folder"), (16, "reset_position"),
        (17, "separator"), (18, "restart"), (19, "quit")]
    assert rows[0].label == "Idle" and rows[0].enabled is False
    assert rows[1].label == "Hide her"
    assert rows[2].label == "Chat with Strawberry…"
    assert rows[3].properties() == {"type": ("s", "separator")}
    assert [r.label for r in rows[6:9]] == ["Voice volume", "Skin", "Sleep after inactivity"]
    assert all(r.properties()["children-display"] == ("s", "submenu") for r in rows[6:9])


def test_the_submenus_are_radio_lists_that_show_the_current_choice():
    rows = menu_items(TrayState(volume=0.5, skin="mint", sleep_minutes=10.0))
    volume, skins, sleep = rows[6].children, rows[7].children, rows[8].children
    assert [c.label for c in volume] == ["25%", "50%", "75%", "100%"]
    assert [c.checked for c in volume] == [False, True, False, False]
    assert volume[1].properties()["toggle-type"] == ("s", "radio")
    assert [c.value for c in skins] == ["strawberry", "peach", "blueberry", "mint", "lavender"]
    assert [c.label for c in skins if c.checked] == ["Mint"]
    assert [c.label for c in sleep] == ["5 minutes", "1 minutes", "10 minutes", "30 minutes", "Never"]
    assert [c.label for c in sleep if c.checked] == ["10 minutes"]
    assert [c.id for c in volume + skins + sleep] == [20, 21, 22, 23, 30, 31, 32, 33, 34, 40, 41, 42, 43, 44]


def test_the_choices_match_the_crabs_own_menu():
    """The tray repeats what widget/menu.gd offers; this is the seam that would drift."""
    import re

    root = checkout_root()
    menu = (root / "widget" / "menu.gd").read_text()
    palettes = (root / "widget" / "skin_palettes.gd").read_text()
    volumes = [float(v) for v in re.search(r"const VOLUMES := \[(.*?)\]", menu).group(1).split(",")]
    minutes = [float(v) for v in re.search(r"const SLEEP_MINUTES := \[(.*?)\]", menu).group(1).split(",")]
    order = re.findall(r'"([a-z]+)"', re.search(r"const ORDER = \[(.*?)\]", palettes).group(1))
    names = dict(re.findall(r'"([a-z]+)": \{"name": "([A-Za-z]+)"', palettes))
    assert list(tray.VOLUMES) == volumes
    assert list(tray.SLEEP_MINUTES) == minutes
    assert list(tray.SKINS) == [(skin, names[skin]) for skin in order]
    assert float(re.search(r"const QUIET_SECONDS := ([\d.]+)", menu).group(1)) == tray.QUIET_S


def test_the_status_row_says_what_she_is_doing():
    assert menu_items(TrayState())[0].label == tray.NO_DAEMON      # no daemon answering
    assert menu_items(TrayState(daemon_ok=True, state="thinking"))[0].label == "Thinking…"
    assert menu_items(TrayState(daemon_ok=True, state="dancing"))[0].label == "Dancing"
    assert menu_items(TrayState(daemon_ok=True, state="nonsense"))[0].label == "Idle"


def test_show_and_hide_swap_by_widget_state():
    shown = menu_items(TrayState(widget_shown=True))[1]
    hidden = menu_items(TrayState(widget_shown=False))[1]
    assert (shown.action, shown.label) == ("hide", "Hide her")
    assert (hidden.action, hidden.label) == ("show", "Show her")


def test_the_checkboxes_follow_the_widgets_preferences():
    import time

    rows = menu_items(TrayState(muted=True, quiet_until=time.time() + 125, top_hat=True, always_on_top=False))
    assert rows[4].properties()["toggle-type"] == ("s", "checkmark")
    assert rows[4].properties()["toggle-state"] == ("i", 1)              # muted
    assert rows[5].properties()["toggle-state"] == ("i", 1)              # quiet
    assert rows[5].label == "Quiet (3 min left)"                          # as her own menu counts it
    assert rows[10].properties()["toggle-state"] == ("i", 1)             # top hat
    assert rows[11].properties()["toggle-state"] == ("i", 0)             # always on top, off
    stale = menu_items(TrayState(quiet_until=time.time() - 60))
    assert stale[5].properties()["toggle-state"] == ("i", 0)
    assert stale[5].label == "Quiet for an hour"


def test_the_layout_carries_the_submenus_under_one_root():
    revision, root = layout(menu_items(TrayState()), revision=3)
    root_id, root_props, children = root
    assert revision == 3 and root_id == 0
    assert root_props["children-display"] == ("s", "submenu")
    assert [child[1][0] for child in children] == list(range(1, 13)) + [50] + list(range(13, 20))
    assert all(child[0] == "(ia{sv}av)" for child in children)
    volume = children[6][1]
    assert [grandchild[1][0] for grandchild in volume[2]] == [20, 21, 22, 23]
    assert children[0][1][2] == []                                       # the status row has no children


# --- the D-Bus replies -------------------------------------------------------------

async def answer(item: Tray, address: DBusAddress, member: str, signature=None, body=()):
    """Put a method call on the wire, hand the tray what comes off it, and read its reply back."""
    call = roundtrip(new_method_call(address, member, signature, body))
    reply = await item.answer(call, address.interface, member, address.object_path)
    return roundtrip(reply)


async def test_getlayout_survives_serialisation():
    item = tray_for(daemon_ok=True)
    reply = await answer(item, MENU, "GetLayout", "iias", (0, -1, []))
    revision, root = reply.body
    assert revision == item.revision
    labels = [dict(child[1][1]).get("label") for child in root[2]]
    assert labels[0] == ("s", "Idle") and labels[-1] == ("s", "Quit")
    volume = root[2][6][1]
    assert [dict(g[1][1])["label"] for g in volume[2]] == [("s", "25%"), ("s", "50%"), ("s", "75%"), ("s", "100%")]


async def test_getgroupproperties_answers_only_the_ids_asked_for():
    item = tray_for()
    reply = await answer(item, MENU, "GetGroupProperties", "aias", ([2, 19, 31], []))
    assert [entry[0] for entry in reply.body[0]] == [2, 31, 19]      # menu order, submenus in place
    reply = await answer(item, MENU, "GetGroupProperties", "aias", ([], []))
    assert len(reply.body[0]) == 20 + 4 + 5 + 5 + 5      # the rows plus the four submenus


async def test_abouttoshow_refreshes_the_status_row():
    item = tray_for()
    reply = await answer(item, MENU, "AboutToShow", "i", (0,))
    assert reply.body == (True,)                      # it changed: the daemon answered "talking"
    assert item.items[0].label == "Talking"
    reply = await answer(item, MENU, "AboutToShow", "i", (0,))
    assert reply.body == (False,)                     # nothing new the second time


async def test_the_item_properties_are_the_ones_the_spec_names():
    item = tray_for(daemon_ok=True)
    reply = await answer(item, PROPS, "GetAll", "s", (tray.SNI_IFACE,))
    props = reply.body[0]
    assert props["Category"] == ("s", "ApplicationStatus")
    assert props["Id"] == ("s", "strawberry")
    assert props["Title"] == ("s", "Strawberry")
    assert props["Status"] == ("s", "Active")
    assert props["Menu"] == ("o", "/MenuBar")
    assert props["IconName"] == ("s", "")             # nothing themed: the pixmaps carry her
    assert props["IconPixmap"][0] == "a(iiay)"
    assert props["ToolTip"][1][3] == "Idle"           # the tooltip carries the status text

    one = await answer(item, PROPS, "Get", "ss", (tray.SNI_IFACE, "Title"))
    assert one.body == (("s", "Strawberry"),)
    missing = await answer(item, PROPS, "Get", "ss", (tray.SNI_IFACE, "Nope"))
    assert missing.header.message_type.name == "error"
    assert missing.header.fields[HeaderFields.error_name] == "org.freedesktop.DBus.Error.UnknownProperty"


async def test_the_menu_object_has_its_own_properties():
    item = tray_for()
    reply = await answer(item, DBusAddress(tray.MENU_PATH, bus_name=MENU.bus_name, interface=tray.PROPS_IFACE),
                         "GetAll", "s", (tray.MENU_IFACE,))
    assert reply.body[0]["Version"] == ("u", 3)


async def test_introspection_is_valid_xml_for_both_objects():
    from xml.etree import ElementTree

    item = tray_for()
    for address, interface in ((ITEM, tray.SNI_IFACE), (MENU, tray.MENU_IFACE)):
        reply = await answer(item, DBusAddress(address.object_path, bus_name=address.bus_name,
                                               interface=tray.INTROSPECT_IFACE), "Introspect")
        root = ElementTree.fromstring(reply.body[0])
        assert interface in [child.get("name") for child in root if child.tag == "interface"]


# --- what a click does -------------------------------------------------------------

async def test_clicking_the_rows_sends_the_widget_commands():
    item = tray_for(daemon_ok=True)
    await item.clicked(2)                              # Hide her
    assert item.daemon.posts[-1] == ("/command", {"command": "hide"})
    assert item.state.widget_shown is False
    assert item.items[1].label == "Show her"           # the row swapped itself

    await item.clicked(2)                              # Show her
    assert item.daemon.posts[-1] == ("/command", {"command": "show"})
    await item.clicked(3)
    assert item.daemon.posts[-1] == ("/command", {"command": "chat"})
    await item.clicked(5)
    assert item.daemon.posts[-1] == ("/command", {"command": "mute", "value": True})
    assert item.state.muted is True
    await item.clicked(5)
    assert item.daemon.posts[-1] == ("/command", {"command": "mute", "value": False})
    await item.clicked(6)
    assert item.daemon.posts[-1] == ("/command", {"command": "quiet", "value": 3600})
    assert item.state.quiet is True
    await item.clicked(6)
    assert item.daemon.posts[-1] == ("/command", {"command": "quiet", "value": 0})
    assert item.state.quiet is False


async def test_clicking_the_preferences_sends_what_her_menu_would():
    item = tray_for(daemon_ok=True)
    await item.clicked(21)                             # Voice volume -> 50%
    assert item.daemon.posts[-1] == ("/command", {"command": "volume", "value": 0.5})
    assert item.state.volume == 0.5
    await item.clicked(33)                             # Skin -> Mint
    assert item.daemon.posts[-1] == ("/command", {"command": "skin", "value": "mint"})
    assert item.state.skin == "mint"
    await item.clicked(44)                             # Sleep after inactivity -> Never
    assert item.daemon.posts[-1] == ("/command", {"command": "sleep_after", "value": 0.0})
    await item.clicked(10)
    assert item.daemon.posts[-1] == ("/command", {"command": "sleep_now"})
    await item.clicked(11)
    assert item.daemon.posts[-1] == ("/command", {"command": "hat", "value": True})
    await item.clicked(12)
    assert item.daemon.posts[-1] == ("/command", {"command": "on_top", "value": False})
    await item.clicked(16)
    assert item.daemon.posts[-1] == ("/command", {"command": "reset_position"})
    # The submenu rows themselves are not commands.
    await item.clicked(7)
    assert item.daemon.posts[-1] == ("/command", {"command": "reset_position"})


async def test_the_preferences_are_read_back_from_the_widgets_own_file(tmp_path):
    prefs = tmp_path / "widget.cfg"
    prefs.write_text('[appearance]\n\nskin="mint"\ntop_hat=true\n\n[window]\n\nx=12\nalways_on_top=false\n\n'
                     '[audio]\n\nmuted=true\nquiet_until=0.0\nvolume=0.25\n\n[sleep]\n\nafter_minutes=30.0\n')
    assert tray.read_widget_prefs(prefs) == {
        "skin": "mint", "top_hat": True, "x": 12.0, "always_on_top": False,
        "muted": True, "quiet_until": 0.0, "volume": 0.25, "after_minutes": 30.0,
    }
    assert tray.read_widget_prefs(tmp_path / "gone.cfg") == {}

    item = tray_for()
    item.prefs_path = prefs
    await item.refresh()
    assert (item.state.muted, item.state.volume, item.state.skin) == (True, 0.25, "mint")
    assert (item.state.top_hat, item.state.always_on_top, item.state.sleep_minutes) == (True, False, 30.0)
    assert [row.label for row in item.items[7].children if row.checked] == ["Mint"]


async def test_the_disabled_status_row_does_nothing_and_quit_stops_the_tray():
    item = tray_for()
    await item.clicked(1)
    assert item.daemon.posts == []
    await item.clicked(19)
    assert item.stopping.is_set()


async def test_a_daemon_that_is_down_does_not_flip_the_menu():
    item = tray_for()
    item.daemon.up = False
    await item.clicked(5)
    assert item.state.muted is False


async def test_activate_shows_her_when_a_host_does_not_open_the_menu():
    item = tray_for(widget_shown=True)
    reply = await answer(item, ITEM, "Activate", "ii", (0, 0))
    assert reply.header.message_type.name == "method_return"
    assert item.daemon.posts[-1] == ("/command", {"command": "hide"})


# --- the icons ---------------------------------------------------------------------

def make_png(width, height, rgba: bytes) -> bytes:
    """A minimal 8-bit RGBA PNG, every scanline unfiltered."""
    import struct

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + rgba[row * width * 4:(row + 1) * width * 4] for row in range(height))
    return (icons.PNG_MAGIC + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_a_png_becomes_argb32_in_network_byte_order():
    rgba = bytes([1, 2, 3, 255, 10, 20, 30, 128])
    png = make_png(2, 1, rgba)
    assert icons.read_png(png) == (2, 1, rgba)
    assert icons.rgba_to_argb(rgba) == bytes([255, 1, 2, 3, 128, 10, 20, 30])


def test_the_checked_in_icons_load_at_every_size():
    loaded = icons.pixmaps()
    assert [(w, h) for w, h, _ in loaded] == [(s, s) for s in icons.ICON_SIZES]
    for width, height, argb in loaded:
        assert len(argb) == width * height * 4
    biggest = loaded[-1][2]
    assert any(argb != 0 for argb in biggest)          # not a blank square


def test_a_png_we_do_not_write_is_refused():
    with pytest.raises(ValueError):
        icons.read_png(b"not a png at all")


def test_iconname_is_claimed_only_when_the_icon_is_in_a_theme(tmp_path):
    # GNOME's AppIndicator extension draws a placeholder for a name it cannot resolve, so an
    # unresolvable IconName hides the berry (2026-09-22).
    assert tray.themed_icon_name(roots=[tmp_path]) == ""
    apps = tmp_path / "hicolor" / "48x48" / "apps"
    apps.mkdir(parents=True)
    (apps / "strawberry.png").write_bytes(b"x")
    assert tray.themed_icon_name(roots=[tmp_path]) == "strawberry"


# --- the children ------------------------------------------------------------------

def test_the_children_are_the_daemon_the_doorways_and_the_widget(tmp_path):
    from strawberry_crab import widgetbin

    dev = lambda: widgetbin.Widget("checkout", Path("/opt/godot"), tmp_path / "widget")
    specs = tray.child_specs(8771, None, resolve_widget=dev, doorways=LINUX)
    assert [c.name for c in specs] == ["daemon", "mpris_watch", "notify_watch", "beat_watch", "widget"]
    python = specs[0].argv[0]
    assert all(c.argv[0] == python for c in specs)          # one interpreter, nothing from a checkout
    assert specs[0].argv[1:] == ["-m", "strawberry_crab.strawberryd", "--port", "8771"]
    assert specs[1].argv[1:] == ["-m", "strawberry_crab.doorways.mpris_watch", "--daemon", "http://127.0.0.1:8771"]
    assert specs[3].argv[1:] == ["-m", "strawberry_crab.doorways.beat_watch", "--daemon", "http://127.0.0.1:8771"]
    assert specs[4].argv[1:] == ["-m", "strawberry_crab", "widget"]  # developer mode: the CLI imports and runs godot
    assert specs[4].env["STRAWBERRYD_PORT"] == "8771"
    quiet = tray.child_specs(8771, None, widget=False, resolve_widget=dev, doorways=LINUX)
    assert [c.name for c in quiet][-1] == "beat_watch"
    config = tmp_path / "c.toml"
    assert tray.child_specs(8771, config, resolve_widget=dev)[0].argv[-2:] == ["--config", str(config)]


def test_each_system_gets_its_own_doorways(tmp_path):
    from strawberry_crab import widgetbin

    dev = lambda: widgetbin.Widget("checkout", Path("/opt/godot"), tmp_path / "widget")
    names = [c.name for c in tray.child_specs(8771, None, widget=False, resolve_widget=dev)]
    assert names == ["daemon", *doorways.for_system()]
    assert doorways.for_system("linux") == LINUX
    assert doorways.for_system("win32") == ("smtc_watch", "toast_watch", "beat_watch")   # WINDOWS.md
    assert doorways.for_system("darwin") == ()
    windows = tray.child_specs(8771, None, widget=False, resolve_widget=dev, doorways=doorways.for_system("win32"))
    assert windows[1].argv[1:] == ["-m", "strawberry_crab.doorways.smtc_watch", "--daemon", "http://127.0.0.1:8771"]
    # The same beat doorway on both systems; it picks its capture (beat_watch.backend).
    assert windows[3].argv[1:] == ["-m", "strawberry_crab.doorways.beat_watch", "--daemon", "http://127.0.0.1:8771"]


def test_the_widget_child_is_the_binary_when_installed(tmp_path):
    from strawberry_crab import widgetbin

    binary = tmp_path / "strawberry-widget"
    specs = tray.child_specs(8771, None, resolve_widget=lambda: widgetbin.Widget("binary", binary))
    assert specs[-1].name == "widget"
    assert specs[-1].argv == [str(binary), *widgetbin.display_args(), "--", "--ws=ws://127.0.0.1:8771/ws"]
    assert widgetbin.display_args() == (() if sys.platform == "win32" else ("--display-driver", "x11"))
    assert Children([])._env(specs[-1])["STRAWBERRY_TRAY"] == "1"


def test_no_widget_to_run_leaves_the_rest(tmp_path, caplog):
    from strawberry_crab import widgetbin

    def missing():
        raise widgetbin.WidgetMissing("no widget binary at X. Get it with: strawberry widget --fetch")

    assert [c.name for c in tray.child_specs(8771, None, resolve_widget=missing, doorways=LINUX)][-1] == "beat_watch"
    assert "strawberry widget --fetch" in caplog.text


async def test_a_child_that_exits_is_started_again_and_stop_ends_it(tmp_path, monkeypatch):
    monkeypatch.setattr(Children, "FIRST_BACKOFF_S", 0.01)
    monkeypatch.setattr(Children, "MAX_BACKOFF_S", 0.01)
    state = tmp_path / "tray.json"
    quick = Child("quick", [sys.executable, "-c", "raise SystemExit(7)"])
    children = Children([quick], state_path=state)
    children.start()
    for _ in range(100):                     # each run is a Python start-up
        await asyncio.sleep(0.05)
        if quick.restarts >= 2:
            break
    assert quick.restarts >= 2
    assert state.is_file() and '"name": "quick"' in state.read_text()
    await children.stop()
    assert not state.exists()


async def test_stop_terminates_a_child_that_is_still_running(tmp_path):
    children = Children([Child("sleeper", SLEEPER)], state_path=tmp_path / "tray.json")
    children.start()
    await asyncio.sleep(0.1)
    assert children.children[0].pid is not None
    await children.stop()
    assert children.children[0].pid is None


def test_the_state_file_is_what_the_launcher_reads(tmp_path):
    children = Children([Child("daemon", ["/bin/true"])], state_path=tmp_path / "tray.json")
    children.write_state()
    import json

    written = json.loads((tmp_path / "tray.json").read_text())
    assert written["children"] == [{"name": "daemon", "pid": None, "restarts": 0, "running": False,
                                    "command": ["/bin/true"]}]
    assert written["pid"] > 0


def test_the_checkout_holds_the_launcher_and_the_icons_ship_in_the_package():
    root = checkout_root()
    assert root is not None and (root / "bin" / "strawberry").is_file()
    assert Path(icons.icons_dir()) == Path(tray.__file__).parent / "assets" / "icons"
    assert (icons.icons_dir() / "strawberry-22.png").is_file()


# --- Message bodies: config.toml, then live ---------------------------------------

USER_CONFIG = """# my settings
[notifications]
ignore_apps = ["Spotify"]
body = "off"   # keep bodies here
body_apps = { Slack = "react" }

[voice]
enabled = false
"""


class FakeChildren:
    """What set_body_mode asks of the supervisor, recorded instead of done."""

    def __init__(self):
        self.restarted = []

    def restart_child(self, name):
        self.restarted.append(name)
        return True


def body_rows(items):
    row = next(r for r in items if r.id == 50)
    return row, row.children


def test_message_bodies_is_a_radio_submenu_with_the_current_mode_checked():
    from strawberry_crab.config import BODY_MODES

    row, children = body_rows(menu_items(TrayState(body_mode="react")))
    assert (row.label, row.properties()["children-display"]) == ("Message bodies", ("s", "submenu"))
    assert [c.id for c in children] == [51, 52, 53, 54, 55]
    radios = children[:3]
    assert [(c.label, c.value) for c in radios] == [("Off", "off"), ("React", "react"), ("Glance", "glance")]
    assert tuple(c.value for c in radios) == BODY_MODES
    assert all(c.properties()["toggle-type"] == ("s", "radio") for c in radios)
    assert [c.properties()["toggle-state"] for c in radios] == [("i", 0), ("i", 1), ("i", 0)]
    # No body_apps: the note and its separator are there but hidden, so no row moves.
    assert children[3].properties() == {"type": ("s", "separator"), "visible": ("b", False)}
    assert children[4].properties()["visible"] == ("b", False)
    _, shown = body_rows(menu_items(TrayState(body_overrides=True)))
    assert shown[3].properties() == {"type": ("s", "separator")}
    assert shown[4].label == "Per-app overrides in config"
    assert shown[4].properties()["visible"] == ("b", True) and shown[4].enabled is False


def test_the_body_setting_is_read_from_the_config_file(tmp_path):
    path = tmp_path / "config.toml"
    assert tray.read_body_setting(path) == ("off", False)              # no file: the default
    path.write_text(USER_CONFIG)
    assert tray.read_body_setting(path) == ("off", True)
    path.write_text('[notifications]\ninclude_body = true\n')         # the old key, read as config.py does
    assert tray.read_body_setting(path) == ("react", False)
    path.write_text('[notifications]\nbody = "glance"\nbody_apps = {}\n')
    assert tray.read_body_setting(path) == ("glance", False)
    path.write_text('[notifications\nbody = "glance"\n')             # half-saved: keep what the menu shows
    assert tray.read_body_setting(path) is None


async def test_the_menu_follows_the_file_and_shows_the_note_with_overrides(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[notifications]\nbody = "react"\n')
    item = tray_for()
    item.config_path = path
    await item.refresh()
    _, children = body_rows(item.items)
    assert [c.label for c in children if c.checked] == ["React"] and children[4].visible is False
    path.write_text(USER_CONFIG)
    await item.refresh()
    _, children = body_rows(item.items)
    assert [c.label for c in children if c.checked] == ["Off"] and children[4].visible is True
    path.write_text('[notifications\n')                              # broken mid-edit
    await item.refresh()
    assert item.state.body_mode == "off" and item.state.body_overrides is True


async def test_the_disabled_overrides_note_does_nothing(tmp_path):
    item = tray_for(body_overrides=True)
    await item.clicked(55)
    assert item.daemon.posts == []


async def test_choosing_a_mode_writes_the_file_and_applies_it_live(tmp_path, caplog):
    import logging
    import tomllib

    path = tmp_path / "config.toml"
    path.write_text(USER_CONFIG)
    item = tray_for(daemon_ok=True)
    item.config_path = path
    item.children = FakeChildren()
    caplog.set_level(logging.INFO, logger="strawberryd.tray")
    await item.clicked(53)                                          # Message bodies -> Glance

    text = path.read_text()
    assert 'body = "glance"   # keep bodies here' in text             # the value changed, the comment stayed
    assert tomllib.loads(text)["notifications"]["body_apps"] == {"Slack": "react"}   # overrides untouched
    assert text.replace('"glance"', '"off"', 1) == USER_CONFIG         # nothing else in the file moved
    backups = sorted(tmp_path.glob("config.toml.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == USER_CONFIG
    # Live: the daemon re-reads [notifications], then the watcher restarts with the new mode.
    assert item.daemon.posts[-1] == ("/command", {"command": "reload_notifications"})
    assert item.children.restarted == ["notify_watch", "toast_watch"]   # whichever this system runs
    assert item.state.body_mode == "glance"
    assert [c.label for c in body_rows(item.items)[1] if c.checked] == ["Glance"]
    assert "message bodies: glance" in caplog.text
    assert "Slack" not in caplog.text and "keep bodies here" not in caplog.text   # the mode, nothing else


async def test_a_missing_file_starts_as_the_template(tmp_path):
    import tomllib

    path = tmp_path / "strawberry" / "config.toml"
    item = tray_for()
    item.config_path = path
    item.children = FakeChildren()
    await item.clicked(52)                                          # React
    text = path.read_text()
    assert tomllib.loads(text)["notifications"]["body"] == "react"
    assert "# Message bodies. off:" in text                           # the commented template, not a bare line
    assert list(path.parent.glob("*.bak-*")) == []                     # nothing to back up
    assert item.children.restarted == ["notify_watch", "toast_watch"]   # whichever this system runs


async def test_a_file_that_would_not_load_is_left_alone_and_nothing_is_applied(tmp_path):
    path = tmp_path / "config.toml"
    broken = USER_CONFIG.replace("enabled = false", 'device = "tpu"')    # config.load refuses this
    path.write_text(broken)
    item = tray_for(daemon_ok=True)
    item.config_path = path
    item.children = FakeChildren()
    await item.clicked(53)
    assert path.read_text() == broken and list(tmp_path.glob("*.bak-*")) == []
    assert item.daemon.posts == [] and item.children.restarted == []
    assert item.state.body_mode == "off"


async def test_a_multi_line_body_is_not_claimed_as_set(tmp_path):
    path = tmp_path / "config.toml"
    odd = '[notifications]\nbody = """\nreact"""\n'
    path.write_text(odd)
    item = tray_for()
    item.config_path = path
    item.children = FakeChildren()
    await item.clicked(53)
    assert path.read_text() == odd and item.daemon.posts == [] and item.children.restarted == []


async def test_without_children_the_daemon_is_still_told(tmp_path, caplog):
    item = tray_for()
    item.config_path = tmp_path / "config.toml"
    assert item.children is None
    await item.clicked(51)
    assert item.daemon.posts[-1] == ("/command", {"command": "reload_notifications"})
    assert "restart notify_watch yourself" in caplog.text


async def test_restart_child_restarts_just_that_one_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(Children, "FIRST_BACKOFF_S", 30.0)          # a crash would wait this long
    watcher = Child("notify_watch", SLEEPER)
    other = Child("mpris_watch", SLEEPER)
    children = Children([watcher, other], state_path=tmp_path / "tray.json")
    children.start()
    await asyncio.sleep(0.1)
    first, untouched = watcher.pid, other.pid
    assert first is not None
    assert children.restart_child("notify_watch") is True
    for _ in range(50):
        await asyncio.sleep(0.05)
        if watcher.pid not in (None, first):
            break
    assert watcher.pid not in (None, first)                         # a new process, no 30 s backoff
    assert watcher.restarts == 0                                     # asked for, not a crash
    assert other.pid == untouched
    assert children.restart_child("nothing_by_that_name") is False
    await children.stop()


def test_the_notification_watcher_reads_the_trays_config_file(tmp_path):
    from strawberry_crab import widgetbin

    dev = lambda: widgetbin.Widget("checkout", Path("/opt/godot"), tmp_path / "widget")
    config = tmp_path / "c.toml"
    specs = {c.name: c for c in tray.child_specs(8771, config, resolve_widget=dev, doorways=LINUX)}
    assert specs["notify_watch"].argv[-2:] == ["--config", str(config)]
    assert "--config" not in specs["mpris_watch"].argv
    default = {c.name: c for c in tray.child_specs(8771, None, resolve_widget=dev, doorways=LINUX)}
    assert "--config" not in default["notify_watch"].argv
    windows = {c.name: c for c in tray.child_specs(8771, config, resolve_widget=dev,
                                                   doorways=doorways.for_system("win32"))}
    assert windows["toast_watch"].argv[-2:] == ["--config", str(config)]
    assert "--config" not in windows["smtc_watch"].argv
