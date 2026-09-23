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
def allow_unsupported_os(monkeypatch):
    """The CLI and the daemon run on a system osguard does not list yet (the Windows port);
    tests/test_osguard.py removes this to see the refusal itself."""
    monkeypatch.setenv(osguard.OVERRIDE_ENV, "1")


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
