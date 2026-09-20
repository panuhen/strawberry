"""Inbound events and the reactor that turns them into performances (WIRING.md §2, §3).

Every doorway (dunst script, git hook, voice) POSTs an `Event`. A `Reactor` decides
what Strawberry says about it. Phase 1 ships `CannedReactor`; the Ollama reaction
path replaces it behind the same interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .contract import ContractError, Performance, anim_for

SOURCES = ("notification", "git", "voice", "media", "manual")
URGENCIES = ("low", "normal", "critical")
MAX_BODY = 2000
MAX_LINE = 140


@dataclass(frozen=True, slots=True)
class Event:
    source: str
    app: str = ""
    title: str = ""
    body: str = ""
    urgency: str = "normal"

    @classmethod
    def from_dict(cls, data: Any) -> Event:
        if not isinstance(data, dict):
            raise ContractError("event must be a JSON object")
        source = data.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ContractError(f"source is required, one of {list(SOURCES)}")
        source = source.strip().lower()
        if source not in SOURCES:
            raise ContractError(f"source must be one of {list(SOURCES)}, got {source!r}")

        def text(key: str) -> str:
            value = data.get(key, "")
            if value is None:
                return ""
            if not isinstance(value, str):
                raise ContractError(f"{key} must be a string")
            return value.strip()[:MAX_BODY]

        # dunst hands urgency over as LOW/NORMAL/CRITICAL; normalise rather than reject.
        urgency = text("urgency").lower() or "normal"
        if urgency not in URGENCIES:
            urgency = "normal"
        return cls(source=source, app=text("app"), title=text("title"), body=text("body"), urgency=urgency)


class Reactor(Protocol):
    async def react(self, event: Event) -> Performance: ...


class CannedReactor:
    """Phase 1 stand-in for the brain: a fixed line per source, no model call.

    Exists so the whole path (hook -> /event -> perform -> widget) is testable before
    Ollama is wired in. Keep its output shape identical to what the model path returns.
    """

    async def react(self, event: Event) -> Performance:
        emotion = "alert" if event.urgency == "critical" else "neutral"
        if event.source == "media":
            emotion = "happy"
        line = self._line(event)
        return Performance(state="talking", anim=anim_for(emotion), text=line[:MAX_LINE], emotion=emotion)

    @staticmethod
    def _line(event: Event) -> str:
        if event.source == "git":
            if event.title and event.body:
                return f"Commit in {event.title}: {event.body}"
            return f"Something got committed in {event.title or 'a repo'}."
        if event.source == "notification":
            who = event.app or "Something"
            what = event.title or event.body
            return f"{who}: {what}" if what else f"{who} wants your attention."
        if event.source == "media":
            return f"Now playing: {event.title or event.body}" if (event.title or event.body) else "Music!"
        if event.source == "voice":
            return f"You said: {event.body or event.title}" if (event.body or event.title) else "I heard you."
        return event.title or event.body or "Something happened."
