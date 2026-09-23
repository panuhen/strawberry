r"""Start on login on Windows: a shortcut in the user's Startup folder (WINDOWS.md step 4).

`strawberry install` writes `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Strawberry.lnk`
and `strawberry uninstall` deletes it. At login Explorer runs every shortcut there, as the user,
with no rights beyond the user's own; a scheduled task would need more and is harder to undo,
so none is made. Task Manager's Startup apps page lists it as "Strawberry" and can turn it off.

What the shortcut runs is the tray, without a console window:

1. `strawberry-tray.exe --port <port>` beside the `strawberry.exe` that ran install: the package's
   GUI entry point (`[project.gui-scripts]`), which uv and pip make as a windowless launcher;
2. else `pythonw.exe -m strawberry_crab tray --port <port>` beside this interpreter;
3. else `python.exe` (a console window stays open while she runs; install says so).

The shortcut is written with the Windows Script Host's `WScript.Shell` through PowerShell 5.1,
which every Windows 10 and 11 has: no extra dependency, and the values go in through environment
variables, so no path needs quoting. Its icon is the berry as an .ico (`paths.app_ico_file()`),
made from the packaged PNGs.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import icons, paths

GUI_SCRIPT = "strawberry-tray"
DESCRIPTION = "Strawberry: the tray icon, and under it the daemon, the doorways and the widget"
POWERSHELL_TIMEOUT_S = 60

WRITE_SHORTCUT = r"""
$ErrorActionPreference = 'Stop'
$link = (New-Object -ComObject WScript.Shell).CreateShortcut($env:STRAWBERRY_LNK)
$link.TargetPath = $env:STRAWBERRY_LNK_TARGET
$link.Arguments = $env:STRAWBERRY_LNK_ARGS
$link.WorkingDirectory = $env:STRAWBERRY_LNK_DIR
$link.Description = $env:STRAWBERRY_LNK_DESCRIPTION
if ($env:STRAWBERRY_LNK_ICON) { $link.IconLocation = $env:STRAWBERRY_LNK_ICON + ',0' }
$link.Save()
"""


class StartupError(Exception):
    """The shortcut could not be written; the message says why."""


@dataclass(frozen=True)
class Launch:
    """What the shortcut starts."""

    target: Path
    arguments: list[str] = field(default_factory=list)
    windowless: bool = True

    def argv(self) -> list[str]:
        return [str(self.target), *self.arguments]

    def command_line(self) -> str:
        return subprocess.list2cmdline(self.argv())


def gui_name() -> str:
    return f"{GUI_SCRIPT}.exe" if paths.windows() else GUI_SCRIPT


def launch(port: int, cli: list[str]) -> Launch:
    """The tray, windowless if this install can: `cli` is how this CLI runs (cli.cli_argv())."""
    if len(cli) == 1:
        gui = Path(cli[0]).with_name(gui_name())
        if gui.is_file():
            return Launch(gui, ["--port", str(port)])
    args = ["-m", "strawberry_crab", "tray", "--port", str(port)]
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    if pythonw.is_file():
        return Launch(pythonw, args)
    return Launch(python, args, windowless=False)


def installed() -> bool:
    return paths.startup_shortcut().is_file()


def powershell() -> str:
    """Windows PowerShell 5.1 where Windows keeps it, else whatever `powershell` is on PATH."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    candidate = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(candidate) if candidate.is_file() else "powershell.exe"


def write_icon(path: Path | None = None) -> Path:
    path = path or paths.app_ico_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(icons.ico_bytes())
    return path


def write_shortcut(link: Path, what: Launch, icon: Path | None = None, working_dir: Path | None = None) -> None:
    """Write (or rewrite) the .lnk at `link`. Raises StartupError with PowerShell's reason."""
    link.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ,
           "STRAWBERRY_LNK": str(link),
           "STRAWBERRY_LNK_TARGET": str(what.target),
           "STRAWBERRY_LNK_ARGS": subprocess.list2cmdline(what.arguments),
           "STRAWBERRY_LNK_DIR": str(working_dir or Path.home()),
           "STRAWBERRY_LNK_DESCRIPTION": DESCRIPTION,
           "STRAWBERRY_LNK_ICON": str(icon) if icon else ""}
    encoded = base64.b64encode(WRITE_SHORTCUT.encode("utf-16-le")).decode("ascii")
    argv = [powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]
    try:
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=POWERSHELL_TIMEOUT_S,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StartupError(f"could not run PowerShell to write {link} ({exc})") from None
    if result.returncode != 0 or not link.is_file():
        reason = (result.stderr or result.stdout).strip().splitlines()
        raise StartupError(f"PowerShell could not write {link}: {reason[0] if reason else f'exit {result.returncode}'}")


def remove() -> bool:
    """Delete the shortcut and its icon. True if there was a shortcut."""
    link = paths.startup_shortcut()
    existed = link.is_file()
    link.unlink(missing_ok=True)
    paths.app_ico_file().unlink(missing_ok=True)
    return existed


def start(what: Launch) -> int:
    """Start the tray now, as the shortcut would at login: detached from this console, so closing
    the terminal does not take it along. Returns the pid of what was started."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if not what.windowless:
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(what.argv(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, cwd=str(Path.home()), creationflags=flags, close_fds=True)
    return process.pid
