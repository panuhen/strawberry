"""The tray icon on Windows: the berry in the notification area, with the Linux tray's menu
(WIRING.md §14, WINDOWS.md step 4).

Written on the Win32 API through ctypes, as the Linux one is written on jeepney: no tray library
and no Pillow. A hidden window of our own owns the icon (`Shell_NotifyIconW`, NOTIFYICON_VERSION_4)
and runs on a thread of its own with its message loop; the asyncio side is `TrayCore`
(traymenu.py), the same menu, clicks and health poll as on Linux, plus the supervisor.

- Right click (or the menu key): the popup menu, built from `menu_items()` at that moment with
  `InsertMenuItemW`: check marks (MFS_CHECKED), radio rows (MFT_RADIOCHECK), the four submenus,
  the disabled status row, separators; rows hidden on Linux (the per-app note when body_apps is
  empty) are simply not added. "Hide her"/"Show her" is the default row (bold), and a left click
  on the icon runs it, as Activate does on Linux.
- The tooltip is "Strawberry: <status>", updated when the status row changes.
- The icon is made from the packaged PNGs at runtime (icons.py), as a 32-bit HICON with alpha,
  at the size the notification area wants.
- Explorer restarting broadcasts "TaskbarCreated"; the icon is added again. At login the shell
  may not be ready: adding is retried every 2 s until it takes.
- Logging off: WM_ENDSESSION stops the children before Windows ends the process.
- The listen hotkey (`[voice] hotkey`, hotkey.py; default Ctrl+Alt+Space): `RegisterHotKey` on
  the same window, so WM_HOTKEY arrives in its message loop and becomes `POST /listen`, what
  `strawberry listen` sends. It is registered again whenever config.toml changes. A combination
  another program (or Windows) holds is one warning in the log, and the tray runs on without it.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import sys
import threading
import tomllib
import urllib.parse
from ctypes import wintypes
from pathlib import Path
from typing import Callable

from . import hotkey, icons, paths, winproc
from .client import stop_on_signals
from .supervisor import Children, child_specs, rotate_log
from .traymenu import SHOW_HIDE_ID, MenuItem, TrayCore

log = logging.getLogger("strawberryd.tray")

TITLE = "Strawberry"
WINDOW_CLASS = "StrawberryTrayWindow"
ICON_ID = 1
RETRY_ADD_MS = 2000
MENU_REFRESH_S = 0.5          # how long a right click waits for a fresh status before showing the menu

# Messages
WM_NULL, WM_DESTROY, WM_CLOSE = 0x0000, 0x0002, 0x0010
WM_QUERYENDSESSION, WM_ENDSESSION, WM_CONTEXTMENU, WM_TIMER = 0x0011, 0x0016, 0x007B, 0x0113
WM_APP = 0x8000
WM_TRAY = WM_APP + 1          # the icon's callback message
WM_TOOLTIP = WM_APP + 2       # the asyncio side changed the tooltip
WM_HIDE = WM_APP + 3          # the asyncio side is quitting: take the icon away now
WM_SETHOTKEY = WM_APP + 4     # the asyncio side read a new [voice] hotkey
WM_HOTKEY = 0x0312
HOTKEY_ID = 1
MOD_NOREPEAT = 0x4000         # holding the keys down sends one WM_HOTKEY, not a stream
ERROR_HOTKEY_ALREADY_REGISTERED = 1409
NIN_SELECT, NIN_KEYSELECT = 0x0400, 0x0401
# Shell_NotifyIcon
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_SHOWTIP = 0x01, 0x02, 0x04, 0x80
NOTIFYICON_VERSION_4 = 4
# Menus
MIIM_STATE, MIIM_ID, MIIM_SUBMENU, MIIM_STRING, MIIM_FTYPE = 0x001, 0x002, 0x004, 0x040, 0x100
MFT_STRING, MFT_RADIOCHECK, MFT_SEPARATOR = 0x000, 0x200, 0x800
MFS_DISABLED, MFS_CHECKED, MFS_DEFAULT = 0x003, 0x008, 0x1000
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x0002, 0x0080, 0x0100
# Icons
SM_CXSMICON = 49
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

LRESULT = ctypes.c_ssize_t


class NotifyIconError(RuntimeError):
    """The window or the icon could not be made."""


# --- structures -------------------------------------------------------------------------

class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),      # a union with uTimeout
                ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID),
                ("hBalloonIcon", wintypes.HICON)]


class MENUITEMINFOW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("fMask", wintypes.UINT), ("fType", wintypes.UINT),
                ("fState", wintypes.UINT), ("wID", wintypes.UINT), ("hSubMenu", wintypes.HMENU),
                ("hbmpChecked", wintypes.HBITMAP), ("hbmpUnchecked", wintypes.HBITMAP),
                ("dwItemData", ctypes.c_size_t), ("dwTypeData", wintypes.LPWSTR), ("cch", wintypes.UINT),
                ("hbmpItem", wintypes.HBITMAP)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]


class _Api:
    """user32, shell32 and gdi32 with the signatures we call, bound once."""

    def __init__(self) -> None:
        self.WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", self.WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
                        ("hIconSm", wintypes.HICON)]

        self.WNDCLASSEXW = WNDCLASSEXW
        u = self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        s = self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        g = self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        k = self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetModuleHandleW.restype = wintypes.HMODULE
        k.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        u.RegisterClassExW.restype = wintypes.ATOM
        u.RegisterClassExW.argtypes = (ctypes.POINTER(WNDCLASSEXW),)
        u.UnregisterClassW.argtypes = (wintypes.LPCWSTR, wintypes.HINSTANCE)
        u.CreateWindowExW.restype = wintypes.HWND
        u.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                      wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
        u.DestroyWindow.argtypes = (wintypes.HWND,)
        u.DefWindowProcW.restype = LRESULT
        u.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        u.GetMessageW.restype = wintypes.BOOL
        u.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
        u.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
        u.DispatchMessageW.restype = LRESULT
        u.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
        u.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        u.PostQuitMessage.argtypes = (ctypes.c_int,)
        u.RegisterWindowMessageW.restype = wintypes.UINT
        u.RegisterWindowMessageW.argtypes = (wintypes.LPCWSTR,)
        u.SetTimer.restype = ctypes.c_size_t
        u.SetTimer.argtypes = (wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID)
        u.KillTimer.argtypes = (wintypes.HWND, ctypes.c_size_t)
        u.SetForegroundWindow.argtypes = (wintypes.HWND,)
        u.CreatePopupMenu.restype = wintypes.HMENU
        u.DestroyMenu.argtypes = (wintypes.HMENU,)
        u.InsertMenuItemW.argtypes = (wintypes.HMENU, wintypes.UINT, wintypes.BOOL, ctypes.POINTER(MENUITEMINFOW))
        u.GetMenuItemCount.argtypes = (wintypes.HMENU,)
        u.GetMenuItemInfoW.argtypes = (wintypes.HMENU, wintypes.UINT, wintypes.BOOL, ctypes.POINTER(MENUITEMINFOW))
        u.TrackPopupMenuEx.restype = wintypes.BOOL
        u.TrackPopupMenuEx.argtypes = (wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       wintypes.LPVOID)
        u.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
        u.GetSystemMetrics.argtypes = (ctypes.c_int,)
        u.CreateIconIndirect.restype = wintypes.HICON
        u.CreateIconIndirect.argtypes = (ctypes.POINTER(ICONINFO),)
        u.DestroyIcon.argtypes = (wintypes.HICON,)
        u.GetIconInfo.argtypes = (wintypes.HICON, ctypes.POINTER(ICONINFO))
        u.RegisterHotKey.restype = wintypes.BOOL
        u.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
        u.UnregisterHotKey.restype = wintypes.BOOL
        u.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
        try:
            u.SetThreadDpiAwarenessContext.restype = wintypes.HANDLE
            u.SetThreadDpiAwarenessContext.argtypes = (wintypes.HANDLE,)
        except AttributeError:          # before Windows 10 1607
            pass
        s.Shell_NotifyIconW.restype = wintypes.BOOL
        s.Shell_NotifyIconW.argtypes = (wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
        g.CreateDIBSection.restype = wintypes.HBITMAP
        g.CreateDIBSection.argtypes = (wintypes.HDC, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD)
        g.CreateBitmap.restype = wintypes.HBITMAP
        g.CreateBitmap.argtypes = (ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, wintypes.LPVOID)
        g.DeleteObject.argtypes = (wintypes.HGDIOBJ,)


_api: _Api | None = None


def api() -> _Api:
    global _api
    if _api is None:
        _api = _Api()
    return _api


def shell_notify_icon(message: int, data: NOTIFYICONDATAW) -> bool:
    """The one call that shows anything on the user's taskbar (tests/conftest.py blocks it)."""
    return bool(api().shell32.Shell_NotifyIconW(message, ctypes.byref(data)))


def register_hotkey(hwnd, hotkey_id: int, modifiers: int, vk: int) -> int:
    """RegisterHotKey: 0 when it took, else the Windows error (1409: someone holds that key).
    The one call that takes a key from the user's desktop (tests/conftest.py allows only F24)."""
    if api().user32.RegisterHotKey(hwnd, hotkey_id, modifiers | MOD_NOREPEAT, vk):
        return 0
    return ctypes.get_last_error() or -1


def hotkey_error(error: int) -> str:
    if error == ERROR_HOTKEY_ALREADY_REGISTERED:
        return "another program or Windows already uses it"
    return ctypes.FormatError(error).strip() if error > 0 else "RegisterHotKey failed"


# --- the icon's pixels --------------------------------------------------------------------

def icon_size() -> int:
    """The notification area's icon size at this DPI (16 at 100 %, 24 at 150 %, 32 at 200 %)."""
    size = api().user32.GetSystemMetrics(SM_CXSMICON)
    return size if size > 0 else 16


def best_png(size: int, directory: Path | None = None) -> Path:
    """The smallest packaged PNG at least `size` wide, else the largest."""
    directory = Path(directory) if directory is not None else icons.icons_dir()
    have = [s for s in icons.ICON_SIZES if (directory / f"strawberry-{s}.png").is_file()]
    if not have:
        raise NotifyIconError(f"no icon PNGs in {directory}")
    chosen = min((s for s in have if s >= size), default=max(have))
    return directory / f"strawberry-{chosen}.png"


def make_hicon(width: int, height: int, rgba: bytes) -> int:
    """A 32-bit icon with alpha from RGBA pixels: a top-down BGRA DIB section and an empty mask."""
    a = api()
    header = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=width, biHeight=-height,
                              biPlanes=1, biBitCount=32, biCompression=0)
    bits = ctypes.c_void_p()
    color = a.gdi32.CreateDIBSection(None, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
    if not color:
        raise NotifyIconError(f"CreateDIBSection failed ({ctypes.get_last_error()})")
    mask = a.gdi32.CreateBitmap(width, height, 1, 1, None)
    try:
        ctypes.memmove(bits, icons.rgba_to_bgra(rgba), width * height * 4)
        info = ICONINFO(fIcon=True, xHotspot=0, yHotspot=0, hbmMask=mask, hbmColor=color)
        hicon = a.user32.CreateIconIndirect(ctypes.byref(info))
        if not hicon:
            raise NotifyIconError(f"CreateIconIndirect failed ({ctypes.get_last_error()})")
        return hicon
    finally:
        a.gdi32.DeleteObject(color)
        if mask:
            a.gdi32.DeleteObject(mask)


def load_icon(size: int | None = None) -> int:
    width, height, rgba = icons.read_png(best_png(size or icon_size()).read_bytes())
    return make_hicon(width, height, rgba)


# --- the menu -----------------------------------------------------------------------------

def menu_label(label: str) -> str:
    """A Win32 menu reads "&x" as a keyboard accelerator; ours are plain text."""
    return label.replace("&", "&&")


def build_menu(items: list[MenuItem], default_id: int | None = SHOW_HIDE_ID) -> int:
    """A popup menu (HMENU) of `items`, submenus included. The caller destroys it (DestroyMenu
    takes the submenus with it)."""
    a = api()
    menu = a.user32.CreatePopupMenu()
    if not menu:
        raise NotifyIconError(f"CreatePopupMenu failed ({ctypes.get_last_error()})")
    position = 0
    for item in items:
        if not item.visible:
            continue
        info = MENUITEMINFOW(cbSize=ctypes.sizeof(MENUITEMINFOW))
        if item.separator:
            info.fMask, info.fType = MIIM_FTYPE, MFT_SEPARATOR
        else:
            info.fMask = MIIM_FTYPE | MIIM_ID | MIIM_STRING | MIIM_STATE
            info.fType = MFT_STRING | (MFT_RADIOCHECK if item.toggle == "radio" else 0)
            info.wID = item.id
            info.dwTypeData = menu_label(item.label)
            info.fState = ((MFS_CHECKED if item.toggle and item.checked else 0)
                           | (0 if item.enabled else MFS_DISABLED)
                           | (MFS_DEFAULT if item.id == default_id else 0))
            if item.children:
                info.fMask |= MIIM_SUBMENU
                info.hSubMenu = build_menu(item.children, None)
        if not a.user32.InsertMenuItemW(menu, position, True, ctypes.byref(info)):
            error = ctypes.get_last_error()
            a.user32.DestroyMenu(menu)
            raise NotifyIconError(f"InsertMenuItemW failed ({error})")
        position += 1
    return menu


def read_menu(menu: int) -> list[dict]:
    """A menu read back through the API: what the user would see, for tests and the live check."""
    a = api()
    rows = []
    for position in range(a.user32.GetMenuItemCount(menu)):
        buffer = ctypes.create_unicode_buffer(256)
        info = MENUITEMINFOW(cbSize=ctypes.sizeof(MENUITEMINFOW),
                             fMask=MIIM_FTYPE | MIIM_ID | MIIM_STRING | MIIM_STATE | MIIM_SUBMENU,
                             dwTypeData=ctypes.cast(buffer, wintypes.LPWSTR), cch=len(buffer))
        if not a.user32.GetMenuItemInfoW(menu, position, True, ctypes.byref(info)):
            raise NotifyIconError(f"GetMenuItemInfoW failed ({ctypes.get_last_error()})")
        if info.fType & MFT_SEPARATOR:
            rows.append({"separator": True})
            continue
        rows.append({
            "id": info.wID, "label": buffer.value,
            "checked": bool(info.fState & MFS_CHECKED), "radio": bool(info.fType & MFT_RADIOCHECK),
            "enabled": not (info.fState & MFS_DISABLED), "default": bool(info.fState & MFS_DEFAULT),
            "submenu": read_menu(info.hSubMenu) if info.hSubMenu else None,
        })
    return rows


def tooltip(status: str) -> str:
    return f"{TITLE}: {status}"[:127]


# --- the icon and its window --------------------------------------------------------------

class NotifyIcon:
    """The hidden window and the icon, on a thread of their own. The callbacks run on that
    thread; they hand work to the asyncio loop and return."""

    def __init__(self, tip: str, items: Callable[[], list[MenuItem]], on_command: Callable[[int], None],
                 on_default: Callable[[], None], before_menu: Callable[[], None] = lambda: None,
                 on_end_session: Callable[[], None] = lambda: None,
                 on_hotkey: Callable[[], None] = lambda: None) -> None:
        self.tip = tip
        self.items = items
        self.on_command = on_command
        self.on_default = on_default
        self.before_menu = before_menu
        self.on_end_session = on_end_session
        self.on_hotkey = on_hotkey
        self.hotkey: hotkey.Hotkey | None = None      # what set_hotkey asked for
        self.hotkey_registered: hotkey.Hotkey | None = None
        self.hotkey_error = 0                          # the last registration's error, 0 = none
        self.hotkey_done = threading.Event()           # set after each registration attempt
        self.hwnd = None
        self.hicon = None
        self.shown = False
        self.thread: threading.Thread | None = None
        self.ready = threading.Event()
        self.error: BaseException | None = None
        self._proc = None                      # the WNDPROC, kept alive as long as the window
        self._taskbar_created = 0

    def start(self, timeout_s: float = 10.0) -> None:
        """Make the window and show the icon; raises NotifyIconError if the window cannot be made."""
        self.thread = threading.Thread(target=self._run, name="notify-icon", daemon=True)
        self.thread.start()
        if not self.ready.wait(timeout_s):
            raise NotifyIconError("the notification icon's thread did not start")
        if self.error is not None:
            raise NotifyIconError(str(self.error)) from self.error

    def set_tooltip(self, tip: str) -> None:
        self.tip = tip
        self._post(WM_TOOLTIP)

    def hide(self) -> None:
        self._post(WM_HIDE)

    def set_hotkey(self, wanted: hotkey.Hotkey | None) -> None:
        """Register `wanted` in place of the current hotkey (None: none), on the window's thread:
        a hotkey belongs to the thread and the window that registered it."""
        self.hotkey = wanted
        self.hotkey_done.clear()
        self._post(WM_SETHOTKEY)

    def close(self, timeout_s: float = 5.0) -> None:
        self._post(WM_CLOSE)
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout_s)

    def _post(self, message: int) -> None:
        if self.hwnd:
            api().user32.PostMessageW(self.hwnd, message, 0, 0)

    # --- the window's thread --------------------------------------------------

    def _run(self) -> None:
        a = api()
        instance = a.kernel32.GetModuleHandleW(None)
        try:
            try:
                a.user32.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
            except AttributeError:
                pass
            self._proc = a.WNDPROC(self._window_proc)
            window_class = a.WNDCLASSEXW(cbSize=ctypes.sizeof(a.WNDCLASSEXW), lpfnWndProc=self._proc,
                                         hInstance=instance, lpszClassName=WINDOW_CLASS)
            if not a.user32.RegisterClassExW(ctypes.byref(window_class)) and ctypes.get_last_error() != 1410:
                raise NotifyIconError(f"RegisterClassExW failed ({ctypes.get_last_error()})")   # 1410: exists
            # A top-level window that is never shown: a message-only one would not hear TaskbarCreated.
            self.hwnd = a.user32.CreateWindowExW(0, WINDOW_CLASS, TITLE, 0, 0, 0, 0, 0, None, None, instance, None)
            if not self.hwnd:
                raise NotifyIconError(f"CreateWindowExW failed ({ctypes.get_last_error()})")
            self._taskbar_created = a.user32.RegisterWindowMessageW("TaskbarCreated")
            self.hicon = load_icon()
            self._add()
        except BaseException as exc:  # noqa: BLE001 - reported to start()
            self.error = exc
            if self.hwnd:
                a.user32.DestroyWindow(self.hwnd)
                self.hwnd = None
            if self.hicon:
                a.user32.DestroyIcon(self.hicon)
                self.hicon = None
            a.user32.UnregisterClassW(WINDOW_CLASS, instance)
            self.ready.set()
            return
        self.ready.set()
        message = wintypes.MSG()
        while a.user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            a.user32.TranslateMessage(ctypes.byref(message))
            a.user32.DispatchMessageW(ctypes.byref(message))
        if self.hicon:
            a.user32.DestroyIcon(self.hicon)
            self.hicon = None
        # The class holds this icon's window procedure: left registered, the next icon in this
        # process would find it (1410) and its messages would go to this one's closed window.
        a.user32.UnregisterClassW(WINDOW_CLASS, instance)

    def _data(self, flags: int) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=ICON_ID, uFlags=flags,
                               uCallbackMessage=WM_TRAY, hIcon=self.hicon)
        data.szTip = self.tip[:127]
        return data

    def _add(self) -> bool:
        if not shell_notify_icon(NIM_ADD, self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP)):
            if not self.shown:
                log.warning("the notification area is not there yet; trying again every %d s", RETRY_ADD_MS // 1000)
            api().user32.SetTimer(self.hwnd, 1, RETRY_ADD_MS, None)
            self.shown = False
            return False
        version = self._data(0)
        version.uVersion = NOTIFYICON_VERSION_4
        shell_notify_icon(NIM_SETVERSION, version)
        api().user32.KillTimer(self.hwnd, 1)
        self.shown = True
        log.info("icon in the notification area")
        return True

    def _remove(self) -> None:
        if self.shown:
            shell_notify_icon(NIM_DELETE, self._data(0))
            self.shown = False

    def _window_proc(self, hwnd, message, wparam, lparam):
        try:
            handled = self._handle(hwnd, message, wparam, lparam)
        except Exception as exc:  # noqa: BLE001 - an exception must not cross back into user32
            log.warning("tray window message %#x failed: %r", message, exc)
            handled = None
        if handled is not None:
            return handled
        return api().user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _handle(self, hwnd, message, wparam, lparam):
        if message == WM_TRAY:
            event = lparam & 0xFFFF
            if event == WM_CONTEXTMENU:
                x, y = ctypes.c_short(wparam & 0xFFFF).value, ctypes.c_short((wparam >> 16) & 0xFFFF).value
                self._show_menu(x, y)
            elif event in (NIN_SELECT, NIN_KEYSELECT):
                self.on_default()
            return 0
        if message == self._taskbar_created and message:
            self.shown = False
            log.info("the taskbar came back; adding the icon again")
            self._add()
            return 0
        if message == WM_TIMER:
            if not self.shown:
                self._add()
            return 0
        if message == WM_TOOLTIP:
            if self.shown:
                shell_notify_icon(NIM_MODIFY, self._data(NIF_TIP | NIF_SHOWTIP))
            return 0
        if message == WM_HIDE:
            self._remove()
            return 0
        if message == WM_SETHOTKEY:
            self._register_hotkey()
            return 0
        if message == WM_HOTKEY:
            if wparam == HOTKEY_ID:
                self.on_hotkey()
            return 0
        if message == WM_QUERYENDSESSION:
            return 1
        if message == WM_ENDSESSION:
            if wparam:
                self._remove()
                self.on_end_session()      # blocks until the children have stopped (or a timeout)
            return 0
        if message == WM_CLOSE:
            self._remove()
            self._unregister_hotkey()
            api().user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            self.hwnd = None
            api().user32.PostQuitMessage(0)
            return 0
        return None

    def _unregister_hotkey(self) -> None:
        if self.hotkey_registered is not None:
            api().user32.UnregisterHotKey(self.hwnd, HOTKEY_ID)
            self.hotkey_registered = None

    def _register_hotkey(self) -> None:
        wanted = self.hotkey
        try:
            if wanted == self.hotkey_registered:
                return
            self._unregister_hotkey()
            if wanted is None:
                self.hotkey_error = 0
                log.info("hotkey: none")
                return
            self.hotkey_error = register_hotkey(self.hwnd, HOTKEY_ID, wanted.modifiers, wanted.vk)
            if self.hotkey_error:
                log.warning("hotkey %s not registered: %s; choose another with: strawberry hotkey COMBO",
                            wanted.text, hotkey_error(self.hotkey_error))
            else:
                self.hotkey_registered = wanted
                log.info("hotkey %s registered: it runs listen", wanted.text)
        finally:
            self.hotkey_done.set()

    def _show_menu(self, x: int, y: int) -> None:
        a = api()
        self.before_menu()
        menu = build_menu(self.items())
        try:
            # The documented dance: foreground first, or the menu does not close when you click away.
            a.user32.SetForegroundWindow(self.hwnd)
            chosen = a.user32.TrackPopupMenuEx(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY, x, y,
                                               self.hwnd, None)
            a.user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        finally:
            a.user32.DestroyMenu(menu)
        if chosen:
            self.on_command(int(chosen))


# --- the tray -----------------------------------------------------------------------------

class WindowsTray(TrayCore):
    """TrayCore with the notification-area icon in front of it."""

    END_SESSION_WAIT_S = 10.0

    def __init__(self, daemon_url: str, children: Children | None = None, prefs_path: Path | None = None,
                 config_path: Path | None = None) -> None:
        super().__init__(daemon_url, children, prefs_path=prefs_path, config_path=config_path)
        self.icon: NotifyIcon | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stopped = threading.Event()        # the children are down: logoff may go on
        self.hotkey_setting: str | None = None  # `[voice] hotkey` as last read
        self.hotkey: hotkey.Hotkey | None = None
        self.listens = 0                        # hotkey presses handed to the daemon
        self.port = urllib.parse.urlsplit(daemon_url).port or 0

    def read_config(self) -> None:
        """The body mode as on Linux, and `[voice] hotkey`: a new one is registered at once."""
        seen = self.config_seen
        super().read_config()
        if self.config_seen != seen or self.hotkey_setting is None:
            self.read_hotkey()

    def read_hotkey(self) -> None:
        try:
            setting = hotkey.read_setting(self.config_file())
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            return                              # mid-edit: the next change reads it again
        if setting == self.hotkey_setting:
            return
        self.hotkey_setting = setting
        try:
            self.hotkey = hotkey.windows_hotkey(setting)
        except ValueError as exc:
            log.warning("hotkey: voice.hotkey %r is not a key combination (%s); none registered", setting, exc)
            self.hotkey = None
        if self.icon is not None:
            self.icon.set_hotkey(self.hotkey)

    async def listen(self) -> None:
        """The hotkey: what `strawberry listen` does, a bare POST /listen (again = stop early)."""
        from .cli import listen_fast

        self.listens += 1
        if await asyncio.to_thread(listen_fast, self.port):
            log.warning("hotkey: the daemon did not take /listen (not running, or voice is off)")
        else:
            log.info("hotkey: listen")

    async def announce(self, updated: list[tuple[int, dict]], status_changed: bool) -> None:
        if status_changed and self.icon is not None:
            self.icon.set_tooltip(tooltip(self.state.status_label()))

    def restart_requested(self) -> None:
        """`strawberry restart` (her menu's "Apply settings"): what the Restart row does."""
        log.info("restart requested")
        asyncio.ensure_future(self.activate("restart"))

    # --- called on the icon's thread ------------------------------------------

    def _submit(self, coroutine) -> None:
        asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def _refresh_before_menu(self) -> None:
        """AboutToShow, as on Linux: a fresh status row, if the daemon answers quickly."""
        future = asyncio.run_coroutine_threadsafe(self.refresh(), self.loop)
        try:
            future.result(MENU_REFRESH_S)
        except Exception:  # noqa: BLE001 - a slow or failed refresh shows the rows we have
            pass

    def _end_session(self) -> None:
        log.info("the session is ending; stopping")
        self.loop.call_soon_threadsafe(self.stopping.set)
        self.stopped.wait(self.END_SESSION_WAIT_S)

    # --- the loop -------------------------------------------------------------

    async def run(self) -> int:
        self.loop = asyncio.get_running_loop()
        if self.children is None:
            await self.refresh()      # ask before showing: the first tooltip should be true
        else:
            # Our own daemon is not started yet, and a refused connection to 127.0.0.1 takes
            # about 2 s on Windows: show the icon now, the poll fills in the status.
            self.read_prefs()
            self.read_config()
            await self.publish()
        self.icon = NotifyIcon(
            tooltip(self.state.status_label()), items=lambda: self.items,
            on_command=lambda item_id: self._submit(self.clicked(item_id)),
            on_default=lambda: self._submit(self.activate(self.show_or_hide())),
            before_menu=self._refresh_before_menu, on_end_session=self._end_session,
            on_hotkey=lambda: self._submit(self.listen()))
        try:
            await asyncio.to_thread(self.icon.start)
        except NotifyIconError as exc:
            log.error("no notification icon (%s)", exc)
            return 3
        self.read_hotkey()
        self.icon.set_hotkey(self.hotkey)
        try:
            if self.children:
                self.children.start()
            poller = asyncio.ensure_future(self.poll())
            stop = asyncio.ensure_future(self.stopping.wait())
            await asyncio.wait([poller, stop], return_when=asyncio.FIRST_COMPLETED)
            for task in (poller, stop):
                task.cancel()
            return 0
        finally:
            self.icon.hide()
            if self.children:
                await self.children.stop()
            self.stopped.set()
            await asyncio.to_thread(self.icon.close)
            log.info("tray stopped")


def log_handlers() -> list[logging.Handler]:
    """`<state>\\tray.log` (with dates: it outlives a day), and the console when there is one; a
    tray started from the Startup shortcut has none (pythonw)."""
    path = paths.state_dir() / "tray.log"
    rotate_log(path)
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                                                datefmt="%Y-%m-%d %H:%M:%S"))
    handlers: list[logging.Handler] = [file_handler]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    return handlers


async def run_tray(port: int, children: bool = True, widget: bool = True, config: Path | None = None) -> int:
    tray = WindowsTray(f"http://127.0.0.1:{port}", config_path=config)
    if children:
        tray.children = Children(child_specs(port, config, widget=widget),
                                 state_path=paths.tray_state_file(), log_dir=paths.state_dir())
    stop_on_signals(tray.stopping)     # Ctrl+C in a console, and the stop event (`strawberry stop`)
    loop = asyncio.get_running_loop()
    try:
        winproc.listen_for_restart(lambda: loop.call_soon_threadsafe(tray.restart_requested))
    except OSError as exc:
        log.warning("no restart event (%s); `strawberry restart` will stop and start the tray", exc)
    return await tray.run()
