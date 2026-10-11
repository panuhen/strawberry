"""One-off reminders (WIRING.md §31): "remind me in 20 minutes to stretch", said back when it is time.

Three parts:

    the times      `parse_when`: "in 20 minutes", "in an hour", "in 1h30", "at 15:00", "at 3 pm", "at quarter past
                   three", "half three", "tomorrow at 9", "on Friday at 10", in the local timezone. Anything it cannot
                   read for certain (a bare "at 6", "next Friday", a time already gone, more than a year ahead) is a
                   reason to ask, never a guess.
    the server     a builtin server `reminders` (`Toolbox.add_builtin`, like her profile): set_reminder and
                   cancel_reminder (tier `change`: from the user's own sentence they go ahead; after strangers' text
                   they ask on her card, in code's words with the time only), list_reminders (`read`). Whole
                   sentences are said reflexes, without a model; anything else is the thinker's, which is offered the
                   server only on a sentence about reminders (`offer = "asked"`). A reminder's words are always the
                   user's own: the reflex takes them from the sentence, and the thinker's call is refused unless they
                   are a span of the user's sentence in that run (profile.from_sentence), so a notification, a song or
                   a web page can never plant one.
    the scheduler  `Reminders`: a task that looks every 30 s (wall-clock time, so a suspend or a clock jump loses
                   nothing) and sooner when one is due. A due reminder is claimed in one statement (pending -> fired),
                   so it is said at most once, then said: her line in her bubble, through the widget, never the OS's
                   notifications; no voice while she is quiet. During a run, an open question or the user talking it
                   waits until that ends, at most 60 s. One still pending from before a start (or more than 5 minutes
                   late, a suspend) is told once, grouped, as "While I was off: …" and marked missed.

They live in her history's file (history.py, the `timers` table, 0600): with `[history] enabled = false` there is
nowhere to keep them, and she says reminders are unavailable. The words are the user's own; no log line carries
them (counts and ids only), and a sentence about reminders and her answer are logged as their lengths
(logtext.private_sentences, as the profile's are).
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
import time
import weakref
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from . import logtext
from . import persona as personas
from .actions import Outcome
from .adapters.base import Adapter
from .profile import from_sentence, sentence_of_this_run

log = logging.getLogger("strawberryd.reminders")

SERVER = "reminders"
TOPIC = "other"
MAX_TEXT = 120                 # a reminder's words at most
MAX_AHEAD_S = 366 * 86400.0    # further ahead than this is not a reminder
MAX_PENDING = 100
CHECK_S = 30.0                 # the scheduler looks at least this often (wall-clock time)
HOLD_S = 60.0                  # a due reminder waits for a run or a question at most this long
LATE_S = 300.0                 # seen first this late (a suspend): told as missed
AWAIT_S = 120.0                # her "when?" waits this long for a time as the answer
MAX_TOLD = 5                   # missed reminders named in one line; the rest are counted

UNAVAILABLE = "I can't keep reminders while my history is switched off; the Config file tab in the Brain UI can turn it on."
BROKEN = "I can't keep reminders right now: my history file could not be opened."

# The shipped persona's lines (persona.md `reminder_*`); a user's line that leaves out a placeholder falls back to these.
SET_LINE = "Okay, {when}: {text}."
DUE_LINE = "Reminder: {text}."
MISSED_LINE = "While I was off: {list}."
ASK_LINE = "When should I remind you?"


# ----------------------------------------------------------------------------- the times

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
         "seventeen eighteen nineteen").split()
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60}
NUMBER_WORDS = {w: i for i, w in enumerate(_ONES)} | _TENS
HOUR_WORDS = "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
UNITS = {"second": 1, "seconds": 1, "sec": 1, "secs": 1, "s": 1,
         "minute": 60, "minutes": 60, "min": 60, "mins": 60, "m": 60,
         "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600, "h": 3600,
         "day": 86400, "days": 86400, "week": 604800, "weeks": 604800}
# A bare hour (1-12, no am or pm) on a day when both readings are ahead: the waking-day reading, or none (ask).
DAYTIME = {7: "am", 8: "am", 9: "am", 10: "am", 11: "am", 12: "pm", 1: "pm", 2: "pm", 3: "pm", 4: "pm", 5: "pm"}

_PART = r"(?:morning|afternoon|evening|night)"
DAY = (rf"(?:today|tonight|tomorrow(?:\s+{_PART})?|this\s+(?:morning|afternoon|evening)|"
       rf"(?:on\s+)?(?:(?:this|next|coming|this\s+coming)\s+)?(?:{'|'.join(WEEKDAYS)})(?:\s+{_PART})?)")
_MINUTES = r"(?:quarter|half|\d{1,2}|five|ten|twenty|twenty\s+five)"
CLOCK = (rf"(?:(?P<noon>noon|midday|midnight)"
         rf"|(?P<q>{_MINUTES})\s+(?:minutes?\s+)?(?P<dir>past|after|to)\s+(?P<qh>\d{{1,2}}|{HOUR_WORDS})"
         rf"|half\s+(?P<halfh>\d{{1,2}}|{HOUR_WORDS})"
         rf"|(?P<h>\d{{1,2}}|{HOUR_WORDS})(?:[:.](?P<m>\d{{2}}))?)"
         rf"(?:\s*(?P<ap>am|pm))?"
         rf"(?:\s+(?P<part>in\s+the\s+(?:morning|afternoon|evening)|at\s+night|tonight|this\s+(?:morning|afternoon|evening)))?")
AT = re.compile(rf"^(?:(?P<d1>{DAY})\s+)?(?:(?:at|for|by|around)\s+)?(?P<clock>{CLOCK})(?:\s+(?P<d2>{DAY}))?$")
DAY_ONLY = re.compile(rf"^(?P<d>{DAY})$")
ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ t](\d{1,2}):(\d{2})(?::(\d{2}))?$")
RELATIVE = re.compile(r"^(?:in|within|after)\s+(?P<dur>.+)$|^(?P<dur2>.+?)\s+from\s+now$")
# Times nobody can set a reminder for: asked about, as having no time.
VAGUE = re.compile(r"^(?:later(?:\s+on|\s+today)?|soon|in\s+a\s+(?:bit|while|moment|sec|second|jiffy)|at\s+some\s+point|"
                   r"some\s*time|next\s+week|this\s+week(?:end)?|at\s+the\s+weekend|in\s+a\s+few(?:\s+\w+)?|"
                   r"in\s+some\s+\w+|tonight|tomorrow)$")


@dataclass(frozen=True)
class When:
    """A time read off words: `due` (an epoch) or why not (`why`: no_time, unclear, ambiguous, meridiem, past,
    too_far, too_soon). `day` is the day words it had ("tomorrow"), kept for the user's answer to her question;
    `hour` the bare hour of a `meridiem` question."""

    due: float | None
    why: str = ""
    day: str = ""
    hour: int = 0
    clock: str = ""

    @property
    def ok(self) -> bool:
        return self.due is not None and not self.why


def normal(text: str) -> str:
    """Words as the time patterns read them: lower case, "p.m." as "pm", "o'clock" gone, 1h30 as "1 h 30", the dots
    and colons of a time kept and every other mark a space."""
    text = (text or "").lower().replace("’", "'").replace("–", "-")
    text = re.sub(r"\b([ap])\s*\.\s*m\b\.?", r"\1m", text)
    text = re.sub(r"\bo'?\s?clock\b", " ", text)
    text = re.sub(r"(?<![\d])[.:]|[.:](?!\d)", " ", text)
    text = re.sub(r"[^\w:.'\s]", " ", text)
    text = re.sub(r"(\d)(am|pm)\b", r"\1 \2", text)
    text = re.sub(r"(\d)\s*(h|hrs?|hours?|m|mins?|minutes?|s|secs?|seconds?)(?=\d|\s|$)", r"\1 \2 ", text)
    return " ".join(text.split())


def _number(tokens: list[str], i: int) -> tuple[float | None, int]:
    """A number at tokens[i]: digits, "a"/"an"/"one", "a couple of", words up to "fifty nine". (value, next)."""
    if i >= len(tokens):
        return None, i
    word = tokens[i]
    if re.fullmatch(r"\d{1,6}(?:\.\d{1,3})?", word):
        return float(word), i + 1
    if word in ("a", "an") and tokens[i + 1:i + 3] == ["couple", "of"]:
        return 2.0, i + 3
    if word == "couple" and tokens[i + 1:i + 2] == ["of"]:
        return 2.0, i + 2
    if word in ("a", "an"):
        return 1.0, i + 1
    if word in _TENS:
        value = _TENS[word]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if nxt in NUMBER_WORDS and 0 < NUMBER_WORDS[nxt] < 10:
            return float(value + NUMBER_WORDS[nxt]), i + 2
        return float(value), i + 1
    if word in NUMBER_WORDS:
        return float(NUMBER_WORDS[word]), i + 1
    return None, i


def duration(text: str) -> float | None:
    """Seconds in "20 minutes", "an hour and a half", "1 h 30", "half an hour", "2 hours and 15 minutes"; None for
    anything else ("a few minutes", "a bit")."""
    text = text.replace("-", " ")
    fixed = {"half an hour": 1800, "half hour": 1800, "a half hour": 1800, "half a minute": 30,
             "quarter of an hour": 900, "a quarter of an hour": 900, "quarter hour": 900, "a quarter hour": 900,
             "half a day": 43200}
    if text in fixed:
        return float(fixed[text])
    tokens = text.split()
    total, i, last = 0.0, 0, ""
    while i < len(tokens):
        if tokens[i] == "and":
            i += 1
            continue
        value, j = _number(tokens, i)
        if value is None:
            return None
        if tokens[j:j + 3] == ["and", "a", "half"]:
            value, j = value + 0.5, j + 3
        unit = tokens[j] if j < len(tokens) else ""
        if unit in UNITS:
            total += value * UNITS[unit]
            last, j = unit, j + 1
            if tokens[j:j + 3] == ["and", "a", "half"]:
                total, j = total + 0.5 * UNITS[unit], j + 3
        elif not unit and UNITS.get(last) == 3600 and value == int(value) and 0 <= value < 60:
            total += value * 60          # "1 h 30": the minutes after the hours
        else:
            return None
        i = j
    return total if tokens else None


def _hour(word: str) -> int:
    return int(word) if word.isdigit() else _ONES.index(word)


def _day(words: str, today: date) -> tuple[date | None, str, str]:
    """(the date, a part-of-day hint "am"/"pm"/"", why not ("" or ambiguous)) for day words."""
    words = words.removeprefix("on ").strip()
    hint = ""
    part = re.search(rf"\s({_PART})$", words)
    if part:
        hint = "am" if part.group(1) == "morning" else "pm"
        words = words[:part.start()].strip()
    if words == "today":
        return today, hint, ""
    if words == "tonight":
        return today, "pm", ""
    if words == "tomorrow":
        return today + timedelta(days=1), hint, ""
    if words.startswith("this ") and words[5:] in ("morning", "afternoon", "evening"):
        return today, "am" if words.endswith("morning") else "pm", ""
    found = re.fullmatch(rf"(?:(this|next|coming|this\s+coming)\s+)?({'|'.join(WEEKDAYS)})", words)
    if not found:
        return None, hint, "unclear"
    which, name = found.group(1) or "", found.group(2)
    ahead = (WEEKDAYS.index(name) - today.weekday()) % 7
    if which == "next":
        return None, hint, "ambiguous"        # this coming one, or the one after? English does not say
    if ahead == 0:
        if which == "this":
            return today, hint, ""
        return None, hint, "ambiguous"        # "on Friday", said on a Friday: today or next week?
    return today + timedelta(days=ahead), hint, ""


def _at(day: date, hour: int, minute: int) -> float:
    return datetime(day.year, day.month, day.day, hour, minute).timestamp()


def _checked(due: float, now: float, day_words: str = "", clock: str = "") -> When:
    if due <= now:
        return When(None, "past", day_words, clock=clock)
    if due - now > MAX_AHEAD_S:
        return When(None, "too_far", day_words)
    return When(due, "", day_words, clock=clock)


def parse_when(phrase: str, now: float) -> When:
    """The time in `phrase` (words only: "tomorrow at 9"), local time, against `now` (an epoch)."""
    iso_text = " ".join((phrase or "").split()).lower()
    text = normal(phrase)
    text = re.sub(r"^(?:make\s+it|let'?s\s+say|say|how\s+about|about)\s+", "", text)
    text = re.sub(r"\s+(?:please|then|thanks|thank\s+you)$", "", text).strip()
    if not text:
        return When(None, "no_time")
    iso = ISO.match(iso_text)
    if iso:
        y, mo, d, h, mi, s = (int(g) if g else 0 for g in iso.groups())
        try:
            due = datetime(y, mo, d, h, mi, s).timestamp()
        except ValueError:
            return When(None, "unclear")
        return _checked(due, now)
    relative = RELATIVE.match(text)
    if relative and not VAGUE.match(text):
        seconds = duration(relative.group("dur") or relative.group("dur2") or "")
        if seconds is None:
            return When(None, "unclear")
        if seconds < 1:
            return When(None, "too_soon")
        if seconds > MAX_AHEAD_S:
            return When(None, "too_far")
        return When(now + seconds, "")
    if VAGUE.match(text):
        day = text if text in ("tonight", "tomorrow") else ""
        return When(None, "no_time", day)
    only = DAY_ONLY.match(text)
    if only:
        return When(None, "no_time", only.group("d"))
    found = AT.match(text)
    if not found:
        return When(None, "unclear")
    if found.group("d1") and found.group("d2"):
        return When(None, "unclear")
    day_words = found.group("d1") or found.group("d2") or ""
    today = datetime.fromtimestamp(now).date()
    day, hint, why = (today, "", "") if not day_words else _day(day_words, today)
    if why:
        return When(None, why, day_words)
    assert day is not None
    part = found.group("part") or ""
    if part:
        hint = "am" if part.endswith("morning") else "pm"
    ap = found.group("ap") or ""
    if found.group("noon"):
        if found.group("noon") != "midnight":
            return _checked(_at(day, 12, 0), now, day_words, "12:00")
        if day_words:
            return When(None, "ambiguous", day_words)   # the midnight that starts that day, or the one that ends it?
        return _checked(_at(today + timedelta(days=1), 0, 0), now, "", "00:00")
    if found.group("qh"):
        hour, q, before = _hour(found.group("qh")), found.group("q"), found.group("dir") == "to"
        minutes = {"quarter": 15, "half": 30, "five": 5, "ten": 10, "twenty": 20}.get(q)
        minutes = minutes if minutes is not None else 25 if q.startswith("twenty") else int(q)
        if not 1 <= hour <= 12 or not 0 < minutes < 60 or (before and q == "half"):
            return When(None, "unclear", day_words)
        minute = 60 - minutes if before else minutes
        hour = hour - 1 if before else hour
        hour = 12 if hour == 0 else hour
        bare, exact_hour = True, hour
    elif found.group("halfh"):
        hour, minute, bare = _hour(found.group("halfh")), 30, True      # British: "half three" is 3:30
        exact_hour = hour
        if not 1 <= hour <= 12:
            return When(None, "unclear", day_words)
    else:
        raw = found.group("h")
        hour, minute = _hour(raw), int(found.group("m") or 0)
        if hour > 23 or minute > 59:
            return When(None, "unclear", day_words)
        # 24-hour time ("15:00", "09:30", "0:15") is exact; a bare 1-12 has two readings.
        bare = 1 <= hour <= 12 and not (raw.isdigit() and raw.startswith("0"))
        exact_hour = hour
    if ap:
        if not 1 <= hour <= 12:
            return When(None, "unclear", day_words)
        exact_hour, bare = hour % 12 + (12 if ap == "pm" else 0), False
    elif bare and hint:
        exact_hour, bare = hour % 12 + (12 if hint == "pm" else 0), False
    if not bare:
        return _checked(_at(day, exact_hour, minute), now, day_words, f"{exact_hour:02d}:{minute:02d}")
    readings = [hour % 12, hour % 12 + 12]
    if not day_words or day == today:
        ahead = [h for h in readings if _at(day, h, minute) > now]
        if not ahead:
            return When(None, "past", day_words, clock=f"{hour}:{minute:02d}")
        if len(ahead) == 1:
            return _checked(_at(day, ahead[0], minute), now, day_words, f"{ahead[0]:02d}:{minute:02d}")
    reading = DAYTIME.get(hour)
    if reading is None:
        return When(None, "meridiem", day_words, hour=hour)
    chosen = hour % 12 + (12 if reading == "pm" else 0)
    return _checked(_at(day, chosen, minute), now, day_words, f"{chosen:02d}:{minute:02d}")


def iso(due: float) -> str:
    """A due time as the server takes it back exactly (local, to the second): "2026-10-11 15:00:00"."""
    return datetime.fromtimestamp(due).strftime("%Y-%m-%d %H:%M:%S")


def label(due: float, now: float) -> str:
    """When, as she says it: "at 15:00", "tomorrow at 09:00", "on Friday at 10:00", "on 12 October at 10:00"; for a
    time gone, "yesterday at 15:00" and "on 9 October at 15:00"."""
    at, today = datetime.fromtimestamp(due), datetime.fromtimestamp(now).date()
    clock = at.strftime("%H:%M")
    days = (at.date() - today).days
    if days == 0:
        return f"at {clock}"
    if days == 1:
        return f"tomorrow at {clock}"
    if days == -1:
        return f"yesterday at {clock}"
    if 1 < days < 7:
        return f"on {at.strftime('%A')} at {clock}"
    return f"on {at.day} {at.strftime('%B')} at {clock}"


# ----------------------------------------------------------------------------- the words

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  -‏ -‮⁦-⁩﻿]")


def clean_text(text: Any) -> str:
    """A reminder's words as kept: one line, no control characters, no quotes or end marks around them."""
    text = " ".join(_CONTROL.sub(" ", str(text or "")).split())
    return text.strip(" \"'“”‘’").rstrip(".!?,;:").strip()


_PERSON = {"i": "you", "me": "you", "my": "your", "mine": "yours", "myself": "yourself", "i'm": "you're",
           "i've": "you've", "i'll": "you'll", "i'd": "you'd", "am": "are"}


def spoken(text: str) -> str:
    """The user's words as she says them back: "call my mum" -> "call your mum" (I/me/my become you/your)."""
    words = text.split(" ")
    out = []
    for i, word in enumerate(words):
        low = word.lower().replace("’", "'")
        swap = _PERSON.get(low)
        if low == "am" and (i == 0 or words[i - 1].lower() != "i"):
            swap = None                  # "am" only after "I" ("I am" -> "you are"); "9 am" stays
        out.append(swap if swap else word)
    return " ".join(out)


def _fill(template: str, fallback: str, **values: str) -> str:
    """A persona line with its placeholders filled; the shipped line when the user's leaves one out (the time and
    the words are what the line is for)."""
    for line in (template, fallback):
        if line and all("{" + key + "}" in line for key in values):
            out = line
            for key, value in values.items():
                out = out.replace("{" + key + "}", value)
            return out
    return fallback


def line(key: str, fallback: str, **values: str) -> str:
    """Her persona's line for `key` (persona.md), filled."""
    try:
        template = personas.line(key)
    except Exception:   # noqa: BLE001 - her lines never cost a reminder
        template = ""
    return _fill(template, fallback, **values)


def set_line(due: float, text: str, now: float) -> str:
    return line("reminder_set", SET_LINE, when=label(due, now), text=spoken(text))


def due_line(text: str) -> str:
    return line("reminder_due", DUE_LINE, text=spoken(text))


def missed_line(items: list[dict[str, Any]], now: float) -> str:
    named = [f"{label(t['due'], now)}, {spoken(t['text'])}" for t in items[:MAX_TOLD]]
    if len(items) > MAX_TOLD:
        named.append(f"and {len(items) - MAX_TOLD} more")
    return line("reminder_missed", MISSED_LINE, list="; ".join(named))


def ask_line(when: When) -> str:
    """Her question when the time cannot be read for certain: the reason in code's words, then her persona's ask."""
    ask = line("reminder_ask_time", ASK_LINE)
    if when.why == "meridiem":
        return f"{when.hour} in the morning or in the evening?"
    reason = {"past": f"{when.clock} has already gone today." if when.clock else "That time has already gone.",
              "too_far": "That's more than a year away.",
              "ambiguous": "I'm not sure which day you mean.",
              "unclear": "I didn't catch the time."}.get(when.why, "")
    return f"{reason} {ask}".strip()


def listing(items: list[dict[str, Any]], now: float) -> str:
    if not items:
        return "You have no reminders."
    named = "; ".join(f"{label(t['due'], now)}, {spoken(t['text'])}" for t in items[:8])
    more = f"; and {len(items) - 8} more" if len(items) > 8 else ""
    return f"You have {'one reminder' if len(items) == 1 else f'{len(items)} reminders'}: {named}{more}."


# ----------------------------------------------------------------------------- whole sentences

_ASK = r"(?:(?:hey|ok|okay|oh)\s+)?(?:strawberry\s+)?(?:(?:can|could|will|would)\s+you\s+)?(?:please\s+)?"
_POLITE = r"(?:\s+(?:please|for\s+me|strawberry|thanks|thank\s+you))*"
SET = re.compile(rf"^{_ASK}(?:remind\s+me|(?:set|make|add|create|put\s+in)\s+(?:me\s+)?(?:a|an)\s+reminder)\s+(?P<rest>.+?){_POLITE}$", re.I)
WHEN_FIRST = re.compile(rf"^(?P<when>.+?)\s+(?:please\s+)?remind\s+me\s+(?P<rest>.+?){_POLITE}$", re.I)
CONNECTOR = re.compile(r"^(?:to|about|that|of)\s+(?P<text>.+)$", re.I)
LIST = re.compile(
    rf"^{_ASK}(?:(?:what|which)\s+reminders?\s+(?:do\s+i\s+have|have\s+i\s+got|are\s+there|are\s+set|have\s+i\s+set)"
    rf"(?:\s+(?:set|today|now))?|(?:what\s+are|what's|whats)\s+my\s+reminders|(?:do\s+i\s+have|have\s+i\s+got|are\s+there)\s+"
    rf"any\s+reminders(?:\s+set)?|any\s+reminders|(?:list|show|tell\s+me|read|give\s+me)(?:\s+me)?\s+(?:all\s+)?(?:my|the)?\s*"
    rf"reminders|my\s+reminders){_POLITE}$", re.I)
CANCEL = re.compile(rf"^{_ASK}(?:cancel|delete|remove|clear|drop|forget(?:\s+about)?)\s+(?P<what>.+?){_POLITE}$", re.I)
CANCEL_ALL = re.compile(r"^(?:all\s+(?:of\s+)?(?:my|the)\s+reminders|all\s+reminders|every\s+reminder|my\s+reminders|"
                        r"the\s+reminders|all\s+my\s+reminders|all\s+the\s+reminders|reminders)$", re.I)
CANCEL_LAST = re.compile(r"^(?:that|this|the\s+last|my\s+last|the\s+latest|the\s+new)\s+(?:one\s+)?reminder$", re.I)
CANCEL_ONE = re.compile(r"^(?:the|my|that|this)?\s*(?P<before>.*?)\s*reminder(?:\s+(?:about|for|to|at)\s+(?P<after>.+))?$", re.I)
# A sentence about reminders at all: the thinker's offer (`wanted`), and what keeps it out of the journal.
ASKS = re.compile(r"\bremind\s+me\b|\breminders?\b", re.IGNORECASE)
logtext.private_sentences(ASKS)   # such a sentence, and her answer, are logged as their lengths only


class _Answering:
    """Read as a pattern by logtext.private: while her "when?" is out, the user's next sentence (a bare "at 16:00"
    that sets a reminder, its words in her answer) is kept out of the journal too. Asked as the sentence's handling
    starts (logtext.hearing), before the said reflex reads it."""

    def __init__(self) -> None:
        self.adapters: weakref.WeakSet[RemindersAdapter] = weakref.WeakSet()

    def search(self, text: str) -> bool:
        return any(adapter.expecting() for adapter in list(self.adapters))


ANSWERING = _Answering()
logtext.private_sentences(ANSWERING)   # type: ignore[arg-type] - duck-typed: only `search` is used
_MATCH_STOP = frozenset("the a an my that this to about for at on of reminder reminders one".split())


def plain(text: str) -> str:
    """The sentence as the patterns read it: one line, quotes, commas and end marks off, the case kept for the
    reminder's words (the patterns ignore it)."""
    text = (text or "").replace("’", "'").replace("“", " ").replace("”", " ")
    text = re.sub(r"[,;!?\"]", " ", text)
    text = " ".join(text.split()).rstrip(".")
    return text.strip()


@dataclass
class Request:
    """What a whole sentence asks: `kind` set | list | cancel; for set the words and the time read (or why not)."""

    kind: str
    text: str = ""
    when: When | None = None
    all: bool = False
    match: str = ""


# What a time in a sentence starts with (or carries): so "read chapter 9" or "a table for 8" is not read as a time.
_TIME_FIRST = re.compile(rf"^(?:at|in|on|by|around|for|within|after|today|tonight|tomorrow|this|next|coming|noon|midday|"
                         rf"midnight|later|soon|{'|'.join(WEEKDAYS)})\b|\d[:.]\d\d|\b(?:am|pm)\b|\bfrom\s+now$", re.I)
_TIME_LAST = re.compile(rf"^(?:at|in|on|around|within|today|tonight|tomorrow|this|next|coming|noon|midday|midnight|later|"
                        rf"soon|{'|'.join(WEEKDAYS)})\b|\b(?:am|pm)$|\bfrom\s+now$", re.I)


def _split(rest: str, now: float) -> tuple[str, When] | None:
    """The words and the time of "<when> to <words>", "to <words> <when>" or "to <words>" (no time). None when it
    is not a reminder's shape at all."""
    words = rest.split()
    # The time first: "in 20 minutes to stretch", "tomorrow at 9 to call the dentist".
    for i in range(len(words) - 1, 0, -1):
        head, tail = " ".join(words[:i]), " ".join(words[i:])
        found = CONNECTOR.match(tail)
        if not found or not _TIME_FIRST.search(normal(head)):
            continue
        when = parse_when(head, now)
        if when.why != "unclear":
            return found.group("text"), when
    found = CONNECTOR.match(rest)
    if not found:
        return None
    words = found.group("text").split()
    # The time last: "to call the dentist at 15:00": the longest tail that reads as a time.
    for i in range(1, len(words)):
        head, tail = " ".join(words[:i]), " ".join(words[i:])
        if not _TIME_LAST.search(normal(tail)):
            continue
        when = parse_when(tail, now)
        if when.why != "unclear":
            return head, when
    return found.group("text"), When(None, "no_time")


def read(sentence: str, now: float) -> Request | None:
    """A whole sentence about reminders, read without a model; None for anything else (the thinker's)."""
    said = plain(sentence)
    if LIST.match(said):
        return Request("list")
    cancel = CANCEL.match(said)
    if cancel and re.search(r"\breminders?\b", cancel.group("what"), re.IGNORECASE):
        what = cancel.group("what").strip()
        if CANCEL_ALL.match(what):
            return Request("cancel", all=True)
        if CANCEL_LAST.match(what):
            return Request("cancel", match="last")
        one = CANCEL_ONE.match(what)
        if one is None:
            return None
        match = " ".join(x for x in (one.group("before"), one.group("after") or "") if x).strip()
        return Request("cancel", match=match)
    found = SET.match(said)
    if found:
        parts = _split(found.group("rest"), now)
    else:
        first = WHEN_FIRST.match(said)
        if first is None:
            return None
        when = parse_when(first.group("when"), now)
        text = CONNECTOR.match(first.group("rest"))
        if when.why == "unclear" or text is None or not _TIME_FIRST.search(normal(first.group("when"))):
            return None
        parts = text.group("text"), when
    if parts is None:
        return None
    text, when = clean_text(parts[0]), parts[1]
    if not text:
        return None
    return Request("set", text=text, when=when)


def matching(items: list[dict[str, Any]], match: str, now: float) -> list[dict[str, Any]]:
    """The pending reminders `match` names: by words in them ("dentist"), by a time ("15:00", "3 pm"), or, with
    nothing to go on or "that"/"last", every one (the caller asks which when there are several)."""
    words = [w for w in re.findall(r"[\w':.]+", match.lower()) if w not in _MATCH_STOP]
    if not words:
        return list(items)
    if words in (["last"], ["latest"], ["newest"]):
        return [max(items, key=lambda t: (t["created_at"], t["id"]))] if items else []
    start = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    when = parse_when(match, start) if re.search(r"\d|noon|midday", match.lower()) else None
    if when is not None and when.ok:
        clock = datetime.fromtimestamp(when.due).strftime("%H:%M")
        return [t for t in items if datetime.fromtimestamp(t["due"]).strftime("%H:%M") == clock]
    found = []
    for item in items:
        have = re.findall(r"[\w']+", item["text"].lower())
        if all(any(h == w or (len(w) >= 4 and h.startswith(w)) for h in have) for w in words):
            found.append(item)
    return found


# ----------------------------------------------------------------------------- the store and the scheduler


class Unavailable(RuntimeError):
    """No history to keep reminders in (switched off, or the file could not be opened). The message is hers."""


class Reminders:
    """The reminders over her history's `timers` table, and the scheduler that says them. `history` is a callable
    (the daemon's history is made after this). `say(line, about)` says a line on the bodies and puts `about` in the
    timeline; `busy()` is true while a run, a question or the user's voice is going on; `bodies()` how many bodies
    are connected. Time is `clock()` (wall-clock; the tests' fake)."""

    def __init__(self, history: Callable[[], Any], clock: Callable[[], float] = time.time,
                 check_s: float = CHECK_S, hold_s: float = HOLD_S, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.history_of = history
        self.clock = clock
        self.monotonic = monotonic
        self.check_s = check_s
        self.hold_s = hold_s
        self.say: Callable[[str, str], Awaitable[int]] | None = None
        self.busy: Callable[[], bool] = lambda: False
        self.bodies: Callable[[], int] = lambda: 1
        self.task: asyncio.Task | None = None
        self.wake = asyncio.Event()
        self.lock = asyncio.Lock()
        self.started_at: float | None = None
        self.held_since: float | None = None
        self.counts = {"set": 0, "cancelled": 0, "fired": 0, "missed": 0, "held": 0}
        self.next_due: float | None = None
        self.no_body = False

    # --- the store

    def history(self) -> Any:
        history = self.history_of()
        if history is None:
            raise Unavailable(UNAVAILABLE)
        if getattr(history, "conn", True) is None:
            raise Unavailable(BROKEN)
        return history

    @property
    def available(self) -> bool:
        try:
            self.history()
        except Unavailable:
            return False
        return True

    async def pending(self) -> list[dict[str, Any]]:
        history = self.history()
        return await history.call(history.timers, "pending")

    async def add(self, due: float, text: str) -> int:
        history = self.history()
        if len(await self.pending()) >= MAX_PENDING:
            raise ValueError(f"there are {MAX_PENDING} reminders already")
        timer_id = await history.call(history.add_timer, due, text)
        self.counts["set"] += 1
        log.info("reminders: %d set, due in %.0f s", timer_id, due - self.clock())
        self.poke()
        return timer_id

    async def cancel(self, timer_id: int) -> bool:
        history = self.history()
        done = await history.call(history.set_timer_state, int(timer_id), "cancelled", "pending")
        if done:
            self.counts["cancelled"] += 1
            log.info("reminders: %d cancelled", timer_id)
        return bool(done)

    def poke(self) -> None:
        """Look again now (a reminder set or cancelled, a resume from suspend)."""
        self.wake.set()

    # --- the scheduler

    def start(self) -> None:
        if self.task is None and self.available:
            self.started_at = self.clock()
            self.task = asyncio.get_running_loop().create_task(self.run())

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):   # noqa: BLE001 - shutting down
                pass
            self.task = None

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:   # noqa: BLE001 - one bad look never ends the scheduler
                log.warning("reminders: a check failed (%s)", type(exc).__name__)
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), self.delay())
            except asyncio.TimeoutError:
                pass

    def delay(self) -> float:
        """Until the next look: the next due time, at most `check_s` (a suspend stops the monotonic clock the wait
        runs on, so the wall clock is read again at least this often), every second while one is held."""
        if self.held_since is not None:
            return 1.0
        if self.no_body:
            return 2.0
        if self.next_due is None:
            return self.check_s
        return max(0.2, min(self.check_s, self.next_due - self.clock()))

    async def tick(self) -> int:
        """Say what is due: each at most once. Returns how many were told."""
        async with self.lock:
            try:
                pending = await self.pending()
            except Unavailable:
                return 0
            now = self.clock()
            due = [t for t in pending if t["due"] <= now]
            later = [t["due"] for t in pending if t["due"] > now]
            self.next_due = min(later) if later else None
            self.no_body = False
            if not due:
                self.held_since = None
                return 0
            if self.bodies() <= 0:
                self.no_body = True
                return 0                    # nobody to show it to yet: it waits for a body
            if self.busy():
                if self.held_since is None:
                    self.held_since = self.monotonic()
                    self.counts["held"] += 1
                    log.info("reminders: %d due during a run or a question; waiting (at most %.0f s)", len(due),
                             self.hold_s)
                if self.monotonic() - self.held_since < self.hold_s:
                    return 0
            self.held_since = None
            started = self.started_at if self.started_at is not None else now
            missed = [t for t in due if t["due"] < started or now - t["due"] > LATE_S]
            fired = [t for t in due if t not in missed]
            history = self.history()
            lines: list[str] = []
            about: list[str] = []
            claimed = [t for t in missed if await history.call(history.set_timer_state, t["id"], "missed", "pending")]
            if claimed:
                self.counts["missed"] += len(claimed)
                log.info("reminders: %d missed while she was off; told as one line", len(claimed))
                lines.append(missed_line(claimed, now))
                about.append("reminders that fell due while you were off: " + "; ".join(
                    f'{label(t["due"], now)}, "{t["text"]}"' for t in claimed[:MAX_TOLD]))
            told = len(claimed)
            for item in fired:
                if not await history.call(history.set_timer_state, item["id"], "fired", "pending"):
                    continue                # cancelled or told meanwhile: never twice
                self.counts["fired"] += 1
                told += 1
                log.info("reminders: %d due; saying it", item["id"])
                lines.append(due_line(item["text"]))
                about.append(f'a reminder the user set went off: "{item["text"]}"')
            if lines:
                await self._say(" ".join(lines), "; ".join(about))
            return told

    async def _say(self, text: str, about: str) -> None:
        if self.say is None:
            return
        try:
            await self.say(text, about)
        except Exception as exc:   # noqa: BLE001 - it is marked already: never said twice
            log.warning("reminders: could not say one (%s)", type(exc).__name__)

    def stats(self) -> dict[str, Any]:
        """/health's `reminders`: counts only."""
        return {"available": self.available, "running": self.task is not None and not self.task.done(),
                "holding": self.held_since is not None, **self.counts,
                "next_in_s": round(self.next_due - self.clock(), 1) if self.next_due is not None else None}


# ----------------------------------------------------------------------------- the builtin server


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> Any:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return SimpleNamespace(name=name, description=description, input_schema=schema, annotations=None)


TOOLS = [
    _tool("set_reminder", "Set a one-off reminder: she says it at that time.",
          {"due": {"type": "string", "description": "When, in the user's words: 'in 20 minutes', 'at 15:00', 'at 3 pm', "
                                                   "'tomorrow at 9', 'on Friday at 10'."},
           "text": {"type": "string", "description": "What to remind them of, copied word for word from their "
                                                    "sentence (only I/me/my may become you/your)."}},
          ["due", "text"]),
    _tool("cancel_reminder", "Cancel a reminder: by its id, by words in it, or all of them.",
          {"id": {"type": "integer"}, "match": {"type": "string", "description": "Words in it, or its time."},
           "all": {"type": "boolean", "description": "Cancel every reminder."}}, []),
    _tool("list_reminders", "The reminders still to come.", {}, []),
]


def _result(value: Any, error: bool = False) -> dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": error}


def not_done(why: str) -> dict[str, Any]:
    return _result(f"Not done: {why}", True)


def _item(t: dict[str, Any], now: float) -> dict[str, Any]:
    return {"id": t["id"], "when": label(t["due"], now), "text": t["text"]}


class RemindersSession:
    """The builtin server's session (tools.Server): list_tools() and call_tool(name, arguments)."""

    def __init__(self, reminders: Reminders, adapter: "RemindersAdapter | None" = None) -> None:
        self.reminders = reminders
        self.adapter = adapter

    async def list_tools(self) -> Any:
        return SimpleNamespace(tools=TOOLS)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        arguments = arguments if isinstance(arguments, dict) else {}
        try:
            if name == "set_reminder":
                return await self.set_reminder(arguments.get("due"), arguments.get("text"))
            if name == "cancel_reminder":
                return await self.cancel_reminder(arguments.get("id"), arguments.get("match"), arguments.get("all"))
            if name == "list_reminders":
                now = self.reminders.clock()
                return _result({"reminders": [_item(t, now) for t in await self.reminders.pending()]})
        except Unavailable as exc:
            return not_done(str(exc))
        return _result(f"no tool named {name!r}", True)

    async def set_reminder(self, due: Any, text: Any) -> dict[str, Any]:
        self.reminders.history()
        words = clean_text(text)
        if not words:
            return not_done("say what the reminder is for.")
        if len(words) > MAX_TEXT:
            return not_done(f"a reminder's words are at most {MAX_TEXT} characters; ask the user for a shorter one.")
        now = self.reminders.clock()
        when = parse_when(str(due or "")[:80], now)
        if not when.ok:
            if self.adapter is not None:
                self.adapter.waiting(words, when)
            return not_done(f"the time is not certain ({when.why}). Ask the user exactly this: \"{ask_line(when)}\"")
        assert when.due is not None
        try:
            timer_id = await self.reminders.add(when.due, words)
        except ValueError as exc:
            return not_done(f"{exc}.")
        return _result({"id": timer_id, "due": iso(when.due), "when": label(when.due, now), "text": words,
                        "say": set_line(when.due, words, now)})

    async def cancel_reminder(self, timer_id: Any, match: Any, every: Any) -> dict[str, Any]:
        now = self.reminders.clock()
        items = await self.reminders.pending()
        if every is True:
            chosen = items
        elif isinstance(timer_id, int) and not isinstance(timer_id, bool):
            chosen = [t for t in items if t["id"] == timer_id]
        elif isinstance(match, str) and match.strip():
            chosen = matching(items, match[:80], now)
            if len(chosen) > 1:
                return not_done("several reminders match: " + json.dumps([_item(t, now) for t in chosen],
                                                                          ensure_ascii=False) + "; ask the user which.")
        else:
            return not_done("give the reminder's id, words in it, or all.")
        if not chosen:
            return not_done("no reminder matches; list_reminders shows them.")
        cancelled = [t for t in chosen if await self.reminders.cancel(t["id"])]
        return _result({"cancelled": [_item(t, now) for t in cancelled]})


# ----------------------------------------------------------------------------- the said reflexes

# What the said reflex read off the sentence, for the reflex that runs next in the same task (as desktop.py does).
_REQUEST: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("reminders_request", default={})


def _adapter(toolbox: Any, server: str) -> "RemindersAdapter":
    return getattr(toolbox, "adapters", {})[server]


def _told(result: Any) -> str:
    text = (getattr(result, "text", "") or "").strip().removeprefix("Not done: ").rstrip(".")
    return text[:1].lower() + text[1:] if text[:1].isupper() and not text.startswith("I ") else text


async def set_reflex(toolbox: Any, server: str) -> Outcome:
    asked = _REQUEST.get()
    adapter = _adapter(toolbox, server)
    if not adapter.reminders.available:
        return Outcome("could not set a reminder", _unavailable(adapter), False)
    if len(asked["text"]) > MAX_TEXT:
        return Outcome("did not set a reminder", "That's a lot to remember. Say it in fewer words?", False)
    result = await toolbox.call(server, "set_reminder", {"due": iso(asked["due"]), "text": asked["text"]})
    if not result.ok:
        return Outcome("tried to set a reminder", f"I couldn't set that reminder: {_told(result)}.", False, (result,))
    now = adapter.reminders.clock()
    return Outcome(f"set a reminder {label(asked['due'], now)}", set_line(asked["due"], asked["text"], now), True,
                   (result,))


async def ask_reflex(toolbox: Any, server: str) -> Outcome:
    asked = _REQUEST.get()
    adapter = _adapter(toolbox, server)
    if not adapter.reminders.available:
        return Outcome("could not set a reminder", _unavailable(adapter), False)
    adapter.waiting(asked["text"], asked["when"])
    return Outcome("asked when to remind the user", ask_line(asked["when"]), True)


async def list_reflex(toolbox: Any, server: str) -> Outcome:
    adapter = _adapter(toolbox, server)
    if not adapter.reminders.available:
        return Outcome("could not look at the reminders", _unavailable(adapter), False)
    result = await toolbox.call(server, "list_reminders", {})
    try:
        items = json.loads(result.text)["reminders"]
    except (ValueError, KeyError, TypeError):
        return Outcome("tried to look at the reminders", f"I couldn't read the reminders: {_told(result)}.", False,
                       (result,))
    if not items:
        return Outcome("looked at the reminders", "You have no reminders.", True, (result,))
    named = "; ".join(f"{t['when']}, {spoken(t['text'])}" for t in items[:8])
    more = f"; and {len(items) - 8} more" if len(items) > 8 else ""
    count = "one reminder" if len(items) == 1 else f"{len(items)} reminders"
    return Outcome("looked at the reminders", f"You have {count}: {named}{more}.", True, (result,))


async def cancel_reflex(toolbox: Any, server: str) -> Outcome:
    asked = _REQUEST.get()
    adapter = _adapter(toolbox, server)
    reminders = adapter.reminders
    if not reminders.available:
        return Outcome("could not cancel a reminder", _unavailable(adapter), False)
    now = reminders.clock()
    items = await reminders.pending()
    if not items:
        return Outcome("found no reminders to cancel", "You have no reminders to cancel.", True)
    if asked.get("all"):
        result = await toolbox.call(server, "cancel_reminder", {"all": True})
        if not result.ok:
            return Outcome("tried to cancel the reminders", f"I couldn't cancel them: {_told(result)}.", False, (result,))
        n = len(json.loads(result.text).get("cancelled", []))
        return Outcome("cancelled every reminder", "Cancelled your reminder." if n == 1 else
                       f"Cancelled all {n} reminders.", True, (result,))
    chosen = matching(items, asked.get("match", ""), now)
    if not chosen:
        return Outcome("found no such reminder", "I don't have a reminder like that. " + listing(items, now), True)
    if len(chosen) > 1:
        named = "; ".join(f"{label(t['due'], now)}, {spoken(t['text'])}" for t in chosen[:5])
        return Outcome("asked which reminder", f"Which one? {named}.", True)
    item = chosen[0]
    result = await toolbox.call(server, "cancel_reminder", {"id": item["id"]})
    if not result.ok:
        return Outcome("tried to cancel a reminder", f"I couldn't cancel it: {_told(result)}.", False, (result,))
    return Outcome("cancelled a reminder", f"Cancelled the reminder {label(item['due'], now)}: {spoken(item['text'])}.",
                   True, (result,))


def _unavailable(adapter: "RemindersAdapter") -> str:
    try:
        adapter.reminders.history()
    except Unavailable as exc:
        return str(exc)
    return BROKEN


# ----------------------------------------------------------------------------- the adapter


GUIDE = ("The reminders tools keep one-off reminders that you say at their time. `set_reminder` takes `due` in the "
         "user's own time words ('in 20 minutes', 'at 15:00', 'tomorrow at 9') and `text` copied word for word from "
         "their sentence; when its result says the time is not certain, ask the user exactly the question it gives and "
         "set nothing. After a reminder is set, say the result's `say` line. `list_reminders` shows the ones to come; "
         "`cancel_reminder` takes an id, words in it, or all. Never claim a reminder is set without the tool.")


@dataclass
class _Waiting:
    text: str
    when: When
    until: float
    phrase: str = field(default="")


class RemindersAdapter(Adapter):
    """Her reminders as a builtin server: private (the user's own words), offered only to a sentence about reminders,
    known by whole sentences of any gate topic (`any_topic`)."""

    name = SERVER
    title = "Reminders"
    private = True
    offer = "asked"
    any_topic = True
    # Her questions name a reminder by its time only (code's words), never an argument as it came.
    asks_after_foreign = True
    #: its reflexes' facts are code's words around the user's own (Daemon._handle_voice): the turn is not foreign in
    #: the timeline, so "remind me…" does not make the next sentence's run ask before every change
    own_facts = True
    max_calls = 4
    reads = ("list_reminders",)
    # Setting and cancelling change what she will say later: no question on the user's own sentence, a question once
    # strangers' text is in the run (approvals.needed `foreign`). A list only reads.
    risks = {"set_reminder": "change", "cancel_reminder": "change", "list_reminders": "read"}
    reflex_tools = {"remind": ("set_reminder",), "remind_ask": (), "reminders_list": ("list_reminders",),
                    "reminder_cancel": ("cancel_reminder",)}
    said_reflexes = {"remind": ("set_reminder", set_reflex), "remind_ask": ("set_reminder", ask_reflex),
                     "reminders_list": ("list_reminders", list_reflex),
                     "reminder_cancel": ("cancel_reminder", cancel_reflex)}
    labels = {"set_reminder": "setting a reminder…", "cancel_reminder": "cancelling a reminder…",
              "list_reminders": "looking at reminders…", "remind": "setting a reminder…",
              "remind_ask": "a reminder…", "reminders_list": "looking at reminders…",
              "reminder_cancel": "cancelling a reminder…"}
    guide = GUIDE

    def __init__(self, reminders: Reminders) -> None:
        self.reminders = reminders
        self.awaiting: _Waiting | None = None
        ANSWERING.adapters.add(self)

    def waiting(self, text: str, when: When) -> None:
        """Her question about the time is out: the user's next sentence may be just a time (AWAIT_S)."""
        self.awaiting = _Waiting(text, when, self.reminders.monotonic() + AWAIT_S)

    def _answer(self, said: str, now: float) -> When | None:
        """The user's answer to "when?": a time alone ("at 16:00", "in the evening"), read with what she asked about
        (the day: "tomorrow" + "at 9"; the hour: "6" + "in the evening"). None: not a time at all."""
        assert self.awaiting is not None
        asked = self.awaiting.when
        text = normal(said)
        text = re.sub(r"^(?:make\s+it|let'?s\s+say|say|how\s+about|um|erm|oh)\s+", "", text)
        text = re.sub(r"^(?:the\s+)?(morning|afternoon|evening)$", r"in the \1", text)
        text = {"night": "at night", "at night": "at night"}.get(text, text)
        tries = []
        if asked.why == "meridiem" and re.fullmatch(r"(?:in\s+the\s+(?:morning|afternoon|evening)|at\s+night|am|pm)",
                                                    text):
            tries.append(f"{asked.day} at {asked.hour} {text}".strip())
        has_day = bool(re.search(rf"\b(?:today|tonight|tomorrow|{'|'.join(WEEKDAYS)})\b", text))
        if asked.day and not has_day:
            tries.append(f"{asked.day} {text}")
        tries.append(text)
        results = [parse_when(t, now) for t in tries]
        for when in results:
            if when.ok:
                return when
        heard = [w for w in results if w.why not in ("unclear", "no_time")]
        return heard[0] if heard else None

    def said_reflex(self, text: str, route: Any) -> str | None:
        now = self.reminders.clock()
        said = plain(text)
        if self.awaiting is not None:
            fresh = self.reminders.monotonic() < self.awaiting.until
            when = self._answer(said, now) if fresh and not ASKS.search(said) else None
            pending, self.awaiting = self.awaiting, None
            if when is not None:
                _REQUEST.set({"text": pending.text, "due": when.due, "when": when})
                return "remind" if when.ok else "remind_ask"
        request = read(text, now)
        if request is None:
            return None
        if request.kind == "list":
            _REQUEST.set({})
            return "reminders_list"
        if request.kind == "cancel":
            _REQUEST.set({"all": request.all, "match": request.match})
            return "reminder_cancel"
        assert request.when is not None
        _REQUEST.set({"text": request.text, "due": request.when.due, "when": request.when})
        return "remind" if request.when.ok else "remind_ask"

    def expecting(self) -> bool:
        """Her "when?" is out and fresh: the next sentence is read for a time even when the gate reads it as a
        fragment (Actor.said_reflex_for)."""
        return self.awaiting is not None and self.reminders.monotonic() < self.awaiting.until

    def wanted(self, text: str, route: Any) -> bool | None:
        return True if ASKS.search(text or "") or self.expecting() else None

    def nudge(self, text: str, route: Any) -> str:
        return ("They asked about reminders: set_reminder with `due` in their time words and `text` copied from their "
                "sentence, or list_reminders, or cancel_reminder. If the time is not certain, ask them when.")

    def guard(self, state: dict[str, Any], name: str, arguments: dict[str, Any]) -> str | None:
        if not isinstance(arguments, dict):
            return "Not done: give the arguments as an object."
        if name == "set_reminder":
            due, words = arguments.get("due"), arguments.get("text")
            if not isinstance(due, str) or not due.strip() or len(due) > 80:
                return "Not done: give `due` as the user's time words, e.g. 'in 20 minutes' or 'at 15:00'."
            if not isinstance(words, str) or not words.strip() or len(words) > MAX_TEXT * 2:
                return f"Not done: give `text`, at most {MAX_TEXT} characters."
            sentence = sentence_of_this_run()
            if sentence is None:
                log.info("reminders: a set_reminder not made: this run is not the user's own sentence")
                return "Not done: a reminder is set only from the user's own sentence."
            if not from_sentence(clean_text(words), sentence):
                log.info("reminders: a set_reminder not made: its words are not a span of the user's sentence")
                return ("Not done: the reminder's words must be copied word for word from the user's sentence (only "
                        "I/me/my may become you/your).")
        if name == "cancel_reminder":
            timer_id, match, every = arguments.get("id"), arguments.get("match"), arguments.get("all")
            if timer_id is not None and (not isinstance(timer_id, int) or isinstance(timer_id, bool)):
                return "Not done: `id` is a whole number."
            if match is not None and (not isinstance(match, str) or len(match) > 80):
                return "Not done: `match` is a few words."
            if every is not None and not isinstance(every, bool):
                return "Not done: `all` is true or false."
        return None

    async def ask(self, toolbox: Any, server: str, name: str,
                  arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Her question before a set or a cancel that waits for a yes (strangers' text is in the run): the time only,
        in code's words, never the words as they came; the call pinned (the due time as it is now, the one id)."""
        now = self.reminders.clock()
        if name == "set_reminder" and isinstance(arguments, dict):
            when = parse_when(str(arguments.get("due") or "")[:80], now)
            if when.ok and when.due is not None:
                return (f"Set a reminder {label(when.due, now)}? Say yes.",
                        {"due": iso(when.due), "text": arguments.get("text")})
            return "Set a reminder? Say yes.", dict(arguments)
        if name == "cancel_reminder" and isinstance(arguments, dict):
            try:
                items = await self.reminders.pending()
            except Unavailable:
                items = []
            if arguments.get("all") is True:
                return f"Cancel all {len(items)} reminders? Say yes.", {"all": True}
            if isinstance(arguments.get("id"), int):
                chosen = [t for t in items if t["id"] == arguments["id"]]
            else:
                chosen = matching(items, str(arguments.get("match") or "")[:80], now) if arguments.get("match") else []
            if len(chosen) == 1:
                return f"Cancel the reminder {label(chosen[0]['due'], now)}? Say yes.", {"id": chosen[0]["id"]}
            return "Cancel a reminder? Say yes.", dict(arguments)
        return await super().ask(toolbox, server, name, arguments)

    def done(self, name: str, arguments: dict[str, Any], result: Any) -> Outcome | None:
        """After the yes: the set line, or what was cancelled, in code's words."""
        if not result.ok:
            return Outcome("tried to change a reminder", f"I couldn't do that: {_told(result)}.", False, (result,))
        try:
            data = json.loads(result.text)
        except (ValueError, TypeError):
            return None
        if name == "set_reminder" and isinstance(data, dict) and data.get("say"):
            return Outcome(f"set a reminder {data.get('when', '')}".strip(), data["say"], True, (result,))
        if name == "cancel_reminder" and isinstance(data, dict):
            n = len(data.get("cancelled") or [])
            return Outcome("cancelled reminders", "Cancelled." if n == 1 else f"Cancelled {n} reminders.", True,
                           (result,))
        return None

    def log_result(self, name: str, text: str, ok: bool) -> str | None:
        return f"{len(text or '')} chars (not logged)"
