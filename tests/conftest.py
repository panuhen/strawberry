"""Safety nets for the whole test run, and the `linux_only` marker."""

from __future__ import annotations

import sys

import pytest

from strawberry_crab import mpris, osguard, smtc
from tests.portable import point_dirs


def pytest_collection_modifyitems(config, items):
    """`@pytest.mark.linux_only` (registered in pyproject.toml): skipped on any other system,
    for tests of D-Bus, systemd or POSIX process semantics that have no counterpart there yet."""
    if sys.platform.startswith("linux"):
        return
    skip = pytest.mark.skip(reason=f"Linux only (this is {sys.platform})")
    for item in items:
        if item.get_closest_marker("linux_only"):
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def no_developer_override(monkeypatch):
    """Linux and Windows are supported (osguard), so nothing here needs the override; one set in
    the shell that runs the tests must not hide a refusal."""
    monkeypatch.delenv(osguard.OVERRIDE_ENV, raising=False)


@pytest.fixture(autouse=True)
def no_real_session_bus(monkeypatch):
    """No unit test may reach the desktop's D-Bus.

    The MPRIS reflexes act on whatever is playing on this machine, so a test that opened the real
    session bus would skip or pause the music of whoever is running it. Tests that exercise MPRIS
    pass a fake bus instead (tests/test_mpris.py); everything else gets a failure it can report.
    """

    async def refuse(self) -> None:
        raise mpris.MprisError("the session bus is out of bounds in tests")

    monkeypatch.setattr(mpris.SessionBus, "_router_ready", refuse)


@pytest.fixture(autouse=True)
def no_real_media_sessions(monkeypatch):
    """Nor Windows' media sessions, for the same reason: the SMTC reflexes and the Windows media
    doorway would press the buttons of the user's own players. tests/test_smtc.py and
    tests/test_smtc_watch.py hand them a fake session manager instead."""

    async def refuse(timeout_s: float = 4.0):
        raise mpris.MprisError("the media sessions are out of bounds in tests")

    monkeypatch.setattr(smtc, "request_manager", refuse)


@pytest.fixture(autouse=True)
def no_real_notification_listener(monkeypatch):
    """Nor Windows' notification centre: the toast doorway would read the user's own toasts.
    tests/test_toast_watch.py hands it a fake listener instead."""
    from strawberry_crab.doorways import toast_watch

    async def refuse():
        raise toast_watch.ToastError("the notification centre is out of bounds in tests")

    monkeypatch.setattr(toast_watch, "request_listener", refuse)


@pytest.fixture(autouse=True)
def no_real_loopback_capture(monkeypatch):
    """Nor another process's sound: the beat doorway's Windows capture would listen to the user's
    own player. tests/test_beat_loopback.py hands it a fake system, and captures only its own
    (silent) process through wasapi directly."""
    from strawberry_crab.doorways import beat_loopback

    def refuse(pid, rate):
        raise AssertionError("another process's audio is out of bounds in tests")

    monkeypatch.setattr(beat_loopback.System, "capture", staticmethod(refuse))


@pytest.fixture(autouse=True)
def no_real_notification_area(monkeypatch):
    """Nor the taskbar: no test may put an icon in the user's notification area. The Windows
    tray's tests build and read back its menu and icon, which shows nothing, and drive the rest
    with a fake icon (tests/test_wintray.py)."""
    from strawberry_crab import wintray

    def refuse(message, data):
        raise wintray.NotifyIconError("the notification area is out of bounds in tests")

    monkeypatch.setattr(wintray, "shell_notify_icon", refuse)


@pytest.fixture(autouse=True)
def no_real_tray_start(monkeypatch):
    """Nor may a test start a real tray the way `strawberry install` does on Windows; the tests of
    install record the start instead (tests/test_startup.py)."""
    from strawberry_crab import startup

    def refuse(what):
        raise AssertionError(f"a test tried to start a real tray: {what.command_line()}")

    monkeypatch.setattr(startup, "start", refuse)


@pytest.fixture(autouse=True)
def throwaway_xdg_dirs(monkeypatch, tmp_path_factory):
    """No unit test may write the user's config, data or state: each gets its own XDG dirs.

    The first-run privacy note counts as already shown there, so a test widget's hello is not
    followed by an extra bubble; tests/test_firstrun.py removes the marker to see the note.
    """
    from strawberry_crab import paths

    point_dirs(monkeypatch, tmp_path_factory.mktemp("xdg"))
    paths.privacy_notice_marker().parent.mkdir(parents=True)
    paths.privacy_notice_marker().write_text("shown\n")


@pytest.fixture(autouse=True)
def no_real_system_bus(monkeypatch):
    """Nor the system bus: the daemon's wake watcher (wake.py) finds none and says so once.
    tests/test_wake.py passes a fake bus to see a resume."""
    from strawberry_crab import wake

    async def refuse(queue_size: int = 16):
        raise ConnectionError("the system bus is out of bounds in tests")

    monkeypatch.setattr(wake, "open_system_bus", refuse)


@pytest.fixture(autouse=True)
def no_real_power_notifications(monkeypatch):
    """Nor Windows' suspend and resume notification: the daemon's wake watcher there (winwake.py)
    registers none and says so once. tests/test_winwake.py registers the real one where it
    fires its own callback, and passes a fake one everywhere else."""
    from strawberry_crab import winwake

    def refuse(handler):
        raise winwake.PowerError("the power notifications are out of bounds in tests")

    monkeypatch.setattr(winwake, "register", refuse)


@pytest.fixture(autouse=True)
def no_real_microphone(monkeypatch):
    """Nor the microphone: no test may open the user's recording device. The Windows recorder's
    tests hand winmic a fake sounddevice (tests/test_winmic.py); tests/test_voice.py fakes the
    recorder itself."""
    from strawberry_crab import winmic

    def refuse():
        raise winmic.MicrophoneError("the microphone is out of bounds in tests")

    monkeypatch.setattr(winmic, "_sounddevice", refuse)


@pytest.fixture(autouse=True)
def no_real_hotkey(monkeypatch):
    """Nor a key the user presses: the Windows tray's hotkey tests register Ctrl+Alt+Shift+F24,
    which no keyboard has, and anything else is refused (tests/test_wintray.py)."""
    from strawberry_crab import wintray

    real = wintray.register_hotkey

    def only_f24(hwnd, hotkey_id, modifiers, vk):
        if vk != 0x87:
            raise AssertionError(f"a test tried to register a real hotkey (vk {vk:#x})")
        return real(hwnd, hotkey_id, modifiers, vk)

    monkeypatch.setattr(wintray, "register_hotkey", only_f24)
