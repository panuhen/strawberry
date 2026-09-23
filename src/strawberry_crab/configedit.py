"""config.toml, edited as text: one key at a time, comments kept, validated, backed up first.

tomllib reads but does not write, and rewriting the file from a dict would lose the user's
comments. So keys are set line by line inside their [section], and the result is parsed (and
validated by config.load) before anything is written. `strawberry setup` (setupcmd.py) and the
tray's "Message bodies" rows (tray.py) both write through here.
"""

from __future__ import annotations

import json
import re
import shutil
import time
import tomllib
from pathlib import Path
from typing import Any, Callable

_HEADER = re.compile(r"^\s*\[\[?\s*([A-Za-z0-9_.\-\"' ]+?)\s*\]\]?\s*(#.*)?$")


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)      # a JSON string is a TOML basic string
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(f"{_toml_key(k)} = {toml_value(v)}" for k, v in value.items())
        return "{ " + inner + " }" if inner else "{}"
    raise TypeError(f"no TOML for {type(value).__name__}")


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key)


def _section_span(lines: list[str], section: str) -> tuple[int, int] | None:
    """(header index, end index) of `[section]`: its keys are lines header+1 .. end-1."""
    start = None
    for i, line in enumerate(lines):
        match = _HEADER.match(line)
        if not match:
            continue
        if start is not None:
            return start, i
        if not line.lstrip().startswith("[[") and match.group(1).strip() == section:
            start = i
    return (start, len(lines)) if start is not None else None


def set_key(text: str, dotted: str, value: Any, overwrite: bool, note: str = "") -> tuple[str, bool]:
    """`text` with `section.key = value`, and whether it changed. An existing key is replaced
    only when `overwrite`; a new one goes after the section's last key line (or in a new section
    at the end). `note` becomes a trailing comment on a line this writes."""
    section, key = dotted.rsplit(".", 1)
    lines = text.splitlines()
    line = f"{key} = {toml_value(value)}" + (f"   # {note}" if note else "")
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    span = _section_span(lines, section)
    if span is None:
        tail = [] if not lines or not lines[-1].strip() else [""]
        lines += [*tail, f"[{section}]", line]
        return "\n".join(lines) + "\n", True
    start, end = span
    for i in range(start + 1, end):
        if key_re.match(lines[i]):
            old_value, comment = _split_comment(lines[i])
            if not overwrite or old_value is None or old_value == value:   # None: a multi-line value, left alone
                return text, False
            lines[i] = f"{key} = {toml_value(value)}{comment}"      # the user's comment stays
            return "\n".join(lines) + "\n", True
    last = start
    for i in range(start + 1, end):
        if lines[i].strip() and not lines[i].lstrip().startswith("#"):
            last = i
    lines.insert(last + 1, line)
    return "\n".join(lines) + "\n", True


def _split_comment(line: str) -> tuple[Any, str]:
    """A one-line `key = value   # comment` as (value, "   # comment"); (None, "") when the value
    does not parse on its own line (a multi-line array or string starts there)."""
    for match in re.finditer(r"\s+#|$", line):
        try:
            data = tomllib.loads(line[: match.start()])
        except tomllib.TOMLDecodeError:
            continue
        return next(iter(data.values()), None), line[match.start():]
    return None, ""


def backup(path: Path, clock: Callable[[], float] = time.time) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))
    target = path.with_name(f"{path.name}.bak-{stamp}")
    n = 1
    while target.exists():
        target = path.with_name(f"{path.name}.bak-{stamp}-{n}")
        n += 1
    shutil.copy2(path, target)
    return target


def check_config_text(text: str) -> None:
    """Raise ConfigError when `text` would not load; nothing is written until this passes."""
    import tempfile

    from .config import ConfigError, load

    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"the edited config would not parse: {exc}") from exc
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".toml", delete=False) as handle:
        handle.write(text)
    try:
        load(Path(handle.name), env={})
    finally:
        Path(handle.name).unlink(missing_ok=True)


def write_config(path: Path, text: str, say: Callable[[str], None]) -> Path | None:
    """Back up the file if it exists, then write `text`. Returns the backup's path."""
    check_config_text(text)
    saved = None
    if path.exists():
        if path.read_text(encoding="utf-8") == text:
            return None
        saved = backup(path)
        say(f"  backed up {path} -> {saved.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return saved


def set_value(path: Path, dotted: str, value: Any, say: Callable[[str], None] = lambda line: None) -> Path | None:
    """`section.key = value` in the file at `path`, replacing what is there and keeping the rest of
    the file, comments included. A missing or empty file starts as the commented template.
    Validated before writing and backed up first; returns the backup (None: a new file, or no change).

    Raises ConfigError, and writes nothing, when the key cannot be set on one line (a multi-line
    value, or the section written as an inline table) or the result would not load."""
    from .config import ConfigError, default_toml

    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if not text.strip():
        text = default_toml()
    text, _ = set_key(text, dotted, value, overwrite=True)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"the edited config would not parse: {exc}") from exc
    section, key = dotted.rsplit(".", 1)
    if not isinstance(data.get(section), dict) or data[section].get(key) != value:
        raise ConfigError(f"{dotted} is not on a line of its own in [{section}]; edit it by hand")
    return write_config(path, text, say)
