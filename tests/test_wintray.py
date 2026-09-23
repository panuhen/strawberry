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

from strawberry_crab import icons, wintray
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

    def __init__(self, tip, items, on_command, on_default, before_menu, on_end_session):
        self.tip, self.items, self.on_command, self.on_default = tip, items, on_command, on_default
        self.tips, self.hidden, self.closed = [tip], False, False
        FakeIcon.made.append(self)

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
