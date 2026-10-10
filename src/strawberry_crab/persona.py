"""Who she is and how she talks: persona.md (WIRING.md §21).

One file holds her persona for both models and her fixed lines. The shipped default is
`data/persona.md`; `~/.config/strawberry/persona.md` replaces it whole. Four sections, fixed:

    ## Who she is       written to her, read first by both models
    ## How she talks    one description of her voice, read by both: the reaction model
                        ("…in your own voice: <it>") and the thinker ("Your voice: <it>")
    ## Examples         what the reaction model copies: event fields, her line, its emotion
    ## Lines            her phrasebook: "### key" and "- variant" items (cover lines, "Okay,
                        stopped.", the poke lines, the no-music-add-on lines, ...)

`parse` reads a file into a `Persona` and checks it: every section there, each under its token cap,
examples with a known source, their fields and an emotion of neutral/happy/alert/angry, lines short
and on one line. `PersonaStore` reads the user's file whenever it changed (its mtime and size) and
falls back to the shipped one on any problem, logging why and keeping the reason for the Brain UI:
a bad edit never makes her mute.

What stays in code, added after the persona so a persona.md can never remove it: the output
contracts the code parses (the reaction model's JSON, the thinker's [mood] tag), the privacy
wording (a message is private, BODY_RULE) and the task framing around the description. The tool
rules are the thinker's own (thinker.py), and the approval lines confirm.py's.

The old config keys keep working, with a deprecation warning: `[brain] persona` (the reaction
model's whole system prompt), `[brain] examples` and `[thinker] acks` (config.py).

The bake-off (scripts/reactor_bakeoff.py) showed a 1B model ignores a description of the register
but copies examples of it faithfully, so the examples are the real lever on her reactions.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import paths
from .contract import EMOTIONS

log = logging.getLogger("strawberryd.persona")

SECTIONS = ("Who she is", "How she talks", "Examples", "Lines")
# Token caps per section, by the thinker's estimate (3 characters a token, on the safe side): the
# reaction model reads the first three on every event, and the shipped persona is well under them.
CAPS = {"Who she is": 120, "How she talks": 300, "Examples": 2000, "Lines": 1500}
MAX_EXAMPLES = 40
MAX_EXAMPLE_LINE = 140      # events.MAX_LINE: a reaction is cut there anyway
MAX_PHRASE = 200            # one line of the phrasebook
MAX_FILE_CHARS = 40_000
CHARS_PER_TOKEN = 3.0       # = thinker.CHARS_PER_TOKEN

# Appended to every notification that carries a body (brain.describe, WIRING.md §4): the Slack code
# word she once quoted aloud is the reason. It is code, so no persona can take it out.
BODY_RULE = "(react to it in your own words: never repeat its words, names, numbers or links)"

# The reaction model's frame around the persona, and what follows it: the task, the privacy rule for
# messages and the output contract (Gemma's JSON, brain.SCHEMA).
REACT_TASK = "Something just happened. React with ONE short sentence, at most {max_words} words, in your own voice: "
REACT_RULES = (
    "Do not repeat the event text word for word; react to it. A message someone sent is private: never repeat its "
    "words, names, numbers or links, only say in your own words what it is about. Pick the emotion that fits: "
    "neutral, happy, alert (something needs attention), or angry (something went wrong). Answer only with JSON."
)
REACT_PROFILE = "About the user (background only; mention it only when it fits): {profile} "
REACT_PROFILE_TOKENS = 60    # a longer profile is left out of the reaction model's prompt

# The thinker's frame: who is talking to her, the shape of a spoken reply, and the [mood] tag the code
# parses off the front of it (thinker.split_emotion).
THINK_TASK = ("The user is talking to you now: the sentence below is theirs, heard through speech-to-text. Answer "
              "them yourself, as {name}. Your voice: ")
THINK_FORM = ("No markdown, no lists, no follow-up questions, and never a name for the user - say 'you' to them. "
              "Small talk and confirmations get ONE short sentence of at most 15 words. An answer that carries facts "
              "may run to two or three plain sentences, no more.")
THINK_FORM_PROFILE = ("No markdown, no lists, no follow-up questions; call the user what their profile below says, "
                      "else say 'you' to them. Small talk and confirmations get ONE short sentence of at most 15 words. "
                      "An answer that carries facts may run to two or three plain sentences, no more.")
MOOD = ("Start every reply with your mood in square brackets - [neutral], [happy], [alert] (something needs attention) "
        "or [angry] (something went wrong) - then a space, then what you say. Example: [happy] Skipped. Blue Monday next.")

# Example sources and the fields each may have (what brain.describe shows for that source).
SOURCES: dict[str, tuple[str, ...]] = {
    "git": ("app", "title", "body", "urgency"),
    "notification": ("app", "title", "body", "urgency", "told"),
    "media": ("app", "title", "body"),
    "voice": ("said",),
    "action": ("told", "asked", "did"),
}
REQUIRED = {"voice": ("said",), "action": ("told", "asked", "did")}
URGENCIES = ("low", "normal", "critical")
KEY = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f  ​-‏‪-‮⁦-⁩﻿]")


def tokens(text: str) -> int:
    """The thinker's estimate of a text's tokens (3 characters a token: on the safe side)."""
    return int(len(text) / CHARS_PER_TOKEN) + 1 if text else 0


def continued(text: str) -> str:
    """The description as the end of a sentence ("…your own voice: playful, …"): its first letter lower-
    cased when the first word is an ordinary capitalised word (not "I", not an acronym)."""
    first = text.split(" ", 1)[0]
    word = first.rstrip(",.;:!?")
    if len(word) > 1 and word[0].isupper() and word[1:].islower() and word not in ("I'm", "I've", "I'd", "I'll"):
        return text[0].lower() + text[1:]
    return text


class PersonaError(ValueError):
    """A persona.md that does not check out; `problems` says each thing wrong, by section."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Example:
    """One example exchange: the event's fields (as persona.md names them), her line and its emotion."""

    source: str
    fields: tuple[tuple[str, str], ...]
    line: str
    emotion: str

    def event(self):
        from .events import Event

        values = dict(self.fields)
        if self.source == "voice":
            return Event(source="voice", title=values.get("said", ""))
        if self.source == "action":
            return Event(source="action", body=values.get("told", ""), title=values.get("asked", ""),
                         app=values.get("did", ""))
        return Event(source=self.source, app=values.get("app", ""), title=values.get("title", ""),
                     body=values.get("body", ""), urgency=values.get("urgency", "normal"), said=values.get("told", ""))

    def rendered(self) -> dict[str, str]:
        """As the reaction model is shown it: the event exactly as a real one is described (brain.describe,
        BODY_RULE included), then her line."""
        from .brain import describe   # local: brain imports this module

        return {"event": describe(self.event()), "line": self.line, "emotion": self.emotion}


@dataclass(frozen=True)
class Persona:
    who: str
    talks: str
    examples: tuple[Example, ...]
    lines: dict[str, tuple[str, ...]]
    warnings: tuple[str, ...] = ()
    text: str = field(default="", compare=False, repr=False)

    @property
    def name(self) -> str:
        """Her name, from "You are <Name>, …" in Who she is; "" when it does not say."""
        found = re.search(r"\bYou are ([A-Z][\w'-]*)", self.who)
        return found.group(1) if found else ""

    def reaction_system(self, max_words: int = 15, profile: str = "") -> str:
        """The reaction model's system prompt: who she is, the task, her voice, then (code) the rules.
        `profile` is a one-line summary of the user's profile, given only when it is short."""
        about = REACT_PROFILE.format(profile=profile) if profile else ""
        return f"{self.who} {REACT_TASK.format(max_words=max_words)}{continued(self.talks)} {about}{REACT_RULES}"

    def reaction_examples(self) -> list[dict[str, str]]:
        return [example.rendered() for example in self.examples]

    def thinker_voice(self, profile: bool = False) -> str:
        """Who is speaking, for the thinker: who she is, the task, her voice, the form of a spoken reply
        and (code) the [mood] tag. `profile`: there is a profile saying what to call the user."""
        task = THINK_TASK.format(name=self.name) if self.name else THINK_TASK.replace(", as {name}", "")
        form = THINK_FORM_PROFILE if profile else THINK_FORM
        return f"{self.who} {task}{continued(self.talks)} {form}\n{MOOD}"

    def variants(self, key: str) -> tuple[str, ...]:
        """The lines for a phrasebook key; the shipped ones when this persona has none for it."""
        found = self.lines.get(key)
        if found:
            return found
        return shipped().lines.get(key, ())

    def sizes(self) -> dict[str, int]:
        """Each section's tokens (the estimate), as rendered for the models (the lines as written)."""
        return {"Who she is": tokens(self.who), "How she talks": tokens(self.talks),
                "Examples": sum(tokens(e["event"]) + tokens(e["line"]) + 12 for e in self.reaction_examples()),
                "Lines": sum(tokens(v) for vs in self.lines.values() for v in vs)}


def _sections(text: str) -> tuple[dict[str, list[str]], list[str]]:
    """`## Heading` -> its lines (comments taken out), and the problems found on the way."""
    out: dict[str, list[str]] = {}
    problems: list[str] = []
    current: str | None = None
    for raw in COMMENT.sub("", text).splitlines():
        heading = re.match(r"^##\s+(.+?)\s*#*\s*$", raw)
        if heading and not raw.startswith("###"):
            title = heading.group(1).strip()
            known = next((s for s in SECTIONS if s.lower() == title.lower()), None)
            if known is None:
                problems.append(f"unknown section \"## {title}\" (the sections are {', '.join(SECTIONS)})")
                current = None
                continue
            if known in out:
                problems.append(f"\"## {known}\" is there twice")
            current = known
            out[known] = []
            continue
        if current is not None:
            out[current].append(raw)
    for name in SECTIONS:
        if name not in out:
            problems.append(f"the section \"## {name}\" is missing")
    return out, problems


def _prose(lines: list[str]) -> str:
    return " ".join(" ".join(line.split()) for line in lines if line.strip())


def _clean(value: str) -> str:
    return " ".join(CONTROL.sub(" ", value).split())


def _examples(lines: list[str]) -> tuple[list[Example], list[str]]:
    items: list[list[tuple[int, str, str]]] = []
    problems: list[str] = []
    for number, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        starts = raw.lstrip().startswith("- ") and not raw.startswith((" ", "\t"))
        body = raw.strip()[2:] if starts else raw.strip()
        if ":" not in body:
            problems.append(f"Examples: \"{body[:40]}\" is not a \"key: value\" line")
            continue
        key, value = (part.strip() for part in body.split(":", 1))
        key = key.lower()
        if starts:
            items.append([])
        elif not items:
            problems.append("Examples: an example starts with \"- source: …\"")
            continue
        items[-1].append((number, key, _clean(value)))
    out: list[Example] = []
    for index, item in enumerate(items, 1):
        values: dict[str, str] = {}
        where = f"Examples #{index}"
        for _number, key, value in item:
            if key in values:
                problems.append(f"{where}: \"{key}\" is there twice")
            values[key] = value
        source = values.pop("source", "")
        line = values.pop("line", "")
        emotion = values.pop("emotion", "")
        if source not in SOURCES:
            problems.append(f"{where}: source must be one of {', '.join(SOURCES)} (got \"{source}\")")
            continue
        if emotion not in EMOTIONS:
            problems.append(f"{where}: emotion must be one of {', '.join(EMOTIONS)} (got \"{emotion}\")")
        if not line:
            problems.append(f"{where}: it needs her line (\"line: …\")")
        elif len(line) > MAX_EXAMPLE_LINE:
            problems.append(f"{where}: her line is {len(line)} characters, at most {MAX_EXAMPLE_LINE}")
        unknown = sorted(set(values) - set(SOURCES[source]))
        if unknown:
            problems.append(f"{where}: a {source} example has no {', '.join(unknown)} "
                            f"(it has {', '.join(SOURCES[source])})")
        missing = [key for key in REQUIRED.get(source, ()) if not values.get(key)]
        if missing:
            problems.append(f"{where}: a {source} example needs {', '.join(missing)}")
        if source == "notification" and values.get("told") and values.get("body"):
            problems.append(f"{where}: a notification she has already told the user about (told) has no body")
        if values.get("urgency", "normal") not in URGENCIES:
            problems.append(f"{where}: urgency must be one of {', '.join(URGENCIES)}")
        fields = tuple((key, values[key]) for key in SOURCES[source] if key in values)
        out.append(Example(source, fields, line, emotion if emotion in EMOTIONS else "neutral"))
    if not items:
        problems.append("Examples: there must be at least one")
    if len(items) > MAX_EXAMPLES:
        problems.append(f"Examples: {len(items)} of them, at most {MAX_EXAMPLES}")
    return out, problems


def _lines(lines: list[str]) -> tuple[dict[str, tuple[str, ...]], list[str]]:
    out: dict[str, list[str]] = {}
    problems: list[str] = []
    current: str | None = None
    for raw in lines:
        stripped = raw.strip()
        heading = re.match(r"^###\s+(.+?)\s*$", stripped)
        if heading:
            key = heading.group(1).strip().lower()
            if not KEY.match(key):
                problems.append(f"Lines: \"### {key}\" is not a key (letters, digits, _ and . only)")
                current = None
                continue
            if key in out:
                problems.append(f"Lines: \"### {key}\" is there twice")
            current = key
            out[key] = []
            continue
        if stripped.startswith(("- ", "* ")) and current is not None:
            variant = _clean(stripped[2:])
            if not variant:
                continue
            if len(variant) > MAX_PHRASE:
                problems.append(f"Lines: a {current} line is {len(variant)} characters, at most {MAX_PHRASE}")
                continue
            out[current].append(variant)
    for key, variants in out.items():
        if not variants:
            problems.append(f"Lines: \"### {key}\" has no \"- \" line under it")
    return {k: tuple(v) for k, v in out.items() if v}, problems


def parse(text: str, known: tuple[str, ...] | None = None) -> Persona:
    """A persona.md read and checked; PersonaError with every problem when it does not check out.
    `known`: the phrasebook keys the code uses (the shipped file's); a key outside them, or one of them
    left out, is a warning on the Persona, not an error (a missing key keeps the shipped lines)."""
    if len(text) > MAX_FILE_CHARS:
        raise PersonaError([f"the file is {len(text)} characters, at most {MAX_FILE_CHARS}"])
    sections, problems = _sections(text)
    who = _clean(_prose(sections.get("Who she is", [])))
    talks = _clean(_prose(sections.get("How she talks", [])))
    if "Who she is" in sections and not who:
        problems.append("Who she is: it is empty")
    if "How she talks" in sections and not talks:
        problems.append("How she talks: it is empty")
    examples, found = _examples(sections.get("Examples", [])) if "Examples" in sections else ([], [])
    problems += found
    lines, found = _lines(sections.get("Lines", [])) if "Lines" in sections else ({}, [])
    problems += found
    warnings: list[str] = []
    if known is not None and "Lines" in sections:
        unknown = sorted(set(lines) - set(known))
        if unknown:
            warnings.append(f"Lines: {', '.join(unknown)} {'is' if len(unknown) == 1 else 'are'} not used by her "
                            "(ignored)")
        missing = [key for key in known if key not in lines]
        if missing:
            warnings.append(f"Lines: {', '.join(missing)} not in the file; the shipped lines are used")
    persona = Persona(who, talks, tuple(examples), lines, tuple(warnings), text)
    if not problems:
        for section, size in persona.sizes().items():
            if size > CAPS[section]:
                problems.append(f"{section}: ~{size} tokens, at most {CAPS[section]}")
    if problems:
        raise PersonaError(problems)
    return persona


def shipped_path() -> Path:
    return Path(__file__).parent / "data" / "persona.md"


@lru_cache(maxsize=1)
def shipped() -> Persona:
    """The persona the package ships (data/persona.md). It must parse: the tests hold it to that."""
    text = shipped_path().read_text(encoding="utf-8")
    return parse(text)


def known_keys() -> tuple[str, ...]:
    return tuple(shipped().lines)


class PersonaStore:
    """Her persona as it is now: the user's persona.md when there is one and it checks out, else the shipped
    one. The file is looked at on every use (a stat; parsed again only when its mtime or size changed), so an
    edit is live at her next line. `status` says which one is in use and why."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._seen: tuple[str, int, int] | None = None
        self._persona: Persona = shipped()
        self.error: list[str] = []        # why the user's file is not in use; [] when it is, or there is none
        self.source = "shipped"
        self.loaded_at: float | None = None

    @property
    def path(self) -> Path:
        return self._path or paths.persona_file()

    def current(self) -> Persona:
        path = self.path
        try:
            stat = path.stat()
        except OSError:
            if self._seen is not None:
                log.info("persona: %s is gone; using the shipped persona", path)
            self._seen, self._persona, self.error, self.source = None, shipped(), [], "shipped"
            return self._persona
        seen = (str(path), stat.st_mtime_ns, stat.st_size)
        if seen == self._seen:
            return self._persona
        self._seen = seen
        try:
            text = path.read_text(encoding="utf-8")
            persona = parse(text, known_keys())
        except PersonaError as exc:
            self._persona, self.error, self.source = shipped(), exc.problems, "shipped"
            log.warning("persona: %s is not used, the shipped persona is (%d problem(s): %s)", path,
                        len(exc.problems), "; ".join(exc.problems)[:400])
            return self._persona
        except (OSError, UnicodeDecodeError) as exc:
            self._persona, self.error, self.source = shipped(), [f"the file cannot be read ({type(exc).__name__})"], \
                "shipped"
            log.warning("persona: %s cannot be read (%s); the shipped persona is used", path, type(exc).__name__)
            return self._persona
        self._persona, self.error, self.source, self.loaded_at = persona, [], "file", time.time()
        log.info("persona: %s in use (%d examples, %d line keys%s)", path, len(persona.examples), len(persona.lines),
                 "; " + "; ".join(persona.warnings) if persona.warnings else "")
        return self._persona

    def variants(self, key: str) -> tuple[str, ...]:
        return self.current().variants(key)

    def line(self, key: str, rng: Any = None) -> str:
        """One of the key's lines, at random (`rng.choice`), or the first."""
        options = self.variants(key)
        if not options:
            return ""
        return rng.choice(options) if rng is not None else options[0]

    def text(self) -> str:
        """The user's persona.md as it is, also when it does not check out (to be fixed), else the shipped one."""
        try:
            return self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return shipped_path().read_text(encoding="utf-8")

    def status(self) -> dict[str, Any]:
        persona = self.current()
        return {"source": self.source, "path": str(self.path), "exists": self.path.exists(), "error": list(self.error),
                "warnings": list(persona.warnings), "sizes": persona.sizes(), "caps": dict(CAPS),
                "examples": len(persona.examples), "lines": len(persona.lines), "name": persona.name}


def check(text: str) -> tuple[Persona | None, list[str]]:
    """A draft checked as the store would read it: the persona, or None and the problems."""
    try:
        return parse(text, known_keys()), []
    except PersonaError as exc:
        return None, exc.problems


def save(text: str, path: Path | None = None) -> Path | None:
    """Write a draft that checks out as the user's persona.md (atomic), keeping the file it replaces as
    persona.md.bak beside it. Raises PersonaError when it does not check out. Returns the backup's path."""
    check_persona, problems = check(text)
    if check_persona is None:
        raise PersonaError(problems)
    path = path or paths.persona_file()
    backup = None
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        paths.write_atomic(backup, path.read_text(encoding="utf-8"))
    paths.write_atomic(path, text if text.endswith("\n") else text + "\n")
    os.utime(path)   # a new mtime even within the file system's timestamp granularity
    return backup


_store: PersonaStore | None = None


def store() -> PersonaStore:
    """The one store: the user's file at paths.persona_file(), looked up afresh on each use (the tests move
    the XDG dirs)."""
    global _store
    if _store is None:
        _store = PersonaStore()
    return _store


def line(key: str, rng: Any = None) -> str:
    return store().line(key, rng)


def variants(key: str) -> tuple[str, ...]:
    return store().variants(key)


def __getattr__(name: str) -> Any:
    """PERSONA and EXAMPLES: the shipped persona as the strings the code used to hold (for code and tests that
    read them), worked out on first use (rendering an example imports brain, which imports this module)."""
    if name == "PERSONA":
        return shipped().reaction_system()
    if name == "EXAMPLES":
        return shipped().reaction_examples()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
