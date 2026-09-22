"""`strawberry doctor`: each check against a machine described by fake probes, the exit code, and
`--talk` over a fake /probe. No real Ollama, GPU, bus or daemon is asked anything."""

from __future__ import annotations

import subprocess

import pytest

from strawberry_crab import __version__, doctor, paths
from strawberry_crab.config import Config
from strawberry_crab.doctor import FAIL, OK, WARN


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for name in ("CONFIG", "DATA", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.delenv("STRAWBERRYD_PORT", raising=False)
    monkeypatch.setattr(paths, "widget_project", lambda: None)


class Machine(doctor.Probes):
    """A healthy machine with the tested setup; tests break one thing at a time."""

    def __init__(self):
        self.tools = {"pw-record", "pw-dump", "git", "nvidia-smi", "ollama"}
        self.gpu = "NVIDIA GeForce RTX 3090, 24576, 20000"
        self.urls: dict[str, object] = {
            "http://127.0.0.1:11434/api/tags": {"models": [{"name": "embeddinggemma:latest"}, {"name": "gemma3:1b"},
                                                           {"name": "qwen3.8:27b"}]},
            "http://127.0.0.1:11434/api/version": {"version": "0.12.0"},
        }
        self.watcher = True
        self.modules = {"faster_whisper"}
        self.cuda = 1
        self.posted: list[tuple[str, dict]] = []

    def which(self, name):
        return f"/usr/bin/{name}" if name in self.tools else None

    def run(self, argv, timeout=10.0, **_):
        assert argv[0] == "nvidia-smi"
        return subprocess.CompletedProcess(argv, 0, self.gpu + "\n", "")

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
    paths.widget_dir().mkdir(parents=True, exist_ok=True)
    paths.widget_binary().write_text("#!/bin/sh\n")
    paths.widget_binary().chmod(0o755)
    paths.widget_version_file().write_text(version + "\n")


def test_a_healthy_machine_passes_and_exits_zero():
    write_config(TESTED)
    install_voice()
    install_widget()
    machine = Machine()
    machine.urls["http://127.0.0.1:8770/health"] = {"version": __version__, "widgets": 1, "widget_versions": [__version__]}
    lines: list[str] = []
    assert doctor.main(probes=machine, say=lines.append) == 0
    failed = [line for line in lines if line.startswith(FAIL)]
    assert failed == []
    assert any(line.startswith(f"{OK} model qwen3.8:27b") for line in lines)
    assert any(line.startswith(f"{OK} daemon") for line in lines)


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
