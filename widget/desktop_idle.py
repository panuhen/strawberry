"""Read aggregate desktop idle time; never reads or records keys or mouse contents.

One JSON result per invocation. Godot runs this off its rendering thread.
X11 uses XScreenSaver; GNOME Wayland uses Mutter's aggregate idle monitor; Windows uses
GetLastInputInfo, the time of the session's last input event.
An unavailable monitor reports null, so automatic sleep stays disabled safely.
"""
import ctypes
import ctypes.util
import json
import os
import re
import subprocess


def x11_idle():
    class Info(ctypes.Structure):
        _fields_ = [('window', ctypes.c_ulong), ('state', ctypes.c_int),
                    ('kind', ctypes.c_int), ('since', ctypes.c_ulong),
                    ('idle', ctypes.c_ulong), ('event_mask', ctypes.c_ulong)]
    x11 = ctypes.CDLL(ctypes.util.find_library('X11') or 'libX11.so.6')
    xss = ctypes.CDLL(ctypes.util.find_library('Xss') or 'libXss.so.1')
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x11.XDefaultRootWindow.restype = ctypes.c_ulong
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    xss.XScreenSaverQueryInfo.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Info)]
    display = x11.XOpenDisplay(None)
    if not display:
        raise RuntimeError('No X11 display')
    try:
        info = Info()
        if not xss.XScreenSaverQueryInfo(display, x11.XDefaultRootWindow(display), ctypes.byref(info)):
            raise RuntimeError('XScreenSaver unavailable')
        return info.idle / 1000.0
    finally:
        x11.XCloseDisplay(display)


def gnome_idle():
    result = subprocess.run([
        'gdbus', 'call', '--session', '--dest', 'org.gnome.Mutter.IdleMonitor',
        '--object-path', '/org/gnome/Mutter/IdleMonitor/Core',
        '--method', 'org.gnome.Mutter.IdleMonitor.GetIdletime'],
        capture_output=True, text=True, timeout=1.5, check=True)
    match = re.fullmatch(r'\(uint64 (\d+),?\)\s*', result.stdout)
    if not match:
        raise RuntimeError('Unexpected idle monitor response')
    return int(match.group(1)) / 1000.0


def windows_idle():
    class LastInput(ctypes.Structure):
        _fields_ = [('size', ctypes.c_uint), ('time', ctypes.c_uint32)]
    user32 = ctypes.WinDLL('user32')
    kernel32 = ctypes.WinDLL('kernel32')
    kernel32.GetTickCount.restype = ctypes.c_uint32
    info = LastInput(ctypes.sizeof(LastInput), 0)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        raise RuntimeError('GetLastInputInfo failed')
    # Both are 32-bit millisecond tick counts, which wrap after 49.7 days.
    return ((kernel32.GetTickCount() - info.time) & 0xFFFFFFFF) / 1000.0


def sample():
    if os.name == 'nt':
        try:
            return {'seconds': windows_idle(), 'source': 'windows'}
        except (OSError, RuntimeError):
            return {'seconds': None, 'source': 'unavailable'}
    # XWayland's counter excludes native Wayland input. Never use it on Wayland.
    wayland = os.environ.get('XDG_SESSION_TYPE') == 'wayland' or bool(os.environ.get('WAYLAND_DISPLAY'))
    try:
        return {'seconds': gnome_idle() if wayland else x11_idle(),
                'source': 'gnome' if wayland else 'x11'}
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return {'seconds': None, 'source': 'unavailable'}


if __name__ == '__main__':
    print(json.dumps(sample()))
