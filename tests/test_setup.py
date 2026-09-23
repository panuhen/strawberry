"""`strawberry setup`: the tier for a card, editing the user's config without losing what is in it,
the keys it lacks, and when the widget is fetched. Nothing here runs ollama, nvidia-smi, piper or
systemctl for real, and the XDG dirs are throwaway."""

from __future__ import annotations

import subprocess
import tomllib

import pytest

from strawberry_crab import cli, paths, setupcmd, widgetbin
from strawberry_crab.config import ConfigError, default_toml, load
from tests.portable import point_dirs


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    point_dirs(monkeypatch, tmp_path)
    monkeypatch.delenv("STRAWBERRYD_PORT", raising=False)
    monkeypatch.setattr(cli, "systemctl", lambda *a, capture=True: subprocess.CompletedProcess(a, 1, "", ""))
    monkeypatch.setattr(setupcmd, "DRM_ROOT", tmp_path / "drm")       # never this machine's sysfs


def smi(stdout: str, code: int = 0, rocm: str | None = None):
    def run(argv, **kwargs):
        if argv[0] == "rocm-smi":
            return subprocess.CompletedProcess(argv, 127 if rocm is None else 0, rocm or "", "")
        assert argv[0] == "nvidia-smi"
        return subprocess.CompletedProcess(argv, code, stdout, "")
    return run


def amd_card(root, name="card0", total_mb=16384, used_mb=1024, vendor="0x1002", product=""):
    device = root / name / "device"
    device.mkdir(parents=True)
    (device / "vendor").write_text(vendor + "\n")
    (device / "mem_info_vram_total").write_text(f"{total_mb * 2**20}\n")
    (device / "mem_info_vram_used").write_text(f"{used_mb * 2**20}\n")
    if product:
        (device / "product_name").write_text(product + "\n")


def test_amd_cards_come_from_rocm_smi_then_sysfs_and_intel_is_no_gpu(tmp_path):
    rocm = '{"card0": {"VRAM Total Memory (B)": "25753026560", "VRAM Total Used Memory (B)": "1073741824"}}'
    gpu = setupcmd.detect_gpu(smi("", code=9, rocm=rocm))
    assert gpu == setupcmd.Gpu("AMD GPU (card0)", 24560, 23536, "amd")
    assert setupcmd.detect_gpu(smi("", code=9, rocm="not json")) is None
    root = tmp_path / "sys"
    amd_card(root, "card0", total_mb=512, vendor="0x8086")               # Intel: not counted
    amd_card(root, "card1", total_mb=12288, product="Radeon RX 6700 XT")
    (root / "card1-HDMI-A-1").mkdir()
    gpu = setupcmd.detect_gpu(smi("", code=9), drm_root=root)
    assert gpu == setupcmd.Gpu("Radeon RX 6700 XT", 12288, 11264, "amd")
    assert setupcmd.detect_gpu(smi("", code=9), drm_root=tmp_path / "none") is None
    nvidia = setupcmd.detect_gpu(smi("NVIDIA GeForce RTX 3090, 24576, 20000\n"), drm_root=root)
    assert nvidia.vendor == "nvidia"                                     # nvidia-smi comes first


def test_amd_tiers_keep_the_brain_and_put_whisper_on_the_cpu():
    tier = setupcmd.pick_tier(setupcmd.Gpu("AMD GPU (card0)", 24560, 20000, "amd"))
    assert tier.name == "24gb" and tier.brain == setupcmd.DEFAULT_TIER.brain
    assert (tier.whisper, tier.whisper_device, tier.whisper_compute) == ("small", "cpu", "int8")
    assert "whisper small on the CPU" in tier.describe()
    assert setupcmd.tier_named("10gb", "amd").whisper_device == "cpu"
    assert setupcmd.tier_named("10gb").whisper_device == "cuda"
    assert "whisper medium on CUDA" in setupcmd.DEFAULT_TIER.describe()


# --- what fits -------------------------------------------------------------------

def test_the_gpu_comes_from_nvidia_smi_and_the_biggest_card_wins():
    gpu = setupcmd.detect_gpu(smi("NVIDIA GeForce RTX 3060, 12288, 11000\nNVIDIA GeForce RTX 3090, 24576, 20000\n"))
    assert gpu == setupcmd.Gpu("NVIDIA GeForce RTX 3090", 24576, 20000)
    assert setupcmd.detect_gpu(smi("", code=9)) is None
    assert setupcmd.detect_gpu(smi("garbage\n")) is None


@pytest.mark.parametrize("total, tier", [(24576, "24gb"), (16376, "16gb"), (12288, "10gb"), (10240, "10gb"),
                                         (8192, "6gb"), (6144, "6gb"), (4096, "cpu"), (None, "cpu")])
def test_the_tier_follows_total_vram(total, tier):
    gpu = setupcmd.Gpu("card", total, 0) if total else None
    assert setupcmd.pick_tier(gpu).name == tier


def test_the_default_tier_is_the_tested_setup():
    values = setupcmd.tier_values(setupcmd.DEFAULT_TIER)
    assert values == {
        "gate.model": "embeddinggemma", "brain.reaction_model": "gemma3:1b", "brain.action_model": "qwen3.8:27b",
        "voice.model": "medium", "voice.device": "cuda", "voice.compute_type": "int8_float16",
        "speech.voice": "en_GB-alba-medium", "thinker.enabled": True, "speech.enabled": True,
    }
    cpu = setupcmd.tier_values(setupcmd.tier_named("cpu"))
    assert cpu["thinker.enabled"] is False and cpu["voice.device"] == "cpu" and cpu["voice.compute_type"] == "int8"


def test_licence_lines_name_each_family():
    assert "Gemma Terms of Use" in setupcmd.model_licence("gemma3:1b")
    assert "ai.google.dev/gemma/terms" in setupcmd.model_licence("embeddinggemma")
    assert "Apache-2.0" in setupcmd.model_licence("qwen3.8:27b")
    assert "ollama.com/library/phi4" in setupcmd.model_licence("phi4:14b")
    assert "CC BY 4.0" in setupcmd.voice_licence("en_GB-alba-medium")


# --- the config file ----------------------------------------------------------------

USER_FILE = """# my settings
[brain]
reaction_model = "gemma3:4b"   # I like this one

[voice]
enabled = true
"""


def test_set_key_adds_to_the_section_keeps_existing_values_and_comments():
    text, changed = setupcmd.set_key(USER_FILE, "brain.reaction_model", "gemma3:1b", overwrite=False)
    assert not changed and text == USER_FILE
    text, changed = setupcmd.set_key(USER_FILE, "brain.action_model", "qwen3:8b", overwrite=False, note="setup")
    assert changed
    lines = text.splitlines()
    assert lines[lines.index('reaction_model = "gemma3:4b"   # I like this one') + 1] == 'action_model = "qwen3:8b"   # setup'
    text, changed = setupcmd.set_key(text, "brain.reaction_model", "gemma3:1b", overwrite=True)
    assert 'reaction_model = "gemma3:1b"   # I like this one' in text      # value replaced, comment kept
    text, _ = setupcmd.set_key(text, "gate.model", "embeddinggemma", overwrite=False)
    assert text.rstrip().endswith('[gate]\nmodel = "embeddinggemma"')
    assert tomllib.loads(text)["brain"] == {"reaction_model": "gemma3:1b", "action_model": "qwen3:8b"}


def test_set_key_stops_at_the_next_table_even_an_array_of_tables():
    text = '[brain]\nenabled = true\n\n[[brain.examples]]\nevent = "x"\nline = "y"\nemotion = "happy"\n'
    text, _ = setupcmd.set_key(text, "brain.reaction_model", "gemma3:1b", overwrite=False)
    data = tomllib.loads(text)
    assert data["brain"]["reaction_model"] == "gemma3:1b"
    assert data["brain"]["examples"] == [{"event": "x", "line": "y", "emotion": "happy"}]


def test_set_key_leaves_a_multi_line_value_alone():
    text = '[notifications]\nignore_apps = [\n  "Spotify",\n]\n'
    assert setupcmd.set_key(text, "notifications.ignore_apps", ["x"], overwrite=True) == (text, False)


def test_missing_keys_are_the_defaults_the_file_does_not_set():
    missing = dict(setupcmd.missing_keys(USER_FILE))
    assert "brain.reaction_model" not in missing and "voice.enabled" not in missing
    assert missing["daemon.port"] == 8770 and missing["voice.device"] == "cpu"
    assert not setupcmd.LONG_KEYS & set(missing)             # no persona, no example lists
    appended = setupcmd.append_missing(USER_FILE, list(missing.items()), "2026-09-22")
    assert setupcmd.missing_keys(appended) == []
    assert "# default, added by strawberry setup 2026-09-22" in appended
    assert "# I like this one" in appended
    setupcmd.check_config_text(appended)                     # loads and validates


def test_the_template_itself_lacks_only_some_keys():
    missing = dict(setupcmd.missing_keys(default_toml()))
    assert "voice.compute_type" in missing and "brain.reaction_model" not in missing


def test_a_broken_edit_is_never_written(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(USER_FILE)
    with pytest.raises(ConfigError):
        setupcmd.write_config(path, USER_FILE + "[voice]\ndevice = \"tpu\"\n", print)
    assert path.read_text() == USER_FILE
    assert list(tmp_path.iterdir()) == [path]                # no backup either


def test_writing_backs_up_first(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(USER_FILE)
    saved = setupcmd.write_config(path, USER_FILE + "\n[gate]\nenabled = true\n", lambda line: None)
    assert saved is not None and saved.read_text() == USER_FILE and saved.name.startswith("config.toml.bak-")
    assert setupcmd.write_config(path, path.read_text(), lambda line: None) is None     # unchanged: no backup


# --- the whole run, with the outside world faked ------------------------------------

class World:
    """nvidia-smi, ollama and piper as the tests want them; every command is recorded."""

    def __init__(self, gpu: str = "NVIDIA GeForce RTX 3090, 24576, 20000", models=("embeddinggemma:latest",)):
        self.gpu = gpu
        self.rocm = ""
        self.models = list(models)
        self.commands: list[list[str]] = []

    def run(self, argv, **kwargs):
        self.commands.append(list(argv))
        if argv[0] == "nvidia-smi":
            return subprocess.CompletedProcess(argv, 0 if self.gpu else 9, self.gpu + "\n", "")
        if argv[0] == "rocm-smi":
            return subprocess.CompletedProcess(argv, 0 if self.rocm else 127, self.rocm, "")
        if argv[:2] == ["ollama", "pull"]:
            self.models.append(argv[2])
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "piper.download_voices" in argv:
            directory, voice = argv[argv.index("--download-dir") + 1], argv[-1]
            from pathlib import Path

            (Path(directory) / f"{voice}.onnx").write_bytes(b"onnx")
            (Path(directory) / f"{voice}.onnx.json").write_text("{}")
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command {argv}")

    def pulls(self) -> list[str]:
        return [c[2] for c in self.commands if c[:2] == ["ollama", "pull"]]


@pytest.fixture
def world(monkeypatch):
    w = World()
    monkeypatch.setattr(setupcmd, "ollama_models", lambda url, timeout=3.0: list(w.models))
    fetched = []
    monkeypatch.setattr(widgetbin, "fetch", lambda version, **kw: fetched.append(version))
    w.fetched = fetched
    return w


def run_setup(world, answers=None, **kwargs):
    lines: list[str] = []
    asked: list[str] = []

    def ask(question, default):
        asked.append(question)
        return (answers or {}).get(question.strip().split(" [")[0], default)

    setup = setupcmd.Setup(ask=ask, run=world.run, say=lines.append, **kwargs)
    code = setup.main()
    return code, "\n".join(lines), asked


def test_yes_on_a_fresh_machine_writes_the_tested_setup_and_pulls_what_is_missing(world):
    code, out, _ = run_setup(world, yes=True)
    assert code == 0, out
    config = load(paths.config_file())
    assert (config.brain.action_model, config.brain.reaction_model, config.gate.model) == \
        ("qwen3.8:27b", "gemma3:1b", "embeddinggemma")
    assert (config.voice.model, config.voice.device, config.voice.compute_type) == ("medium", "cuda", "int8_float16")
    assert config.speech.enabled and config.speech.voice == "en_GB-alba-medium"
    assert world.pulls() == ["gemma3:1b", "qwen3.8:27b"]              # embeddinggemma was there
    assert "gemma3:1b — Gemma Terms of Use, https://ai.google.dev/gemma/terms" in out
    assert "qwen3.8:27b — Apache-2.0" in out
    assert "not part of this package" in out
    assert (paths.voices_dir() / "en_GB-alba-medium.onnx").is_file()
    assert world.fetched == [__import__("strawberry_crab").__version__]
    assert "setup --yes --install" in out                              # install offered, not run
    assert not list(paths.config_file().parent.glob("*.bak-*"))       # nothing to back up


def test_a_second_run_changes_nothing_and_pulls_nothing(world, monkeypatch):
    run_setup(world, yes=True)
    before = paths.config_file().read_text()
    world.commands.clear()
    monkeypatch.setattr(widgetbin, "installed_version", lambda: __import__("strawberry_crab").__version__)
    code, out, _ = run_setup(world, yes=True)
    assert code == 0
    assert world.pulls() == [] and world.fetched == [__import__("strawberry_crab").__version__]
    assert paths.config_file().read_text() == before
    assert "✓ strawberry-widget" in out


def test_an_existing_file_keeps_its_values_and_gets_a_backup(world):
    paths.config_file().parent.mkdir(parents=True)
    paths.config_file().write_text(USER_FILE)
    code, out, _ = run_setup(world, yes=True)
    assert code == 0, out
    text = paths.config_file().read_text()
    assert 'reaction_model = "gemma3:4b"   # I like this one' in text
    assert "kept brain.reaction_model = \"gemma3:4b\"" in out
    assert 'action_model = "qwen3.8:27b"   # strawberry setup' in text
    backups = list(paths.config_file().parent.glob("config.toml.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == USER_FILE
    assert "gemma3:4b" in world.pulls()                                # her file's model is the one pulled


def test_a_typed_model_overrides_even_the_users_file(world):
    paths.config_file().parent.mkdir(parents=True)
    paths.config_file().write_text(USER_FILE)
    code, out, asked = run_setup(world, answers={"desktop voice (small Ollama model)": "gemma3:270m",
                                                 "brain (Ollama model with tool calling)": "qwen3:14b"})
    assert load(paths.config_file()).brain.reaction_model == "gemma3:270m"
    assert load(paths.config_file()).brain.action_model == "qwen3:14b"
    assert "gemma3:270m" in world.pulls() and "qwen3:14b" in world.pulls()
    assert any("tier" in q for q in asked)


def test_a_small_card_gets_a_smaller_brain_and_no_gpu_turns_the_thinker_off(world):
    world.gpu = "NVIDIA GeForce RTX 3060, 12288, 12000"
    run_setup(world, yes=True)
    config = load(paths.config_file())
    assert config.brain.action_model == "qwen3:8b" and config.voice.device == "cuda"
    paths.config_file().unlink()
    world.gpu = ""
    world.commands.clear()
    code, out, _ = run_setup(world, yes=True)
    config = load(paths.config_file())
    assert not config.thinker.enabled and config.voice.device == "cpu"
    assert "qwen3:8b" not in world.pulls() and "qwen3.8:27b" not in world.pulls()
    assert "needs a ~24 GB card" in out


def test_an_amd_card_keeps_the_brain_and_runs_whisper_on_the_cpu(world):
    world.gpu = ""
    world.rocm = '{"card0": {"VRAM Total Memory (B)": "25753026560", "VRAM Total Used Memory (B)": "0"}}'
    code, out, _ = run_setup(world, yes=True)
    assert code == 0, out
    config = load(paths.config_file())
    assert config.thinker.enabled and config.brain.action_model == "qwen3.8:27b"
    assert (config.voice.model, config.voice.device, config.voice.compute_type) == ("small", "cpu", "int8")
    assert "Ollama runs the models on ROCm" in out and "whisper small on the CPU" in out


def test_no_ollama_is_a_failure_with_the_installer_named(world, monkeypatch):
    monkeypatch.setattr(setupcmd, "ollama_models", lambda url, timeout=3.0: None)
    monkeypatch.setattr(setupcmd.shutil, "which", lambda name: None)
    code, out, _ = run_setup(world, yes=True)
    assert code == 1
    assert "https://ollama.com/install.sh" in out and world.pulls() == []
    assert not any(c[:2] == ["sh", "-c"] for c in world.commands)     # --yes never runs a remote script


@pytest.mark.parametrize("installed, fetch", [(None, True), ("0.0.9", True), ("VERSION", False)])
def test_the_widget_is_fetched_only_when_missing_or_another_version(installed, fetch):
    from strawberry_crab import __version__

    assert setupcmd.needs_widget(__version__ if installed == "VERSION" else installed, __version__) is fetch


def test_a_failed_widget_fetch_fails_setup_outside_a_checkout(world, monkeypatch):
    def boom(version, **kw):
        raise widgetbin.FetchError("HTTP 404 (is there a release v0.1.0?)")

    monkeypatch.setattr(widgetbin, "fetch", boom)
    monkeypatch.setattr(paths, "widget_project", lambda: None)
    code, out, _ = run_setup(world, yes=True)
    assert code == 1 and "widget fetch failed: HTTP 404" in out


def test_missing_keys_are_appended_only_when_asked(world):
    paths.config_file().parent.mkdir(parents=True)
    paths.config_file().write_text(USER_FILE)
    run_setup(world, yes=True)
    assert setupcmd.missing_keys(paths.config_file().read_text())      # --yes leaves them to their defaults
    code, out, _ = run_setup(world, answers={
        "append them with their defaults (commented as such) so you can see and edit them?": "y"})
    assert setupcmd.missing_keys(paths.config_file().read_text()) == []
    assert "appended" in out


def test_an_asked_for_tier_replaces_the_files_models(world):
    paths.config_file().parent.mkdir(parents=True)
    paths.config_file().write_text('[brain]\naction_model = "qwen3.8:27b"   # mine\n[voice]\nmodel = "medium"\n')
    code, out, _ = run_setup(world, yes=True, tier="10gb")
    config = load(paths.config_file())
    assert config.brain.action_model == "qwen3:8b" and config.voice.model == "small"
    assert 'action_model = "qwen3:8b"   # mine' in paths.config_file().read_text()
    assert "kept" not in out
    assert len(list(paths.config_file().parent.glob("config.toml.bak-*"))) == 1


def test_install_runs_with_yes_only_when_asked(world, monkeypatch):
    ran = []
    monkeypatch.setattr(cli, "cmd_install", lambda here: ran.append(here) or 0)
    run_setup(world, yes=True)
    assert ran == []
    run_setup(world, yes=True, install=True)
    assert len(ran) == 1
