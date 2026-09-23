r"""Stopping our own processes cleanly on Windows, from another process (WIRING.md §2, §14).

On Linux that is SIGTERM. On Windows `os.kill(pid, SIGTERM)` is TerminateProcess, which nothing
can catch, and Ctrl+Break reaches only a process that shares the sender's console. So each of our
long-running processes (the daemon, the doorways, the tray) waits on a **named event** of its own
and shuts down as on SIGTERM when it is set:

    Local\strawberry-stop-<key>

`<key>` is the process's own pid, and also the key its parent handed it in STRAWBERRY_STOP_EVENT
(the tray's supervisor and `strawberry daemon` do that). Both are needed because on Windows the
pid a parent sees is often not the interpreter's: a venv's python.exe and uv's script launchers
start the real interpreter as a child of their own.

Who may set it: `Local\` is the logon session's own namespace, and an event created without a
security descriptor takes the default DACL of the creating process's token, which grants the
user, SYSTEM and the logon session, and nobody else. No port is opened for it. `stop()` sets the
event, waits for the process to end, and falls back to TerminateProcess after a timeout.

A tray also answers `Local\strawberry-restart-<pid>` (listen_for_restart): `strawberry restart`
sets it, and the tray restarts its children rather than being stopped from inside its own job.

`KillOnCloseJob` is the other half: a job object that ends every process in it when the last
handle to it closes, so the tray's children (and a Godot started by `strawberry widget`) go with
the process that started them, even when that one is killed.

Only the standard library (ctypes). Importable on any system; the functions are for Windows.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time
from ctypes import wintypes
from typing import Callable

STOP_ENV = "STRAWBERRY_STOP_EVENT"
PREFIX = "Local\\strawberry-stop-"
RESTART_PREFIX = "Local\\strawberry-restart-"

SYNCHRONIZE = 0x00100000
EVENT_MODIFY_STATE = 0x0002
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WAIT_OBJECT_0 = 0
INFINITE = 0xFFFFFFFF
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


def event_name(key: str) -> str:
    return PREFIX + key


_kernel32 = None


def kernel32():
    """kernel32 with the signatures we call, set once (handles are pointer-sized)."""
    global _kernel32
    if _kernel32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateEventW.restype = wintypes.HANDLE
        k.CreateEventW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
        k.OpenEventW.restype = wintypes.HANDLE
        k.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
        k.SetEvent.argtypes = (wintypes.HANDLE,)
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k.WaitForMultipleObjects.restype = wintypes.DWORD
        k.WaitForMultipleObjects.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL,
                                             wintypes.DWORD)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        k.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
        k.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        _kernel32 = k
    return _kernel32


# --- the process that can be stopped --------------------------------------------------

def listen_for_stop(callback: Callable[[], None]) -> list[str]:
    """Create this process's stop events and call `callback` (from a thread of its own) once one
    is set. Returns the names listened on; [] when none could be made.

    STRAWBERRY_STOP_EVENT is taken out of the environment, so a process this one starts does not
    answer to its parent's key."""
    keys = [str(os.getpid())]
    handed = os.environ.pop(STOP_ENV, "").strip()
    if handed and handed not in keys:
        keys.append(handed)
    k = kernel32()
    handles, names = [], []
    for key in keys:
        handle = k.CreateEventW(None, True, False, event_name(key))     # manual reset: stays set
        if handle:
            handles.append(handle)
            names.append(event_name(key))
    if not handles:
        return []
    array = (wintypes.HANDLE * len(handles))(*handles)

    def wait() -> None:
        if k.WaitForMultipleObjects(len(handles), array, False, INFINITE) < WAIT_OBJECT_0 + len(handles):
            callback()

    threading.Thread(target=wait, name="stop-event", daemon=True).start()
    return names


def listen_for_restart(callback: Callable[[], None]) -> str | None:
    """The tray's second event, `Local\\strawberry-restart-<pid>`: `strawberry restart` (her menu's
    "Apply settings") sets it and the tray restarts its children, as its own Restart row does.
    Auto-reset, so it can be set again. Returns its name, or None."""
    k = kernel32()
    name = RESTART_PREFIX + str(os.getpid())
    handle = k.CreateEventW(None, False, False, name)
    if not handle:
        return None

    def wait() -> None:
        while k.WaitForSingleObject(handle, INFINITE) == WAIT_OBJECT_0:
            callback()

    threading.Thread(target=wait, name="restart-event", daemon=True).start()
    return name


# --- the process that stops it --------------------------------------------------------

def request_restart(pid: int) -> bool:
    """Set a tray's restart event. False when it has none (not a tray, or not running)."""
    k = kernel32()
    handle = k.OpenEventW(EVENT_MODIFY_STATE, False, RESTART_PREFIX + str(pid))
    if not handle:
        return False
    try:
        return bool(k.SetEvent(handle))
    finally:
        k.CloseHandle(handle)


def request_stop(key: str) -> bool:
    """Set `key`'s stop event. False when there is none (the process is gone, or never listened)."""
    k = kernel32()
    handle = k.OpenEventW(EVENT_MODIFY_STATE, False, event_name(key))
    if not handle:
        return False
    try:
        return bool(k.SetEvent(handle))
    finally:
        k.CloseHandle(handle)


def stop_event_exists(key: str) -> bool:
    """Whether a process listens on `key`'s stop event. Opened for SYNCHRONIZE only, which cannot
    set it, and closed at once: `strawberry doctor` asks this of the tray."""
    k = kernel32()
    handle = k.OpenEventW(SYNCHRONIZE, False, event_name(key))
    if not handle:
        return False
    k.CloseHandle(handle)
    return True


def wait_exit(pid: int, timeout_s: float) -> bool:
    """True once `pid` has ended (or was never there), False if it is still running at the timeout."""
    k = kernel32()
    handle = k.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return ctypes.get_last_error() != 5      # ERROR_ACCESS_DENIED: running, and not ours to wait on
    try:
        return k.WaitForSingleObject(handle, max(0, int(timeout_s * 1000))) == WAIT_OBJECT_0
    finally:
        k.CloseHandle(handle)


def terminate(pid: int) -> bool:
    k = kernel32()
    handle = k.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        if not k.TerminateProcess(handle, 1):
            return False
        k.WaitForSingleObject(handle, 5000)
        return True
    finally:
        k.CloseHandle(handle)


def stop(pid: int, key: str | None = None, timeout_s: float = 10.0) -> str:
    """Stop `pid`: its stop event (`key`, else its own pid), then TerminateProcess after `timeout_s`.

    "stopped" (it shut down itself), "killed" (it had no event, or did not go in time), "gone"
    (it was not running)."""
    if wait_exit(pid, 0):
        return "gone"
    asked = request_stop(key or str(pid))
    if not asked and key:
        asked = request_stop(str(pid))
    if asked:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if wait_exit(pid, 0.25):
                return "stopped"
    terminate(pid)
    return "killed"


# --- children that go with their parent -----------------------------------------------

class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount",
                                                    "OtherOperationCount", "ReadTransferCount",
                                                    "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class KillOnCloseJob:
    """A job object whose processes end when its last handle closes: ours, at exit at the latest."""

    def __init__(self) -> None:
        k = kernel32()
        self.handle = k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(self.handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                                         ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            k.CloseHandle(self.handle)
            raise ctypes.WinError(error)

    def add(self, pid: int) -> None:
        k = kernel32()
        process = k.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not k.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            k.CloseHandle(process)

    def close(self) -> None:
        """Ends whatever is still in the job."""
        if self.handle:
            kernel32().CloseHandle(self.handle)
            self.handle = None


def call_tied(argv: list[str]) -> int:
    """subprocess.call, with the child in a kill-on-close job: when this process ends in any way
    (TerminateProcess from the tray's restart or `strawberry stop` included), so does the child.
    Without it `strawberry widget` in developer mode, ended so, left Godot running on its own."""
    process = subprocess.Popen(argv)
    job = None
    try:
        try:
            job = KillOnCloseJob()
            job.add(process.pid)
        except OSError:
            job = None          # the child runs untied, as subprocess.call would have it
        return process.wait()
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        if job is not None:
            job.close()
