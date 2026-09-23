"""The listen hotkey as a setting: the combinations, `[voice] hotkey`, and `strawberry hotkey` on
Windows (it writes the setting; the tray registers it: tests/test_wintray.py)."""

from __future__ import annotations

import pytest

from strawberry_crab import cli, hotkey, paths
from strawberry_crab.config import ConfigError, default_path, load


@pytest.mark.parametrize("combo, modifiers, vk, text", [
    ("<Control><Alt>space", hotkey.MOD_CONTROL | hotkey.MOD_ALT, 0x20, "<Control><Alt>space"),
    ("Ctrl+Alt+Space", hotkey.MOD_CONTROL | hotkey.MOD_ALT, 0x20, "<Control><Alt>space"),
    ("<Super><Shift>space", hotkey.MOD_WIN | hotkey.MOD_SHIFT, 0x20, "<Shift><Super>space"),
    ("<Primary><Shift>L", hotkey.MOD_CONTROL | hotkey.MOD_SHIFT, 0x4C, "<Control><Shift>l"),
    ("win+f9", hotkey.MOD_WIN, 0x78, "<Super>F9"),
    ("<Control><Alt><Shift>F24", hotkey.MOD_CONTROL | hotkey.MOD_ALT | hotkey.MOD_SHIFT, 0x87,
     "<Control><Alt><Shift>F24"),
    ("Alt+Page_Down", hotkey.MOD_ALT, 0x22, "<Alt>Page_Down"),
    ("<Alt>7", hotkey.MOD_ALT, 0x37, "<Alt>7"),
])
def test_combinations_parse_in_either_syntax(combo, modifiers, vk, text):
    parsed = hotkey.parse(combo)
    assert (parsed.modifiers, parsed.vk, parsed.text) == (modifiers, vk, text)
    assert hotkey.parse(parsed.text) == parsed


@pytest.mark.parametrize("combo, says", [
    ("space", "needs a modifier"),
    ("<Hyper>space", "not a modifier"),
    ("<Control>semicolon", "not a key"),
    ("Ctrl+", "not a key"),
    ("", "not a key"),
])
def test_what_does_not_parse_says_why(combo, says):
    with pytest.raises(ValueError, match=says):
        hotkey.parse(combo)


def test_the_windows_setting_empty_is_the_default_and_off_is_none():
    assert hotkey.windows_hotkey("").text == hotkey.WINDOWS_DEFAULT == "<Control><Alt>space"
    assert hotkey.windows_hotkey("off") is None and hotkey.windows_hotkey(" OFF ") is None
    assert hotkey.windows_hotkey("Ctrl+Shift+F9").text == "<Control><Shift>F9"


def test_read_setting(tmp_path):
    path = tmp_path / "config.toml"
    assert hotkey.read_setting(path) == ""
    path.write_text("[voice]\nmodel = \"small\"\n", encoding="utf-8")
    assert hotkey.read_setting(path) == ""
    path.write_text("[voice]\nhotkey = \"Ctrl+Alt+H\"\n", encoding="utf-8")
    assert hotkey.read_setting(path) == "Ctrl+Alt+H"


def test_the_config_refuses_a_hotkey_that_does_not_parse(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[voice]\nhotkey = \"<Control>\"\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="voice.hotkey"):
        load(path, env={})
    path.write_text("[voice]\nhotkey = \"off\"\n", encoding="utf-8")
    assert load(path, env={}).voice.hotkey == "off"


@pytest.fixture
def on_windows(monkeypatch):
    """`strawberry hotkey` takes the Windows branch; the config goes to the throwaway APPDATA."""
    monkeypatch.setattr(paths, "windows", lambda: True)


def test_hotkey_on_windows_writes_the_setting(on_windows, capsys):
    assert cli.main(["hotkey", "Ctrl+Shift+F9"]) == 0
    assert "hotkey <Control><Shift>F9 -> listen, registered by the tray" in capsys.readouterr().out
    assert hotkey.read_setting(default_path()) == "Ctrl+Shift+F9"
    assert load(default_path(), env={}).voice.hotkey == "Ctrl+Shift+F9"

    assert cli.main(["hotkey", "--remove"]) == 0
    assert "hotkey off" in capsys.readouterr().out
    assert hotkey.read_setting(default_path()) == "off"

    assert cli.main(["hotkey"]) == 0                                   # back to the default
    assert "hotkey <Control><Alt>space -> listen" in capsys.readouterr().out
    assert hotkey.read_setting(default_path()) == ""


def test_hotkey_on_windows_refuses_what_it_cannot_register(on_windows, capsys):
    assert cli.main(["hotkey", "space"]) == 2
    assert "needs a modifier" in capsys.readouterr().err
    assert not default_path().exists()
