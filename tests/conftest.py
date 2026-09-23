"""Safety nets for the whole test run."""

from __future__ import annotations

import pytest

from strawberry_crab import mpris


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
def throwaway_xdg_dirs(monkeypatch, tmp_path_factory):
    """No unit test may write the user's config, data or state: each gets its own XDG dirs.

    The first-run privacy note counts as already shown there, so a test widget's hello is not
    followed by an extra bubble; tests/test_firstrun.py removes the marker to see the note.
    """
    from strawberry_crab import paths

    base = tmp_path_factory.mktemp("xdg")
    for name in ("CONFIG", "DATA", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(base / name.lower()))
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
