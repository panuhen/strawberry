"""Safety nets for the whole test run."""

from __future__ import annotations

import pytest

from strawberryd import mpris


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
