"""Start on login on Windows: the Startup shortcut, what it runs, and the CLI's install,
uninstall, status, restart and tray paths there. Every file goes under the throwaway APPDATA
(tests/conftest.py); no tray is started (the start is recorded) and nothing touches the
registry or the task scheduler."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from strawberry_crab import cli, paths, startup, winproc
from tests.portable import program

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the Windows Startup folder")


@pytest.fixture
def started(monkeypatch):
    """What `startup.start` was asked to start; wait_daemon answers at once."""
    calls: list[startup.Launch] = []
    monkeypatch.setattr(startup, "start", lambda what: calls.append(what) or 4242)
    monkeypatch.setattr(cli, "wait_daemon", lambda here: True)
    monkeypatch.delenv("STRAWBERRYD_PORT", raising=False)
    return calls


@pytest.fixture
def shortcuts(monkeypatch):
    """write_shortcut recorded, with a stand-in file where the .lnk would be."""
    written: list[tuple[Path, startup.Launch, Path | None]] = []

    def write(link, what, icon=None, working_dir=None):
        link.parent.mkdir(parents=True, exist_ok=True)
        link.write_bytes(b"L\x00\x00\x00")
        written.append((link, what, icon))

    monkeypatch.setattr(startup, "write_shortcut", write)
    return written


# --- where and what ----------------------------------------------------------------

def test_the_shortcut_is_in_the_startup_folder_of_this_appdata():
    link = paths.startup_shortcut()
    assert link.name == "Strawberry.lnk"
    assert link.parent == paths.appdata() / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    assert "appdata" in str(link)                                  # the conftest's throwaway one
    assert paths.app_ico_file() == paths.data_dir() / "strawberry.ico"


def test_it_runs_the_windowless_launcher_beside_the_cli(tmp_path):
    cli_exe = program(tmp_path / "bin" / ("strawberry.exe" if sys.platform == "win32" else "strawberry"))
    gui = program(tmp_path / "bin" / startup.gui_name())
    what = startup.launch(8784, [str(cli_exe)])
    assert what == startup.Launch(gui, ["--port", "8784"], windowless=True)


def test_without_the_launcher_it_is_pythonw_or_python(tmp_path, monkeypatch):
    python = program(tmp_path / "venv" / "python.exe")
    monkeypatch.setattr(sys, "executable", str(python))
    what = startup.launch(8784, [str(python), "-m", "strawberry_crab"])
    assert what.target == python and not what.windowless           # a console window: install says so
    pythonw = program(tmp_path / "venv" / "pythonw.exe")
    what = startup.launch(8784, [str(tmp_path / "bin" / "strawberry.exe")])     # no strawberry-tray beside it
    assert what == startup.Launch(pythonw, ["-m", "strawberry_crab", "tray", "--port", "8784"], windowless=True)
    assert what.command_line() == f"{pythonw} -m strawberry_crab tray --port 8784"


def test_the_gui_entry_point_is_the_tray(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "main", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr(sys, "argv", ["strawberry-tray", "--port", "8784"])
    with pytest.raises(SystemExit) as stopped:
        cli.tray_main()
    assert stopped.value.code == 0 and seen == [["tray", "--port", "8784"]]


def test_the_package_declares_the_gui_script():
    import tomllib

    project = tomllib.loads((paths.checkout_root() / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["gui-scripts"] == {"strawberry-tray": "strawberry_crab.cli:tray_main"}


@windows_only
def test_powershell_writes_a_real_shortcut_under_the_throwaway_appdata(tmp_path):
    link = paths.startup_shortcut()
    target = program(tmp_path / "bin" / "strawberry-tray.exe")
    icon = startup.write_icon()
    startup.write_shortcut(link, startup.Launch(target, ["--port", "8784"]), icon=icon, working_dir=tmp_path)
    data = link.read_bytes()
    assert data[:4] == b"L\x00\x00\x00"                             # a shell link's header size
    assert data[4:20] == bytes.fromhex("0114020000000000c000000000000046")   # CLSID_ShellLink
    assert "--port 8784".encode("utf-16-le") in data                 # the arguments, stored as UTF-16
    assert str(icon).encode("utf-16-le") in data
    assert icon.read_bytes()[:4] == b"\x00\x00\x01\x00"              # an .ico
    before = link.read_bytes()
    assert startup.read_shortcut(link) == (target, "--port 8784")   # what doctor reads back
    assert link.read_bytes() == before                               # read, not saved
    assert startup.remove() and not link.exists() and not icon.exists()
    assert startup.read_shortcut(link) is None
    assert startup.remove() is False


def test_a_shortcut_powershell_cannot_write_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(startup, "powershell", lambda: str(tmp_path / "no-such-powershell.exe"))
    with pytest.raises(startup.StartupError, match="could not run PowerShell"):
        startup.write_shortcut(tmp_path / "x.lnk", startup.Launch(tmp_path / "t.exe"))


# --- the CLI on Windows --------------------------------------------------------------

@windows_only
def test_install_writes_the_shortcut_and_starts_the_tray(started, shortcuts, capsys):
    here = cli.Here()
    assert cli.cmd_install(here) == 0
    [(link, what, icon)] = shortcuts
    assert link == paths.startup_shortcut() and icon == paths.app_ico_file() and icon.is_file()
    assert what.arguments[-2:] == ["--port", "8770"]
    assert started == [what]                                          # the same command as at login
    out = capsys.readouterr().out
    assert f"wrote {link}: " in out and "installed: at login" in out
    assert str(here.tray_log) in out
    assert not (here.unit_dir / cli.TRAY_UNIT).exists()             # nothing of systemd's
    assert cli.managed() and cli.startup_installed()
    assert cli.cmd_install(here) == 0                                 # again: rewritten, not duplicated
    assert "rewrote" in capsys.readouterr().out and len(list(link.parent.iterdir())) == 1


@windows_only
def test_install_hands_over_from_a_running_tray(started, shortcuts, monkeypatch, capsys):
    stopped = []
    monkeypatch.setattr(cli, "stop_tray", lambda here, quiet=False: stopped.append(quiet) or True)
    assert cli.cmd_install(cli.Here()) == 0
    assert stopped == [True] and "the new one takes over" in capsys.readouterr().out


@windows_only
def test_install_says_when_the_shortcut_cannot_be_written(started, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise startup.StartupError("PowerShell could not write it: denied")

    monkeypatch.setattr(startup, "write_shortcut", fail)
    assert cli.main(["install"]) == 1
    assert "denied" in capsys.readouterr().err and started == []


@windows_only
def test_uninstall_removes_the_shortcut_and_stops_the_tray(started, shortcuts, monkeypatch, capsys):
    here = cli.Here()
    cli.cmd_install(here)
    stopped = []
    monkeypatch.setattr(cli, "stop_tray", lambda here, quiet=False: stopped.append(here) or True)
    assert cli.cmd_uninstall(here) == 0
    assert not paths.startup_shortcut().exists() and not paths.app_ico_file().exists()
    assert len(stopped) == 1 and not cli.managed()
    assert f"removed {paths.startup_shortcut()}" in capsys.readouterr().out


@windows_only
def test_status_says_she_starts_at_login(shortcuts, monkeypatch, capsys):
    monkeypatch.setenv("STRAWBERRYD_PORT", "1")
    startup.write_shortcut(paths.startup_shortcut(), startup.Launch(Path("x")))
    cli.main(["status"])
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"starts at login: {paths.startup_shortcut()} (strawberry uninstall to stop that)"
    assert out[1].startswith("tray: down (strawberry daemon starts it); log: ")
    state = paths.tray_state_file()
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"pid": os.getpid(), "children": []}))
    cli.main(["status"])
    assert capsys.readouterr().out.splitlines()[1].startswith(f"tray: up (pid {os.getpid()}); log: ")


@windows_only
def test_daemon_starts_the_installed_tray_rather_than_a_daemon_of_its_own(started, shortcuts, monkeypatch):
    spawned = []
    monkeypatch.setattr(cli, "spawn", lambda *args: spawned.append(args))
    monkeypatch.setattr(cli, "daemon_up", lambda here: False)
    startup.write_shortcut(paths.startup_shortcut(), startup.Launch(Path("x")))
    assert cli.cmd_daemon(cli.Here()) == 0
    assert len(started) == 1 and spawned == []                       # the tray starts the daemon and doorways


@windows_only
def test_restart_asks_the_running_tray_to_restart_its_children(started, monkeypatch, capsys):
    monkeypatch.setattr(cli, "tray_pid", lambda here: 1234)
    asked = []
    monkeypatch.setattr(winproc, "request_restart", lambda pid: asked.append(pid) or True)
    monkeypatch.setattr(cli, "cmd_stop", lambda here: pytest.fail("the tray must not be stopped from inside"))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.cmd_restart(cli.Here()) == 0
    assert asked == [1234] and "restarted (the tray's children, pid 1234)" in capsys.readouterr().out


@windows_only
def test_a_second_tray_is_not_started(monkeypatch, capsys):
    monkeypatch.setattr(cli, "tray_pid", lambda here: 1234)
    monkeypatch.setattr(cli, "run_daemon_main", lambda argv: pytest.fail("a second tray"))
    assert cli.cmd_tray(cli.Here(), []) == 0
    assert "already running (pid 1234)" in capsys.readouterr().out


@windows_only
def test_stop_ends_the_tray_through_its_stop_event(monkeypatch, capsys):
    monkeypatch.setattr(cli, "tray_pid", lambda here: 1234)
    calls = []
    monkeypatch.setattr(winproc, "stop", lambda pid, key, timeout_s: calls.append((pid, key, timeout_s)) or "stopped")
    assert cli.cmd_stop(cli.Here()) == 0
    assert calls == [(1234, None, cli.TRAY_STOP_TIMEOUT_S)]          # keyed by the pid tray.json gives
    assert "tray stopped (pid 1234, with its children)" in capsys.readouterr().out
