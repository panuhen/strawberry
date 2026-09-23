"""The desktop's media controls, whichever system this is (WIRING.md §8b, WINDOWS.md).

`controls()` is what the daemon asks for: `smtc.Smtc` on Windows (the System Media Transport
Controls), `mpris.Mpris` everywhere else (MPRIS on the session bus; a system without one gets
the sentence saying so). Both are the same reflexes behind the same three methods, `reflexes()`,
`situation()` and `close()`, so the daemon and `actions.Actor` import neither module.
"""

from __future__ import annotations

import sys
from typing import Any


def controls(platform: str | None = None) -> Any:
    """The bare music reflexes for this system: `Smtc` on Windows, `Mpris` elsewhere."""
    if (platform or sys.platform) == "win32":
        from .smtc import Smtc

        return Smtc()
    from .mpris import Mpris

    return Mpris()
