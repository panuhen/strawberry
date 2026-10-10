"""A short spoken line when the user pokes her (WIRING.md §13, Touch; PROTOCOL.md §6, `poked`).

The widget reacts to a poke on its own. Only with its "Talk when poked" setting on (off by
default) does it send `{"type": "poked", "zone": …, "level": 1|2}` now and then, and the daemon
may answer with one of these lines, voiced like any other. The lines are persona.md's `poke.<zone>`
and `poke.annoyed` (persona.py; LINES are the shipped ones): no model is asked, nothing goes into the ledger, and nothing the user said is involved. She says one at
most every COOLDOWN_S, and never over a line, a run or a pipeline state (`Pokes.allowed`).
"""

from __future__ import annotations

import time
from collections.abc import Callable

ZONES = ("shell", "belly", "eye", "claw", "near")
LEVELS = (1, 2)
COOLDOWN_S = 20.0

# (zone, level 1) -> lines; level 2 (pokes in a row: mildly annoyed) has its own, whatever the zone. The
# shipped persona.md's poke lines; a Pokes given `lines` (the daemon's persona) says those instead.
LINES: dict[str, tuple[str, ...]] = {
    "shell": ("Oh, hello.", "Mind the shell, it's freshly polished.", "That's rather nice, actually."),
    "belly": ("Hey, that tickles!", "Not the belly!", "Ha! Stop that."),
    "eye": ("Ow, my eye!", "Careful, I need those.", "Eyes are not buttons."),
    "claw": ("Snip snap.", "Shake on it?", "Watch the pincers."),
    "near": ("Hm?", "Yes?", "Did you want something?"),
    "annoyed": ("Alright, alright!", "I'm working here, you know.", "Okay, that's enough poking."),
}
EMOTIONS = {"shell": "happy", "belly": "happy", "eye": "alert", "claw": "happy", "near": "neutral",
            "annoyed": "angry"}


def parse(data: dict) -> tuple[str, int] | None:
    """The message's zone and level, or None when it is not a poke the daemon knows."""
    zone, level = data.get("zone"), data.get("level")
    if zone not in ZONES or isinstance(level, bool) or level not in LEVELS:
        return None
    return zone, level


class Pokes:
    """Which line comes next per kind (each in turn, so none repeats twice running) and when she
    last said one."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, cooldown_s: float = COOLDOWN_S,
                 lines: Callable[[str], tuple[str, ...]] | None = None):
        self.clock = clock
        self.lines = lines      # persona key ("poke.belly") -> its lines; None: LINES
        self.cooldown_s = cooldown_s
        self.said_at: float | None = None
        self.turns: dict[str, int] = {}
        self.said = 0
        self.declined = 0

    def allowed(self, busy: bool) -> bool:
        """Not while she is busy, and not again within the cooldown."""
        if busy or (self.said_at is not None and self.clock() - self.said_at < self.cooldown_s):
            self.declined += 1
            return False
        return True

    def line(self, zone: str, level: int) -> tuple[str, str]:
        """The next line for this poke and its emotion; marks it said."""
        kind = "annoyed" if level >= 2 else zone
        lines = (self.lines(f"poke.{kind}") if self.lines is not None else ()) or LINES[kind]
        turn = self.turns.get(kind, 0)
        self.turns[kind] = turn + 1
        self.said_at = self.clock()
        self.said += 1
        return lines[turn % len(lines)], EMOTIONS[kind]
