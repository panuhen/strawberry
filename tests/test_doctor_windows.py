"""`strawberry doctor` on Windows: the checks that replace PipeWire, D-Bus, the tray host and
systemd, against a Windows machine described by fake probes (they run on any system), and the
real probes on Windows, which only read."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from strawberry_crab import __version__, doctor, paths
from strawberry_crab.doctor import FAIL, OK, WARN
from tests.test_doctor import TESTED, Machine, by_label, install_voice, install_widget, write_config

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the Windows probes")

LINUX_LABELS = {"PipeWire tools", "tray host", "notification monitor", "MPRIS players", "systemd unit", "unit ExecStart"}


class WindowsMachine(Machine):
    """A healthy Windows 11 machine with the tested setup; tests break one thing at a time."""

    def __init__(self):
        super().__init__()
        self.system = "win32"
        self.tools = {"git", "nvidia-smi", "ollama"}
        self.access: tuple[str | None, str] = ("allowed", "")
        self.sessions: int | None = 2
        self.mic: str | None = "Microphone (USB Audio Device)"
        self.switches: dict[str, str | None] = {"device": "Allow", "user": "Allow", "desktop": "Allow"}
        self.build: int | None = 26200
        self.link: tuple[Path, str] | None = (Path(sys.executable).with_name("strawberry-tray.exe"), "--port 8770")
        self.tray_state: tuple[int | None, bool] = (None, False)
        self.asked_mic: list[str] = []

    def notification_access(self):
        return self.access

    def media_sessions(self):
        return self.sessions

    def microphone(self, preferred=""):
        self.asked_mic.append(preferred)
        return self.mic

    def consent(self, capability):
        assert capability == "microphone"
        return self.switches

    def windows_build(self):
        return self.build

    def shortcut(self, link):
        return self.link

    def tray(self):
        return self.tray_state

    def can_monitor(self):
        raise AssertionError("D-Bus asked on Windows")

    bus_names = bus_has_owner = can_monitor


def install_shortcut(machine: WindowsMachine, target: Path | None = None, args: str = "--port 8770") -> Path:
    link = paths.startup_shortcut()
    link.parent.mkdir(parents=True, exist_ok=True)
    link.write_bytes(b"L\x00\x00\x00")
    target = target or machine.link[0]
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"MZ")
    machine.link = (target, args)
    return link


def healthy() -> WindowsMachine:
    write_config(TESTED)
    install_voice()
    install_widget()
    machine = WindowsMachine()
    machine.urls["http://127.0.0.1:8770/health"] = {"version": __version__, "widgets": 1, "widget_versions": [__version__],
                                                    "tempo": {"silent": False}, "tempo_age_s": 1.0}
    return machine


def test_a_healthy_windows_machine_passes_with_windows_checks_only():
    machine = healthy()
    lines: list[str] = []
    assert doctor.main(probes=machine, say=lines.append) == 0, "\n".join(lines)
    checks, _ = doctor.run_checks(machine)
    labels = [c.label for c in checks]
    assert not LINUX_LABELS & set(labels)
    for label, detail in (("notification access", "allowed"), ("media sessions", "2 session(s)"),
                          ("microphone", "Microphone (USB Audio Device)"), ("process loopback", "Windows build 26200"),
                          ("Startup shortcut", "not installed"), ("tray", "not running"), ("beat watcher", "posting"),
                          ("git", "/usr/bin/git"), ("GPU", "RTX 3090"), ("model gemma3:1b", "voice")):
        check = by_label(checks, label)
        assert check.status == OK and detail in check.detail, check
    # read-only: nvidia-smi and git config are the only commands, and nothing on the bus
    assert {c[0] for c in machine.commands} <= {"nvidia-smi", "rocm-smi", "git"}
    assert all(c[:3] == ["git", "config", "--global"] for c in machine.commands if c[0] == "git")


def test_the_linux_checks_are_the_same_as_before():
    """The same fake machine, as Linux: the Linux labels in their order, no Windows probe asked
    (Machine raises on any)."""
    write_config(TESTED)
    machine = Machine()
    labels = [c.label for c in doctor.run_checks(machine)[0]]
    assert labels[labels.index("widget") + 1:] == ["PipeWire tools", "git", "tray host", "notification monitor",
                                                   "MPRIS players", "systemd unit", "git hooks", "daemon"]


def test_ollama_hints_on_windows():
    machine = WindowsMachine()
    machine.urls.clear()
    machine.tools.discard("ollama")
    check = by_label(doctor.check_ollama(_config(), machine), "ollama")
    assert check.status == FAIL and "https://ollama.com/download" in check.fix and "winget install Ollama.Ollama" in check.fix
    assert "curl" not in check.fix and "systemctl" not in check.fix
    machine.tools.add("ollama")
    check = by_label(doctor.check_ollama(_config(), machine), "ollama")
    assert "Start menu" in check.fix and "ollama serve" in check.fix


def _config():
    from strawberry_crab.config import Config

    return Config()


def test_the_models_that_are_not_pulled_are_named_plainly():
    """This machine's case: embeddinggemma and gemma3:1b not in Ollama."""
    machine = WindowsMachine()
    machine.urls["http://127.0.0.1:11434/api/tags"] = {"models": [{"name": "qwen3.8:27b"}]}
    checks = doctor.check_ollama(_config(), machine)
    assert by_label(checks, "model embeddinggemma").detail == "gate: not pulled"
    assert by_label(checks, "model gemma3:1b").detail == "voice: not pulled"
    assert by_label(checks, "model gemma3:1b").fix.startswith("ollama pull gemma3:1b")


@pytest.mark.parametrize("status", ["denied", "unspecified"])
def test_notification_access_not_allowed_says_where_to_turn_it_on(status):
    machine = WindowsMachine()
    machine.access = (status, "")
    [check] = doctor.check_notification_access(machine)
    assert check.status == WARN and status in check.detail
    assert "Let apps access your notifications" in check.fix
    machine.access = (None, "ToastError")
    [check] = doctor.check_notification_access(machine)
    assert check.status == WARN and "not checked" in check.detail and "ToastError" in check.detail


def test_media_sessions_are_counted_never_named():
    machine = WindowsMachine()
    assert doctor.check_media_sessions(machine)[0].detail == "2 session(s)"
    machine.sessions = 0
    assert "none right now" in doctor.check_media_sessions(machine)[0].detail
    machine.sessions = None
    assert doctor.check_media_sessions(machine)[0].status == WARN


def test_microphone_device_and_privacy_switches():
    config = _config()
    config.voice.enabled = True
    config.voice.source = "usb"
    machine = WindowsMachine()
    [check] = doctor.check_microphone(config, machine)
    assert check.status == OK and machine.asked_mic == ["usb"]
    machine.switches = {"device": "Allow", "user": "Allow", "desktop": "Deny"}
    [check] = doctor.check_microphone(config, machine)
    assert check.status == FAIL and "Let desktop apps access your microphone" in check.detail
    assert "Settings > Privacy & security > Microphone" in check.fix
    machine.switches = {"device": "Allow", "user": "Allow", "desktop": "Allow"}
    machine.mic = None
    [check] = doctor.check_microphone(config, machine)
    assert check.status == WARN and 'source = "usb"' in check.detail and "cannot hear you" in check.detail
    config.voice.enabled = False
    [check] = doctor.check_microphone(config, machine)
    assert check.status == OK and "voice off" in check.detail and "no input found" in check.detail
    machine.switches = {"device": None, "user": None, "desktop": None}
    machine.mic = "Mic"
    config.voice.enabled = True
    [check] = doctor.check_microphone(config, machine)
    assert check.status == OK and "not in the registry" in check.detail


def test_process_loopback_needs_build_19041():
    config = _config()
    machine = WindowsMachine()
    machine.build = 18363
    [check] = doctor.check_loopback(config, machine)
    assert check.status == WARN and "19041" in check.detail
    config.beat.enabled = False
    assert doctor.check_loopback(config, machine)[0].status == OK
    machine.build = 19041
    assert doctor.check_loopback(config, machine)[0].status == OK


def test_startup_shortcut_present_stale_other_install_and_port(tmp_path):
    machine = WindowsMachine()
    assert doctor.check_startup(machine)[0].status == OK                        # not installed
    install_shortcut(machine, target=Path(sys.executable))
    [check] = doctor.check_startup(machine)
    assert check.status == OK and Path(sys.executable).name in check.detail
    machine.link = (tmp_path / "gone" / "strawberry-tray.exe", "--port 8770")
    [check] = doctor.check_startup(machine)
    assert check.status == FAIL and "does not exist" in check.detail
    install_shortcut(machine, target=tmp_path / "other" / "Scripts" / "strawberry-tray.exe")
    [check] = doctor.check_startup(machine)
    assert check.status == WARN and "another install" in check.detail
    install_shortcut(machine, target=Path(sys.executable), args="--port 8790")
    checks = doctor.check_startup(machine)
    assert by_label(checks, "Startup port").status == WARN and "--port 8790" in by_label(checks, "Startup port").detail
    machine.link = None
    assert doctor.check_startup(machine)[0].status == WARN


def test_tray_running_listening_or_missing():
    machine = WindowsMachine()
    assert doctor.check_tray_windows(machine)[0].status == OK
    install_shortcut(machine)
    [check] = doctor.check_tray_windows(machine)
    assert check.status == WARN and "not running" in check.detail
    machine.tray_state = (4242, True)
    [check] = doctor.check_tray_windows(machine)
    assert check.status == OK and "pid 4242" in check.detail
    machine.tray_state = (4242, False)
    assert doctor.check_tray_windows(machine)[0].status == WARN


def test_beat_watcher_on_windows_reads_only_the_daemon():
    config = _config()
    machine = WindowsMachine()
    assert doctor.check_beat_windows(config, machine, None) == []
    assert "no recent estimate" in doctor.check_beat_windows(config, machine, {"tempo_age_s": None})[0].detail
    assert doctor.check_beat_windows(config, machine, {"tempo_age_s": 1.0, "tempo": {"silent": True}})[0].detail == \
        "posting, silent"
    assert machine.commands == []                                              # no pw-dump


@windows_only
def test_the_real_windows_probes_only_read():
    """In tests the listener, the media sessions and the microphone are out of bounds
    (tests/conftest.py), so each says it could not ask; the registry and the build are read."""
    probes = doctor.Probes()
    assert probes.system == "win32"
    assert probes.notification_access() == (None, "ToastError")
    assert probes.media_sessions() is None
    assert probes.microphone() is None
    switches = probes.consent("microphone")
    assert set(switches) == {"device", "user", "desktop"}
    assert all(v is None or isinstance(v, str) for v in switches.values())
    assert probes.windows_build() >= 10240
    assert probes.tray() == (None, False)                                      # the throwaway state dir
    assert probes.shortcut(paths.startup_shortcut()) is None
