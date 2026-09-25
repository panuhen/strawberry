"""The ledger: what was said and done in the last few minutes (WIRING.md §8b).

Her only memory across turns, and deliberately small: a rolling list of the last few
exchanges, dropped after a few minutes. It is what makes "skip this one too", "no, the
other one" and a "yes" to an offer mean something. Both models get it as a few lines of
context; nothing else accumulates anywhere.

    ledger.record("skip this song", "Skipped. Now Blue Monday by New Order.", did="skipped to the next track")
    ledger.context()  ->  "Recent exchanges (newest last):\\n- 12 s ago you heard: ... "
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Turn:
    at: float               # time.monotonic()
    said: str               # what the user said (the transcript)
    reply: str              # what she said back
    did: str = ""           # what she did, when she did something ("skipped to the next track")

    def to_dict(self, now: float) -> dict[str, Any]:
        return {"ago_s": round(now - self.at, 1), "said": self.said, "reply": self.reply, "did": self.did}


class Ledger:
    def __init__(self, max_turns: int = 6, max_age_s: float = 600.0, clock=time.monotonic) -> None:
        self.max_turns = max_turns
        self.max_age_s = max_age_s
        self.clock = clock
        self.turns: deque[Turn] = deque(maxlen=max_turns)

    def record(self, said: str, reply: str, did: str = "") -> None:
        self.turns.append(Turn(self.clock(), said.strip(), reply.strip(), did.strip()))

    def recent(self) -> list[Turn]:
        cutoff = self.clock() - self.max_age_s
        return [t for t in self.turns if t.at >= cutoff]

    def lines(self, limit: int | None = None) -> list[str]:
        """One prompt line per recent turn, oldest first (the thinker drops from the front when
        its prompt would not fit num_ctx)."""
        turns = self.recent()
        if limit is not None:
            turns = turns[-limit:]
        now = self.clock()
        out = []
        for t in turns:
            what = f' you did "{t.did}" and said' if t.did else " you said"
            out.append(f'- {_ago(now - t.at)} the user said "{t.said}";{what} "{t.reply}"')
        return out

    def context(self, limit: int | None = None) -> str:
        """The recent turns as lines for a prompt; '' when there is nothing fresh."""
        return as_context(self.lines(limit))

    def to_list(self) -> list[dict[str, Any]]:
        now = self.clock()
        return [t.to_dict(now) for t in self.recent()]


def as_context(lines: list[str]) -> str:
    """Ledger lines under their heading, for a prompt; '' for none."""
    return "\n".join(["Recent exchanges (newest last):", *lines]) if lines else ""


def _ago(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} s ago"
    minutes = int(seconds // 60)
    return f"{minutes} min ago"
