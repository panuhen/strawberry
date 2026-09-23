"""The config, data and state directories come from one module, and the environment decides
them: the XDG variables on Linux, APPDATA and LOCALAPPDATA on Windows. Each test runs as both
systems (sys.platform patched), whichever one the suite is on."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from strawberry_crab import config, paths, speech, tray


@pytest.fixture(params=["linux", "win32"])
def platform(request, monkeypatch):
    monkeypatch.setattr(sys, "platform", request.param)
    return request.param


def point(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))


def test_xdg_variables_are_respected(platform, monkeypatch, tmp_path):
    point(monkeypatch, tmp_path)
    if platform == "win32":
        config_dir, data_dir = tmp_path / "roaming" / "strawberry", tmp_path / "local" / "strawberry"
        state_dir = data_dir / "state"
        hooks = config_dir / "git-hooks"
        legacy = tmp_path / "roaming" / "Godot" / "app_userdata" / "Strawberry" / "widget.cfg"
        binary = data_dir / "widget" / "strawberry-widget.exe"
    else:
        config_dir, data_dir = tmp_path / "c" / "strawberry", tmp_path / "d" / "strawberry"
        state_dir = tmp_path / "s" / "strawberry"
        hooks = tmp_path / "c" / "git" / "hooks"
        legacy = tmp_path / "d" / "godot" / "app_userdata" / "Strawberry" / "widget.cfg"
        binary = data_dir / "widget" / "strawberry-widget"
    assert paths.config_file() == config_dir / "config.toml"
    assert config.default_path() == paths.config_file()
    assert paths.voices_dir() == data_dir / "voices"
    assert speech.default_voices_dir() == paths.voices_dir()
    assert paths.tray_state_file() == state_dir / "tray.json"
    assert paths.git_hooks_dir() == hooks
    assert tray.widget_prefs_path() == paths.widget_prefs_file() == config_dir / "widget.cfg"
    assert paths.legacy_widget_prefs_file() == legacy
    assert paths.widget_binary() == binary
    assert paths.widget_version_file() == data_dir / "widget" / "strawberry-widget.version"


def test_the_linux_session_files_follow_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    point(monkeypatch, tmp_path)
    assert paths.systemd_user_dir() == tmp_path / "c" / "systemd" / "user"
    assert paths.autostart_file() == tmp_path / "c" / "autostart" / "strawberry.desktop"


def test_the_tray_reads_the_old_prefs_until_the_widget_has_moved_them(platform, monkeypatch, tmp_path):
    point(monkeypatch, tmp_path)
    assert tray.read_widget_prefs() == {}
    old = paths.legacy_widget_prefs_file()
    old.parent.mkdir(parents=True)
    old.write_text('[appearance]\n\nskin="mint"\n')
    assert tray.read_widget_prefs()["skin"] == "mint"
    new = paths.widget_prefs_file()
    new.parent.mkdir(parents=True)
    new.write_text('[appearance]\n\nskin="midnight"\n')
    assert tray.read_widget_prefs()["skin"] == "midnight"
    assert old.read_text().count("mint") == 1              # the tray never writes either file


def test_unset_empty_or_relative_fall_back_to_the_defaults(platform, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", "")
    monkeypatch.setenv("XDG_STATE_HOME", "relative/state")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", "relative/local")
    home = Path.home()
    if platform == "win32":
        assert paths.config_dir() == home / "AppData" / "Roaming" / "strawberry"
        assert paths.data_dir() == home / "AppData" / "Local" / "strawberry"
        assert paths.state_dir() == home / "AppData" / "Local" / "strawberry" / "state"
    else:
        assert paths.config_dir() == home / ".config" / "strawberry"
        assert paths.data_dir() == home / ".local" / "share" / "strawberry"
        assert paths.state_dir() == home / ".local" / "state" / "strawberry"


def test_the_icons_are_package_data():
    directory = paths.icons_dir()
    assert directory == Path(paths.__file__).parent / "assets" / "icons"
    assert sorted((p.name for p in directory.glob("*.png")), key=lambda n: int(n[11:-4])) == [
        f"strawberry-{n}.png" for n in (16, 22, 24, 32, 48, 64, 128)]
