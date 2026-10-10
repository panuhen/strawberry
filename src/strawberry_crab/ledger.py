"""The ledger: one short timeline of what was said and done lately (WIRING.md §8b, §23).

Her only memory across turns, and deliberately small. Two kinds of entry:

    turn     a sentence of the user's and her answer (and what she did), written as each one is
             handled (Daemon); what makes "skip this one too", "no, the other one" and a "yes" to an
             offer mean something
    notice   something she reacted to on her own (System 1): a commit, a notification, a track, a
             reflex posted as an action. When, the source, the metadata the privacy mode allows (the
             repo and the commit's subject, the app and the sender, the player and the track, what the
             reflex did) and the line she said. Never a notification's body.

The thinker (System 2) gets it as its "recent" lines, oldest first, each with its age ("40 s ago",
"25 min ago"), within a token budget, so "what was that commit about?" half a minute after she
announced it has an answer. Each notice carries a trust label: a commit from the user's repos and a
reflex are the user's own; a notification's sender and a track's name are strangers' text (WIRING
§20), so a run that sees one asks before anything above `playback`, as with a foreign situation line
(`timeline` says so). A foreign notice stops counting after `foreign_age_s` and is then left out of
the thinker's lines too. Everything expires by count (`max_turns`, `max_notices`) and by age
(`max_age_s`), and lives in memory only: a restart forgets it.

    ledger.record("skip this song", "Skipped. Now Blue Monday by New Order.", did="skipped to the next track")
    ledger.notice("git", 'a commit in strawberry: "Add websocket"', "Ooh, websockets.")
    ledger.timeline()  ->  (['- 12 s ago (git) a commit in strawberry: "Add websocket"; you said "Ooh, websockets."',
                             '- 3 s ago the user said "skip this song"; you did "…" and said "…"'], False)
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any

# The thinker's lines are kept within this many tokens (its own estimate: 3 characters a token), the oldest
# going first; the thinker trims further when its whole prompt would not fit num_ctx (Thinker.fit_prompt).
LINES_TOKENS = 900
CHARS_PER_TOKEN = 3.0
SOURCES = ("git", "notification", "music", "reflex")
MAX_FIELD = 200


@dataclass(frozen=True)
class Turn:
    at: float               # time.monotonic()
    said: str               # what the user said (the transcript)
    reply: str              # what she said back
    did: str = ""           # what she did, when she did something ("skipped to the next track")

    kind = "turn"

    def to_dict(self, now: float, foreign_age_s: float = 0.0) -> dict[str, Any]:
        return {"kind": "turn", "ago_s": round(now - self.at, 1), "said": self.said, "reply": self.reply,
                "did": self.did}

    def line(self, now: float) -> str:
        what = f' you did "{self.did}" and said' if self.did else " you said"
        return f'- {_ago(now - self.at)} the user said "{self.said}";{what} "{self.reply}"'


@dataclass(frozen=True)
class Notice:
    at: float
    source: str             # git | notification | music | reflex
    about: str              # what it was, as far as the privacy mode allows ('a commit in strawberry: "Fix x"')
    line: str               # what she said about it
    foreign: bool           # strangers' text in it (a sender, a track's name): the run that sees it asks first

    kind = "notice"

    def to_dict(self, now: float, foreign_age_s: float = 0.0) -> dict[str, Any]:
        return {"kind": "notice", "ago_s": round(now - self.at, 1), "source": self.source, "about": self.about,
                "line": self.line, "foreign": self.foreign,
                "tainting": self.foreign and now - self.at <= foreign_age_s}

    def text(self, now: float) -> str:
        said = f'; you said "{self.line}"' if self.line else ""
        return f"- {_ago(now - self.at)} ({self.source}) {self.about}{said}"


class Ledger:
    def __init__(self, max_turns: int = 8, max_age_s: float = 3600.0, clock=time.monotonic,
                 foreign_age_s: float = 600.0, max_notices: int = 8) -> None:
        self.max_turns = max_turns
        self.max_age_s = max_age_s
        self.foreign_age_s = foreign_age_s
        self.max_notices = max_notices
        self.clock = clock
        self.turns: deque[Turn] = deque(maxlen=max_turns)
        self.notices: deque[Notice] = deque(maxlen=max(1, max_notices))

    def record(self, said: str, reply: str, did: str = "") -> None:
        self.turns.append(Turn(self.clock(), said.strip(), reply.strip(), did.strip()))

    def notice(self, source: str, about: str, line: str, foreign: bool = True) -> None:
        """Something she reacted to on her own. `about` is what the privacy mode lets through (never a
        message's body: the caller passes the app and the sender, a repo and a subject, a track); `foreign`
        whether any of it was written by others (True unless the caller knows it is the user's own)."""
        if self.max_notices < 1 or source not in SOURCES:
            return
        about = " ".join(about.split())[:MAX_FIELD]
        line = " ".join(line.split())[:MAX_FIELD * 2]
        if about or line:
            self.notices.append(Notice(self.clock(), source, about, line, bool(foreign)))

    def recent(self) -> list[Turn]:
        cutoff = self.clock() - self.max_age_s
        return [t for t in self.turns if t.at >= cutoff]

    def recent_notices(self) -> list[Notice]:
        cutoff = self.clock() - self.max_age_s
        return [n for n in self.notices if n.at >= cutoff]

    def entries(self, trusted_only: bool = False) -> list[Turn | Notice]:
        """Turns and notices within the window, oldest first. A foreign notice older than `foreign_age_s` is
        left out; with `trusted_only`, every foreign one is (a sentence about the profile, profile.py)."""
        now = self.clock()
        notices = [n for n in self.recent_notices()
                   if not n.foreign or (not trusted_only and now - n.at <= self.foreign_age_s)]
        return sorted([*self.recent(), *notices], key=lambda e: e.at)

    def timeline(self, limit: int | None = None, trusted_only: bool = False,
                 budget: int = LINES_TOKENS) -> tuple[list[str], bool]:
        """The thinker's lines, oldest first, within `budget` tokens (the oldest go first), and whether any of
        them carries strangers' text (a notice younger than `foreign_age_s`): the run then starts foreign
        (Thinker.run `foreign_context`)."""
        now = self.clock()
        entries = self.entries(trusted_only)
        if limit is not None:
            entries = entries[-limit:] if limit > 0 else []
        lines = [e.line(now) if isinstance(e, Turn) else e.text(now) for e in entries]
        while lines and _tokens("\n".join(lines)) > budget:
            lines.pop(0)
            entries.pop(0)
        return lines, any(isinstance(e, Notice) and e.foreign for e in entries)

    def lines(self, limit: int | None = None) -> list[str]:
        """The thinker's lines, oldest first (`timeline` without the flag)."""
        return self.timeline(limit)[0]

    def context(self, limit: int | None = None) -> str:
        """The recent turns (the user's own exchanges only) as lines for the reaction model's prompt when it
        answers a sentence (the thinker off); '' when there is nothing fresh."""
        now = self.clock()
        turns = self.recent()
        if limit is not None:
            turns = turns[-limit:]
        return as_context([t.line(now) for t in turns])

    def to_list(self, notices: bool = False) -> list[dict[str, Any]]:
        """The turns in the window, oldest first; with `notices` (/health, the Brain UI) the notices among them:
        a notice's source, what it was, her line, and whether it is strangers' text still counting."""
        now = self.clock()
        entries: list[Turn | Notice] = [*self.recent(), *(self.recent_notices() if notices else [])]
        return [e.to_dict(now, self.foreign_age_s) for e in sorted(entries, key=lambda e: e.at)]


def as_context(lines: list[str]) -> str:
    """Ledger lines under their heading, for a prompt; '' for none."""
    return "\n".join(["Recent exchanges (newest last):", *lines]) if lines else ""


def _tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1 if text else 0


def _ago(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} s ago"
    minutes = int(seconds // 60)
    if minutes < 120:
        return f"{minutes} min ago"
    return f"{minutes // 60} h ago"
