"""What the journal says of the user's own sentences (WIRING.md §15, `[daemon] log_sentences`).

A sentence the user says or types passes the server, whisper, the gate, the reflexes, the thinker
and the tool calls it fills in, and each of them logs a line about it. Off (the default), those
lines carry a placeholder with the sentence's length; on, the sentence as it was. Every such line
goes through `sentence()` (and a tool call's arguments through `arguments()`), so the switch is
in one place. Her own lines are logged as they always were, except that the sentence she is
answering is taken out of them (`line()`): the canned fallback for a voice event is "You said: …",
and a model may quote the user too. A line written from web results is logged as its length only
while log_sentences is off (`from_web`). Notification bodies are never logged, whatever this says (§4).
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

LOG_SENTENCES = False

# The sentence being answered, for line(): set around one sentence's handling (Daemon.handle_voice),
# so it follows that handling's awaits and the tasks it starts, and no other.
_hearing: ContextVar[str] = ContextVar("hearing", default="")
# Set once her answer to that sentence carries text from web results (Thinker.run): her line is
# then logged as its length only, as the results are (adapters/web.py).
_from_web: ContextVar[bool] = ContextVar("from_web", default=False)
# Set once her answer says back a change to the user's profile (Thinker.run, profile.py): her line is then
# logged as its length only, as the user's sentence is.
_about_profile: ContextVar[bool] = ContextVar("about_profile", default=False)
# Set once her answer comes from a server that is both private and foreign (her inbox, inbox.py: the user's
# messages, written by others): her line is then logged as its length only, whatever log_sentences says.
_from_private: ContextVar[bool] = ContextVar("from_private", default=False)


# Sentences that stay out of the journal whatever log_sentences says, and whose answer does too: one asking her
# to remember something about the user (profile.ASKS, registered by profile.py). Their words end up in the
# profile, which is never logged.
_private: list[re.Pattern] = []


def private_sentences(pattern: re.Pattern) -> None:
    if pattern not in _private:
        _private.append(pattern)


def private(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _private)


def configure(log_sentences: bool) -> None:
    """Set once by the daemon from its config."""
    global LOG_SENTENCES
    LOG_SENTENCES = log_sentences


def sentence(text: str) -> str:
    """The user's sentence for a log line: quoted when log_sentences is on, else only its length."""
    if LOG_SENTENCES and not private(text):
        return repr(text)
    return f"<sentence, {len(text)} chars>"


def arguments(values: dict[str, Any]) -> str:
    """A tool call's arguments for a log line. The model fills them from the sentence ("daft punk"
    for a search), so when log_sentences is off a text value is only its length; numbers and
    booleans (a volume, a flag) stay."""
    if LOG_SENTENCES:
        return json.dumps(values, ensure_ascii=False)[:120]
    shown = {key: value if isinstance(value, (bool, int, float)) or value is None
             else f"<{len(value)} chars>" if isinstance(value, str) else f"<{type(value).__name__}>"
             for key, value in values.items()}
    return json.dumps(shown, ensure_ascii=False)[:120]


def names(values: dict[str, Any]) -> str:
    """A tool call's arguments as their names only: for a private, foreign or unknown server, whose
    arguments are never logged, whatever log_sentences says (tools.Server.call)."""
    return ", ".join(str(key)[:32] for key in values) if values else ""


@contextmanager
def hearing(text: str) -> Iterator[None]:
    """Mark `text` as the sentence being answered while the block runs."""
    token = _hearing.set(text)
    web = _from_web.set(False)
    about = _about_profile.set(private(text))   # a sentence for the profile: her answer is withheld too
    inbox = _from_private.set(False)
    try:
        yield
    finally:
        _from_private.reset(inbox)
        _about_profile.reset(about)
        _from_web.reset(web)
        _hearing.reset(token)


def from_web() -> None:
    """Her line for the sentence being answered carries text from a foreign server's results (a web
    search, a page; trust.py): see line()."""
    _from_web.set(True)


from_foreign = from_web


def about_profile() -> None:
    """Her line for the sentence being answered says back a change to the user's profile (profile.py): see
    line()."""
    _about_profile.set(True)


def from_private() -> None:
    """Her line for the sentence being answered was written from a private and foreign server's results (the
    user's messages): see line()."""
    _from_private.set(True)


def line(text: str) -> str:
    """Her line for a log line: as it is, with the sentence she is answering replaced by its
    placeholder when log_sentences is off."""
    heard = _hearing.get()
    if not LOG_SENTENCES and text and _from_web.get():
        return f"<her line from web results, {len(text)} chars>"
    if text and _from_private.get():
        return f"<her line from the user's messages, {len(text)} chars>"   # whatever log_sentences says
    if text and _about_profile.get():
        return f"<her line about the profile, {len(text)} chars>"   # whatever log_sentences says
    if LOG_SENTENCES or not heard or not text:
        return text
    return re.sub(re.escape(heard), lambda _: sentence(heard), text, flags=re.IGNORECASE)
