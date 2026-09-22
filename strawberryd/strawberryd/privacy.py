"""What a notification body may tell the model, and what it may not (WIRING.md §4).

Two checks before any body reaches Gemma, and both must say no:

* **patterns**, plain code: one-time codes near a word like "code" or "PIN", a body that is only
  a code, password-reset and magic-login links, "do not share this code". Also run on the title.
* **IS_SENSITIVE**, a yes/no on the gate's embedder (systemone.py): sign-ins, account security,
  bank and card alerts, the things no regex lists.

Fail closed: a gate that is off, not ready, or erroring counts as a yes. A sensitive body is
dropped and she says a neutral line with the app alone ("Slack sent something private.").

And after the model: `leaks()` catches a line that quotes the body anyway (a link, a number from
it, four words in a row), and the daemon says the canned line instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .systemone import Gate

# A one-time code: 4-8 digits (optionally "G-" style prefixed), or 123-456 / 123 456. Not a
# PR number (#4821), a time (14:00), a decimal or amount (1,200.50), a version (3.12.1).
CODE = re.compile(
    r"(?<![\w#.,:/$€£-])(?:(?:[A-Z]{1,4}-)?\d{4,8}|\d{3}[- ]\d{3})(?![\w%€£$-]|[.,:/]\d)"
)
CODE_WORDS = re.compile(
    r"\b(?:codes?|otp|pins?|passcodes?|pass codes?|verif\w*|one[- ]?time|2fa|two[- ]factor|mfa|"
    r"security codes?|log[- ]?in codes?|sign[- ]?in codes?|auth\w* codes?|confirmation codes?|tan)\b",
    re.IGNORECASE,
)
CODE_NEAR = 60  # characters either side of a code in which one of the words must appear
BARE_CODE = re.compile(r"^\W*(?:(?:[A-Z]{1,4}-)?\d{4,8}|\d{3}[- ]\d{3})\W*$")
PHRASES = re.compile(
    r"\b(?:reset\W+(?:\w+\W+){0,2}password|password\W+(?:\w+\W+){0,2}reset|magic[- ]link|"
    r"(?:log|sign)[- ]?in\W+(?:link|url)|one[- ]time\W+(?:link|password)|"
    r"(?:do\W+not|don'?t|never)\W+(?:share|give|tell|forward|send)\b[^.!?]{0,40}\b(?:code|pin|password|otp)|"
    r"(?:do\W+not|don'?t|never)\W+share\W+(?:this|it)\W+with\W+anyone|"
    r"(?:code|pin|otp)\b[^.!?]{0,30}\b(?:expires?|valid\s+for))",
    re.IGNORECASE,
)
SECRET_LINK = re.compile(
    r"https?://\S*(?:reset|magic|token=|otp|verif|/login\?|/signin\?|auth\?|code=)", re.IGNORECASE
)
URL = re.compile(r"(?:https?://|www\.)\S+|\b[\w.-]+\.(?:com|org|net|io|dev|fi|se|co|uk|de)/\S*", re.IGNORECASE)
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
DIGITS = re.compile(r"\d+")

SENSITIVE_P = 0.5   # p(yes) of IS_SENSITIVE at or above which the body is dropped


def pattern(text: str) -> str | None:
    """The first sensitive pattern in `text`, by name, or None."""
    if not text:
        return None
    if BARE_CODE.match(text.strip()):
        return "code-only"
    for match in CODE.finditer(text):
        window = text[max(0, match.start() - CODE_NEAR): match.end() + CODE_NEAR]
        if CODE_WORDS.search(window):
            return "one-time code"
    if SECRET_LINK.search(text):
        return "sign-in or reset link"
    if PHRASES.search(text):
        return "sign-in or reset wording"
    return None


def gate_state(title: str, body: str) -> str:
    """What IS_SENSITIVE reads: the sender or subject, then the body."""
    return f"{title}: {body}" if title else body


@dataclass(frozen=True)
class Verdict:
    sensitive: bool
    reason: str                 # "pattern: one-time code", "gate 0.93", "gate unavailable", "clear 0.04"
    p: float | None = None      # IS_SENSITIVE p(yes), when it was asked
    ms: float = 0.0             # the gate's time

    @property
    def summary(self) -> str:
        return f"{self.reason} ({self.ms:.0f} ms)" if self.ms else self.reason


async def check(gate: Gate, title: str, body: str, ask_gate: bool = True) -> Verdict:
    """Patterns first (free), then the gate. A gate that cannot answer counts as sensitive.

    `ask_gate=False` (no body, or a burst whose body is only titles) runs the patterns alone.
    """
    for text in (title, body):
        hit = pattern(text)
        if hit:
            return Verdict(True, f"pattern: {hit}")
    if not body or not ask_gate:
        return Verdict(False, "patterns clear")
    reading = await gate.sensitive(gate_state(title, body))
    if reading is None:
        return Verdict(True, "gate unavailable")
    p, ms = reading
    if p >= SENSITIVE_P:
        return Verdict(True, f"gate {p:.2f}", p, ms)
    return Verdict(False, f"clear {p:.2f}", p, ms)


def private_line(app: str) -> str:
    """What she says instead of a sensitive message: the app, nothing of the content."""
    app = app.strip()
    return f"{app} sent something private." if app else "Something private just came in."


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def leaks(line: str, body: str, strict: bool = False) -> str | None:
    """Why `line` gives away the message `body`, or None.

    A link or an email address, a number of two or more digits that is in the body, or four words
    in a row from it. `strict` (the glance gist) refuses any digit at all.
    """
    if not line:
        return None
    if URL.search(line) or EMAIL.search(line):
        return "a link or an address"
    numbers = set(DIGITS.findall(line))
    if strict and numbers:
        return "a number"
    if any(len(n) >= 2 and n in set(DIGITS.findall(body)) for n in numbers):
        return "a number from the message"
    words = _words(body)
    shingles = {tuple(words[i:i + 4]) for i in range(len(words) - 3)}
    said = _words(line)
    if any(tuple(said[i:i + 4]) in shingles for i in range(len(said) - 3)):
        return "four words from the message"
    return None
