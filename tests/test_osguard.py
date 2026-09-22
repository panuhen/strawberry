import pytest

from strawberry_crab import osguard


def test_linux_is_supported():
    assert osguard.unsupported_message("linux") is None


@pytest.mark.parametrize("platform", ["win32", "darwin", "cygwin"])
def test_other_systems_get_a_sentence(platform):
    message = osguard.unsupported_message(platform)
    assert message and "Linux only" in message and platform in message


def test_require_supported_exits_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(osguard.sys, "platform", "win32")
    with pytest.raises(SystemExit) as stop:
        osguard.require_supported()
    assert stop.value.code == 1
    assert "Linux only" in capsys.readouterr().err
