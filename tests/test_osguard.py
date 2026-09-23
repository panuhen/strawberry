import pytest

from strawberry_crab import osguard


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_linux_and_windows_are_supported(platform):
    assert osguard.unsupported_message(platform) is None


@pytest.mark.parametrize("platform", ["darwin", "cygwin", "freebsd14"])
def test_other_systems_get_a_sentence(platform):
    message = osguard.unsupported_message(platform)
    assert message and "Linux and Windows only" in message and platform in message


def test_require_supported_exits_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(osguard.sys, "platform", "darwin")
    monkeypatch.delenv(osguard.OVERRIDE_ENV, raising=False)       # conftest sets it for the rest
    with pytest.raises(SystemExit) as stop:
        osguard.require_supported()
    assert stop.value.code == 1
    assert "Linux and Windows only" in capsys.readouterr().err


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_a_supported_system_needs_no_override(monkeypatch, platform):
    monkeypatch.setattr(osguard.sys, "platform", platform)
    monkeypatch.delenv(osguard.OVERRIDE_ENV, raising=False)
    osguard.require_supported()


def test_the_developer_override_lets_it_run(monkeypatch):
    monkeypatch.setattr(osguard.sys, "platform", "darwin")
    monkeypatch.setenv(osguard.OVERRIDE_ENV, "1")
    osguard.require_supported()
    monkeypatch.setenv(osguard.OVERRIDE_ENV, "yes")          # only "1" counts
    with pytest.raises(SystemExit):
        osguard.require_supported()
