"""What the journal says of the user's own sentences (WIRING.md §15, `[daemon] log_sentences`).

A sentence the user says or types passes the server, whisper, the gate, the reflexes, the thinker
and the tool calls it fills in, and each of them logs a line about it. Off (the default), those
lines carry a placeholder with the sentence's length; on, the sentence as it was. Every such line
goes through `sentence()` (and a tool call's arguments through `arguments()`), so the switch is
in one place. Her own lines are logged as they always were, except that the sentence she is
answering is taken out of them (`line()`): the canned fallback for a voice event is "You said: …",
and a model may quote the user too. Notification bodies are never logged, whatever this says (§4).
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


def configure(log_sentences: bool) -> None:
    """Set once by the daemon from its config."""
    global LOG_SENTENCES
    LOG_SENTENCES = log_sentences


def sentence(text: str) -> str:
    """The user's sentence for a log line: quoted when log_sentences is on, else only its length."""
    if LOG_SENTENCES:
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


@contextmanager
def hearing(text: str) -> Iterator[None]:
    """Mark `text` as the sentence being answered while the block runs."""
    token = _hearing.set(text)
    try:
        yield
    finally:
        _hearing.reset(token)


def line(text: str) -> str:
    """Her line for a log line: as it is, with the sentence she is answering replaced by its
    placeholder when log_sentences is off."""
    heard = _hearing.get()
    if LOG_SENTENCES or not heard or not text:
        return text
    return re.sub(re.escape(heard), lambda _: sentence(heard), text, flags=re.IGNORECASE)
