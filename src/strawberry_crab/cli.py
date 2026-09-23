"""`strawberry`: start her, stop her, and everything else you do by hand (WIRING.md §13, §14).

    strawberry                 start the daemon and doorways if needed, open the widget
    strawberry daemon          the daemon and doorways only (idempotent)
    strawberry tray            the 🍓, and under it the daemon, the doorways and the widget
    strawberry install         start on login (a systemd user unit for the tray; on Windows a
                               shortcut in the Startup folder)
    strawberry setup | doctor  models, voice and widget; then check it all (setupcmd.py, doctor.py)
    strawberry status | stop | restart | config | say | listen | route | talk | ...

The daemon itself is `strawberryd` (strawberryd.py); the by-hand tools that share its config
(route, tools, tool, think, talk, tray) call its main() in this process. Everything else is the
standard library, so `strawberry listen` (the hotkey) starts in a few tens of milliseconds.

After `install`, daemon/stop/restart/status drive the tray unit instead of pidfiles (on Windows
the tray the shortcut starts, through its stop and restart events: winproc.py). Settings live in
~/.config/strawberry/config.toml (WIRING.md §15); STRAWBERRYD_PORT still overrides.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from . import osguard, paths

DEFAULT_PORT = 8770
TRAY_UNIT = "strawberry-tray.service"
LEGACY_UNIT = "strawberryd.service"
DAEMON_MODULE = "strawberry_crab.strawberryd"
DOORWAYS = ("mpris_watch", "notify_watch", "beat_watch")   # = strawberry_crab.doorways.DOORWAYS, without importing it
WINDOWS_DOORWAYS = ("smtc_watch", "toast_watch")          # = strawberry_crab.doorways.WINDOWS_DOORWAYS
OLD_UNITS = (LEGACY_UNIT, *(f"strawberry-{name}.service" for name in DOORWAYS))
SESSION_ENV = ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "DBUS_SESSION_BUS_ADDRESS")
AUDITION_LINE = "James, hold the phone, the time is up! Your commit landed, nice work."
VOICES_HINT = "(download more with: strawberry voices en_US-amy-medium; catalogue: https://rhasspy.github.io/piper-samples/)"
HOTKEY_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/strawberry/"
HOTKEY_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
HOTKEY_DEFAULT = "<Super><Shift>space"
GIT_HOOKS = ("post-commit", "pre-push")
GIT_OLD_HELPER = "strawberry-git-event"      # the jq+curl helper the old symlinked hooks called
GIT_MARKER = "# strawberry git doorway"
ZERO_SHA = "0" * 40


class CliError(Exception):
    """A message for the user and an exit code; main() prints it."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


# --- where things are -----------------------------------------------------------

def config_port() -> int:
    """STRAWBERRYD_PORT, else [daemon] port from the config, else 8770.

    Read with tomllib rather than config.load(): the hotkey runs `strawberry listen`, and the
    full config module (and a validation pass) is start-up time she would visibly lose.
    """
    env = os.environ.get("STRAWBERRYD_PORT", "").strip()
    if env:
        try:
            return int(env)
        except ValueError:
            raise CliError(f"STRAWBERRYD_PORT={env!r} is not a port", 2) from None
    try:
        data = tomllib.loads(paths.config_file().read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return DEFAULT_PORT      # no file yet (a fresh machine) or a broken one: the daemon says which
    port = (data.get("daemon") or {}).get("port") if isinstance(data.get("daemon"), dict) else None
    return port if isinstance(port, int) and not isinstance(port, bool) else DEFAULT_PORT


def cli_argv() -> list[str]:
    """How to run this very CLI again from a unit, a hook or a shortcut: the absolute path of the
    `strawberry` executable that is running, or this interpreter with `-m strawberry_crab`."""
    from .widgetbin import cli_name, is_runnable

    exe = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if exe is not None and paths.windows() and exe.name == "strawberry":
        exe = exe.with_name(cli_name())             # a console script's argv[0] may leave out .exe
    if exe is not None and exe.name == cli_name() and is_runnable(exe):
        return [str(exe.absolute())]
    return [sys.executable, "-m", "strawberry_crab"]


def doorways() -> tuple[str, ...]:
    """The doorways this system has (= strawberry_crab.doorways.for_system): the three D-Bus and
    PipeWire ones on Linux, the media and notification ones on Windows so far, none elsewhere."""
    if sys.platform.startswith("linux"):
        return DOORWAYS
    return WINDOWS_DOORWAYS if sys.platform == "win32" else ()


def module_argv(module: str, *args: str) -> list[str]:
    return [sys.executable, "-m", module, *args]


class Here:
    """The port and the files every command talks about, worked out once."""

    def __init__(self, port: int | None = None) -> None:
        self.port = port or config_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.state = paths.state_dir()
        self.pidfile = self.state / "strawberryd.pid"
        self.log = self.state / "strawberryd.log"
        self.tray_state = paths.tray_state_file()
        self.unit_dir = paths.systemd_user_dir()
        self.autostart = paths.autostart_file()
        self.tray_log = self.state / "tray.log"          # Windows: the tray writes its own log


# --- the daemon over HTTP -------------------------------------------------------

def http(here: Here, method: str, path: str, body: dict | None = None, timeout: float = 2.0) -> bytes | None:
    """The response body, or None when the daemon did not answer with a 2xx."""
    import urllib.error      # here, not at the top: `strawberry listen` skips it (listen_fast)
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    request = urllib.request.Request(here.base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None


def daemon_up(here: Here) -> bool:
    return http(here, "GET", "/health") is not None


WAIT_DAEMON_S = 45.0   # the port opens after the voice, whisper and gate models load (~8-10 s warm, longer cold)


def wait_daemon(here: Here, timeout_s: float = WAIT_DAEMON_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if daemon_up(here):
            return True
        time.sleep(0.25)
    if paths.windows():
        print(f"strawberryd did not come up; see {here.log} (and {here.tray_log} under the tray)", file=sys.stderr)
    else:
        print(f"strawberryd did not come up; see {here.log} (or: journalctl --user -u {TRAY_UNIT[:-8]})",
              file=sys.stderr)
    return False


# --- systemd --user and pidfiles --------------------------------------------------

def systemctl(*args: str, capture: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=capture, text=True)
    except OSError as exc:
        return subprocess.CompletedProcess(["systemctl", "--user", *args], 127, "", str(exc))


def is_enabled(unit: str) -> bool:
    return systemctl("is-enabled", unit).returncode == 0


def tray_managed() -> bool:
    return is_enabled(TRAY_UNIT)


def legacy_managed() -> bool:
    return is_enabled(LEGACY_UNIT)


def managed() -> bool:
    return tray_managed() or legacy_managed() or startup_installed()


def startup_installed() -> bool:
    """Windows: `strawberry install` wrote the Startup shortcut, so the tray owns her."""
    if not paths.windows():
        return False
    from . import startup

    return startup.installed()


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if paths.windows():
        return windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def windows_pid_alive(pid: int) -> bool:
    """Asked of the kernel: on Windows os.kill(pid, 0) is not a probe, it ends the process."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5                   # ERROR_ACCESS_DENIED: there, not ours
    try:
        code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def read_pid(pidfile: Path) -> int | None:
    try:
        return int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return None


def tray_pid(here: Here) -> int | None:
    """The pid of a tray that is actually running (the state file outlives a crash)."""
    try:
        pid = int(json.loads(here.tray_state.read_text())["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return pid if pid_alive(pid) else None


def tray_owns_children(here: Here) -> bool:
    return tray_managed() or tray_pid(here) is not None or os.environ.get("STRAWBERRY_TRAY") == "1"


STOP_TIMEOUT_S = 10.0       # Windows: how long a clean stop may take before TerminateProcess
TRAY_STOP_TIMEOUT_S = 20.0  # ... for the tray, which stops its children first (5 s grace each)


def stop_pidfile(pidfile: Path, name: str, quiet: bool = False) -> None:
    pid = read_pid(pidfile)
    stopped = False
    if pid and paths.windows():
        stopped = windows_stop(pid, pidfile_stop_key(pidfile), STOP_TIMEOUT_S, name, quiet)
    elif pid:
        try:
            os.kill(pid, signal.SIGTERM)
            stopped = True
        except OSError:
            pass
    if not quiet:
        print(f"{name} stopped" if stopped else f"{name}: not running")
    pidfile.unlink(missing_ok=True)


def pidfile_stop_key(pidfile: Path) -> str:
    """The stop event (winproc.py) of what spawn() started with this pidfile: its name and the
    file's full path, so a throwaway state dir never answers for the user's own."""
    import zlib

    return f"{pidfile.stem}-{zlib.crc32(str(pidfile.absolute()).lower().encode()):08x}"


def windows_stop(pid: int, key: str | None, timeout_s: float, name: str, quiet: bool = False) -> bool:
    """Its stop event, then TerminateProcess if it has not gone in `timeout_s`. True if it was running."""
    from . import winproc

    outcome = winproc.stop(pid, key, timeout_s)
    if outcome == "killed" and not quiet:
        print(f"{name} did not stop within {timeout_s:.0f} s (or has no stop event); ended it", file=sys.stderr)
    return outcome != "gone"


def spawn(argv: list[str], logfile: Path, pidfile: Path) -> None:
    """nohup ... >>log 2>&1 &, with the pid written down.

    On Windows the child gets a hidden console of its own and its own process group, so closing
    this terminal or pressing Ctrl+C in it does not end the daemon, and the key of its stop event
    (pidfile_stop_key), so `strawberry stop` can end it cleanly."""
    logfile.parent.mkdir(parents=True, exist_ok=True)
    if paths.windows():
        from .winproc import STOP_ENV

        detach = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
                  "env": {**os.environ, STOP_ENV: pidfile_stop_key(pidfile)}}
    else:
        detach = {"start_new_session": True}
    with logfile.open("ab") as log:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   **detach)
    pidfile.write_text(f"{process.pid}\n")


def start_watcher(here: Here, name: str) -> None:
    pidfile, logfile = here.state / f"{name}.pid", here.state / f"{name}.log"
    if pid_alive(read_pid(pidfile)):
        return
    print(f"starting {name} (log: {logfile})")
    spawn(module_argv(f"strawberry_crab.doorways.{name}", "--daemon", here.base), logfile, pidfile)


def start_watchers(here: Here) -> None:
    if managed() or tray_owns_children(here):
        return     # the tray (or the old units) start them
    for name in doorways():
        start_watcher(here, name)


def stop_watchers_pidfile(here: Here, quiet: bool = False) -> None:
    for name in doorways():
        stop_pidfile(here.state / f"{name}.pid", name, quiet=quiet)


def stop_watchers(here: Here) -> None:
    if managed() or tray_owns_children(here):
        return     # they go with the tray, or with the old daemon unit
    stop_watchers_pidfile(here)


def start_daemon(here: Here) -> None:
    if daemon_up(here):
        return
    if os.environ.get("STRAWBERRY_TRAY") == "1":
        return     # the tray starts the daemon itself
    for unit, is_managed in ((TRAY_UNIT, tray_managed), (LEGACY_UNIT, legacy_managed)):
        if is_managed():
            systemctl("start", unit, capture=False)
            if not wait_daemon(here):
                raise CliError("", 1)
            return
    if startup_installed() and tray_pid(here) is None:
        start_tray_now(here)
        if not wait_daemon(here):
            raise CliError("", 1)
        return
    print(f"starting strawberryd on :{here.port} (log: {here.log})")
    spawn(module_argv(DAEMON_MODULE, "--port", str(here.port)), here.log, here.pidfile)
    if not wait_daemon(here):
        raise CliError("", 1)


def stop_daemon(here: Here) -> None:
    if tray_managed():
        if systemctl("stop", TRAY_UNIT, capture=False).returncode == 0:
            print("tray stopped (with the daemon, the doorways and the widget)")
        return
    if legacy_managed():
        if systemctl("stop", LEGACY_UNIT, capture=False).returncode == 0:
            print("strawberryd stopped (systemd)")
        return
    if stop_tray(here):
        return
    stop_pidfile(here.pidfile, "strawberryd")


def stop_tray(here: Here, quiet: bool = False) -> bool:
    """Stop a tray started by hand (or, on Windows, by the Startup shortcut); it stops its
    children. True if one was running."""
    pid = tray_pid(here)
    if not pid:
        return False
    if paths.windows():
        # Its stop event, keyed by the pid tray.json gives (the interpreter's own).
        windows_stop(pid, None, TRAY_STOP_TIMEOUT_S, "the tray", quiet)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
    if not quiet:
        print(f"tray stopped (pid {pid}, with its children)")
    return True


# --- commands: running her ------------------------------------------------------

def cmd_widget(here: Here, extra: list[str]) -> int:
    """Open the widget: the installed binary, else the checkout's Godot project (widgetbin)."""
    from . import widgetbin

    try:
        widget = widgetbin.resolve()
    except widgetbin.WidgetMissing as exc:
        raise CliError(str(exc), 2) from None
    start_daemon(here)
    start_watchers(here)
    if widget.kind == "checkout" and not (widget.project / ".godot").is_dir():
        print("importing widget project (first run)")
        subprocess.run([str(widget.program), "--headless", "--path", str(widget.project), "--editor", "--import"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Pinned to the X11 backend on purpose: native on an X11 session, XWayland on a Wayland
    # one. Native Wayland clients on GNOME get neither always-on-top nor self-positioning,
    # which is what makes a desktop pet a pet (WIRING.md §13).
    argv = widget.argv(here.port, extra)
    strawberry = widgetbin.strawberry_cli()
    if strawberry:
        os.environ["STRAWBERRY_CLI"] = strawberry     # her menu's "Settings file…" and "Apply settings"
    return hand_over(argv)


def hand_over(argv: list[str]) -> int:
    """Become `argv` (exec), so what the caller waits on is the widget or the repo's own hook.

    Windows has no exec: os.execv there starts a new process and ends this one at once, so git
    or the shell would see us finish first. There `argv` runs as a child and its code is ours.
    """
    sys.stdout.flush()
    if paths.windows():
        return subprocess.call(argv)
    os.execv(argv[0], argv)
    return 0    # not reached


def cmd_widget_fetch(version: str | None) -> int:
    """Download the release's widget binary into the data dir, checked against its SHA-256."""
    from . import __version__, widgetbin

    try:
        widgetbin.fetch(version or __version__)
    except widgetbin.FetchError as exc:
        raise CliError(f"widget fetch failed: {exc}", 1) from None
    return 0


def cmd_daemon(here: Here) -> int:
    start_daemon(here)
    start_watchers(here)
    print(f"strawberryd up on :{here.port}")
    return 0


def run_daemon_main(argv: list[str]) -> int:
    from .strawberryd import main as daemon_main

    try:
        daemon_main(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return 0


def cmd_tray(here: Here, extra: list[str]) -> int:
    if paths.windows():
        # No unit keeps it single: the Startup shortcut, `install` and a hand can all start one.
        pid = tray_pid(here)
        if pid and "--no-children" not in extra:
            print(f"a tray is already running (pid {pid}); `strawberry stop` ends it")
            return 0
    return run_daemon_main(["--tray", "--port", str(here.port), *extra])


def tray_main() -> None:
    """`strawberry-tray` ([project.gui-scripts]): `strawberry tray` without a console window,
    which is what the Windows Startup shortcut runs. Its arguments go to the tray."""
    sys.exit(main(["tray", *sys.argv[1:]]))


def cmd_tray_autostart(here: Here) -> int:
    # The XDG autostart entry: the unit wins when it is enabled, so only one tray ever runs.
    if tray_managed():
        print(f"{TRAY_UNIT} is enabled; leaving the tray to systemd")
        return 0
    return cmd_tray(here, [])


def cmd_status(here: Here) -> int:
    installed = startup_installed()
    if tray_managed():
        print(f"managed by systemd --user ({TRAY_UNIT}; strawberry uninstall to stop that)")
    elif legacy_managed():
        print("managed by the old systemd units (strawberry install moves her to the tray)")
    elif installed:
        print(f"starts at login: {paths.startup_shortcut()} (strawberry uninstall to stop that)")
    pid = tray_pid(here)
    if tray_managed():
        active = systemctl("is-active", TRAY_UNIT).stdout.strip() or "unknown"
        print(f"tray: {active} ({TRAY_UNIT}{f', pid {pid}' if pid else ''})")
    elif installed:
        print(f"tray: {f'up (pid {pid})' if pid else 'down (strawberry daemon starts it)'}; log: {here.tray_log}")
    elif pid:
        print(f"tray: up (pid {pid}, started by hand)")
    else:
        print("tray: down")
    health = http(here, "GET", "/health")
    print(health.decode(errors="replace") if health is not None else "strawberryd: down")
    status_watchers(here, pid)
    return 0 if health is not None else 1


def status_watchers(here: Here, tray: int | None) -> None:
    if tray:
        # Under the tray the doorways are its children; it writes what it has to tray.json.
        try:
            children = json.loads(here.tray_state.read_text())["children"]
        except (OSError, ValueError, KeyError):
            children = []
        for child in children:
            print(f"{child['name']}: {'up' if child['running'] else 'down'}"
                  f"{' pid ' + str(child['pid']) if child['pid'] else ''}"
                  f"{' (' + str(child['restarts']) + ' restarts)' if child['restarts'] else ''}")
        return
    legacy = legacy_managed()
    for name in doorways():
        if legacy:
            print(f"{name}: {systemctl('is-active', f'strawberry-{name}.service').stdout.strip()}")
        else:
            print(f"{name}: {'up' if pid_alive(read_pid(here.state / f'{name}.pid')) else 'down'}")


def cmd_stop(here: Here) -> int:
    stop_watchers(here)
    stop_daemon(here)
    return 0


def cmd_restart(here: Here) -> int:
    for unit, is_managed, done in ((TRAY_UNIT, tray_managed, "restarted (systemd)"),
                                   (LEGACY_UNIT, legacy_managed, "strawberryd restarted (systemd)")):
        if is_managed():
            systemctl("restart", unit, capture=False)
            if not wait_daemon(here):
                return 1
            print(done)
            return 0
    if paths.windows() and restart_tray(here):
        return 0
    cmd_stop(here)
    time.sleep(0.5)
    return cmd_daemon(here)


def restart_tray(here: Here) -> bool:
    """Windows: a running tray restarts its children when asked (its restart event), as its
    Restart row does. Asked from inside the tray (her menu's "Apply settings"), stopping the tray
    instead would end this very process with it. False when there is no tray to ask."""
    from . import winproc

    pid = tray_pid(here)
    if not pid or not winproc.request_restart(pid):
        return False
    time.sleep(1.0)                  # the daemon goes down before it comes back
    if not wait_daemon(here):
        raise CliError("", 1)
    print(f"restarted (the tray's children, pid {pid})")
    return True


# --- commands: start on login ---------------------------------------------------

def unit_text(port: int, argv: list[str] | None = None) -> str:
    """The one systemd user unit: the tray, which starts the daemon, the doorways and the widget.

    ExecStart is the installed entry point (this CLI's own executable, or this interpreter with
    -m strawberry_crab), never a path into a checkout's scripts.
    """
    exec_start = shlex.join([*(argv or cli_argv()), "tray", "--port", str(port)])
    return f"""[Unit]
Description=Strawberry: the tray icon, and under it the daemon, the doorways and the widget
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
"""


def autostart_text(argv: list[str] | None = None) -> str:
    # The fallback for a session that never reaches graphical-session.target. It defers to the
    # unit when that is enabled, so the two can never start two trays.
    return f"""[Desktop Entry]
Type=Application
Name=Strawberry
Comment=Desktop crab
Exec={shlex.join([*(argv or cli_argv()), "tray-autostart"])}
X-GNOME-Autostart-Delay=3
StartupNotify=false
"""


APP_ICON_SIZES = (48, 64, 128)


def app_entry_text(argv: list[str] | None = None) -> str:
    # Not a launcher (NoDisplay): it is here so the app switcher and the dock show her name and
    # the berry instead of a generic icon. GNOME matches it to the widget's window by
    # StartupWMClass, which Godot sets to the project name.
    return f"""[Desktop Entry]
Type=Application
Name=Strawberry
Comment=Desktop crab
Icon=strawberry-crab
Exec={shlex.join([*(argv or cli_argv()), "tray"])}
StartupWMClass=Strawberry
NoDisplay=true
StartupNotify=false
"""


def install_app_entry() -> Path:
    for size in APP_ICON_SIZES:
        target = paths.app_icon_file(size)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((paths.icons_dir() / f"strawberry-{size}.png").read_bytes())
    entry = paths.app_entry_file()
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(app_entry_text())
    return entry


def remove_app_entry() -> None:
    paths.app_entry_file().unlink(missing_ok=True)
    for size in APP_ICON_SIZES:
        paths.app_icon_file(size).unlink(missing_ok=True)


def remove_old_units(here: Here) -> None:
    found = [unit for unit in OLD_UNITS if (here.unit_dir / unit).is_file()]
    if not found:
        return
    print("removing the old per-doorway units (the tray starts them now)")
    systemctl("disable", "--now", *OLD_UNITS)
    for unit in OLD_UNITS:
        (here.unit_dir / unit).unlink(missing_ok=True)


def cmd_install(here: Here) -> int:
    if paths.windows():
        return cmd_install_windows(here)
    # Hand over from pidfile-managed processes, if any, so the port is free for the unit.
    stop_watchers_pidfile(here, quiet=True)
    stop_pidfile(here.pidfile, "strawberryd", quiet=True)
    remove_old_units(here)
    here.unit_dir.mkdir(parents=True, exist_ok=True)
    here.autostart.parent.mkdir(parents=True, exist_ok=True)
    unit = here.unit_dir / TRAY_UNIT
    previous = unit.read_text() if unit.is_file() else ""
    unit.write_text(unit_text(here.port))
    here.autostart.write_text(autostart_text())
    install_app_entry()
    if previous and previous != unit.read_text():
        print(f"rewrote {unit} (ExecStart now: {shlex.join([*cli_argv(), 'tray', '--port', str(here.port)])})")
    systemctl("daemon-reload")
    # The user manager needs the session's DISPLAY and bus address to run the widget and to
    # reach the notification bus; on GNOME they are usually there already, but not always.
    systemctl("import-environment", *SESSION_ENV)
    systemctl("enable", TRAY_UNIT, capture=False)
    # restart, not start: a tray already running from an older unit (another checkout, the
    # old venv) must come back on the ExecStart just written. On a stopped unit it starts it.
    systemctl("restart", TRAY_UNIT, capture=False)
    if wait_daemon(here):
        print(f"installed: {TRAY_UNIT} runs the tray, the daemon, the doorways and the widget "
              f"(autostart fallback: {here.autostart})")
    print(f"logs: journalctl --user -u {TRAY_UNIT[:-8]} -f")
    return 0


def cmd_uninstall(here: Here) -> int:
    if paths.windows():
        return cmd_uninstall_windows(here)
    systemctl("disable", "--now", TRAY_UNIT)
    (here.unit_dir / TRAY_UNIT).unlink(missing_ok=True)
    remove_old_units(here)
    systemctl("daemon-reload")
    here.autostart.unlink(missing_ok=True)
    remove_app_entry()
    print("uninstalled: she now runs only when you start her with `strawberry`")
    return 0


def start_tray_now(here: Here) -> None:
    """Windows: start the tray the way the Startup shortcut does, detached from this console."""
    from . import startup

    print(f"starting the tray (log: {here.tray_log})")
    startup.start(startup.launch(here.port, cli_argv()))


def cmd_install_windows(here: Here) -> int:
    """Start on login on Windows: the Startup folder's shortcut to the windowless tray (startup.py),
    then the tray itself, as the unit is enabled and restarted on Linux."""
    from . import startup

    # Hand over from what runs now, so the port is free and the new tray is the only one.
    stop_watchers_pidfile(here, quiet=True)
    stop_pidfile(here.pidfile, "strawberryd", quiet=True)
    if stop_tray(here, quiet=True):
        print("stopped the tray that was running; the new one takes over")
    what = startup.launch(here.port, cli_argv())
    link = paths.startup_shortcut()
    existed = link.is_file()
    try:
        startup.write_shortcut(link, what, icon=startup.write_icon())
    except startup.StartupError as exc:
        raise CliError(str(exc), 1) from None
    print(f"{'rewrote' if existed else 'wrote'} {link}: {what.command_line()}")
    if not what.windowless:
        print("note: no pythonw.exe beside this Python, so a console window stays open while she runs")
    if osguard.unsupported_message() is not None:
        print(f"note: Windows is not in the supported list yet, so at login the tray needs "
              f"{osguard.OVERRIDE_ENV}=1 in your user environment (WINDOWS.md)")
    startup.start(what)
    if wait_daemon(here):
        print("installed: at login the Startup shortcut runs the tray, the daemon, the doorways and the widget")
    print(f"logs: {here.tray_log}, and one per child beside it")
    return 0


def cmd_uninstall_windows(here: Here) -> int:
    from . import startup

    if startup.remove():
        print(f"removed {paths.startup_shortcut()}")
    stop_tray(here)
    print("uninstalled: she now runs only when you start her with `strawberry`")
    return 0


# --- commands: settings and small things -----------------------------------------

def cmd_config(init_only: bool = False) -> int:
    path = paths.config_file()
    existed = path.exists()
    if not existed:
        code = run_daemon_main(["--init-config"])     # prints the path it wrote
        if code:
            return code
    if init_only:
        if existed:
            print(path)
        return 0
    editor = os.environ.get("EDITOR", "")
    if editor:
        return subprocess.call([*shlex.split(editor), str(path)])
    print(path)
    print("(set $EDITOR to open it automatically; restart with: strawberry restart)")
    return 0


def cmd_listen(here: Here) -> int:
    # The hotkey target: start listening, or end the recording early if she already is.
    return listen_fast(here.port)


def listen_fast(port: int) -> int:
    """POST /listen on a bare socket: urllib and argparse cost more start-up than the whole
    request, and the hotkey's delay shows in her reaction (the bash launcher used awk + curl)."""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
            conn.sendall(b"POST /listen HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 0\r\n"
                         b"Connection: close\r\n\r\n")
            status = conn.recv(64).split(b" ", 2)
    except OSError:
        return 1
    return 0 if len(status) > 1 and status[1][:1] == b"2" else 1


def _gsettings(*args: str, check: bool = True) -> str:
    result = subprocess.run(["gsettings", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise CliError(f"gsettings {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _keybinding_paths() -> list[str]:
    import ast

    raw = _gsettings("get", HOTKEY_SCHEMA, "custom-keybindings")
    return [] if raw.startswith("@as") else list(ast.literal_eval(raw))


def cmd_hotkey(combo: str | None, remove: bool) -> int:
    key = f"{HOTKEY_SCHEMA}.custom-keybinding:{HOTKEY_PATH}"
    if remove:
        remaining = [p for p in _keybinding_paths() if p != HOTKEY_PATH]
        _gsettings("set", HOTKEY_SCHEMA, "custom-keybindings", str(remaining))
        _gsettings("reset-recursively", key, check=False)
        print("hotkey removed")
        return 0
    combo = combo or HOTKEY_DEFAULT
    command = shlex.join([*cli_argv(), "listen"])
    current = _keybinding_paths()
    if HOTKEY_PATH not in current:
        _gsettings("set", HOTKEY_SCHEMA, "custom-keybindings", str([*current, HOTKEY_PATH]))
    _gsettings("set", key, "name", "Strawberry: listen")
    _gsettings("set", key, "command", command)
    _gsettings("set", key, "binding", combo)
    print(f"hotkey {combo} -> {command}")
    return 0


def cmd_say(here: Here, text: str) -> int:
    if not daemon_up(here):
        raise CliError("strawberryd is down; start it with: strawberry daemon")
    if http(here, "POST", "/perform", {"state": "talking", "text": text, "emotion": "happy"}, timeout=30) is None:
        return 1
    print(f"said: {text}")
    return 0


def installed_voices() -> list[str]:
    return sorted(p.name[:-len(".onnx")] for p in paths.voices_dir().glob("*.onnx"))


def cmd_voices(names: list[str]) -> int:
    if not names:
        for name in installed_voices():
            print(name)
        print(VOICES_HINT)
        return 0
    directory = paths.voices_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return subprocess.call([sys.executable, "-m", "piper.download_voices", "--download-dir", str(directory), *names])


def cmd_audition(text: str) -> int:
    line = text or AUDITION_LINE
    for voice in installed_voices():
        result = subprocess.run(module_argv(DAEMON_MODULE, "--say", line, "--voice", voice),
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        wav = result.stdout.strip()
        if result.returncode != 0 or not wav:
            print(f"{voice}: failed", file=sys.stderr)
            continue
        print(voice, flush=True)
        try:
            played = subprocess.run(["paplay", wav], stderr=subprocess.DEVNULL).returncode == 0
        except OSError:
            played = False
        if not played:
            try:
                subprocess.run(["aplay", "-q", wav])
            except OSError:
                pass
    return 0


# --- the git doorway (WIRING.md §5) ---------------------------------------------

def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(["git", *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def push_count(lines: str) -> int:
    """Commits a push sends, from the lines git gives pre-push on stdin:
    "<local ref> <local sha> <remote ref> <remote sha>" per ref."""
    count = 0
    for line in lines.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local_sha, remote_sha = parts[1], parts[3]
        if local_sha == ZERO_SHA:
            continue                                   # deleting a remote branch
        span = local_sha if remote_sha == ZERO_SHA else f"{remote_sha}..{local_sha}"   # new branch: everything
        try:
            count += int(_git("rev-list", "--count", span) or 0)
        except ValueError:
            pass
    return count


def git_event_payload(hook: str, args: list[str], stdin: str = "") -> dict | None:
    """The one event a hook firing becomes, or None when there is nothing to say."""
    toplevel = _git("rev-parse", "--show-toplevel")
    repo = Path(toplevel).name if toplevel else Path.cwd().name
    branch = _git("rev-parse", "--abbrev-ref", "HEAD") or "?"
    if hook == "post-commit":
        body = _git("log", "-1", "--pretty=%s") or ""
    elif hook == "pre-push":
        remote = args[0] if args else "origin"
        if len(args) >= 2 and args[1].isdigit() and not stdin:
            count = int(args[1])                     # by hand: pre-push <remote> <count>
        else:
            count = push_count(stdin)
        if count <= 0:
            return None
        body = f"pushing {count} {'commit' if count == 1 else 'commits'} on {branch} to {remote}"
    else:
        body = hook
    return {"source": "git", "app": hook, "title": repo, "body": body}


def post_detached(url: str, payload: dict) -> None:
    """POST in a forked, session-less child that gives up after a second, so git never waits."""
    if paths.windows():
        post_in_child(url, payload)
        return
    try:
        pid = os.fork()
    except OSError:
        return
    if pid:
        return
    try:
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        import urllib.request

        request = urllib.request.Request(url + "/event", data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
        urllib.request.urlopen(request, timeout=1).close()
    except BaseException:
        pass
    finally:
        os._exit(0)


POST_CHILD = """import sys, urllib.request
request = urllib.request.Request(sys.argv[1] + "/event", data=sys.argv[2].encode(),
                                 headers={"Content-Type": "application/json"})
urllib.request.urlopen(request, timeout=1).close()
"""


def post_in_child(url: str, payload: dict) -> None:
    """The same POST where there is no fork (Windows): a separate Python, never waited for, with
    no console window and no handle of ours, so git does not wait for it either."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen([sys.executable, "-c", POST_CHILD, url, json.dumps(payload)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags, close_fds=True)
    except OSError:
        pass


def repo_hook(hook: str) -> Path | None:
    """The repository's own hook, which core.hooksPath would otherwise silence."""
    common = _git("rev-parse", "--git-common-dir")
    if not common:
        return None
    candidate = Path(common) / "hooks" / hook
    if not (candidate.is_file() and os.access(candidate, os.X_OK)):
        return None
    ours = paths.git_hooks_dir() / hook
    if ours.exists() and candidate.resolve() == ours.resolve():
        return None
    try:
        if GIT_MARKER in candidate.read_text(errors="replace")[:1024]:
            return None                              # one of ours: running it would loop
    except OSError:
        return None
    return candidate


def cmd_git_event(hook: str, args: list[str]) -> int:
    """What the global hooks run: tell her (never failing, never waiting), then hand over to
    the repository's own hook with the same arguments and stdin, and return its verdict."""
    stdin = ""
    if hook == "pre-push" and not sys.stdin.isatty():
        try:
            stdin = sys.stdin.read()
        except OSError:
            stdin = ""
    try:
        payload = git_event_payload(hook, args, stdin)
        if payload is not None:
            url = os.environ.get("STRAWBERRYD_URL") or f"http://127.0.0.1:{config_port()}"
            post_detached(url.rstrip("/"), payload)
    except Exception:       # noqa: BLE001 - a hook must never break git, whoever is driving it
        pass
    own = repo_hook(hook) if hook in GIT_HOOKS else None
    if own is None:
        return 0
    if hook == "pre-push":
        if paths.windows():     # bytes: a text pipe there would hand the hook git's lines as CRLF
            return subprocess.run(hook_argv(own, args), input=stdin.encode()).returncode
        return subprocess.run(hook_argv(own, args), input=stdin, text=True).returncode
    return hand_over(hook_argv(own, args))


def hook_argv(hook: Path, args: list[str]) -> list[str]:
    """How to run a repository's hook. On Windows through Git's own sh, as git itself does (a
    script cannot be started directly there), with the path in forward slashes for its $0."""
    if paths.windows() and hook.suffix.lower() != ".exe":
        return [git_sh(), hook.as_posix(), *args]
    return [str(hook), *args]


def git_sh() -> str:
    """Git for Windows' sh: on PATH inside a hook, else beside git (<Git>\\cmd\\git.exe, <Git>\\bin\\sh.exe)."""
    found = shutil.which("sh")
    if found:
        return found
    git = shutil.which("git")
    if git:
        candidate = Path(git).resolve().parents[1] / "bin" / "sh.exe"
        if candidate.is_file():
            return str(candidate)
    return "sh"


def hook_text(hook: str, argv: list[str] | None = None) -> str:
    argv = argv or cli_argv()
    if paths.windows():
        argv = [Path(argv[0]).as_posix(), *argv[1:]]    # Git's sh reads C:/x/strawberry.exe, not C:\x
    return f"""#!/bin/sh
{GIT_MARKER} (WIRING.md §5), written by `strawberry git-hooks install`.
# Tells her, then runs the repository's own {hook} hook, if any; without strawberry it does nothing.
[ -x {shlex.quote(argv[0])} ] && exec {shlex.join(argv)} git-event {hook} "$@"
exit 0
"""


def _ours(path: Path) -> bool:
    """A symlink (the old installer's, into a checkout) or a file this CLI wrote."""
    if path.is_symlink():
        return True
    try:
        return GIT_MARKER in path.read_text(errors="replace")[:1024]
    except OSError:
        return False


def cmd_git_hooks(action: str) -> int:
    hooks_dir = paths.git_hooks_dir()
    current = _git("config", "--global", "core.hooksPath") or ""
    if action == "remove":
        for name in (*GIT_HOOKS, GIT_OLD_HELPER):
            path = hooks_dir / name
            if (path.exists() or path.is_symlink()) and _ours(path):
                path.unlink()
                print(f"removed {path}")
        if current and Path(current).expanduser() == hooks_dir:
            subprocess.run(["git", "config", "--global", "--unset", "core.hooksPath"])
            print("unset core.hooksPath")
        return 0
    if current and Path(current).expanduser() != hooks_dir:
        raise CliError(f"core.hooksPath is already {current}; not changing it.\n"
                       f"Either move your hooks into {hooks_dir} or add the Strawberry hooks to {current} by hand.")
    for name in GIT_HOOKS:
        path = hooks_dir / name
        if (path.exists() or path.is_symlink()) and not _ours(path):
            raise CliError(f"{path} exists and is not a Strawberry hook; leaving it alone.")
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for name in GIT_HOOKS:
        path = hooks_dir / name
        if path.is_symlink():
            path.unlink()                            # the old symlink into a checkout
        path.write_text(hook_text(name), encoding="utf-8", newline="\n")     # sh on Windows too: no CRLF
        path.chmod(0o755)
        print(f"wrote {path}")
    helper = hooks_dir / GIT_OLD_HELPER
    if helper.is_symlink():
        helper.unlink()
        print(f"removed {helper} (the hooks call `strawberry git-event` now)")
    subprocess.run(["git", "config", "--global", "core.hooksPath", str(hooks_dir)], check=True)
    print(f"core.hooksPath = {hooks_dir}")
    print("Every commit and push on this machine now pings strawberryd (if it is running).")
    return 0


def cmd_setup(yes: bool, install: bool, tier: str | None) -> int:
    from . import setupcmd

    try:
        return setupcmd.main(yes=yes, install=install, tier=tier)
    except ValueError as exc:      # an unknown --tier
        raise CliError(str(exc), 2) from None


def cmd_doctor(talk: bool) -> int:
    from . import doctor

    return doctor.main(talk_too=talk)


# --- the command line -----------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="strawberry",
        description="Strawberry, the desktop crab. With no command: start the daemon and doorways if "
                    "needed, then open the widget.",
        epilog="Settings: ~/.config/strawberry/config.toml (WIRING.md §15). STRAWBERRYD_PORT overrides the port.")
    from . import __version__

    top.add_argument("--version", action="version", version=f"strawberry {__version__}")
    sub = top.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, help: str, **kwargs) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help, description=help, **kwargs)

    p = add("widget", "start daemon + doorway watchers if needed, open the widget (extra args go to the widget); "
                      "--fetch downloads the released widget binary")
    p.add_argument("--fetch", action="store_true",
                   help="download strawberry-widget for this version from the GitHub release, check its "
                        "SHA-256 and install it in ~/.local/share/strawberry/widget/")
    p.add_argument("--version", dest="widget_version", metavar="V", default=None,
                   help="with --fetch: the release to fetch (default: this package's version)")
    add("daemon", "start the daemon + watchers only (idempotent)")
    add("tray", "the tray icon, and under it the daemon, the doorways and the widget "
                "(--no-children, --no-widget, --config PATH pass through)")
    add("tray-autostart", "what the XDG autostart entry runs: the tray, unless the systemd unit is enabled")
    add("status", "daemon health, tray and watcher states")
    add("stop", "stop the daemon and watchers")
    add("restart", "stop + daemon (after editing the config)")
    add("install", "start on login: one systemd user unit for the tray, autostart as a fallback "
                   "(Windows: a shortcut in the Startup folder)")
    add("uninstall", "undo install and stop the tray (she only runs when you launch her)")
    p = add("config", "create ~/.config/strawberry/config.toml if missing, then open it in $EDITOR")
    p.add_argument("--init", action="store_true", help="only create it if missing and print its path "
                                                       "(what the widget's \"Settings file…\" runs)")
    add("listen", "talk to her once (what the hotkey runs); press again to stop early")
    p = add("hotkey", f"GNOME shortcut for `listen` (default {HOTKEY_DEFAULT})")
    p.add_argument("combo", nargs="?", default=None)
    p.add_argument("--remove", action="store_true", help="remove the shortcut")
    p = add("route", "what the gate makes of a sentence (kind, topic, confidence, decision); for tuning")
    p.add_argument("text", nargs="+")
    p = add("tools", "list the MCP tools she can reach")
    p.add_argument("topic", nargs="?", default=None)
    p = add("tool", "call one MCP tool by hand, e.g. strawberry tool spotify next")
    p.add_argument("server")
    p.add_argument("name")
    p.add_argument("json", nargs="?", default="{}", help="the arguments as a JSON object")
    p = add("think", "run TEXT through the thinker (Qwen + the tools) by hand")
    p.add_argument("text")
    add("talk", "type to her: each line goes through the daemon as if spoken, routing shown")
    p = add("say", "make her say TEXT now (through the running daemon and widget)")
    p.add_argument("text", nargs="+")
    p = add("voices", "list installed Piper voices, or download the named ones")
    p.add_argument("names", nargs="*")
    p = add("audition", "play a sample line in every installed voice")
    p.add_argument("text", nargs="*")
    p = add("git-event", "what the git hooks run: tell her about a commit or a push, then run the repo's own hook")
    p.add_argument("hook", help="post-commit | pre-push")
    p.add_argument("args", nargs="*", help="the hook's own arguments (pre-push: remote name and url)")
    p = add("git-hooks", "install or remove the global git hooks (core.hooksPath)")
    p.add_argument("action", choices=("install", "remove"))
    p = add("setup", "choose models that fit this GPU, pull them, fetch a voice and the widget, fill in the config")
    p.add_argument("--yes", "-y", action="store_true", help="take every default without asking")
    p.add_argument("--install", action="store_true", help="with --yes: also run `strawberry install` at the end")
    p.add_argument("--tier", default=None, metavar="NAME",
                   help="the model tier instead of the one VRAM suggests: 24gb | 16gb | 10gb | 6gb | cpu")
    p = add("doctor", "check everything she needs and say how to fix what is missing (exit 1 if something is)")
    p.add_argument("--talk", action="store_true",
                   help="also time a short scripted conversation through the running daemon, per slot")
    return top


PASSTHROUGH = ("widget", "tray")


def main(argv: list[str] | None = None) -> int:
    osguard.require_supported()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["listen"]:
        try:
            return listen_fast(config_port())
        except CliError as exc:
            print(exc, file=sys.stderr)
            return exc.code
    top = parser()
    args, extra = top.parse_known_args(argv)
    command = args.command or "widget"
    if extra and command not in PASSTHROUGH:
        top.error(f"unrecognized arguments: {' '.join(extra)}")
    try:
        return dispatch(command, args, extra)
    except CliError as exc:
        if str(exc):
            print(exc, file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130


def dispatch(command: str, args: argparse.Namespace, extra: list[str]) -> int:
    if command == "git-event":
        return cmd_git_event(args.hook, args.args)
    if command == "git-hooks":
        return cmd_git_hooks(args.action)
    if command == "setup":
        return cmd_setup(args.yes, args.install, args.tier)
    if command == "doctor":
        return cmd_doctor(args.talk)
    if command == "config":
        return cmd_config(init_only=args.init)
    if command == "widget" and (args.fetch or args.widget_version):
        if not args.fetch:
            raise CliError("strawberry widget: --version goes with --fetch", 2)
        return cmd_widget_fetch(args.widget_version)
    if command == "hotkey":
        return cmd_hotkey(args.combo, args.remove)
    if command == "voices":
        return cmd_voices(args.names)
    if command == "audition":
        return cmd_audition(" ".join(args.text))
    # The by-hand tools read the config themselves, exactly as `strawberryd --route` does.
    if command == "route":
        return run_daemon_main(["--route", " ".join(args.text)])
    if command == "tools":
        return run_daemon_main(["--tools", *([args.topic] if args.topic else [])])
    if command == "tool":
        return run_daemon_main(["--tool", args.server, args.name, "--args", args.json])
    if command == "think":
        return run_daemon_main(["--think", args.text])
    if command == "talk":
        return run_daemon_main(["--talk"])

    here = Here()
    here.state.mkdir(parents=True, exist_ok=True)
    handlers = {
        "widget": lambda: cmd_widget(here, extra),
        "daemon": lambda: cmd_daemon(here),
        "tray": lambda: cmd_tray(here, extra),
        "tray-autostart": lambda: cmd_tray_autostart(here),
        "status": lambda: cmd_status(here),
        "stop": lambda: cmd_stop(here),
        "restart": lambda: cmd_restart(here),
        "install": lambda: cmd_install(here),
        "uninstall": lambda: cmd_uninstall(here),
        "listen": lambda: cmd_listen(here),
        "say": lambda: cmd_say(here, " ".join(args.text)),
    }
    return handlers[command]()


if __name__ == "__main__":
    sys.exit(main())
