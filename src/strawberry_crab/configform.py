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
import re
import threading
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
SECRET_NAME = re.compile(r"token|secret|passw|api[_-]?key|auth|credential|bearer|cookie|private[_-]?key", re.IGNORECASE)
SECRET_FLAG = re.compile(r"^-{1,2}[\w.-]*(?:token|secret|passw|api[_-]?key|auth|credential|bearer|cookie)[\w.-]*$",
                         re.IGNORECASE)
HIDDEN = re.compile(r"<hidden (\d{1,6}): kept as is>")
HIDDEN_LINE = re.compile(r"^(\s*)# <hidden comment (\d{1,6}): kept as is>\s*$", re.MULTILINE)


def _server_key(path: tuple) -> str | None:
    return str(path[3]) if len(path) == 4 and path[:2] == ("tools", "servers") else None


def _secret_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return True
    if parts.username or parts.password:
        return True
    return any(SECRET_NAME.search(k) for k, _v in parse_qsl(parts.query, keep_blank_values=True))


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
                named = secret or bool(SECRET_NAME.search(str(key)))
                env = len(here) == 5 and here[:2] == ("tools", "servers") and here[3] == "env"
                if isinstance(value, str):
                    url = re.match(r"(?i)^[a-z][a-z0-9+.-]*://", value) is not None
                    if value and (named or env or (url and _secret_url(value))):
                        found.append(here)
                elif _server_key(here) == "args" and isinstance(value, list):
                    found.extend(here + (i,) for i in range(len(value)) if _secret_arg(value, i))
                else:
                    walk(value, here, named)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                if isinstance(item, str):
                    if item and secret:
                        found.append(path + (i,))
                else:
                    walk(item, path + (i,), secret)

    walk(data, (), False)
    return found


def _secret_comment(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("#") and bool(SECRET_NAME.search(stripped)) and bool(re.search(r"[=:]\s*\S", stripped))


class Vault:
    """One page's hidden values, in memory only: each secret, and each comment line that names one, gets a number
    the first time it is seen, and the masked text carries the number instead. The page sends the masked text back
    and `unmask` puts the values in again; a value the user typed in the draft is hidden from then on too."""

    MAX = 2000

    def __init__(self) -> None:
        self.lock = threading.Lock()      # the page's checks run in worker threads
        self.values: dict[int, str] = {}
        self.numbers: dict[str, int] = {}
        self.lines: dict[int, str] = {}
        self.line_numbers: dict[str, int] = {}

    def _number(self, value: str, values: dict[int, str], numbers: dict[str, int]) -> int:
        if value not in numbers:
            if len(self.values) + len(self.lines) >= self.MAX:
                raise ConfigError("too many hidden values on this page; reload it")
            n = len(self.values) + len(self.lines) + 1
            numbers[value], values[n] = n, value
        return numbers[value]

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
        for path in secret_paths(data):
            node: Any = doc
            for part in path[:-1]:
                node = node[part]
            value = _plain(node[path[-1]])
            if HIDDEN.fullmatch(value):
                continue
            node[path[-1]] = f"<hidden {self._number(value, self.values, self.numbers)}: kept as is>"
        # A secret moved somewhere its name does not say (a hidden value put in a plain key's place: the page may
        # do that, and the daemon puts the value back there) is hidden wherever it is: any string with one in it.
        known = self._known()

        def by_value(node: Any) -> None:
            for key in _keys(node):
                value = node[key]
                if isinstance(value, str):
                    plain = _plain(value)
                    if not HIDDEN.fullmatch(plain) and any(secret in plain for secret in known):
                        node[key] = f"<hidden {self._number(plain, self.values, self.numbers)}: kept as is>"
                elif isinstance(value, (dict, list)):
                    by_value(value)

        if known:
            by_value(doc)
        known = self._known()
        lines = tomlkit.dumps(doc).split("\n")
        for i, line in enumerate(lines):
            if HIDDEN_LINE.match(line):
                continue
            # A comment that names a secret, and any line a known secret is still in (a trailing comment, a key's
            # name): hidden whole, and put back as it was.
            if _secret_comment(line) or any(secret in line for secret in known):
                indent = line[: len(line) - len(line.lstrip())]
                lines[i] = f"{indent}# <hidden comment {self._number(line, self.lines, self.line_numbers)}: kept as is>"
        return "\n".join(lines)

    def _known(self) -> list[str]:
        """The secrets this page has hidden that are long enough to look for inside other text."""
        return sorted((v for v in self.values.values() if len(v) >= MIN_SECRET), key=len, reverse=True)

    def scrub(self, text: str) -> str:
        """`text` (a message for the page or her card: a problem, a reason) with every secret this page has hidden
        as its placeholder. A startup check may quote a value, and a hidden value may have been moved where it does."""
        with self.lock:
            for secret in self._known():
                if secret in text:
                    text = text.replace(secret, f"<hidden {self.numbers[secret]}: kept as is>")
            return text

    def _unmask(self, text: str) -> str:
        lines = text.split("\n")
        for i, line in enumerate(lines):
            match = HIDDEN_LINE.match(line)
            if match:
                n = int(match.group(2))
                if n not in self.lines:
                    raise ConfigError("a hidden comment line is not one this page showed; delete that line")
                lines[i] = self.lines[n]
        text = "\n".join(lines)
        if not HIDDEN.search(text):
            return text
        try:
            doc = tomlkit.parse(text)
        except tomlkit.exceptions.TOMLKitError as exc:
            raise ConfigError(f"the draft does not parse: {_first_line(exc)}") from None

        def put_back(node: Any) -> None:
            for key in _keys(node):
                value = node[key]
                if isinstance(value, str):
                    plain = _plain(value)
                    match = HIDDEN.fullmatch(plain)
                    if match:
                        n = int(match.group(1))
                        if n not in self.values:
                            raise ConfigError("a hidden value is not one this page showed; type the value in full")
                        node[key] = self.values[n]
                    elif HIDDEN.search(plain):
                        raise ConfigError("a hidden value was changed by hand; type the whole new value in its place")
                elif isinstance(value, (dict, list)):
                    put_back(value)

        put_back(doc)
        out = tomlkit.dumps(doc)
        if HIDDEN.search(out):
            raise ConfigError("a hidden value is in a place it cannot go back to (a key's name or a comment)")
        return out


MIN_SECRET = 4      # shorter hidden values are not looked for inside other text (too many false matches)


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


def base_of(path: Path) -> dict[str, Any]:
    """What the page loaded: the file's text, its hash and its mtime (none when there is no file yet)."""
    try:
        raw = path.read_bytes()
        stat = path.stat()
    except FileNotFoundError:
        return {"exists": False, "text": "", "sha256": sha256(""), "mtime_ns": None}
    text = raw.decode("utf-8")
    return {"exists": True, "text": text, "sha256": sha256(text), "mtime_ns": stat.st_mtime_ns}


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

    def view(self) -> dict[str, str]:
        return {"key": self.key, "why": self.why}


BODY_RANK = {mode: i for i, mode in enumerate(cfg.BODY_MODES)}          # off < react < glance
OFFER_RANK = {"asked": 0, "topic": 1, "always": 2}


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


def _tool_tier(config: cfg.Config, key: str) -> str:
    """The tier `[approvals] risk` key `key` ("server.tool" or "server") would have without an entry: the adapter's
    own for a tool, else `change`."""
    from .adapters import adapter_for

    server, _, tool = key.partition(".")
    table = config.tools.servers.get(server)
    adapter = adapter_for(server, table) if isinstance(table, dict) else None
    declared = adapter.risk(tool) if adapter is not None and tool else None
    return declared if declared in cfg.RISKS else "change"


def lowering(old: cfg.Config, new: cfg.Config) -> list[Finding]:
    """What `new` loosens against `old` (both as config.load reads them): the edits her approval card must confirm.

    A tool server that is new, or runs another command (args, env, cwd) or connects elsewhere (url); a server's
    adapter changed; a trust flag gone (private, foreign, egress: trust.py); a tool off its confirm or careful
    list; tools offered more readily (asked < topic < always); recall workspaces added; tools switched on; an
    `[approvals] risk` tier lowered, or a tier taken off `[approvals] hold`; message bodies read more (off < react <
    glance, per app too); sentences kept in the log or the outcome file; the inbox switched on; the daemon listening
    beyond this machine; the models' Ollama on another machine; the camera always watching instead of armed; a thumb
    answering her card; strangers' text making changes ask for less time. Everything else is neutral or raises it."""
    out: list[Finding] = []

    def add(key: str, why: str) -> None:
        out.append(Finding(key, why))

    if _local(old.daemon.host) and not _local(new.daemon.host):
        add("daemon.host", f"the daemon would listen beyond this machine ({new.daemon.host})")
    if new.daemon.log_sentences and not old.daemon.log_sentences:
        add("daemon.log_sentences", "what you say or type would be written to the log")
    if new.learning.log_outcomes and not old.learning.log_outcomes:
        add("learning.log_outcomes", "the sentences you say or type would be kept in the outcome file")
    if new.messages.enabled and not old.messages.enabled:
        add("messages.enabled", "her inbox would keep your notifications for the big model to read")
    for key, old_url, new_url in (("brain.ollama_url", old.brain.ollama_url, new.brain.ollama_url),):
        if new_url != old_url and not _local(_host_of(new_url)):
            add(key, f"the models would run on another machine ({_host_of(new_url) or new_url})")
    apps = set(old.notifications.body_apps) | set(new.notifications.body_apps)
    for app in sorted(apps | {""}):
        before = old.notifications.mode_for(app) if app else old.notifications.body
        after = new.notifications.mode_for(app) if app else new.notifications.body
        if BODY_RANK[after] > BODY_RANK[before]:
            key = f"notifications.body_apps.{app}" if app else "notifications.body"
            add(key, f"message bodies{f' from {app}' if app else ''}: {before} -> {after} (she reads more of them)")
    if new.tools.enabled and not old.tools.enabled:
        add("tools.enabled", "the tool servers would be switched on")
    for name in sorted(new.tools.servers):
        table = new.tools.servers[name]
        after = _server_view(name, table)
        if name not in old.tools.servers:
            what = f"runs {table.get('command')}" if table.get("command") else f"connects to {_host_of(table.get('url', ''))}"
            add(f"tools.servers.{name}", f"a new tool server {name} ({what})")
            continue
        before = _server_view(name, old.tools.servers[name])
        for part, said in (("command", "its command"), ("args", "its arguments"), ("env", "its environment"),
                           ("cwd", "its folder"), ("url", "its address")):
            if before["runs"][part] != after["runs"][part]:
                add(f"tools.servers.{name}.{part}", f"tool server {name}: {said} changed")
        if before["adapter"] != after["adapter"]:
            add(f"tools.servers.{name}.adapter", f"tool server {name}: its adapter changed")
        gone = sorted(before["flags"] - after["flags"])
        if gone:
            add(f"tools.servers.{name}.flags", f"tool server {name} would no longer count as {' or '.join(gone)}")
        for listed, said in (("confirm", "asked about first"), ("careful", "held back as careful")):
            dropped = sorted(before[listed] - after[listed])
            if dropped:
                add(f"tools.servers.{name}.{listed}", f"{name}: {', '.join(dropped)} would no longer be {said}")
        if OFFER_RANK.get(after["offer"], 2) > OFFER_RANK.get(before["offer"], 2):
            add(f"tools.servers.{name}.offer", f"tool server {name} offered {after['offer']} instead of {before['offer']}")
        wider = sorted(after["workspaces"] - before["workspaces"])
        if wider:
            add(f"tools.servers.{name}.workspaces", f"{name} could read the workspace(s) {', '.join(wider)}")
    for key in sorted(set(old.approvals.risk) | set(new.approvals.risk)):
        before = old.approvals.risk.get(key) or _tool_tier(old, key)
        after = new.approvals.risk.get(key) or _tool_tier(new, key)
        if cfg.RISKS.index(after) < cfg.RISKS.index(before):
            add(f"approvals.risk.{key}", f"{key}: {before} -> {after} (asks less)")
    for tier in sorted(set(old.approvals.hold) - set(new.approvals.hold)):
        add("approvals.hold", f"a yes to a {tier} call would no longer need a hold on her card")
    if old.gestures.watch == "armed" and new.gestures.watch == "always":
        add("gestures.watch", "the camera would watch all the time instead of only when armed")
    if new.gestures.approvals and not old.gestures.approvals:
        add("gestures.approvals", "a thumb, a nod or a shake would answer her card")
    if new.ledger.foreign_minutes < old.ledger.foreign_minutes:
        add("ledger.foreign_minutes", f"strangers' text would make changes ask for {new.ledger.foreign_minutes:g} "
                                      f"min instead of {old.ledger.foreign_minutes:g}")
    return out


def refused(old: cfg.Config, new: cfg.Config) -> list[str]:
    """What this editor never changes: the camera goes on and off only by its explicit switches."""
    if old.gestures.enabled != new.gestures.enabled:
        return ["[gestures] enabled is switched with the camera switch on the Input tab (or the tray's Hand gestures "
                "row), not from here: put it back as it was"]
    return []


# --------------------------------------------------------------------------------------------- writing


class Stale(ConfigError):
    """The file changed after the page loaded it."""


def write(path: Path, text: str, base_sha: str) -> Path | None:
    """`text` as config.toml if the file is still the one the page loaded (`base_sha`): the old one backed up beside
    it (config.toml.bak-<time>, as configedit does), then the new one written in one step, as private as the old one
    was. Returns the backup (None for a new file). Raises Stale when the file changed, ConfigError when `text` does
    not load."""
    if HIDDEN.search(text) or HIDDEN_LINE.search(text.replace("\r\n", "\n")):
        raise ConfigError("the text still has a hidden placeholder in it; nothing written")
    load_text(text)
    now = base_of(path)
    if now["sha256"] != base_sha:
        raise Stale("config.toml changed since this page loaded it (another editor, the tray or the camera switch)")
    backup = None
    private = False
    if now["exists"]:
        private = os.name == "posix" and (path.stat().st_mode & 0o077) == 0
        backup = configedit.backup(path)
    paths.write_atomic(path, text, private=private)
    os.utime(path)
    return backup
