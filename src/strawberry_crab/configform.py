"""The Brain UI's config.toml editor (WIRING.md §28): the form's schema, the draft's edits, the secrets kept off
the page, and which edits loosen safety. No web here; configui.py is the routes.

    schema()                          the form: a section per [table], a field per key, from config.py's dataclasses
                                      (types, defaults, choices, the comments the code has beside each key)
    vault = Vault()                   one page's hidden values: a secret leaves as `<hidden 3: kept as is>`
    masked = vault.mask(real)         and comes back with vault.unmask(masked); a comment line that names a
                                      secret (`# api_key = "…"`) is hidden whole the same way
    real = apply_edits(real, edits)   the form's edits on the draft with tomlkit: comments and order kept
    findings = lowering(old, new)     what the change loosens (safety_lowering below): asked about on her card
    problems = refused(old, new)      what the editor never changes: `[gestures] enabled` (the camera switch's)
    backup = write(path, text, sha)   backed up, then written in one step; refused when the file changed under it

The draft is text all the way: the form's edits, the raw view and the Tools page all become config.toml's text,
checked by config.load as at start (configedit.check_config_text) before anything is written.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import io
import json
import os
import hmac
import re
import secrets
import tempfile
import threading
import time
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import tomlkit
import tomllib
from tomlkit.container import Container, OutOfOrderTableProxy
from tomlkit.items import Table

from . import config as cfg
from . import configedit, paths
from .config import ConfigError

# --------------------------------------------------------------------------------------------- the schema

# A select instead of a text box: the values config._validate accepts.
CHOICES: dict[str, tuple[Any, ...]] = {
    "daemon.log_level": ("DEBUG", "INFO", "WARNING", "ERROR"),
    "notifications.min_urgency": ("low", "normal", "critical"),
    "notifications.body": cfg.BODY_MODES,
    "voice.device": ("cpu", "cuda", "auto"),
    "gate.embedder": cfg.EMBEDDERS,
    "gate.scorer": cfg.SCORERS,
    "gestures.watch": cfg.GESTURE_WATCH,
    "gestures.hands": (1, 2),
    "thinker.think": (False, "low", "medium", True),
    "approvals.hold": cfg.RISKS,
}
# Keys this form does not edit, and where they are edited instead.
ELSEWHERE: dict[str, tuple[str, str]] = {
    "tools.servers": ("tools", "the Tools page"),
    "gestures.enabled": ("input", "the camera switch on the Input tab (or the tray's Hand gestures row)"),
    "gestures.map": ("input", "the Input tab (saved to input.toml)"),
    "gestures.calibration": ("input", "the Input tab's Calibrate (saved to input.toml)"),
    "touch.actions": ("input", "the Input tab (saved to input.toml)"),
}
# What the daemon takes without a restart once the editor has saved (configui.apply_live); the rest is read at start.
LIVE_SECTIONS = {
    "notifications": "used from the next notification on",
    "gestures": "used at once (input.toml, when it has [gestures], wins over it)",
    "touch": "used at once (input.toml, when it has [touch], wins over it)",
}
DEPRECATED = {"brain.persona", "brain.examples", "thinker.acks"}


def _kind(dotted: str, default: Any) -> str:
    if dotted == "brain.keep_alive" or dotted == "thinker.keep_alive":
        return "duration"            # seconds or "10m"
    if dotted in CHOICES:
        return "multi" if isinstance(default, list) else "choice"
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, int):
        return "int"
    if isinstance(default, float):
        return "float"
    if isinstance(default, str):
        return "str"
    if isinstance(default, list):
        return "list" if all(isinstance(v, str) for v in default) and dotted != "brain.examples" else "json"
    if isinstance(default, dict):
        return "map" if dotted in ("notifications.body_apps", "approvals.risk") else "json"
    return "json"


def field_hints(cls: type) -> dict[str, str]:
    """The comments config.py has beside each field of a dataclass: the comment lines right above it (at the
    field's indent), the one on its line, and the deeper-indented comment lines that carry it on."""
    source = inspect.getsource(cls)
    lines = source.splitlines()
    comments: dict[int, tuple[int, str]] = {}     # 0-based line -> (column, text)
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            comments[token.start[0] - 1] = (token.start[1], token.string.lstrip("#").strip())
    names = {f.name for f in dataclasses.fields(cls)}
    hints: dict[str, str] = {}
    for i, line in enumerate(lines):
        match = re.match(r"^(\s+)([a-z_][a-z0-9_]*)\s*:", line)
        if match is None or match.group(2) not in names or match.group(2) in hints:
            continue
        indent = len(match.group(1))
        parts: list[str] = []
        above = i - 1
        while above >= 0 and lines[above].strip().startswith("#") and comments.get(above, (-1, ""))[0] == indent:
            above -= 1
        parts += [comments[j][1] for j in range(above + 1, i)]
        if i in comments and comments[i][0] > indent:
            parts.append(comments[i][1])
            below = i + 1
            while below < len(lines) and lines[below].strip().startswith("#") and comments.get(below, (0, ""))[0] > indent:
                parts.append(comments[below][1])
                below += 1
        hints[match.group(2)] = " ".join(p for p in parts if p)
    return hints


def _doc(cls: type) -> str:
    doc = inspect.getdoc(cls) or ""
    if doc.startswith(cls.__name__ + "("):   # a dataclass without a docstring gets its signature
        return ""
    return " ".join(doc.split("\n\n")[0].split())


def schema() -> list[dict[str, Any]]:
    """Every section of config.toml and its keys, for the form: kind, default, choices, the code's hint, and where a
    key is edited instead when not here."""
    out = []
    for name, cls in cfg._SECTIONS.items():
        hints = field_hints(cls)
        defaults = cls()
        fields = []
        for f in dataclasses.fields(cls):
            dotted = f"{name}.{f.name}"
            default = getattr(defaults, f.name)
            row: dict[str, Any] = {"key": f.name, "kind": _kind(dotted, default), "default": default,
                                   "hint": hints.get(f.name, "")}
            if dotted in CHOICES:
                row["choices"] = list(CHOICES[dotted])
            if dotted in ELSEWHERE:
                row["elsewhere"] = {"tab": ELSEWHERE[dotted][0], "where": ELSEWHERE[dotted][1]}
            if dotted in DEPRECATED:
                row["deprecated"] = True
            fields.append(row)
        out.append({"name": name, "doc": _doc(cls), "live": LIVE_SECTIONS.get(name), "fields": fields})
    return out


def section_names() -> tuple[str, ...]:
    return tuple(cfg._SECTIONS)


# --------------------------------------------------------------------------------------------- secrets

# A key whose value is a secret (anywhere in the file), an argument that carries one, a comment that names one.
SECRET_NAME = re.compile(r"token|secret|passw|api[_-]?key|auth|credential|bearer|cookie|private[_-]?key"
                         r"|(?:^|[_.-])key(?:$|[_.-])|\bpin\b", re.IGNORECASE)
SECRET_FLAG = re.compile(r"^-{1,2}[\w.-]*(?:token|secret|passw|api[_-]?key|auth|credential|bearer|cookie)[\w.-]*$",
                         re.IGNORECASE)
NUMERIC_SECRET = re.compile(r"passw|secret|\bpin\b|api[_-]?key|(?:^|[_.-])key(?:$|[_.-])", re.IGNORECASE)
# config.py's own settings ("thinker.tool_tokens" counts tokens): never a secret by their name.
_SETTINGS = frozenset((section, f.name) for section, cls in cfg._SECTIONS.items() for f in dataclasses.fields(cls))
HIDDEN = re.compile(r"<hidden (\d{1,6}): kept as is>")
HIDDEN_COMMENT = re.compile(r"# <hidden comment (\d{1,6}): kept as is>")


def _server_key(path: tuple) -> str | None:
    return str(path[3]) if len(path) == 4 and path[:2] == ("tools", "servers") else None


def _secret_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return True
    if parts.username or parts.password:
        return True
    if any(TOKEN_LIKE.fullmatch(segment) for segment in parts.path.split("/") if segment):
        return True              # a token in the path (a webhook's address)
    return any(SECRET_NAME.search(k) or TOKEN_LIKE.fullmatch(v)
               for k, v in parse_qsl(parts.query, keep_blank_values=True))


def _secret_arg(args: list[Any], i: int) -> bool:
    item = args[i]
    if not isinstance(item, str) or not item:
        return False
    if SECRET_NAME.search(item) and re.search(r"[=:]\s*\S", item):
        return True              # --api-key=sk-…, Authorization: Bearer …
    before = args[i - 1] if i > 0 else None
    return isinstance(before, str) and SECRET_FLAG.match(before) is not None and not item.startswith("-")


def secret_paths(data: dict[str, Any]) -> list[tuple]:
    """Where config.toml's parsed `data` holds a secret: every `env` value of a tool server, any string under a key
    named like one (token, secret, password, api_key, auth…), an argument that carries one (`--token=…`, or the
    word after `--api-key`), and an address with a login part or a token in its query (a server's url, Ollama's)."""
    found: list[tuple] = []

    def walk(node: Any, path: tuple, secret: bool) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                here = path + (key,)
                named = secret or (here not in _SETTINGS and bool(SECRET_NAME.search(str(key))))
                env = len(here) == 5 and here[:2] == ("tools", "servers") and here[3] == "env"
                if isinstance(value, str):
                    url = re.match(r"(?i)^[a-z][a-z0-9+.-]*://", value) is not None
                    run = _server_key(here) in ("command", "cwd") and _secret_arg([value], 0)
                    if value and (named or env or run or TOKEN_LIKE.fullmatch(value) or (url and _secret_url(value))):
                        found.append(here)
                elif (isinstance(value, (int, float)) and not isinstance(value, bool) and here not in _SETTINGS
                      and (secret or NUMERIC_SECRET.search(str(key)))):
                    found.append(here)   # a PIN or a numeric key under a secret's name (not a count of tokens)
                elif _server_key(here) == "args" and isinstance(value, list):
                    found.extend(here + (i,) for i in range(len(value)) if _secret_arg(value, i))
                else:
                    walk(value, here, named)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                if isinstance(item, str):
                    if item and (secret or TOKEN_LIKE.fullmatch(item)):
                        found.append(path + (i,))
                elif secret and isinstance(item, (int, float)) and not isinstance(item, bool):
                    found.append(path + (i,))
                else:
                    walk(item, path + (i,), secret)

    walk(data, (), False)
    return found


# A key named like a secret given a value (`api_key = "…"`, `"NOTES_TOKEN": …`, `Authorization: Bearer …`), and a flag
# named like one followed by a value (`--api-key sk-…`).
SECRET_ASSIGNED = re.compile(r"(?:token|secret|passw|api[_-]?key|auth|credential|bearer|cookie|private[_-]?key)"
                             r"[\w.-]*[\"']?\s*[=:]\s*\S", re.IGNORECASE)
SECRET_ARG = re.compile(r"--?[\w.-]*(?:token|secret|passw|api[_-]?key|auth|credential|bearer|cookie)[\w.-]*[\"']?"
                        r"(?:\s*,)?\s+[\"']?[^\s\"',\]-]", re.IGNORECASE)
# A word that looks like a key or a token: 20 or more letters, digits and _ with both letters and digits in it.
# Or one of 12 or more with dashes in it and at least three of each (sk-proj-…, ghp_…).
TOKEN_LIKE = re.compile(r"(?=[A-Za-z0-9_]*[0-9])(?=[A-Za-z0-9_]*[A-Za-z])[A-Za-z0-9_]{20,}"
                        r"|(?=(?:[A-Za-z_-]*[0-9]){3})(?=(?:[0-9_-]*[A-Za-z]){3})[A-Za-z0-9_-]{12,}")


def _secret_text(comment: str) -> bool:
    """Does a comment look like it holds a secret: a key named like one with a value (`# api_key = "…"`,
    `# Authorization: Bearer …`), an `env` table (whose values are all hidden when not commented out), an address
    with a login part or a token, or a word that looks like a token?"""
    if SECRET_ASSIGNED.search(comment) or SECRET_ARG.search(comment):
        return True
    if re.search(r"\benv\s*=\s*\{\s*[^}\s]", comment) or TOKEN_LIKE.search(comment):
        return True
    return any(_secret_url(url) for url in re.findall(r"[a-z][a-z0-9+.-]*://[^\s\"']+", comment, re.IGNORECASE))


def comment_spans(text: str) -> list[tuple[int, int]]:
    """Where TOML `text`'s comments are, as (start, end) offsets: each # outside a string to the end of its line
    (a line's \\r left out). A lexer of strings and comments over the whole text, as a TOML parser reads it: basic
    and literal strings, their multi-line forms (which can hold a # and a newline), and escapes; quoted keys are
    strings too. Nothing else in TOML can hold a #."""
    spans: list[tuple[int, int]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "#":
            end = text.find("\n", i)
            end = n if end < 0 else end
            spans.append((i, end - 1 if end > i and text[end - 1] == "\r" else end))
            i = end
        elif text.startswith('"""', i) or text.startswith("'''", i):
            quote = text[i:i + 3]
            j = i + 3
            while True:
                k = text.find(quote, j)
                if k < 0:
                    return spans                            # unterminated: the parser refuses it anyway
                if quote == '"""' and _escaped(text, k):
                    j = k + 1
                    continue
                i = k + 3
                while i < n and text[i] == quote[0] and i < k + 5:   # up to two quotes more belong to the string
                    i += 1
                break
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if c == '"' and text[j] == "\\" else 1
            i = j + 1
        else:
            i += 1
    return spans


def _escaped(text: str, at: int) -> bool:
    """Is the character at `at` escaped (an odd run of backslashes before it)?"""
    run = 0
    while at - run - 1 >= 0 and text[at - run - 1] == "\\":
        run += 1
    return run % 2 == 1


_SHIPPED: frozenset[str] | None = None


def _shipped_lines() -> frozenset[str]:
    global _SHIPPED
    if _SHIPPED is None:
        _SHIPPED = frozenset(line.strip() for line in cfg.default_toml().splitlines() if line.strip())
    return _SHIPPED


class Vault:
    """One page's hidden values, in memory only: each secret, and each comment line that names one, gets a number
    the first time it is seen, and the masked text carries the number instead. The page sends the masked text back
    and `unmask` puts the values in again; a value the user typed in the draft is hidden from then on too."""

    MAX = 2000

    def __init__(self) -> None:
        self.lock = threading.Lock()      # the page's checks run in worker threads
        self.values: dict[int, Any] = {}        # a hidden value as it was: a string, or a number under a secret's name
        self.numbers: dict[Any, int] = {}
        self.lines: dict[int, str] = {}
        self.line_numbers: dict[str, int] = {}
        self.where: dict[int, set[tuple]] = {}   # where each hidden value was shown: it goes back there and only there

    def _hide(self, value: Any, path: tuple) -> str:
        n = self._number(value, self.values, self.numbers)
        self.where.setdefault(n, set()).add(tuple(path))
        return f"<hidden {n}: kept as is>"

    def _number(self, value: Any, values: dict[int, Any], numbers: dict[Any, int]) -> int:
        key = (type(value).__name__, value)
        if key not in numbers:
            if len(self.values) + len(self.lines) >= self.MAX:
                raise ConfigError("too many hidden values on this page; reload it")
            n = len(self.values) + len(self.lines) + 1
            numbers[key], values[n] = n, value
        return numbers[key]

    def mask(self, text: str) -> str:
        """`text` with every secret as `<hidden N: kept as is>`. Raises ConfigError when it does not parse (then
        nothing of it can be shown safely)."""
        with self.lock:
            return self._mask(text)

    def unmask(self, text: str) -> str:
        """The masked `text` with the hidden values in again. Raises ConfigError for a hidden value this page never
        gave out, or one with text added to it (type the whole new value instead)."""
        with self.lock:
            return self._unmask(text)

    def _mask(self, text: str) -> str:
        try:
            data = tomllib.loads(text)
            doc = tomlkit.parse(text)
        except (tomllib.TOMLDecodeError, tomlkit.exceptions.TOMLKitError) as exc:
            raise ConfigError(f"config.toml does not parse: {_first_line(exc)}") from None
        try:
            # Where the secrets are comes from the parsed document (tomllib, the daemon's own reader); they are
            # replaced in tomlkit's, which writes the text back as it was.
            for path in secret_paths(data):
                node: Any = doc
                for part in path[:-1]:
                    node = node[part]
                value = _unwrap(node[path[-1]])
                if isinstance(value, str) and HIDDEN.fullmatch(value):
                    continue
                node[path[-1]] = self._hide(value, path)
        except (KeyError, IndexError, TypeError):   # the two parsers disagree on where a value is: show nothing
            raise ConfigError("config.toml cannot be shown here safely; edit it in a text editor (strawberry config)") from None
        # A secret moved somewhere its name does not say (a hidden value put in a plain key's place: the page may
        # do that, and the daemon puts the value back there) is hidden wherever it is: any string with one in it.
        known = self._known()

        def by_value(node: Any, path: tuple) -> None:
            for key in _keys(node):
                value = node[key]
                if isinstance(value, str):
                    plain = _plain(value)
                    if not HIDDEN.fullmatch(plain) and any(secret in plain for secret in known):
                        node[key] = self._hide(plain, path + (key,))
                elif isinstance(value, (dict, list)):
                    by_value(value, path + (key,))

        if known:
            by_value(doc, ())
        out = tomlkit.dumps(doc)
        # Comments, found as the parser finds them (comment_spans): one that looks like it holds a secret (a
        # commented-out token, a trailing note with a key in it) or holds a known one is hidden whole; the line's
        # key and value stay to edit. The shipped template's own lines are no secret.
        known = self._known()
        shipped = _shipped_lines()
        pieces, last = [], 0
        for start, stop in comment_spans(out):
            comment = out[start:stop]
            if HIDDEN_COMMENT.fullmatch(comment.strip()):
                continue
            line_start = out.rfind("\n", 0, start) + 1
            line_end = out.find("\n", stop)
            line = out[line_start:line_end if line_end >= 0 else len(out)].strip()
            if line in shipped or not (_secret_text(comment[1:]) or any(k in comment for k in known)):
                continue
            pieces += [out[last:start], f"# <hidden comment {self._number(comment, self.lines, self.line_numbers)}: kept as is>"]
            last = stop
        masked = "".join(pieces) + out[last:]
        self._verify(masked)
        return masked

    def _verify(self, masked: str) -> None:
        """The last word before anything is shown: the masked text parses, every place secret_paths names holds a
        placeholder, and no value or comment this page has hidden is anywhere in it (a key's name, a string the
        parsers read differently). Else nothing of it is shown."""
        why = ""
        try:
            data = tomllib.loads(masked)
        except tomllib.TOMLDecodeError:
            why = "it does not parse once its secrets are hidden"
        else:
            for path in secret_paths(data):
                node: Any = data
                for part in path:
                    node = node[part]
                if not HIDDEN.fullmatch(str(node)):
                    why = "a secret could not be hidden where it is"
            shipped = _shipped_lines()
            hidden = self._known() + [c for c in self.lines.values() if len(c) >= MIN_SECRET
                                      and not any(c.strip() in line for line in shipped)]
            if any(h in masked for h in hidden):
                why = "a secret is in a place it cannot be hidden in (a key's name)"
        if why:
            raise ConfigError(f"config.toml cannot be shown here safely: {why}; edit it in a text editor "
                              "(strawberry config)")

    def _known(self) -> list[str]:
        """The secrets this page has hidden that are long enough to look for inside other text (a number as written)."""
        texts = {v for v in self.values.values() if isinstance(v, str) and len(v) >= MIN_SECRET}
        texts |= {str(v) for v in self.values.values() if isinstance(v, (int, float)) and not isinstance(v, bool)
                  and len(str(v)) >= MIN_NUMBER}
        return sorted(texts, key=len, reverse=True)

    def scrub(self, text: str) -> str:
        """`text` (a message for the page or her card: a problem, a reason) with every secret this page has hidden
        as its placeholder. A startup check may quote a value, and a hidden value may have been moved where it does."""
        with self.lock:
            for secret in self._known():
                n = next(n for n, v in self.values.items() if str(v) == secret)
                # As written, and as repr() and JSON would quote it (a message may carry either).
                for form in dict.fromkeys((secret, repr(secret)[1:-1], json.dumps(secret)[1:-1])):
                    if form and form in text:
                        text = text.replace(form, f"<hidden {n}: kept as is>")
            return text

    def _unmask(self, text: str) -> str:
        # Hidden comments come back only where they are comments (as the parser finds them): one moved into a
        # string or a key is refused, so a comment's text never turns into a value shown elsewhere.
        pieces, last = [], 0
        spans = comment_spans(text)
        for start, stop in spans:
            match = HIDDEN_COMMENT.fullmatch(text[start:stop].rstrip())
            if match is None:
                continue
            n = int(match.group(1))
            if n not in self.lines:
                raise ConfigError("a hidden comment is not one this page showed; delete it")
            pieces += [text[last:start], self.lines[n]]
            last = stop
        rest = "".join(pieces) + text[last:]
        outside = text
        for start, stop in reversed(spans):
            outside = outside[:start] + outside[stop:]
        if HIDDEN_COMMENT.search(outside):
            raise ConfigError("a hidden comment was moved out of a comment; put it back or delete it")
        text = rest
        if not HIDDEN.search(text):
            return text
        try:
            doc = tomlkit.parse(text)
        except tomlkit.exceptions.TOMLKitError as exc:
            raise ConfigError(f"the draft does not parse: {_first_line(exc)}") from None

        def put_back(node: Any, path: tuple) -> None:
            for key in _keys(node):
                value = node[key]
                here = path + (key,)
                if isinstance(value, str):
                    plain = _plain(value)
                    match = HIDDEN.fullmatch(plain)
                    if match:
                        n = int(match.group(1))
                        if n not in self.values:
                            raise ConfigError("a hidden value is not one this page showed; type the value in full")
                        if here not in self.where.get(n, ()):
                            # Moved: a hidden value goes back only where it was shown, never into another key (where
                            # it might be sent somewhere, or shown).
                            raise ConfigError(f"a hidden value was moved to {'.'.join(map(str, here))}; type the value "
                                              "there in full, or put the placeholder back where it was")
                        node[key] = self.values[n]
                    elif HIDDEN.search(plain):
                        raise ConfigError("a hidden value was changed by hand; type the whole new value in its place")
                elif isinstance(value, (dict, list)):
                    put_back(value, here)

        put_back(doc, ())
        out = tomlkit.dumps(doc)
        if HIDDEN.search(out):
            raise ConfigError("a hidden value is in a place it cannot go back to (a key's name or a comment)")
        return out


MIN_SECRET = 4      # shorter hidden values are not looked for inside other text (too many false matches)
MIN_NUMBER = 6      # …and shorter hidden numbers (a port could be one)


def _keys(node: Any) -> Any:
    if isinstance(node, (dict, Container, Table, OutOfOrderTableProxy)):
        return list(node.keys())
    return range(len(node)) if isinstance(node, list) else ()


def _plain(value: Any) -> str:
    return str(value.unwrap() if hasattr(value, "unwrap") else value)


def _first_line(exc: BaseException) -> str:
    return str(exc).strip().split("\n")[0]


# --------------------------------------------------------------------------------------------- the draft


class EditError(ConfigError):
    pass


SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
SERVER_KEYS = ("topic", "command", "url", "args", "env", "cwd", "careful", "confirm", "adapter", "flags", "offer",
               "workspaces")
MAX_EDITS = 50


def _target(key: Any) -> tuple[str, str]:
    """An edit's key: "section.field" of the schema, or "tools.servers.<name>" (a whole server's table)."""
    if not isinstance(key, str) or len(key) > 80:
        raise EditError("an edit's key must be \"section.key\"")
    if key.startswith("tools.servers."):
        name = key[len("tools.servers."):]
        if not SERVER_NAME.match(name):
            raise EditError("a server's name has letters, digits, - and _ only (at most 40)")
        return "tools.servers", name
    section, _, field = key.partition(".")
    cls = cfg._SECTIONS.get(section)
    if cls is None or field not in {f.name for f in dataclasses.fields(cls)}:
        raise EditError(f"{key} is not a setting")
    if key in ELSEWHERE:
        raise EditError(f"{key} is edited on {ELSEWHERE[key][1]}, not here")
    return section, field


def _section(doc: Any, name: str) -> tuple[Any, Any]:
    """(the document, `[name]` in it to set keys in), the section made when it is missing. A section that is there
    only as the parent of others (`[tools.servers.x]` with no `[tools]`) gets a header of its own at the end of the
    file (a new document): tomlkit would otherwise write one above the first child, with that child's comment."""
    item = doc.get(name)
    if item is None:
        if len(doc.body) and not tomlkit.dumps(doc).endswith("\n\n"):
            doc.add(tomlkit.nl())
        doc.add(name, tomlkit.table())
        return doc, doc[name]
    if isinstance(item, Table) and item.is_super_table():
        doc = tomlkit.parse(tomlkit.dumps(doc).rstrip("\n") + f"\n\n[{name}]\n")
        return doc, doc[name]
    return doc, item


def _same(a: Any, b: Any) -> bool:
    return json.dumps(_unwrap(a), sort_keys=True) == json.dumps(_unwrap(b), sort_keys=True)


def _unwrap(value: Any) -> Any:
    return value.unwrap() if hasattr(value, "unwrap") else value


def _toml(value: Any) -> Any:
    """A JSON value as tomlkit should write it: a table inside a section as an inline table."""
    if isinstance(value, dict):
        table = tomlkit.inline_table()
        for k, v in value.items():
            table[str(k)] = _toml(v)
        return table
    if isinstance(value, list):
        array = tomlkit.array()
        array.extend(_toml(v) for v in value)
        return array
    return value


def _put(table: Any, key: str, value: Any) -> None:
    """`key = value` in `table`, the line (and its comment) kept when the value is the same; a dict over a table
    of its own (`[gate.examples]`) fills that table in place."""
    current = table.get(key)
    if current is not None and _same(current, value):
        return
    if isinstance(value, dict) and isinstance(current, Table):
        for k in [k for k in current if k not in value]:
            del current[k]
        for k, v in value.items():
            _put(current, str(k), v)
        return
    table[key] = _toml(value)


def _value_for(section: str, field: str, value: Any) -> Any:
    """The form's JSON value as config.toml holds it: an int where a float is meant becomes one (2 -> 2.0)."""
    default = getattr(cfg._SECTIONS[section](), field)
    if isinstance(default, float) and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if value is None:
        raise EditError(f"{section}.{field} needs a value (or reset it to the default)")
    return value


def apply_edits(text: str, edits: Any) -> str:
    """The form's edits on config.toml's text: each {"key": "section.key", "value": …} sets a key, with "reset":
    true it is taken out (the default again), and "tools.servers.<name>" sets (or, reset, takes out) a whole
    server's table. Everything else in the file stays as it was written, comments and order included."""
    if not isinstance(edits, list) or len(edits) > MAX_EDITS:
        raise EditError(f"edits must be a list of at most {MAX_EDITS}")
    try:
        doc = tomlkit.parse(text)
    except tomlkit.exceptions.TOMLKitError as exc:
        raise ConfigError(f"the draft does not parse: {_first_line(exc)}") from None
    for edit in edits:
        if not isinstance(edit, dict):
            raise EditError("an edit is {\"key\": …, \"value\": …} or {\"key\": …, \"reset\": true}")
        section, field = _target(edit.get("key"))
        reset = edit.get("reset") is True
        if section == "tools.servers":
            _edit_server(doc, field, None if reset else edit.get("value"))
            continue
        if reset:
            table = doc.get(section)
            if table is not None and field in table:
                del table[field]
            continue
        value = _value_for(section, field, edit.get("value"))
        doc, table = _section(doc, section)
        _put(table, field, value)
    return tomlkit.dumps(doc)


def _edit_server(doc: Any, name: str, value: Any) -> None:
    tools = doc.get("tools")
    servers = tools.get("servers") if tools is not None else None
    if value is None:
        if servers is not None and name in servers:
            del servers[name]
        return
    if not isinstance(value, dict) or not value:
        raise EditError("a server is a table: command or url, and the rest")
    unknown = sorted(set(value) - set(SERVER_KEYS))
    if unknown:
        raise EditError(f"tools.servers.{name}: unknown keys {unknown}")
    value = {k: v for k, v in value.items() if v not in (None, "", [], {}) or k in ("confirm", "workspaces", "flags")}
    if servers is not None and name in servers and isinstance(servers[name], Table):
        current = servers[name]
        for k in [k for k in current if k not in value]:
            del current[k]
        for k, v in value.items():
            _put(current, k, v)
        return
    table = tomlkit.table()
    for k in SERVER_KEYS:
        if k in value:
            table[k] = _toml(value[k])
    table.add(tomlkit.nl())
    if servers is None:
        if not tomlkit.dumps(doc).endswith("\n\n") and len(doc.body):
            doc.add(tomlkit.nl())
        holder = tomlkit.table(is_super_table=True)
        holder[name] = table
        if tools is None:
            parent = tomlkit.table(is_super_table=True)
            parent["servers"] = holder
            doc["tools"] = parent
        else:
            tools["servers"] = holder
        return
    servers[name] = table


# --------------------------------------------------------------------------------------------- reading a draft


def load_text(text: str) -> cfg.Config:
    """The draft as the daemon would read it at start (config.toml alone; env and input.toml left out)."""
    return configedit.check_config_text(text)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# The page gets a keyed hash of the file (`tag`), never its plain sha256: a plain hash of a small file with one
# unknown token in it could be tested against guesses. The key is new at each start, so a tag is good until then.
_PAGE_KEY = secrets.token_bytes(32)


def tag(text: str) -> str:
    return hmac.new(_PAGE_KEY, text.encode("utf-8"), hashlib.sha256).hexdigest()


def base_of(path: Path) -> dict[str, Any]:
    """What the page loaded: the file's text, its hash (kept here), its tag (the page's) and its mtime (none when there
    is no file yet)."""
    try:
        raw = path.read_bytes()
        stat = path.stat()
    except FileNotFoundError:
        return {"exists": False, "text": "", "sha256": sha256(""), "tag": tag(""), "mtime_ns": None}
    text = raw.decode("utf-8")
    return {"exists": True, "text": text, "sha256": sha256(text), "tag": tag(text), "mtime_ns": stat.st_mtime_ns}


def changed_sections(old: cfg.Config, new: cfg.Config) -> list[str]:
    """The sections whose settings the change touches, as the daemon reads them (a comment, or a key written out
    with its default, is no change)."""
    before, after = old.to_dict(), new.to_dict()
    return [name for name in cfg._SECTIONS
            if json.dumps(before[name], sort_keys=True, default=str) != json.dumps(after[name], sort_keys=True, default=str)]


# --------------------------------------------------------------------------------------------- safety


@dataclass(frozen=True)
class Finding:
    """One way an edit loosens safety: the setting and why, for the page and her card."""

    key: str
    why: str
    category: str = "approvals"    # runs | egress | approvals | privacy | gestures (CATEGORIES, most severe first)
    detail: str = ""               # for her card: a server's command line or address

    def view(self) -> dict[str, str]:
        return {"key": self.key, "why": self.why, "category": self.category, "detail": self.detail}


BODY_RANK = {mode: i for i, mode in enumerate(cfg.BODY_MODES)}          # off < react < glance
OFFER_RANK = {"asked": 0, "topic": 1, "always": 2}
WILDCARDS = ("", "0.0.0.0", "::", "*", "[::]")
# The kinds of loosening, most severe first: her card names each kind there is (configui._card_line).
CATEGORIES = ("runs", "egress", "approvals", "privacy", "gestures")


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _local(host: str) -> bool:
    from .remote import is_loopback   # local: remote brings the SDK's HTTP client

    return is_loopback(host)


def _server_view(name: str, table: dict[str, Any]) -> dict[str, Any]:
    """What a server's table means for safety: what runs or where it connects, its flags, its confirm and careful
    lists, its offer and its workspaces, with an adapter's defaults where the table says nothing."""
    from . import trust
    from .adapters import adapter_for   # local: the adapters import the core

    adapter = adapter_for(name, table)
    return {"runs": {k: table.get(k) for k in ("command", "args", "env", "cwd", "url")},
            "adapter": getattr(adapter, "name", None),
            "flags": trust.flags_for(adapter, table),
            "confirm": frozenset(table["confirm"] if "confirm" in table else getattr(adapter, "confirm", ()) or ()),
            "careful": frozenset(table.get("careful", ())),
            "offer": table.get("offer") or getattr(adapter, "offer", "") or "always",
            "workspaces": frozenset(table.get("workspaces") or ())}


def command_line(table: dict[str, Any]) -> str:
    """A server's command and its arguments as one line (for the page and her card, scrubbed by the caller), or its
    address."""
    if table.get("command"):
        args = [str(a) for a in table.get("args") or [] if isinstance(a, (str, int, float))]
        return " ".join([str(table["command"]), *args])
    return str(table.get("url", ""))


def tier(config: cfg.Config, server: str, tool: str, destructive: frozenset[str] = frozenset()) -> str:
    """The approval tier `tool` of `server` has under `config`, by Server.risk's own precedence (tools.py): an
    `[approvals] risk` entry for the tool as written; else the adapter's tier, else `change`, never below the
    server's destructiveHint (`destructive`) or the adapter's tier, which a whole-server entry also never goes under.
    `tool` "" stands for an ordinary tool of the server that nothing names."""
    from .adapters import adapter_for

    risk = config.approvals.risk
    own = risk.get(f"{server}.{tool}") if tool else None
    if own in cfg.RISKS:
        return own
    table = config.tools.servers.get(server)
    adapter = adapter_for(server, table if isinstance(table, dict) else {})
    declared = adapter.risk(tool) if adapter is not None and tool else None
    declared = declared if declared in cfg.RISKS else None
    floor = "destructive" if tool in destructive else "read"
    if declared is not None and cfg.RISKS.index(declared) > cfg.RISKS.index(floor):
        floor = declared
    whole = risk.get(server)
    chosen = whole if whole in cfg.RISKS else (declared or "change")
    return chosen if cfg.RISKS.index(chosen) >= cfg.RISKS.index(floor) else floor


def _tools_of(old: cfg.Config, new: cfg.Config, server: str, known: dict[str, Any]) -> set[str]:
    """The tools of `server` worth comparing: those it lists (the toolbox's, when connected), those `[approvals] risk`
    names in either config, the adapter's own named ones, and "" for an ordinary tool nothing names."""
    from .adapters import adapter_for

    names = {""} | set((known.get(server) or {}).get("listed", ()))
    for config in (old, new):
        names |= {key.partition(".")[2] for key in config.approvals.risk if key.startswith(f"{server}.")}
        table = config.tools.servers.get(server)
        adapter = adapter_for(server, table if isinstance(table, dict) else {})
        if adapter is not None:
            names |= set(getattr(adapter, "risks", {}) or {}) | set(getattr(adapter, "confirm", ()) or ())
    return names


def lowering(old: cfg.Config, new: cfg.Config, known: dict[str, Any] | None = None) -> list[Finding]:
    """What `new` loosens against `old` (both as config.load reads them): the edits her approval card must confirm.
    `known` is what the toolbox knows of each server now: {"listed": its tool names, "destructive": the ones it marks
    destructiveHint}, for the tiers.

    runs: a tool server that is new, or runs another command (args, env, cwd); its adapter changed.
    egress: a server that connects elsewhere (url); the daemon listening beyond this machine or on every address;
      the models' Ollama on another machine.
    approvals: tools switched on; a trust flag gone (trust.py); a tool off its confirm or careful list; tools offered
      more readily (asked < topic < always); an effective tier lowered (`tier`, with Server.risk's precedence); a
      tier taken off `[approvals] hold`; a longer wait for a yes; strangers' text making changes ask for less time
      (the shorter of the ledger's window and foreign_minutes) or kept out of the ledger (notices = 0); a thumb
      answering her card.
    privacy: message bodies read more (off < react < glance, per app too), longer, from more apps; sentences kept in
      the log or the outcome file; DEBUG logging; the inbox switched on; recall workspaces added.
    gestures: the camera always watching instead of armed; the face check, arming or the hold time loosened; the
      zone or the smallest hand made easier; command mode armed for longer.
    Everything else is neutral or raises safety."""
    known = known or {}
    out: list[Finding] = []

    def add(category: str, key: str, why: str, detail: str = "") -> None:
        out.append(Finding(key, why, category, detail))

    # --- what runs, and what leaves the machine
    for name in sorted(new.tools.servers):
        table = new.tools.servers[name]
        after = _server_view(name, table)
        if name not in old.tools.servers:
            if table.get("command"):
                add("runs", f"tools.servers.{name}", f"a new tool server {name} (runs {command_line(table)})",
                    command_line(table))
            else:
                add("egress", f"tools.servers.{name}", f"a new tool server {name} (connects to {table.get('url', '')})",
                    str(table.get("url", "")))
            continue
        before = _server_view(name, old.tools.servers[name])
        for part, said in (("command", "its command"), ("args", "its arguments"), ("env", "its environment"),
                           ("cwd", "its folder")):
            if before["runs"][part] != after["runs"][part]:
                add("runs", f"tools.servers.{name}.{part}", f"tool server {name}: {said} changed "
                    f"(runs {command_line(table)})", command_line(table))
        if before["runs"]["url"] != after["runs"]["url"]:
            add("egress", f"tools.servers.{name}.url", f"tool server {name}: its address changed ({table.get('url')})",
                str(table.get("url", "")))
        if before["adapter"] != after["adapter"]:
            add("runs", f"tools.servers.{name}.adapter", f"tool server {name}: its adapter changed")
        gone = sorted(before["flags"] - after["flags"])
        if gone:
            add("approvals", f"tools.servers.{name}.flags", f"tool server {name} would no longer count as "
                f"{' or '.join(gone)}")
        for listed, said in (("confirm", "asked about first"), ("careful", "held back as careful")):
            dropped = sorted(before[listed] - after[listed])
            if dropped:
                add("approvals", f"tools.servers.{name}.{listed}", f"{name}: {', '.join(dropped)} would no longer be "
                    f"{said}")
        if OFFER_RANK.get(after["offer"], 2) > OFFER_RANK.get(before["offer"], 2):
            add("approvals", f"tools.servers.{name}.offer", f"tool server {name} offered {after['offer']} instead of "
                f"{before['offer']}")
        wider = sorted(after["workspaces"] - before["workspaces"])
        if wider:
            add("privacy", f"tools.servers.{name}.workspaces", f"{name} could read the workspace(s) {', '.join(wider)}")
    host = new.daemon.host.strip().lower()
    if host != old.daemon.host.strip().lower() and (host in WILDCARDS or (_local(old.daemon.host) and not _local(host))):
        add("egress", "daemon.host", f"the daemon would listen beyond this machine ({new.daemon.host or 'every address'})",
            f"listening on {new.daemon.host or 'every address'}")
    if new.brain.ollama_url != old.brain.ollama_url and not _local(_host_of(new.brain.ollama_url)):
        add("egress", "brain.ollama_url", f"the models would run on another machine "
            f"({_host_of(new.brain.ollama_url) or new.brain.ollama_url})", _host_of(new.brain.ollama_url))

    # --- approvals
    if new.tools.enabled and not old.tools.enabled:
        add("approvals", "tools.enabled", "the tool servers would be switched on")
    for server in sorted(set(old.tools.servers) & set(new.tools.servers)):
        destructive = frozenset((known.get(server) or {}).get("destructive", ()))
        for tool in sorted(_tools_of(old, new, server, known)):
            before, after = tier(old, server, tool, destructive), tier(new, server, tool, destructive)
            if cfg.RISKS.index(after) < cfg.RISKS.index(before):
                key = f"{server}.{tool}" if tool else server
                add("approvals", f"approvals.risk.{key}", f"{key if tool else f'{server} (its other tools)'}: "
                    f"{before} -> {after} (asks less)")
    for removed in sorted(set(old.approvals.hold) - set(new.approvals.hold)):
        add("approvals", "approvals.hold", f"a yes to a {removed} call would no longer need a hold on her card")
    for key in ("change_s", "sends_s", "destructive_s", "grace_s"):
        if getattr(new.approvals, key) > getattr(old.approvals, key):
            add("approvals", f"approvals.{key}", f"her card would wait longer for a yes ({key} "
                f"{getattr(old.approvals, key):g} -> {getattr(new.approvals, key):g} s)")
    was = min(old.ledger.window_minutes, old.ledger.foreign_minutes)
    now = min(new.ledger.window_minutes, new.ledger.foreign_minutes)
    if now < was:
        add("approvals", "ledger.foreign_minutes", f"strangers' text would make changes ask for {now:g} min instead "
            f"of {was:g}")
    if old.ledger.notices > 0 and new.ledger.notices == 0:
        add("approvals", "ledger.notices", "what she reacts to (strangers' text included) would leave her short memory, "
            "so it would no longer make changes ask")
    if new.gestures.approvals and not old.gestures.approvals:
        add("approvals", "gestures.approvals", "a thumb, a nod or a shake would answer her card")

    # --- privacy
    apps = set(old.notifications.body_apps) | set(new.notifications.body_apps)
    for app in sorted(apps | {""}):
        before = old.notifications.mode_for(app) if app else old.notifications.body
        after = new.notifications.mode_for(app) if app else new.notifications.body
        if BODY_RANK[after] > BODY_RANK[before]:
            key = f"notifications.body_apps.{app}" if app else "notifications.body"
            add("privacy", key, f"message bodies{f' from {app}' if app else ''}: {before} -> {after} (she reads more of "
                "them)")
    if new.notifications.max_body_chars > old.notifications.max_body_chars:
        add("privacy", "notifications.max_body_chars", f"more of each message body would be read "
            f"({old.notifications.max_body_chars} -> {new.notifications.max_body_chars} characters)")
    unignored = sorted(set(old.notifications.ignore_apps) - set(new.notifications.ignore_apps))
    if unignored:
        add("privacy", "notifications.ignore_apps", f"notifications from {', '.join(unignored)} would reach her")
    if old.notifications.only_apps and (not new.notifications.only_apps
                                        or set(new.notifications.only_apps) - set(old.notifications.only_apps)):
        more = sorted(set(new.notifications.only_apps) - set(old.notifications.only_apps))
        add("privacy", "notifications.only_apps", "notifications from " + (", ".join(more) if more else "every app")
            + " would reach her")
    if new.daemon.log_sentences and not old.daemon.log_sentences:
        add("privacy", "daemon.log_sentences", "what you say or type would be written to the log")
    if new.daemon.log_level.upper() == "DEBUG" and old.daemon.log_level.upper() != "DEBUG":
        add("privacy", "daemon.log_level", "the log would say more (DEBUG)")
    if new.learning.log_outcomes and not old.learning.log_outcomes:
        add("privacy", "learning.log_outcomes", "the sentences you say or type would be kept in the outcome file")
    if new.messages.enabled and not old.messages.enabled:
        add("privacy", "messages.enabled", "her inbox would keep your notifications for the big model to read")

    # --- gestures (the camera is the watcher's: these make a gesture easier to make by accident, or watch longer)
    g0, g1 = old.gestures, new.gestures
    if g0.watch == "armed" and g1.watch == "always":
        add("gestures", "gestures.watch", "the camera would watch all the time instead of only when armed")
    if g0.require_facing and not g1.require_facing:
        add("gestures", "gestures.require_facing", "a gesture would count even when you do not face the screen")
    if g0.arming and not g1.arming:
        add("gestures", "gestures.arming", "gestures would act without the open palm arming them first")
    if g1.hold_ms < g0.hold_ms:
        add("gestures", "gestures.hold_ms", f"a shape would count after {g1.hold_ms:g} ms instead of {g0.hold_ms:g}")
    if g1.zone > g0.zone:
        add("gestures", "gestures.zone", f"a lower hand would count (zone {g0.zone:g} -> {g1.zone:g})")
    if g1.min_size < g0.min_size:
        add("gestures", "gestures.min_size", f"a smaller (farther) hand would count ({g0.min_size:g} -> {g1.min_size:g})")
    if g1.armed_s > g0.armed_s:
        add("gestures", "gestures.armed_s", f"command mode would stay armed longer ({g0.armed_s:g} -> {g1.armed_s:g} s)")
    seen: set[tuple[str, str]] = set()
    unique = []
    for finding in out:
        if (finding.key, finding.why) not in seen:
            seen.add((finding.key, finding.why))
            unique.append(finding)
    return sorted(unique, key=lambda f: CATEGORIES.index(f.category))


def merged(*lists: list[Finding]) -> list[Finding]:
    """Findings from several readings (the file alone, and with input.toml over it), each once, most severe first."""
    seen: dict[tuple[str, str], Finding] = {}
    for found in lists:
        for finding in found:
            seen.setdefault((finding.key, finding.why), finding)
    return sorted(seen.values(), key=lambda f: CATEGORIES.index(f.category))


def refused(old: cfg.Config, new: cfg.Config) -> list[str]:
    """What this editor never writes: a camera switched on or off (only its explicit switches do that), and an Ollama
    address with a login part, a backslash or a space in it (remote.url_problem's reading of an address)."""
    problems = []
    if old.gestures.enabled != new.gestures.enabled:
        problems.append("[gestures] enabled is switched with the camera switch on the Input tab (or the tray's Hand "
                        "gestures row), not from here: put it back as it was")
    url = new.brain.ollama_url
    try:
        parts = urlsplit(url)
        login = parts.username is not None or parts.password is not None
    except ValueError:
        login = True
    if "\\" in url or any(c.isspace() for c in url) or login:
        problems.append("brain.ollama_url must be one plain address, without a login part")
    return problems


# --------------------------------------------------------------------------------------------- writing


class Stale(ConfigError):
    """The file changed after the page loaded it."""


KEEP_BACKUPS = 10


def write(path: Path, text: str, base_tag: str) -> Path | None:
    """`text` as config.toml if the file is still the one the page loaded (its `tag`): under the file's lock (as
    configedit's writers take it) the tag is compared, the old file is copied to config.toml.bak-<time> (made 0600
    before a byte goes in), and the new one is written to a fresh temporary file (0600, O_EXCL) and put in place in
    one step. config.toml may hold tokens, so it is the user's alone (0600). The last KEEP_BACKUPS backups stay.
    Returns the backup (None for a new file). Raises Stale when the file changed, ConfigError when `text` does not
    load or still has a placeholder."""
    if HIDDEN.search(text) or HIDDEN_COMMENT.search(text):
        raise ConfigError("the text still has a hidden placeholder in it; nothing written")
    load_text(text)
    with configedit.locked(path):
        now = base_of(path)
        if not hmac.compare_digest(now["tag"], base_tag):
            raise Stale("config.toml changed since this page loaded it (another editor, the tray or the camera switch)")
        backup = _private_backup(path, now["text"]) if now["exists"] else None
        write_private(path, text)
        _prune_backups(path)
    return backup


def write_private(path: Path, text: str) -> None:
    """`text` as the whole of `path`, 0600, in one step: a temporary file of its own beside it (mkstemp: O_EXCL, a
    random name, 0600), flushed, then renamed over it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    if os.name == "posix":
        os.chmod(path, 0o600)


def _private_backup(path: Path, text: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for n in range(1000):
        target = path.with_name(f"{path.name}.bak-{stamp}" + (f"-{n}" if n else ""))
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # 0600 from the start
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return target
    raise ConfigError("no free backup name")


def _prune_backups(path: Path) -> None:
    pattern = re.compile(re.escape(path.name) + r"\.bak-\d{8}-\d{6}(?:-\d+)?$")
    found = sorted((p for p in path.parent.iterdir() if pattern.match(p.name)), key=lambda p: p.stat().st_mtime_ns)
    for old in found[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
