"""The XDG directories come from one module, and the environment decides them."""

from __future__ import annotations

from pathlib import Path

from strawberry import config, paths, speech, tray


def test_xdg_variables_are_respected(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    assert paths.config_file() == tmp_path / "c" / "strawberry" / "config.toml"
    assert config.default_path() == paths.config_file()
    assert paths.voices_dir() == tmp_path / "d" / "strawberry" / "voices"
    assert speech.default_voices_dir() == paths.voices_dir()
    assert paths.tray_state_file() == tmp_path / "s" / "strawberry" / "tray.json"
    assert paths.systemd_user_dir() == tmp_path / "c" / "systemd" / "user"
    assert paths.autostart_file() == tmp_path / "c" / "autostart" / "strawberry.desktop"
    assert paths.git_hooks_dir() == tmp_path / "c" / "git" / "hooks"
    assert tray.widget_prefs_path() == paths.widget_prefs_file() == tmp_path / "c" / "strawberry" / "widget.cfg"
    assert paths.legacy_widget_prefs_file() == tmp_path / "d" / "godot" / "app_userdata" / "Strawberry" / "widget.cfg"
    assert paths.widget_binary() == tmp_path / "d" / "strawberry" / "widget" / "strawberry-widget"


def test_the_tray_reads_the_old_prefs_until_the_widget_has_moved_them(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
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


def test_unset_empty_or_relative_fall_back_to_the_defaults(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", "")
    monkeypatch.setenv("XDG_STATE_HOME", "relative/state")
    home = Path.home()
    assert paths.config_dir() == home / ".config" / "strawberry"
    assert paths.data_dir() == home / ".local" / "share" / "strawberry"
    assert paths.state_dir() == home / ".local" / "state" / "strawberry"


def test_the_icons_are_package_data():
    directory = paths.icons_dir()
    assert directory == Path(paths.__file__).parent / "assets" / "icons"
    assert sorted(p.name for p in directory.glob("*.png")) == [
        f"strawberry-{n}.png" for n in (16, 22, 24, 32, 48, 64)]
