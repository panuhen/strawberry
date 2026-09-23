r"""WASAPI through ctypes: one process's audio by process loopback, and who is playing what
(WIRING.md §4c, WINDOWS.md step 5).

Process loopback (Windows 10 2004 and later) captures what one process tree renders, and
nothing else: not the microphone, not the other apps, not her own voice.

    ActivateAudioInterfaceAsync("VAD\Process_Loopback", IID_IAudioClient,
                                AUDIOCLIENT_ACTIVATION_PARAMS{PROCESS_LOOPBACK, pid, INCLUDE_TREE})

The activation is asynchronous: Windows calls our IActivateAudioInterfaceCompletionHandler on a
worker thread of its own, so the handler must be agile (it answers IAgileObject, and it only
stores the result and sets an event). The IAudioClient it hands back has no mix format of its
own (GetMixFormat is E_NOTIMPL on the virtual device), so we ask for the format we want, mono
32-bit float at the tracker's rate, and AUTOCONVERTPCM makes the audio engine resample and mix
down to it. The capture is event driven: the engine sets our event for every packet, and a
packet flagged SILENT is zeros.

`audio_sessions()` lists the render sessions on every active output device (IAudioSessionManager2):
which process renders sound, and whether it does right now. `processes()` is the Toolhelp
process table (pid, parent, exe name), `package_app_id()` a packaged process's AppUserModelId.
The beat doorway (doorways/beat_loopback.py) uses these three to find the player's process from
its media session.

Only the standard library (ctypes) and numpy. Importable on any system; the functions are for
Windows.
"""

from __future__ import annotations

import ctypes
import functools
import threading
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass

import numpy as np

WINFUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)   # Windows only; the module imports anywhere
HRESULT = getattr(ctypes, "HRESULT", ctypes.c_long)

S_OK = 0
E_NOINTERFACE = -2147467262          # 0x80004002
RPC_E_CHANGED_MODE = -2147417850     # 0x80010106: COM already set up another way on this thread; fine
COINIT_MULTITHREADED = 0
CLSCTX_ALL = 0x17
VT_BLOB = 65
LOOPBACK_DEVICE = "VAD\\Process_Loopback"   # VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
WAVE_FORMAT_IEEE_FLOAT = 3
E_RENDER = 0
DEVICE_STATE_ACTIVE = 1
AUDIO_SESSION_STATE_ACTIVE = 1
TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
BUFFER_100NS = 2_000_000             # the capture buffer: 200 ms, as Microsoft's loopback sample


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, text: str) -> GUID:
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


IID_IUNKNOWN = "00000000-0000-0000-c000-000000000046"
IID_IAGILEOBJECT = "94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90"
IID_COMPLETION_HANDLER = "41d949ab-9862-444a-80f6-c261334da5eb"   # IActivateAudioInterfaceCompletionHandler
IID_IAUDIOCLIENT = "1cb9ad4c-dbfa-4c32-b178-c2f568a703b2"
IID_IAUDIOCAPTURECLIENT = "c8adbd64-e71e-48a0-a4de-185c395cd317"
CLSID_MMDEVICEENUMERATOR = "bcde0395-e52f-467c-8e3d-c4579291692e"
IID_IMMDEVICEENUMERATOR = "a95664d2-9614-4f35-a746-de8db63617e6"
IID_IAUDIOSESSIONMANAGER2 = "77aa99a0-1bd6-484f-8bc7-2c654c9a9b6f"
IID_IAUDIOSESSIONCONTROL2 = "bfb7ff88-7239-4fc9-8fa2-07c950be9c6d"


class _Blob(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.ULONG), ("pBlobData", ctypes.c_void_p)]


class _PropVariant(ctypes.Structure):
    _fields_ = [("vt", wintypes.USHORT), ("reserved1", wintypes.USHORT), ("reserved2", wintypes.USHORT),
                ("reserved3", wintypes.USHORT), ("blob", _Blob)]


class _ActivationParams(ctypes.Structure):
    """AUDIOCLIENT_ACTIVATION_PARAMS with its AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS."""
    _fields_ = [("ActivationType", ctypes.c_int), ("TargetProcessId", wintypes.DWORD),
                ("ProcessLoopbackMode", ctypes.c_int)]


class _WaveFormatEx(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD), ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD), ("nBlockAlign", wintypes.WORD),
                ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD)]


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


# --- COM by hand --------------------------------------------------------------------

class Com:
    """A COM interface pointer: call a method by its vtable slot, release it once."""

    def __init__(self, ptr: int | None) -> None:
        if not ptr:
            raise OSError("no interface")
        self.ptr = ptr

    def call(self, index: int, argtypes: tuple, *args, restype=HRESULT):
        vtable = ctypes.cast(self.ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        return _prototype(restype, argtypes)(vtable[index])(self.ptr, *args)

    def out(self, index: int, *args, argtypes: tuple = ()) -> Com:
        """A method whose last argument is an interface pointer it hands back."""
        result = ctypes.c_void_p()
        self.call(index, (*argtypes, ctypes.POINTER(ctypes.c_void_p)), *args, ctypes.byref(result))
        return Com(result.value)

    def query(self, iid: str) -> Com:
        return self.out(0, ctypes.byref(GUID.of(iid)), argtypes=(ctypes.POINTER(GUID),))

    def release(self) -> None:
        if self.ptr:
            self.call(2, (), restype=wintypes.ULONG)
            self.ptr = None


@functools.lru_cache(maxsize=None)
def _prototype(restype, argtypes: tuple):
    """One function type per signature: the capture calls the same few methods 100 times a second."""
    return WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)


_ole32 = _kernel32 = _mmdevapi = None


def ole32():
    global _ole32
    if _ole32 is None:
        o = ctypes.WinDLL("ole32")
        o.CoInitializeEx.restype = ctypes.c_long
        o.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
        o.CoCreateInstance.restype = ctypes.c_long
        o.CoCreateInstance.argtypes = (ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(GUID),
                                       ctypes.POINTER(ctypes.c_void_p))
        o.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
        _ole32 = o
    return _ole32


def kernel32():
    global _kernel32
    if _kernel32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateEventW.restype = wintypes.HANDLE
        k.CreateEventW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        k.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))
        k.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))
        k.GetApplicationUserModelId.restype = wintypes.LONG
        k.GetApplicationUserModelId.argtypes = (wintypes.HANDLE, ctypes.POINTER(ctypes.c_uint32), wintypes.LPWSTR)
        _kernel32 = k
    return _kernel32


def mmdevapi():
    global _mmdevapi
    if _mmdevapi is None:
        m = ctypes.WinDLL("mmdevapi")
        m.ActivateAudioInterfaceAsync.restype = HRESULT
        m.ActivateAudioInterfaceAsync.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(GUID), ctypes.POINTER(_PropVariant),
                                                  ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        _mmdevapi = m
    return _mmdevapi


def com_init() -> None:
    """COM on this thread, multithreaded (where winrt got here first, as it may, its choice stands)."""
    hr = ole32().CoInitializeEx(None, COINIT_MULTITHREADED)
    if hr < 0 and hr != RPC_E_CHANGED_MODE:
        raise ctypes.WinError(hr)


# --- the completion handler -----------------------------------------------------------

_QueryInterface = WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
_AddRefRelease = WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)
_ActivateCompleted = WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)


class _HandlerVtbl(ctypes.Structure):
    _fields_ = [("QueryInterface", _QueryInterface), ("AddRef", _AddRefRelease), ("Release", _AddRefRelease),
                ("ActivateCompleted", _ActivateCompleted)]


class _HandlerObject(ctypes.Structure):
    _fields_ = [("vtbl", ctypes.POINTER(_HandlerVtbl))]


# Handlers Windows may still hold a reference to. A handler leaves only once its count is back to
# zero, and never from inside one of its own callbacks (that would free the code that is running).
_handlers: list[_CompletionHandler] = []


class _CompletionHandler:
    """IActivateAudioInterfaceCompletionHandler + IAgileObject, as a C vtable of ctypes callbacks.
    ActivateCompleted runs on a Windows worker thread: it reads the result and sets `done`."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.hr = -1
        self.interface: int | None = None
        self.refs = 1
        self.iids = {bytes(GUID.of(i)) for i in (IID_IUNKNOWN, IID_IAGILEOBJECT, IID_COMPLETION_HANDLER)}
        self.vtbl = _HandlerVtbl(_QueryInterface(self._query), _AddRefRelease(self._add_ref),
                                 _AddRefRelease(self._release), _ActivateCompleted(self._completed))
        self.obj = _HandlerObject(ctypes.pointer(self.vtbl))
        _handlers[:] = [h for h in _handlers if h.refs > 0]
        _handlers.append(self)

    @property
    def pointer(self) -> int:
        return ctypes.addressof(self.obj)

    def _query(self, this, riid, ppv) -> int:
        if bytes(riid.contents) in self.iids:
            ppv[0] = this
            self.refs += 1
            return S_OK
        ppv[0] = None
        return E_NOINTERFACE

    def _add_ref(self, _this) -> int:
        self.refs += 1
        return self.refs

    def _release(self, _this) -> int:
        self.refs -= 1
        return self.refs

    def _completed(self, _this, operation) -> int:
        try:
            hr = ctypes.c_long()
            interface = ctypes.c_void_p()
            # IActivateAudioInterfaceAsyncOperation::GetActivateResult
            Com(operation).call(3, (ctypes.POINTER(ctypes.c_long), ctypes.POINTER(ctypes.c_void_p)),
                                ctypes.byref(hr), ctypes.byref(interface))
            self.hr, self.interface = hr.value, interface.value
        except OSError as exc:
            self.hr = getattr(exc, "winerror", None) or -1
        finally:
            self.done.set()
        return S_OK


# --- process loopback ------------------------------------------------------------------

class LoopbackCapture:
    """What process `pid` and its children play, as mono float32 at `rate`.

        capture = LoopbackCapture(pid, 22050)   # activates and starts; OSError when Windows refuses
        samples = capture.read(1.0, 1024)       # at least 1024 samples, or what came; None after a second without a packet
        capture.close()
    """

    def __init__(self, pid: int, rate: int, timeout_s: float = 5.0) -> None:
        self.pid = pid
        self.rate = rate
        self.client: Com | None = None
        self.capture: Com | None = None
        self.event = None
        self.process = None
        com_init()
        k = kernel32()
        self.process = k.OpenProcess(SYNCHRONIZE, False, pid)
        try:
            self.client = self._activate(pid, timeout_s)
            fmt = _WaveFormatEx(WAVE_FORMAT_IEEE_FLOAT, 1, rate, rate * 4, 4, 32, 0)
            flags = (AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                     | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY)
            # IAudioClient::Initialize(share mode, flags, buffer, periodicity, format, session)
            self.client.call(3, (ctypes.c_int, wintypes.DWORD, ctypes.c_int64, ctypes.c_int64,
                                 ctypes.POINTER(_WaveFormatEx), ctypes.c_void_p),
                             AUDCLNT_SHAREMODE_SHARED, flags, BUFFER_100NS, 0, ctypes.byref(fmt), None)
            self.event = k.CreateEventW(None, False, False, None)
            if not self.event:
                raise ctypes.WinError(ctypes.get_last_error())
            self.client.call(13, (wintypes.HANDLE,), self.event)                        # SetEventHandle
            self.capture = self.client.out(14, ctypes.byref(GUID.of(IID_IAUDIOCAPTURECLIENT)),
                                           argtypes=(ctypes.POINTER(GUID),))            # GetService
            self.client.call(10, ())                                                    # Start
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _activate(pid: int, timeout_s: float) -> Com:
        params = _ActivationParams(AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK, pid,
                                   PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE)
        prop = _PropVariant(vt=VT_BLOB)
        prop.blob.cbSize = ctypes.sizeof(params)
        prop.blob.pBlobData = ctypes.addressof(params)
        handler = _CompletionHandler()
        operation = ctypes.c_void_p()
        try:
            mmdevapi().ActivateAudioInterfaceAsync(LOOPBACK_DEVICE, ctypes.byref(GUID.of(IID_IAUDIOCLIENT)),
                                                   ctypes.byref(prop), handler.pointer, ctypes.byref(operation))
            if not handler.done.wait(timeout_s):
                raise TimeoutError("the process loopback activation did not complete")
        finally:
            if operation.value:
                Com(operation.value).release()
            handler.refs -= 1      # our own reference, from its creation
        if handler.hr < 0 or not handler.interface:
            raise ctypes.WinError(handler.hr)
        return Com(handler.interface)

    def alive(self) -> bool:
        """False once the target process has ended (its audio will not come back)."""
        return not self.process or kernel32().WaitForSingleObject(self.process, 0) != WAIT_OBJECT_0

    def read(self, timeout_s: float, min_samples: int = 0) -> np.ndarray | None:
        """Wait for a packet, then take every packet that is there, until `min_samples` have come
        (packets are 10 ms; fewer, larger reads cost less). None when no packet came in time."""
        k = kernel32()
        if k.WaitForSingleObject(self.event, max(0, int(timeout_s * 1000))) != WAIT_OBJECT_0:
            return None
        deadline = time.monotonic() + timeout_s
        chunks = self._drain()
        got = sum(len(c) for c in chunks)
        while got < min_samples and time.monotonic() < deadline:
            if k.WaitForSingleObject(self.event, max(1, int((deadline - time.monotonic()) * 1000))) != WAIT_OBJECT_0:
                break
            more = self._drain()
            chunks += more
            got += sum(len(c) for c in more)
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    def _drain(self) -> list[np.ndarray]:
        chunks = []
        size = ctypes.c_uint32()
        data = ctypes.c_void_p()
        frames = ctypes.c_uint32()
        flags = wintypes.DWORD()
        get_size = (ctypes.POINTER(ctypes.c_uint32),)
        get_buffer = (ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(wintypes.DWORD),
                      ctypes.c_void_p, ctypes.c_void_p)
        assert self.capture is not None
        self.capture.call(5, get_size, ctypes.byref(size))                             # GetNextPacketSize
        while size.value:
            self.capture.call(3, get_buffer, ctypes.byref(data), ctypes.byref(frames), ctypes.byref(flags),
                              None, None)                                               # GetBuffer
            n = frames.value
            if flags.value & AUDCLNT_BUFFERFLAGS_SILENT or not data.value:
                chunks.append(np.zeros(n, dtype=np.float32))
            else:
                chunks.append(np.ctypeslib.as_array(ctypes.cast(data, ctypes.POINTER(ctypes.c_float)), (n,)).copy())
            self.capture.call(4, (ctypes.c_uint32,), n)                                # ReleaseBuffer
            self.capture.call(5, get_size, ctypes.byref(size))
        return chunks

    def close(self) -> None:
        k = kernel32()
        if self.client is not None and self.client.ptr:
            try:
                self.client.call(11, ())                                                # Stop
            except OSError:
                pass
        for interface in (self.capture, self.client):
            if interface is not None:
                interface.release()
        self.capture = self.client = None
        for name in ("event", "process"):
            handle = getattr(self, name)
            if handle:
                k.CloseHandle(handle)
                setattr(self, name, None)


# --- who plays what ------------------------------------------------------------------

@dataclass(frozen=True)
class AudioSession:
    pid: int
    active: bool      # rendering right now (AudioSessionStateActive)


def audio_sessions() -> list[AudioSession]:
    """The render sessions on every active output device, the system sounds left out."""
    com_init()
    enumerator = ctypes.c_void_p()
    hr = ole32().CoCreateInstance(ctypes.byref(GUID.of(CLSID_MMDEVICEENUMERATOR)), None, CLSCTX_ALL,
                                  ctypes.byref(GUID.of(IID_IMMDEVICEENUMERATOR)), ctypes.byref(enumerator))
    if hr < 0:
        raise ctypes.WinError(hr)
    devices = Com(enumerator.value)
    found: list[AudioSession] = []
    try:
        collection = devices.out(3, E_RENDER, DEVICE_STATE_ACTIVE, argtypes=(ctypes.c_int, wintypes.DWORD))
        try:
            count = ctypes.c_uint32()
            collection.call(3, (ctypes.POINTER(ctypes.c_uint32),), ctypes.byref(count))
            for i in range(count.value):
                device = collection.out(4, i, argtypes=(ctypes.c_uint32,))
                try:
                    found += _device_sessions(device)
                except OSError:
                    pass       # a device that went away meanwhile
                finally:
                    device.release()
        finally:
            collection.release()
    finally:
        devices.release()
    return found


def _device_sessions(device: Com) -> list[AudioSession]:
    # IMMDevice::Activate(IID_IAudioSessionManager2, CLSCTX_ALL, NULL, &manager)
    manager = device.out(3, ctypes.byref(GUID.of(IID_IAUDIOSESSIONMANAGER2)), CLSCTX_ALL, None,
                         argtypes=(ctypes.POINTER(GUID), wintypes.DWORD, ctypes.c_void_p))
    found = []
    try:
        sessions = manager.out(5)                                                       # GetSessionEnumerator
        try:
            count = ctypes.c_int()
            sessions.call(3, (ctypes.POINTER(ctypes.c_int),), ctypes.byref(count))
            for i in range(count.value):
                control = sessions.out(4, i, argtypes=(ctypes.c_int,))
                try:
                    control2 = control.query(IID_IAUDIOSESSIONCONTROL2)
                    try:
                        if control2.call(15, ()) == S_OK:                               # IsSystemSoundsSession
                            continue
                        pid = wintypes.DWORD()
                        control2.call(14, (ctypes.POINTER(wintypes.DWORD),), ctypes.byref(pid))   # GetProcessId
                        if not pid.value:
                            continue   # AUDCLNT_S_NO_SINGLE_PROCESS: a session shared by several processes
                        state = ctypes.c_int()
                        control.call(3, (ctypes.POINTER(ctypes.c_int),), ctypes.byref(state))    # GetState
                        found.append(AudioSession(pid.value, state.value == AUDIO_SESSION_STATE_ACTIVE))
                    finally:
                        control2.release()
                except OSError:
                    pass       # a session whose process has gone (GetProcessId fails for cross-process ones)
                finally:
                    control.release()
        finally:
            sessions.release()
    finally:
        manager.release()
    return found


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    exe: str          # the image name, "Spotify.exe"


def processes() -> dict[int, Process]:
    k = kernel32()
    snapshot = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    table: dict[int, Process] = {}
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = k.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            table[entry.th32ProcessID] = Process(entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile)
            more = k.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k.CloseHandle(snapshot)
    return table


def package_app_id(pid: int) -> str:
    """A packaged (Store/MSIX) process's AppUserModelId, "<family>!<app>"; "" for a desktop process."""
    k = kernel32()
    process = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return ""
    try:
        length = ctypes.c_uint32(0)
        if k.GetApplicationUserModelId(process, ctypes.byref(length), None) != 122:   # ERROR_INSUFFICIENT_BUFFER
            return ""                    # APPMODEL_ERROR_NO_APPLICATION (15703): not packaged
        buffer = ctypes.create_unicode_buffer(length.value)
        if k.GetApplicationUserModelId(process, ctypes.byref(length), buffer) != 0:
            return ""
        return buffer.value
    finally:
        k.CloseHandle(process)
