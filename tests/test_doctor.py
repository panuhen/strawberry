"""`strawberry doctor`: each check against a machine described by fake probes, the exit code, and
`--talk` over a fake /probe. No real Ollama, GPU, bus or daemon is asked anything."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from strawberry_crab import __version__, cli, doctor, paths
from strawberry_crab.config import Config
from strawberry_crab.doctor import FAIL, OK, WARN
from tests.portable import point_dirs, program


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    point_dirs(monkeypatch, tmp_path)
    monkeypatch.delenv("STRAWBERRYD_PORT", raising=False)
    monkeypatch.setattr(paths, "widget_project", lambda: None)


class Machine(doctor.Probes):
    """A healthy machine with the tested setup; tests break one thing at a time."""

    def __init__(self):
        self.tools = {"pw-record", "pw-dump", "git", "nvidia-smi", "ollama", "systemctl"}
        self.gpu = "NVIDIA GeForce RTX 3090, 24576, 20000"
        self.rocm: str | None = None                  # rocm-smi --json output; None: not installed
        self.drm_root = Path(os.environ["XDG_CACHE_HOME"]) / "drm"     # no sysfs cards unless a test adds one
        self.urls: dict[str, object] = {
            "http://127.0.0.1:11434/api/tags": {"models": [{"name": "embeddinggemma:latest"}, {"name": "gemma3:1b"},
                                                           {"name": "qwen3.8:27b"}]},
            "http://127.0.0.1:11434/api/version": {"version": "0.12.0"},
        }
        self.watcher = True
        self.monitor: tuple[bool | None, str] = (True, "")
        self.names: list[str] | None = [":1.1", "org.freedesktop.Notifications", "org.mpris.MediaPlayer2.spotify"]
        self.unit_state = ("disabled", "inactive")
        self.hooks_path: str | None = None            # git config --global core.hooksPath; None: unset
        self.graph: list[dict] = []
        self.modules = {"faster_whisper"}
        self.cuda = 1
        self.posted: list[tuple[str, dict]] = []
        self.commands: list[list[str]] = []

    def which(self, name):
        return f"/usr/bin/{name}" if name in self.tools else None

    def run(self, argv, timeout=10.0, **_):
        self.commands.append(list(argv))
        if argv[0] not in self.tools and argv[0] != "rocm-smi":
            return subprocess.CompletedProcess(argv, 127, "", "not found")
        if argv[0] == "nvidia-smi":
            return subprocess.CompletedProcess(argv, 0, self.gpu + "\n", "")
        if argv[0] == "rocm-smi":
            return subprocess.CompletedProcess(argv, 127 if self.rocm is None else 0, self.rocm or "", "")
        if argv[:2] == ["systemctl", "--user"]:
            assert argv[2] in ("is-enabled", "is-active"), argv      # doctor only asks
            state = self.unit_state[0 if argv[2] == "is-enabled" else 1]
            return subprocess.CompletedProcess(argv, 0 if state in ("enabled", "active") else 1, state + "\n", "")
        if argv[:3] == ["git", "config", "--global"]:
            assert argv[3:] == ["core.hooksPath"], argv
            return subprocess.CompletedProcess(argv, 1 if self.hooks_path is None else 0, (self.hooks_path or "") + "\n", "")
        if argv == ["pw-dump"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.graph), "")
        raise AssertionError(f"unexpected command {argv}")

    def can_monitor(self):
        return self.monitor

    def bus_names(self):
        return self.names

    def http(self, url, body=None, timeout=3.0):
        if body is not None:
            self.posted.append((url, body))
        return self.urls.get(url)

    def bus_has_owner(self, name):
        return self.watcher

    def find_module(self, name):
        return name in self.modules

    def cuda_devices(self):
        return self.cuda


def by_label(checks, label):
    return next(c for c in checks if c.label == label)


def write_config(text: str) -> None:
    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(text)


TESTED = '[voice]\nmodel = "medium"\ndevice = "cuda"\ncompute_type = "int8_float16"\n[speech]\nenabled = true\n'


def install_voice(name="en_GB-alba-medium"):
    paths.voices_dir().mkdir(parents=True, exist_ok=True)
    (paths.voices_dir() / f"{name}.onnx").write_bytes(b"x")
    (paths.voices_dir() / f"{name}.onnx.json").write_text("{}")


def install_widget(version=__version__):
    program(paths.widget_binary(), b"#!/bin/sh\n")
    paths.widget_version_file().write_text(version + "\n")


def test_a_healthy_machine_passes_and_exits_zero():
    write_config(TESTED)
    install_voice()
    install_widget()
    machine = Machine()
    machine.urls["http://127.0.0.1:8770/health"] = {"version": __version__, "widgets": 1, "widget_versions": [__version__],
                                                    "tempo": {"silent": True}, "tempo_age_s": 1.2}
    lines: list[str] = []
    assert doctor.main(probes=machine, say=lines.append) == 0
    failed = [line for line in lines if line.startswith(FAIL)]
    assert failed == []
    assert any(line.startswith(f"{OK} model qwen3.8:27b") for line in lines)
    assert any(line.startswith(f"{OK} daemon") for line in lines)
    for label in ("notification monitor", "MPRIS players — spotify", "systemd unit", "git hooks", "beat watcher"):
        assert any(line.startswith(f"{OK} {label}") for line in lines), label
    # read-only: nothing but queries went out
    assert not [c for c in machine.commands if c[0] == "systemctl" and c[2] not in ("is-enabled", "is-active")]


def test_config_missing_broken_and_with_keys_left_out():
    checks, config = doctor.check_config(Machine())
    assert checks[0].status == WARN and isinstance(config, Config)
    write_config("[voice]\ndevice = \"tpu\"\n")
    checks, config = doctor.check_config(Machine())
    assert checks[0].status == FAIL and "voice.device" in checks[0].detail
    write_config(TESTED)
    checks, _ = doctor.check_config(Machine())
    assert checks[0].status == OK
    assert checks[1].status == WARN and "daemon.port" in checks[1].detail and "strawberry setup" in checks[1].fix


def test_ollama_down_or_a_model_not_pulled():
    config = Config()
    machine = Machine()
    del machine.urls["http://127.0.0.1:11434/api/tags"]
    [check] = doctor.check_ollama(config, machine)
    assert check.status == FAIL and "systemctl start ollama" in check.fix
    machine.tools.discard("ollama")
    [check] = doctor.check_ollama(config, machine)
    assert "ollama.com/install.sh" in check.fix
    machine = Machine()
    machine.urls["http://127.0.0.1:11434/api/tags"] = {"models": [{"name": "embeddinggemma:latest"}]}
    checks = doctor.check_ollama(config, machine)
    assert by_label(checks, "model embeddinggemma").status == OK        # :latest is implied
    assert by_label(checks, "model gemma3:1b").fix == "ollama pull gemma3:1b (or strawberry setup)"
    config.thinker.enabled = False
    assert "model qwen3.8:27b" not in [c.label for c in doctor.check_ollama(config, machine)]


def test_gpu_checks():
    config = Config()
    machine = Machine()
    assert doctor.check_gpu(config, machine)[0].status == OK
    machine.gpu = "NVIDIA GeForce RTX 3060, 12288, 12000"
    [check] = doctor.check_gpu(config, machine)
    assert check.status == WARN and "~24 GB" in check.detail
    machine.tools.discard("nvidia-smi")
    assert doctor.check_gpu(config, machine)[0].status == WARN
    config.voice.device = "cuda"
    assert doctor.check_gpu(config, machine)[0].status == FAIL


def test_whisper_checks():
    config = Config()
    machine = Machine()
    assert doctor.check_whisper(config, machine)[0].status == OK
    config.voice.device = "cuda"
    assert "1 CUDA device" in doctor.check_whisper(config, machine)[0].detail
    machine.cuda = None
    [check] = doctor.check_whisper(config, machine)
    assert check.status == FAIL and "strawberry-crab[gpu]" in check.fix
    machine.modules.clear()
    assert doctor.check_whisper(config, machine)[0].status == FAIL
    config.voice.enabled = False
    assert doctor.check_whisper(config, machine)[0].status == OK


def test_voice_check():
    config = Config()
    assert doctor.check_voice(config, Machine())[0].status == OK      # speech off
    config.speech.enabled = True
    [check] = doctor.check_voice(config, Machine())
    assert check.status == FAIL and check.fix.startswith("strawberry voices en_GB-alba-medium")
    install_voice()
    assert doctor.check_voice(config, Machine())[0].status == OK


def test_widget_check(monkeypatch):
    machine = Machine()
    [check] = doctor.check_widget(machine)
    assert check.status == FAIL and "strawberry widget --fetch" in check.fix
    monkeypatch.setattr(paths, "widget_project", lambda: paths.data_dir())
    machine.tools.add("godot")
    assert doctor.check_widget(machine)[0].status == WARN             # developer mode
    install_widget("0.0.1")
    [check] = doctor.check_widget(machine)
    assert check.status == FAIL and "0.0.1" in check.detail
    install_widget()
    assert doctor.check_widget(machine)[0].status == OK


def test_pipewire_git_and_the_tray_host():
    machine = Machine()
    assert [c.status for c in doctor.check_tools(machine)] == [OK, OK]
    machine.tools -= {"pw-dump", "git"}
    pipewire, git = doctor.check_tools(machine)
    assert pipewire.status == FAIL and "pw-dump" in pipewire.detail and "apt install pipewire-bin" in pipewire.fix
    assert git.status == WARN
    assert doctor.check_tray_host(machine)[0].status == OK
    machine.watcher = False
    [check] = doctor.check_tray_host(machine)
    assert check.status == WARN and "AppIndicator" in check.fix
    machine.watcher = None
    assert doctor.check_tray_host(machine)[0].status == WARN


def test_daemon_check():
    config = Config()
    machine = Machine()
    checks, health = doctor.check_daemon(config, machine)
    assert checks[0].status == WARN and health is None
    machine.urls["http://127.0.0.1:8770/health"] = {
        "version": __version__, "widgets": 1, "widget_versions": ["0.0.1"],
        "gate": {"model": "embeddinggemma", "ready": False, "disabled_reason": "could not embed"},
        "speech": {"enabled": False, "ready": False, "reason": "speech disabled"},
    }
    checks, _ = doctor.check_daemon(config, machine)
    assert checks[0].status == OK
    assert by_label(checks, "connected widget").status == WARN
    assert by_label(checks, "daemon gate").detail == "not ready: could not embed"
    assert "daemon speech" not in [c.label for c in checks]            # off on purpose is not a problem


def test_a_failure_exits_one():
    lines: list[str] = []
    machine = Machine()
    machine.tools.discard("pw-record")
    assert doctor.main(probes=machine, say=lines.append) == 1
    assert any(line.startswith(f"{FAIL} PipeWire tools") for line in lines)
    assert any(line.strip().startswith("fix:") for line in lines)


def test_talk_prints_latency_per_slot():
    machine = Machine()
    lines: list[str] = []
    assert doctor.talk(Config(), machine, lines.append) is False       # no daemon
    assert "not running" in lines[0]
    machine.urls["http://127.0.0.1:8770/health"] = {"version": __version__}
    machine.urls["http://127.0.0.1:8770/probe"] = {
        "lines": [{"text": "Hello", "gate": {"ok": True, "ms": 170.2}, "voice": {"ok": True, "ms": 640.0},
                   "brain": {"ok": True, "ms": 2100.0}, "tts": {"ok": True, "ms": 95.0}, "whisper": {"ok": True, "ms": 130.0}}],
        "skipped": {},
    }
    lines.clear()
    assert doctor.talk(Config(), machine, lines.append) is True
    assert machine.posted == [("http://127.0.0.1:8770/probe", {})]
    row = next(line for line in lines if line.strip().startswith("1"))
    for number in ("170", "640", "2100", "95", "130"):
        assert number in row
    machine.urls["http://127.0.0.1:8770/probe"] = {
        "lines": [{"text": "Hello", "gate": {"ok": False, "error": "GateError"}}], "skipped": {"tts": "speech off"}}
    lines.clear()
    assert doctor.talk(Config(), machine, lines.append) is False
    assert any("tts skipped: speech off" in line for line in lines)


def add_card(root: Path, name="card1", total=16 * 2**30, used=2**30, product="Radeon RX 7800 XT", vendor="0x1002"):
    device = root / name / "device"
    device.mkdir(parents=True)
    (device / "vendor").write_text(vendor + "\n")
    (device / "mem_info_vram_total").write_text(f"{total}\n")
    (device / "mem_info_vram_used").write_text(f"{used}\n")
    if product:
        (device / "product_name").write_text(product + "\n")
    (root / f"{name}-DP-1").mkdir()                      # a connector, not a card


def test_gpu_on_amd_via_rocm_smi_and_sysfs():
    config = Config()
    machine = Machine()
    machine.tools.discard("nvidia-smi")
    machine.rocm = json.dumps({"card0": {"VRAM Total Memory (B)": str(24 * 2**30), "VRAM Total Used Memory (B)": str(2**30)}})
    [check] = doctor.check_gpu(config, machine)
    assert check.status == OK and "AMD GPU (card0), 24576 MiB, 23552 MiB free" in check.detail and "ROCm" in check.detail
    config.voice.device = "cuda"
    [check] = doctor.check_gpu(config, machine)
    assert check.status == FAIL and "does not run on AMD" in check.detail and 'device = "cpu"' in check.fix
    config.voice.device = "cpu"
    machine.rocm = None                                  # no ROCm installed: the amdgpu driver still says
    add_card(machine.drm_root)
    [check] = doctor.check_gpu(config, machine)
    assert check.status == WARN and "Radeon RX 7800 XT, 16384 MiB" in check.detail and "~24 GB" in check.detail


def test_intel_graphics_is_no_usable_gpu():
    machine = Machine()
    machine.tools.discard("nvidia-smi")
    add_card(machine.drm_root, vendor="0x8086", product="")
    [check] = doctor.check_gpu(Config(), machine)
    assert check.status == WARN and "no usable GPU" in check.detail


def test_notification_monitor():
    machine = Machine()
    assert doctor.check_notification_monitor(machine)[0].status == OK
    machine.monitor = (False, "org.freedesktop.DBus.Error.AccessDenied")
    [check] = doctor.check_notification_monitor(machine)
    assert check.status == WARN and "AccessDenied" in check.detail and "sandbox" in check.fix
    machine.monitor = (None, "KeyError")
    assert "no session bus" in doctor.check_notification_monitor(machine)[0].detail


def test_mpris_players_are_listed_by_name():
    machine = Machine()
    machine.names += ["org.mpris.MediaPlayer2.firefox.instance_1_42", "org.gnome.Shell"]
    [check] = doctor.check_mpris(machine)
    assert check.status == OK and check.detail == "firefox.instance_1_42, spotify"
    machine.names = [":1.1"]
    assert "none on the bus" in doctor.check_mpris(machine)[0].detail
    machine.names = None
    assert doctor.check_mpris(machine)[0].status == WARN


def write_unit(argv: list[str], port=8770):
    paths.systemd_user_dir().mkdir(parents=True, exist_ok=True)
    (paths.systemd_user_dir() / cli.TRAY_UNIT).write_text(cli.unit_text(port, argv))


def fake_entry_point(tmp_path: Path, python: str) -> str:
    exe = tmp_path / "bin" / "strawberry"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text(f"#!{python}\nimport sys\n")
    exe.chmod(0o755)
    return str(exe)


def test_unit_not_installed_is_fine():
    [check] = doctor.check_unit(Machine())
    assert check.status == OK and "not installed" in check.detail


def test_unit_enabled_active_and_pointing_at_this_install():
    machine = Machine()
    machine.unit_state = ("enabled", "active")
    write_unit([sys.executable, "-m", "strawberry_crab"])
    unit, exec_start = doctor.check_unit(machine)
    assert unit.status == OK and "enabled, active" in unit.detail
    assert exec_start.status == OK and exec_start.detail == sys.executable


def test_unit_states_and_a_stale_exec_start(tmp_path):
    machine = Machine()
    write_unit([str(tmp_path / "moved" / "strawberry")])
    machine.unit_state = ("disabled", "inactive")
    unit, exec_start = doctor.check_unit(machine)
    assert unit.status == WARN and "will not start on login" in unit.detail
    assert exec_start.status == FAIL and "does not exist" in exec_start.detail and "strawberry install" in exec_start.fix
    machine.unit_state = ("enabled", "failed")
    unit, _ = doctor.check_unit(machine)
    assert unit.status == WARN and "journalctl" in unit.fix
    # the entry point is there, but its venv is gone (the tool was reinstalled elsewhere)
    write_unit([fake_entry_point(tmp_path, str(tmp_path / "old-venv" / "bin" / "python"))])
    _, exec_start = doctor.check_unit(machine)
    assert exec_start.status == FAIL and "old-venv" in exec_start.detail


def test_unit_from_another_install_and_another_port(tmp_path):
    other = tmp_path / "other-venv" / "bin"
    other.mkdir(parents=True)
    (other / "python").symlink_to(sys.executable)
    machine = Machine()
    machine.unit_state = ("enabled", "active")
    write_unit([fake_entry_point(tmp_path, str(other / "python"))], port=8771)
    _, exec_start, port = doctor.check_unit(machine)
    assert exec_start.status == WARN and "another install" in exec_start.detail
    assert port.status == WARN and "--port 8771" in port.detail
    machine.tools.discard("systemctl")
    assert doctor.check_unit(machine)[0].status == WARN


def install_hooks(target: str):
    paths.git_hooks_dir().mkdir(parents=True, exist_ok=True)
    for name in cli.GIT_HOOKS:
        path = paths.git_hooks_dir() / name
        path.write_text(cli.hook_text(name, [target]))
        path.chmod(0o755)


def test_git_hooks(tmp_path):
    machine = Machine()
    [check] = doctor.check_git_hooks(machine)
    assert check.status == OK and "not installed" in check.detail
    install_hooks(fake_entry_point(tmp_path, sys.executable))
    [check] = doctor.check_git_hooks(machine)
    assert check.status == WARN and "not set" in check.detail
    machine.hooks_path = str(paths.git_hooks_dir())
    assert doctor.check_git_hooks(machine)[0].status == OK
    (paths.git_hooks_dir() / "pre-push").unlink()
    [check] = doctor.check_git_hooks(machine)
    assert check.status == FAIL and "pre-push" in check.detail
    install_hooks(str(tmp_path / "gone" / "strawberry"))
    [check] = doctor.check_git_hooks(machine)
    assert check.status == FAIL and "does not exist" in check.detail and check.fix.startswith("strawberry git-hooks install")
    machine.hooks_path = "/somewhere/else"
    assert "core.hooksPath is /somewhere/else" in doctor.check_git_hooks(machine)[0].detail


PLAYER_NODE = {"id": 77, "type": "PipeWire:Interface:Node", "info": {"state": "running", "props": {
    "media.class": "Stream/Output/Audio", "node.name": "spotify", "application.name": "Spotify",
    "media.name": "Some Artist - Some Song"}}}
OUR_NODE = {"id": 90, "type": "PipeWire:Interface:Node", "info": {"props": {
    "media.class": "Stream/Input/Audio", "node.name": "strawberry-beat"}}}
SINK_NODE = {"id": 40, "type": "PipeWire:Interface:Node", "info": {"props": {
    "media.class": "Audio/Sink", "node.name": "alsa_output.pci"}}}


def link(out_id, in_id):
    return {"id": 500 + out_id, "type": "PipeWire:Interface:Link", "info": {"output-node-id": out_id, "input-node-id": in_id}}


def test_beat_watcher_posting_and_linked():
    config = Config()
    machine = Machine()
    assert doctor.check_beat(config, machine, None) == []                 # daemon down: said elsewhere
    health = {"tempo": {"bpm": 120.0}, "tempo_age_s": 1.5}
    [check] = doctor.check_beat(config, machine, health)
    assert check.status == OK and "idle" in check.detail
    machine.graph = [PLAYER_NODE]
    [check] = doctor.check_beat(config, machine, health)
    assert check.status == WARN and "capture node is not in the graph" in check.detail
    machine.graph = [PLAYER_NODE, OUR_NODE, link(77, 90)]
    [check] = doctor.check_beat(config, machine, health)
    assert check.status == OK and "linked to the player stream (Spotify)" in check.detail
    assert "Some Song" not in check.detail                                # never a track title
    machine.graph = [PLAYER_NODE, OUR_NODE, SINK_NODE, link(40, 90)]
    [check] = doctor.check_beat(config, machine, health)
    assert check.status == WARN and "output device" in check.detail
    machine.graph = [PLAYER_NODE, OUR_NODE]
    assert "not linked" in doctor.check_beat(config, machine, health)[0].detail


def test_beat_watcher_silent_stale_or_never_posted():
    config = Config()
    machine = Machine()
    never = {"tempo": None, "tempo_age_s": None}
    assert doctor.check_beat(config, machine, never)[0].status == OK   # nothing playing: nothing to post
    machine.graph = [PLAYER_NODE, OUR_NODE, link(77, 90)]
    [check] = doctor.check_beat(config, machine, never)
    assert check.status == WARN and "Spotify is playing" in check.detail and "since the daemon started" in check.detail
    [check] = doctor.check_beat(config, machine, {"tempo": None, "tempo_age_s": 42.0})
    assert check.status == WARN and "for 42 s" in check.detail
    [check] = doctor.check_beat(config, machine, {"tempo": {"silent": True}})     # an older daemon
    assert check.status == OK and "posting, silent" in check.detail
    machine.tools.discard("pw-dump")
    assert "not checked" in doctor.check_beat(config, machine, never)[0].detail
    config.beat.enabled = False
    assert doctor.check_beat(config, machine, None)[0].detail == "off in the config"
