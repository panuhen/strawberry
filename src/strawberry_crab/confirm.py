"""A spoken yes before a tool that cannot be taken back (WIRING.md §8b, ADAPTERS.md).

Each server's `confirm` list (`[tools.servers.<name>] confirm = [...]`, else its adapter's own
default, Spotify's two removals) names the tools she asks about first. When the thinker calls one
of them, the call is not made: the thinker stops there and she says what she is about to do in one
line written by code ("Remove 'Teardrop' from Strawberry? Say yes."). The call is kept exactly as
it would have been sent, a `Held`, and the daemon waits for the user's next sentence:

    yes, sure, do it, go ahead…   that one call is made, with those arguments; no model is asked again
    no, cancel, leave it…         nothing is done; she says she left it
    anything else                 nothing is done; she says she left it and the sentence is handled as usual
    no answer in [actions] confirm_s (10 s; not while she is listening)
                                  nothing is done; she says she left it

A bare yes (or no) within a minute after she gave up waiting, or after a no, gets a fixed line that
she left it and to ask again, never the thinker, which would read her question in the ledger.

Only the user's own sentences (said or typed, `source: voice`) reach `Daemon.handle_voice`, which is
the only place a held call is answered: a notification, a media change, a tool result or a web page
never confirms one. The yes and the no are read off the whole sentence by word lists (`answer`), so
"yes, and play some jazz" is not a yes: it cancels and goes on to the jazz.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .tools import Toolbox, ToolResult

log = logging.getLogger("strawberryd.confirm")


@dataclass(frozen=True)
class Held:
    """A call the user is asked about, exactly as it will be made after a yes."""

    server: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    question: str = ""

    @property
    def key(self) -> str:
        return f"{self.server}.{self.name}"


# What she says when she did not do it.
LEFT_NO = "Okay, I've left it."
LEFT_SILENT = "No answer, so I've left it."
LEFT_OTHER = "I've left that, then."
# A yes that comes after she gave up waiting (or after a no): said by code, because the thinker, reading
# her question in the ledger, answered a late "yes" with "Removed Teardrop from Gym." and no removal.
LATE_YES = "I've already left that one. Ask me again if you still want it."
LATE_S = 60.0   # how long after a dropped question a bare yes or no is still about it
# What the ledger keeps of her question: the code asks it, and in her words in the ledger the thinker
# copied it, asking for a yes with nothing held, so the next "yes" did nothing (or the wrong thing).
LEDGER_HELD = "(the yes-or-no question the code asks before that tool runs)"

# The words a whole answer is made of. Multi-word phrases are matched first; a sentence is a yes only
# if every word belongs to YES or FILLER and one is a YES, a no if every word belongs to YES, NO or
# FILLER and one is a NO ("okay, never mind"). Anything else is a sentence of its own.
YES = (
    "yes", "yeah", "yea", "yep", "yup", "ya", "aye", "sure", "ok", "okay", "alright", "all right", "fine",
    "absolutely", "definitely", "certainly", "of course", "please do", "do it", "do that", "go ahead", "go on",
    "go for it", "confirm", "confirmed", "correct", "that's right", "that's correct", "affirmative", "why not",
    "i'm sure", "i am sure", "remove it", "delete it", "take it off", "get rid of it", "yes please",
)
NO = (
    "no", "nope", "nah", "don't", "do not", "dont", "cancel", "cancel that", "cancel it", "never mind", "nevermind",
    "stop", "leave it", "leave it alone", "not now", "wait", "hold on", "forget it", "keep it", "negative", "no way",
    "don't do it", "don't do that", "don't remove it", "don't delete it", "i changed my mind", "changed my mind",
)
FILLER = (
    "please", "thanks", "thank you", "strawberry", "then", "now", "just", "oh", "um", "uh", "er", "erm", "well",
    "and", "actually", "hey",
)

_PHRASES = sorted({(p, "yes") for p in YES} | {(p, "no") for p in NO} | {(p, "filler") for p in FILLER},
                  key=lambda item: -len(item[0].split()))


def _words(text: str) -> list[str]:
    return re.sub(r"[^\w']+", " ", (text or "").lower().replace("’", "'")).split()


def answer(text: str) -> str | None:
    """"yes", "no", or None for a sentence that is neither (a new request, a question, small talk)."""
    words = _words(text)
    kinds: list[str] = []
    at = 0
    while at < len(words):
        for phrase, kind in _PHRASES:
            size = len(phrase.split())
            if words[at:at + size] == phrase.split():
                kinds.append(kind)
                at += size
                break
        else:
            return None
    if "no" in kinds:
        return "no"
    if "yes" in kinds:
        return "yes"
    return None


def generic_question(name: str) -> str:
    return f"Shall I go ahead with {name.replace('_', ' ')}? Say yes."


async def hold(toolbox: Toolbox, adapter: Any, server: str, name: str, arguments: dict[str, Any],
               run: Any = None) -> Held:
    """The call as it will be made after a yes, and the line that asks about it. The server's adapter
    writes the line and may pin what would change by then ("current" becomes the playing track's
    URI, so a yes after the song has changed removes the one she named); without one, or if it
    fails, the call is kept as it came and the line names the tool.

    `run` is the run that asks (runs.py). Brain step 6, stage 2 emits its `approval.request` here
    (PROTOCOL §13: the approval id, the risk, the question as written for display, the expiry) and
    the run waits in `awaiting_approval`; today the run ends with the question and the next sentence
    answers it."""
    question, kept = generic_question(name), dict(arguments)
    ask = getattr(adapter, "ask", None) if adapter is not None else None
    if ask is not None:
        try:
            said, pinned = await ask(toolbox, server, name, dict(arguments))
            if isinstance(said, str) and said and isinstance(pinned, dict):
                question, kept = said, pinned
        except Exception as exc:   # an adapter must never cost the sentence its answer
            log.warning("confirm: %s adapter could not word the question (%s)", server, type(exc).__name__)
    return Held(server, name, kept, question)


async def run(toolbox: Toolbox, adapter: Any, held: Held) -> Any:
    """After a yes: the held call, exactly, and the Outcome that says what came of it."""
    from .actions import Outcome   # local: actions imports the config and the tools, not this module

    result = await toolbox.call(held.server, held.name, held.arguments)
    done = getattr(adapter, "done", None) if adapter is not None else None
    if done is not None:
        try:
            outcome = done(held.name, held.arguments, result)
            if isinstance(outcome, Outcome):
                return outcome
        except Exception as exc:
            log.warning("confirm: %s adapter could not word the result (%s)", held.server, type(exc).__name__)
    return generic_outcome(held, result)


def generic_outcome(held: Held, result: ToolResult) -> Any:
    from .actions import Outcome

    verb = held.name.replace("_", " ")
    if result.ok:
        return Outcome(f"did {verb} after a yes", "Done.", True, (result,))
    first = (result.text or "no answer").strip().splitlines()[0][:120] if result.text else "no answer"
    return Outcome(f"tried {verb} after a yes", f"I tried, but it didn't work: {first}", False, (result,))
