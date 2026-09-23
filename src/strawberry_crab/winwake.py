"""Resume from sleep on Windows: `PowerRegisterSuspendResumeNotification` (WIRING.md §4,
WINDOWS.md step 7), the counterpart of logind's `PrepareForSleep` in wake.py.

The daemon has no window, so the notification comes to a callback (DEVICE_NOTIFY_CALLBACK)
rather than as WM_POWERBROADCAST to a hidden window. Windows calls it on a thread of its own
with the same event codes the window message carries:

    PBT_APMSUSPEND          going to sleep (the callback must return at once: Windows waits)
    PBT_APMRESUMESUSPEND    resumed, and a user is there (it follows the automatic one)
    PBT_APMRESUMEAUTOMATIC  resumed, every time, whoever woke it

So PBT_APMRESUMEAUTOMATIC is the resume: the callback hands it to the daemon's loop and returns,
and the loop warms the models (Daemon.warm_models), as on Linux. The resume after hibernation is
the same event; on a Modern Standby machine the daemon hears it when Windows lets desktop apps
run again.

`PowerWatcher` answers what `wake.WakeWatcher` answers (`run`, `stats`, `resumes`, `connected`,
`reason`), so the daemon and /health do not care which one runs. Only the standard library
(ctypes, powrprof.dll); importable on any system, the registration is for Windows.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
from ctypes import wintypes
from typing import Awaitable, Callable

log = logging.getLogger("strawberryd.wake")

DEVICE_NOTIFY_CALLBACK = 2
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
ERROR_SUCCESS = 0

# ULONG CALLBACK DeviceNotifyCallbackRoutine(PVOID Context, ULONG Type, PVOID Setting)
CALLBACK = (ctypes.WINFUNCTYPE if hasattr(ctypes, "WINFUNCTYPE") else ctypes.CFUNCTYPE)(
    wintypes.ULONG, wintypes.LPVOID, wintypes.ULONG, wintypes.LPVOID)


class _SubscribeParameters(ctypes.Structure):
    """DEVICE_NOTIFY_SUBSCRIBE_PARAMETERS."""
    _fields_ = [("Callback", CALLBACK), ("Context", wintypes.LPVOID)]


class PowerError(OSError):
    """The power notification could not be registered (not Windows, or Windows said no)."""


class Registration:
    """One registered callback; `close()` unregisters it. The ctypes callback and the parameters
    stay referenced as long as the registration does, or Windows would call freed memory."""

    def __init__(self, handler: Callable[[int], None]) -> None:
        def callback(_context, event, _setting) -> int:
            try:
                handler(int(event))
            except Exception:  # noqa: BLE001 - never raise into Windows' thread
                log.exception("wake: the power callback failed")
            return ERROR_SUCCESS

        self.callback = CALLBACK(callback)
        self.parameters = _SubscribeParameters(self.callback, None)
        self.handle = wintypes.HANDLE()
        powrprof = _powrprof()
        code = powrprof.PowerRegisterSuspendResumeNotification(DEVICE_NOTIFY_CALLBACK, ctypes.byref(self.parameters),
                                                               ctypes.byref(self.handle))
        if code != ERROR_SUCCESS:
            raise PowerError(code, f"PowerRegisterSuspendResumeNotification failed ({code})")

    def close(self) -> None:
        if self.handle:
            _powrprof().PowerUnregisterSuspendResumeNotification(self.handle)
            self.handle = wintypes.HANDLE()


_dll = None


def _powrprof():
    global _dll
    if _dll is None:
        try:
            dll = ctypes.WinDLL("powrprof")
        except (AttributeError, OSError) as exc:   # not Windows
            raise PowerError(f"no powrprof.dll ({type(exc).__name__})") from exc
        dll.PowerRegisterSuspendResumeNotification.restype = wintypes.DWORD
        dll.PowerRegisterSuspendResumeNotification.argtypes = (wintypes.DWORD, wintypes.LPVOID,
                                                               ctypes.POINTER(wintypes.HANDLE))
        dll.PowerUnregisterSuspendResumeNotification.restype = wintypes.DWORD
        dll.PowerUnregisterSuspendResumeNotification.argtypes = (wintypes.HANDLE,)
        _dll = dll
    return _dll


def register(handler: Callable[[int], None]) -> Registration:
    """Call `handler(event)` from Windows' thread on each suspend and resume. Raises PowerError.
    tests/conftest.py replaces this with a refusal in every test."""
    return Registration(handler)


class PowerWatcher:
    """Calls `on_resume()` on the daemon's loop each time Windows says it has resumed."""

    def __init__(self, on_resume: Callable[[], Awaitable[None] | None],
                 registrar: Callable[[Callable[[int], None]], Registration] | None = None) -> None:
        self.on_resume = on_resume
        self.registrar = registrar
        self.resumes = 0
        self.connected = False
        self.reason = ""
        self._loop: asyncio.AbstractEventLoop | None = None

    async def run(self) -> None:
        """Watch until cancelled. Returns at once, after one log line, when Windows will not register."""
        self._loop = asyncio.get_running_loop()
        try:
            registration = (self.registrar or register)(self._from_windows)
        except Exception as exc:  # noqa: BLE001 - no powrprof (tests, another system): no warm-up, no crash
            self.reason = f"no power notifications ({type(exc).__name__})"
            log.info("wake: %s; no model warm-up on resume", self.reason)
            return
        self.connected = True
        self.reason = ""
        log.info("wake: watching Windows' suspend and resume notifications")
        try:
            await asyncio.Event().wait()
        finally:
            self.connected = False
            registration.close()

    def _from_windows(self, event: int) -> None:
        """On Windows' thread: hand the event to the loop and return at once."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._handle, event)
        except RuntimeError:   # the loop closed meanwhile
            pass

    def _handle(self, event: int) -> None:
        if event == PBT_APMSUSPEND:
            log.info("wake: going to sleep")
            return
        if event != PBT_APMRESUMEAUTOMATIC:
            return             # PBT_APMRESUMESUSPEND comes after the automatic one: the same resume
        self.resumes += 1
        log.info("wake: resumed from sleep; warming the models")
        result = self.on_resume()
        if asyncio.iscoroutine(result):
            asyncio.ensure_future(result)

    def stats(self) -> dict[str, object]:
        return {"watching": self.connected, "resumes": self.resumes, "reason": self.reason or None}
