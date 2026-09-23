"""The Windows tray: its menu as Win32 builds it and reads it back, its icon, and the tray's
loop with a fake icon. Nothing here shows in the notification area (tests/conftest.py blocks
Shell_NotifyIcon); the menus and icons are made and destroyed without being displayed."""

from __future__ import annotations

import asyncio
import ctypes
import struct
import sys
import time

import pytest

from strawberry_crab import cli, configedit, hotkey, icons, wintray
from strawberry_crab.config import default_path
from strawberry_crab.traymenu import TrayState, menu_items

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the Win32 API")


class FakeDaemon:
    def __init__(self):
        self.posts = []

    async def post(self, path, payload):
        self.posts.append((path, payload))
        return True

    async def get(self, path):
        return {"ok": True, "state": "talking"}


class FakeIcon:
    """What WindowsTray asks of NotifyIcon, recorded."""

    made = []

    def __init__(self, tip, items, on_command, on_default, before_menu, on_end_session, on_hotkey=None):
        self.tip, self.items, self.on_command, self.on_default = tip, items, on_command, on_default
        self.on_hotkey = on_hotkey
        self.tips, self.hidden, self.closed, self.hotkeys = [tip], False, False, []
        FakeIcon.made.append(self)

    def set_hotkey(self, wanted):
        self.hotkeys.append(wanted.text if wanted else None)

    def start(self):
        pass

    def set_tooltip(self, tip):
        self.tips.append(tip)

    def hide(self):
        self.hidden = True

    def close(self):
        self.closed = True


# --- what needs no Win32 -------------------------------------------------------------

def test_the_tooltip_is_her_name_and_the_status_row():
    assert wintray.tooltip("Idle") == "Strawberry: Idle"
    assert len(wintray.tooltip("x" * 300)) == 127                 # szTip holds 128 with the NUL


def test_an_ampersand_is_not_an_accelerator():
    assert wintray.menu_label("Tom & Jerry") == "Tom && Jerry"


def test_the_icon_is_the_smallest_png_that_fits():
    assert wintray.best_png(16).name == "strawberry-16.png"
    assert wintray.best_png(20).name == "strawberry-22.png"     # 125 %: scaled down from 22
    assert wintray.best_png(40).name == "strawberry-48.png"
    assert wintray.best_png(256).name == "strawberry-64.png"    # nothing bigger: the largest


def test_bgra_swaps_red_and_blue():
    assert icons.rgba_to_bgra(bytes([1, 2, 3, 4, 5, 6, 7, 8])) == bytes([3, 2, 1, 4, 7, 6, 5, 8])


def test_the_ico_holds_every_png_as_it_is():
    data = icons.ico_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind, count) == (0, 1, len(icons.ICON_SIZES))
    for index, size in enumerate(icons.ICON_SIZES):
        width, height, _, _, planes, bits, length, offset = struct.unpack("<BBBBHHII", data[6 + 16 * index:22 + 16 * index])
        assert (width, height, planes, bits) == (size, size, 1, 32)
        png = (icons.icons_dir() / f"strawberry-{size}.png").read_bytes()
        assert data[offset:offset + length] == png


# --- Win32: the menu and the icon, made and read back -------------------------------------

def read_back(state: TrayState) -> list[dict]:
    menu = wintray.build_menu(menu_items(state))
    try:
        return wintray.read_menu(menu)
    finally:
        wintray.api().user32.DestroyMenu(menu)


@windows_only
def test_the_structures_are_the_sizes_windows_expects():
    expected = 976 if ctypes.sizeof(ctypes.c_void_p) == 8 else 956
    assert ctypes.sizeof(wintray.NOTIFYICONDATAW) == expected
    assert ctypes.sizeof(wintray.MENUITEMINFOW) == (80 if ctypes.sizeof(ctypes.c_void_p) == 8 else 48)


@windows_only
def test_the_menu_is_the_linux_trays_menu():
    rows = read_back(TrayState(daemon_ok=True))
    labels = [row.get("label") for row in rows]
    assert labels == ["Idle", "Hide her", "Chat with Strawberry…", None, "Mute her voice", "Quiet for an hour",
                      "Voice volume", "Skin", "Sleep after inactivity", "Sleep now", "Top hat", "Always on top",
                      "Message bodies", None, "Settings file…", "Voices folder…", "Reset position", None,
                      "Restart (applies the settings)", "Quit"]
    assert [row.get("id") for row in rows if "id" in row] == [1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 50, 14, 15, 16,
                                                             18, 19]
    status, show_hide = rows[0], rows[1]
    assert status["enabled"] is False                              # the status row is greyed
    assert show_hide["default"] is True                            # bold; a left click runs it
    assert [row["label"] for row in rows if row.get("default")] == ["Hide her"]
    assert [len(rows[i]["submenu"]) for i in (6, 7, 8)] == [4, 5, 5]


@windows_only
def test_check_marks_and_radio_rows_follow_the_state():
    rows = read_back(TrayState(daemon_ok=True, muted=True, top_hat=True, always_on_top=False, volume=0.75,
                               skin="mint", sleep_minutes=0.0, body_mode="glance", widget_shown=False))
    by_label = {row["label"]: row for row in rows if "label" in row}
    assert by_label["Mute her voice"]["checked"] and not by_label["Mute her voice"]["radio"]
    assert by_label["Top hat"]["checked"] and not by_label["Always on top"]["checked"]
    assert by_label["Show her"]["default"]
    volume = by_label["Voice volume"]["submenu"]
    assert [(r["label"], r["checked"], r["radio"]) for r in volume] == [
        ("25%", False, True), ("50%", False, True), ("75%", True, True), ("100%", False, True)]
    assert [r["label"] for r in by_label["Skin"]["submenu"] if r["checked"]] == ["Mint"]
    assert [r["label"] for r in by_label["Sleep after inactivity"]["submenu"] if r["checked"]] == ["Never"]
    bodies = by_label["Message bodies"]["submenu"]
    assert [r["label"] for r in bodies] == ["Off", "React", "Glance"]    # the per-app note is hidden
    assert [r["id"] for r in bodies if r["checked"]] == [53]


@windows_only
def test_the_per_app_note_shows_under_the_body_modes_when_there_are_overrides():
    rows = read_back(TrayState(body_overrides=True))
    bodies = next(row for row in rows if row.get("label") == "Message bodies")["submenu"]
    assert bodies[3] == {"separator": True}
    assert bodies[4]["label"] == "Per-app overrides in config" and bodies[4]["enabled"] is False
    assert rows[0]["label"] == "strawberryd is not running"


@windows_only
def test_the_icon_is_a_32_bit_hicon_of_the_berry():
    hicon = wintray.load_icon(24)
    try:
        info = wintray.ICONINFO()
        assert wintray.api().user32.GetIconInfo(hicon, ctypes.byref(info))
        assert info.fIcon and info.hbmColor
        wintray.api().gdi32.DeleteObject(info.hbmColor)
        wintray.api().gdi32.DeleteObject(info.hbmMask)
    finally:
        wintray.api().user32.DestroyIcon(hicon)


@windows_only
def test_the_icon_is_refused_in_tests_and_leaves_no_window():
    icon = wintray.NotifyIcon("Strawberry: Idle", items=lambda: [], on_command=lambda i: None, on_default=lambda: None)
    with pytest.raises(wintray.NotifyIconError, match="out of bounds in tests"):
        icon.start()
    assert icon.hwnd is None and icon.hicon is None
    icon.thread.join(5)
    assert not icon.thread.is_alive()


# --- the tray's loop, with a fake icon -----------------------------------------------------

async def test_the_tray_shows_the_status_in_the_tooltip_and_runs_the_clicks(monkeypatch):
    monkeypatch.setattr(wintray, "NotifyIcon", FakeIcon)
    FakeIcon.made.clear()
    tray = wintray.WindowsTray("http://127.0.0.1:1")
    tray.daemon = FakeDaemon()
    runner = asyncio.ensure_future(tray.run())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if FakeIcon.made:
            break
    icon = FakeIcon.made[0]
    assert icon.tip == "Strawberry: Talking"                      # asked before showing
    assert [row.label for row in icon.items()][:2] == ["Talking", "Hide her"]
    icon.on_default()                                             # a left click: the default row
    icon.on_command(5)                                            # Mute her voice, from the menu
    for _ in range(100):
        await asyncio.sleep(0.01)
        if len(tray.daemon.posts) >= 2:
            break
    assert tray.daemon.posts[:2] == [("/command", {"command": "hide"}), ("/command", {"command": "mute", "value": True})]
    assert [row.label for row in icon.items()][1] == "Show her"
    tray.daemon.get = lambda path: asyncio.sleep(0, result=None)   # the daemon went away
    await tray.refresh()
    assert icon.tips[-1] == "Strawberry: strawberryd is not running"
    icon.on_command(19)                                           # Quit
    assert await asyncio.wait_for(runner, 5) == 0
    assert icon.hidden and icon.closed and tray.stopped.is_set()


async def test_quit_stops_the_children_before_the_icon_goes(monkeypatch):
    monkeypatch.setattr(wintray, "NotifyIcon", FakeIcon)
    order = []

    class FakeChildren:
        def start(self):
            order.append("start")

        async def stop(self):
            order.append("stop")

    tray = wintray.WindowsTray("http://127.0.0.1:1", children=FakeChildren())
    tray.daemon = FakeDaemon()
    runner = asyncio.ensure_future(tray.run())
    await asyncio.sleep(0.05)
    tray.stopping.set()
    assert await asyncio.wait_for(runner, 5) == 0
    assert order == ["start", "stop"] and FakeIcon.made[-1].closed


async def test_logging_off_waits_for_the_children(monkeypatch):
    monkeypatch.setattr(wintray, "NotifyIcon", FakeIcon)
    tray = wintray.WindowsTray("http://127.0.0.1:1")
    tray.daemon = FakeDaemon()
    runner = asyncio.ensure_future(tray.run())
    await asyncio.sleep(0.05)
    started = time.monotonic()
    await asyncio.to_thread(tray._end_session)                    # WM_ENDSESSION, on the icon's thread
    assert time.monotonic() - started < tray.END_SESSION_WAIT_S
    assert tray.stopped.is_set()
    assert await asyncio.wait_for(runner, 5) == 0


# --- the listen hotkey ----------------------------------------------------------------------

TEST_COMBO = "<Control><Alt><Shift>F24"          # no keyboard has F24 (tests/conftest.py allows only it)


async def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def test_the_tray_registers_the_hotkey_from_the_config_and_follows_changes(monkeypatch):
    monkeypatch.setattr(wintray, "NotifyIcon", FakeIcon)
    FakeIcon.made.clear()
    pressed = []
    monkeypatch.setattr(cli, "listen_fast", lambda port: pressed.append(port) or 0)
    tray = wintray.WindowsTray("http://127.0.0.1:8786")
    tray.daemon = FakeDaemon()
    runner = asyncio.ensure_future(tray.run())
    await wait_for(lambda: FakeIcon.made and FakeIcon.made[0].hotkeys)
    icon = FakeIcon.made[0]
    assert icon.hotkeys == ["<Control><Alt>space"]                # no config file: the default
    icon.on_hotkey()                                              # WM_HOTKEY, on the icon's thread
    await wait_for(lambda: pressed)
    assert pressed == [8786] and tray.listens == 1
    configedit.set_value(default_path(), "voice.hotkey", TEST_COMBO)
    await tray.refresh()
    configedit.set_value(default_path(), "voice.hotkey", "off")
    await tray.refresh()
    assert icon.hotkeys == ["<Control><Alt>space", TEST_COMBO, None]
    tray.stopping.set()
    assert await asyncio.wait_for(runner, 5) == 0


def test_a_hotkey_that_does_not_parse_registers_nothing(caplog):
    path = default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[voice]\nhotkey = "<Hyper>space"\n', encoding="utf-8")       # by hand, past configedit
    tray = wintray.WindowsTray("http://127.0.0.1:1")
    with caplog.at_level("WARNING"):
        tray.read_config()
    assert tray.hotkey is None and "not a key combination" in caplog.text


def hidden_window(monkeypatch, on_hotkey=lambda: None):
    """A NotifyIcon whose window and message loop are real and whose icon is never added: the
    taskbar "is not there yet", so nothing shows."""
    monkeypatch.setattr(wintray, "shell_notify_icon", lambda message, data: False)
    icon = wintray.NotifyIcon("Strawberry: Idle", items=lambda: [], on_command=lambda i: None,
                              on_default=lambda: None, on_hotkey=on_hotkey)
    icon.start()
    return icon


def set_and_wait(icon, wanted):
    icon.set_hotkey(wanted)
    assert icon.hotkey_done.wait(5)


@windows_only
def test_the_window_registers_the_hotkey_and_hears_it(monkeypatch):
    heard = []
    icon = hidden_window(monkeypatch, on_hotkey=lambda: heard.append(True))
    try:
        combo = hotkey.parse(TEST_COMBO)
        set_and_wait(icon, combo)
        assert icon.hotkey_registered == combo and icon.hotkey_error == 0
        # What Windows posts when the keys are pressed; no key is sent to the desktop.
        wintray.api().user32.PostMessageW(icon.hwnd, wintray.WM_HOTKEY, wintray.HOTKEY_ID, 0)
        deadline = time.monotonic() + 5
        while not heard and time.monotonic() < deadline:
            time.sleep(0.01)
        assert heard == [True]
        set_and_wait(icon, None)
        assert icon.hotkey_registered is None
    finally:
        icon.close()
    assert not icon.thread.is_alive()


@windows_only
def test_a_hotkey_someone_else_holds_is_a_warning_not_a_crash(monkeypatch, caplog):
    combo = hotkey.parse(TEST_COMBO)
    user32 = wintray.api().user32
    assert user32.RegisterHotKey(None, 7, combo.modifiers, combo.vk)       # another program holds it
    icon = hidden_window(monkeypatch)
    try:
        with caplog.at_level("WARNING"):
            set_and_wait(icon, combo)
        assert icon.hotkey_registered is None
        assert icon.hotkey_error == wintray.ERROR_HOTKEY_ALREADY_REGISTERED
        assert "not registered: another program or Windows already uses it" in caplog.text
        assert icon.thread.is_alive()                            # the tray runs on without it
    finally:
        icon.close()
        user32.UnregisterHotKey(None, 7)


@windows_only
def test_a_missing_notification_area_is_one_warning_and_its_return_one_line(monkeypatch, caplog):
    """At login the taskbar may not be there yet: the add is retried every 2 s on a timer, and the
    warning is logged once, not on every retry. Nothing is added for real: the add fails, then
    "succeeds" through the fake."""
    added = []
    with caplog.at_level("INFO", logger="strawberryd.tray"):
        icon = hidden_window(monkeypatch)
        try:
            user32 = wintray.api().user32
            for _ in range(3):                                   # what the retry timer posts
                user32.PostMessageW(icon.hwnd, wintray.WM_TIMER, 1, 0)
            time.sleep(0.3)                                      # the window thread handles them
            assert icon.waiting and not icon.shown
            monkeypatch.setattr(wintray, "shell_notify_icon", lambda message, data: added.append(message) or True)
            user32.PostMessageW(icon.hwnd, wintray.WM_TIMER, 1, 0)
            deadline = time.monotonic() + 5
            while not icon.shown and time.monotonic() < deadline:
                time.sleep(0.01)
            assert icon.shown and not icon.waiting
        finally:
            icon.close()
    messages = [r.getMessage() for r in caplog.records]
    assert sum("not there yet" in m for m in messages) == 1, messages
    assert "icon in the notification area (it is there now)" in messages
