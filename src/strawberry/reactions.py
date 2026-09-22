"""Which body reaction goes with an event (WIRING.md §2, §13).

The model only picks the emotion. What she *does* with it is decided here, from the
event's source, app, category and urgency, so a message looks different from a commit
and a failure looks different from a song. Recipes are implemented in widget/reactions.gd.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace

from .contract import Performance, anim_for
from .events import Event

# Apps and freedesktop categories that mean "a person wrote to you".
MESSAGE_APPS = (
    "whatsapp", "telegram", "signal", "slack", "discord", "messenger", "teams", "element", "matrix",
    "mattermost", "zulip", "thunderbird", "evolution", "geary", "mail", "gmail", "kmail", "imessage",
)
MESSAGE_CATEGORIES = ("im.", "email.")
BURST_RE = re.compile(r"^\d+ notifications")


@dataclass(frozen=True, slots=True)
class Choice:
    anim: str | None = None
    reaction: str | None = None
    hop: bool = False


def is_message(event: Event) -> bool:
    if event.category.lower().startswith(MESSAGE_CATEGORIES):
        return True
    app = event.app.lower()
    return any(name in app for name in MESSAGE_APPS)


def is_burst(event: Event) -> bool:
    return event.source == "notification" and bool(BURST_RE.match(event.title))


def choose(event: Event, emotion: str) -> Choice:
    if event.source == "media":
        return Choice(reaction="nod")  # she is dancing; a hop would break it
    if event.source in ("voice", "action"):
        return Choice(reaction="nod")
    if event.source == "git":
        if event.app == "pre-push":
            return Choice(reaction="nod")
        return Choice(anim=anim_for(emotion))
    if event.source == "notification":
        if is_burst(event):
            return Choice(reaction="double_hop")
        if event.urgency == "critical" or emotion == "angry":
            return Choice(anim="alert_snap", reaction="shiver")
        if is_message(event):
            return Choice(reaction="wave", hop=True)
        if emotion == "alert":
            return Choice(reaction="peek")
        if emotion == "happy":
            return Choice(anim="notify_perk")
        return Choice(reaction="nod")
    return Choice(anim=anim_for(emotion))


def decorate(event: Event, performance: Performance) -> Performance:
    """Attach the body reaction and the app icon to what the reactor said."""
    choice = choose(event, performance.emotion)
    icon = event.icon if event.icon and os.path.isfile(event.icon) else None
    return replace(performance, anim=choice.anim, reaction=choice.reaction, hop=choice.hop, icon=icon)
