"""jeepney is a Linux-only dependency and winrt a Windows-only one (pyproject.toml markers), so
what runs on each system must import without the other's. The dev group installs jeepney
everywhere for the D-Bus tests, so each check runs in a fresh interpreter with the package
blocked."""

from __future__ import annotations

import subprocess
import sys

import pytest

# What `strawberry`, `strawberryd` and the Windows doorway import at their start.
SHARED = ["strawberry_crab.cli", "strawberry_crab.strawberryd", "strawberry_crab.server", "strawberry_crab.daemon",
          "strawberry_crab.doctor", "strawberry_crab.setupcmd", "strawberry_crab.media", "strawberry_crab.doorways"]


def imports_without(blocked: str, modules: list[str]) -> subprocess.CompletedProcess:
    code = (f"import sys\nsys.modules[{blocked!r}] = None\n"
            + "".join(f"import {name}\n" for name in modules))
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)


WINDOWS = ["strawberry_crab.smtc", "strawberry_crab.doorways.smtc_watch", "strawberry_crab.doorways.toast_watch",
           "strawberry_crab.wintray", "strawberry_crab.winproc", "strawberry_crab.startup", "strawberry_crab.wasapi",
           "strawberry_crab.doorways.beat_loopback"]
# The tray's shared half: the supervisor and the menu run on both systems, the SNI only on Linux.
TRAY = ["strawberry_crab.supervisor", "strawberry_crab.traymenu"]
# The beat doorway is shared; its capture is picked at runtime (PipeWire or process loopback).
BEAT = ["strawberry_crab.doorways.beat_watch"]


@pytest.mark.parametrize("modules", [SHARED, WINDOWS, TRAY, BEAT])
def test_windows_code_imports_without_jeepney(modules):
    result = imports_without("jeepney", modules)
    assert result.returncode == 0, result.stderr


def test_linux_code_imports_without_winrt():
    result = imports_without("winrt", [*SHARED, *WINDOWS, *TRAY, *BEAT, "strawberry_crab.mpris", "strawberry_crab.tray",
                                       "strawberry_crab.doorways.notify_watch", "strawberry_crab.doorways.beat_pipewire"])
    assert result.returncode == 0, result.stderr


def test_the_daemon_starts_its_parts_without_jeepney():
    """The wake watcher is the one daemon part that speaks D-Bus: without jeepney it has no bus."""
    code = ("import sys, asyncio\nsys.modules['jeepney'] = None\n"
            "from strawberry_crab import wake\n"
            "watcher = wake.WakeWatcher(lambda: None)\n"
            "asyncio.run(watcher.run())\n"
            "assert watcher.reason == 'no system bus (ConnectionError)', watcher.reason\n")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr


def test_doctor_skips_the_dbus_checks_without_jeepney():
    code = ("import sys\nsys.modules['jeepney'] = None\n"
            "from strawberry_crab import doctor\n"
            "probes = doctor.Probes()\n"
            "for check in (*doctor.check_tray_host(probes), *doctor.check_notification_monitor(probes),\n"
            "              *doctor.check_mpris(probes)):\n"
            "    assert check.status == doctor.WARN and 'not checked' in check.detail, check\n")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr


def test_the_beat_doorway_imports_neither_capture_until_it_picks_one():
    """beat_watch never imports a system's capture module itself: backend() does, at runtime."""
    code = ("import sys\n"
            "from strawberry_crab.doorways import beat_watch\n"
            "for name in ('beat_pipewire', 'beat_loopback'):\n"
            "    assert 'strawberry_crab.doorways.' + name not in sys.modules, name\n"
            "assert 'strawberry_crab.wasapi' not in sys.modules\n"
            "picked = beat_watch.backend()\n"
            "assert picked.__name__.endswith('beat_loopback' if sys.platform == 'win32' else 'beat_pipewire')\n")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
