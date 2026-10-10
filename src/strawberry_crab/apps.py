"""The apps she may open or close (WIRING.md §27): the installed desktop entries, and `apps.toml`.

    ~/.config/strawberry/apps.toml   [apps."<desktop id>"] tables, written by the Brain UI's Apps tab (or by hand)
       │
       └─ AppStore (the daemon) ─ stat once a second, parsed again when it changed; a file that does not check
                                  out keeps the list she has, is logged once and shown in the Apps tab

An app is named by its desktop id, the file name of an installed `.desktop` entry in an XDG application dir
($XDG_DATA_HOME/applications, each $XDG_DATA_DIRS/applications, and snapd's), never by a command line: she
launches it with `gtk-launch <id>` and nothing else (desktop.py). An id is checked strictly (`ID`: letters,
digits and `._+-`, ending in `.desktop`, no path, no space, no leading dash), and the page may add only an id
that is installed. With no apps.toml, Spotify is allowed to open (not to close) when it is installed.

    [apps."spotify_spotify.desktop"]
    name = "Spotify"          # what she calls it, and a name the user may say
    aliases = ["music app"]   # other names the user may say
    open = true               # she may open it
    close = false             # she may close it (a polite close request; never a kill)
    close_asks = true         # and asks first, on her card

Names in a sentence are matched against `name` and `aliases` only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import ConfigError, default_path

log = logging.getLogger("strawberryd.apps")

FILE_NAME = "apps.toml"
MAX_TEXT = 40_000
MAX_APPS = 100
MAX_ALIASES = 8
NAME_CHARS = 60
ALIAS_CHARS = 40
MAX_ENTRY_BYTES = 64_000          # a .desktop file larger than this is not read
SCAN_S = 30.0                     # the installed entries are scanned again at most this often
# A desktop id as she takes it: a file name in an application dir, nothing that reads as a path, an option or a
# shell word. (Desktop ids from subdirectories, "kde4-foo.desktop", and names with spaces are not offered.)
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,150}\.desktop$")
# A name or alias: letters (any script), digits, spaces and a few marks; it is said and matched, never run.
SPOKEN = re.compile(r"^[^\W_][\w '.&+-]*$")
KEYS = ("name", "aliases", "open", "close", "close_asks")
SNAP_DIR = Path("/var/lib/snapd/desktop/applications")
# Spotify's desktop ids: the snap, a distribution's package, the Flatpak. The first one installed is the default.
SPOTIFY_IDS = ("spotify_spotify.desktop", "spotify.desktop", "com.spotify.Client.desktop")
HEADER = """\
# The apps she may open or close (WIRING.md §27). Written by the Brain UI's Apps tab (`strawberry ui`); edit it
# by hand if you like: she reads it again within a second. Each table is a desktop id (a .desktop file's name);
# she launches it with gtk-launch and closes it only politely (the window's close request, or a media
# player's Quit). `name` and `aliases` are the names you may say.
"""


class AppsError(ConfigError):
    """apps.toml, or the Apps tab's list, does not check out. The message says why."""


# ----------------------------------------------------------------------------- installed desktop entries


@dataclass(frozen=True)
class Entry:
    """One installed `.desktop` file, as the picker and the closer need it."""

    id: str
    name: str
    path: Path
    hidden: bool = False                 # NoDisplay or Hidden: not offered in the picker
    classes: tuple[str, ...] = ()        # lower-case names its windows or its MPRIS player may go by

    @property
    def data_dir(self) -> Path:
        """The XDG data dir it was found in (…/share for …/share/applications): gtk-launch needs it in
        XDG_DATA_DIRS."""
        return self.path.parent.parent


def application_dirs(env: dict[str, str] | None = None) -> list[Path]:
    """The XDG application dirs, most important first: $XDG_DATA_HOME/applications, each of $XDG_DATA_DIRS'
    (default /usr/local/share, /usr/share), and snapd's. None on Windows."""
    if sys.platform == "win32":
        return []
    env = os.environ if env is None else env
    home = env.get("XDG_DATA_HOME", "")
    home_dir = Path(home) if home and os.path.isabs(home) else Path.home() / ".local/share"
    dirs = [home_dir / "applications"]
    for part in (env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":"):
        if part and os.path.isabs(part):
            dirs.append(Path(part) / "applications")
    dirs.append(SNAP_DIR)
    seen: list[Path] = []
    for one in dirs:
        if one not in seen:
            seen.append(one)
    return seen


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"s": " ", "n": " ", "t": " ", "r": " "}.get(m.group(1), m.group(1)), value)


def read_entry(path: Path) -> dict[str, str] | None:
    """The [Desktop Entry] group's unlocalised keys, or None when the file is not one (or too big to be)."""
    try:
        if path.stat().st_size > MAX_ENTRY_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    keys: dict[str, str] = {}
    group = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1]
            continue
        if group != "Desktop Entry" or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if "[" in key or key in keys:
            continue                     # a localised Name[fi], or a second copy of a key
        keys[key] = _unescape(value.strip())
    return keys if keys else None


def _true(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def _class_names(desktop_id: str, keys: dict[str, str]) -> tuple[str, ...]:
    """What its windows' WM_CLASS or its MPRIS player may be called: StartupWMClass, the id's stem (and a snap's
    app part, "spotify" of "spotify_spotify"), a reverse-DNS id's last part, and the program in Exec. Only plain
    names: these are compared, never run."""
    stem = desktop_id[: -len(".desktop")]
    names = [keys.get("StartupWMClass", ""), stem, stem.rpartition("_")[2], stem.rpartition(".")[2]]
    for word in (keys.get("Exec") or "").split():
        word = word.strip("\"'")
        if word == "env" or "=" in word or word.startswith("-"):
            continue
        names.append(word.rpartition("/")[2])
        break
    out: list[str] = []
    for name in names:
        name = name.strip().lower()
        if name and re.fullmatch(r"[a-z0-9._-]{1,64}", name) and name not in out:
            out.append(name)
    return tuple(out)


def scan(dirs: list[Path] | None = None) -> dict[str, Entry]:
    """Every installed application entry by desktop id. An id in an earlier dir hides the same id in a later
    one (XDG precedence), a Hidden one included. Ids that do not pass `ID` are left out."""
    found: dict[str, Entry] = {}
    for directory in application_dirs() if dirs is None else dirs:
        try:
            files = sorted(directory.iterdir())
        except OSError:
            continue
        for path in files:
            desktop_id = path.name
            if desktop_id in found or not ID.match(desktop_id) or not path.is_file():
                continue
            keys = read_entry(path)
            if keys is None:
                continue
            if keys.get("Type", "Application") != "Application":
                continue
            name = " ".join((keys.get("Name") or desktop_id[: -len(".desktop")]).split())[:NAME_CHARS]
            hidden = _true(keys.get("NoDisplay")) or _true(keys.get("Hidden"))
            found[desktop_id] = Entry(desktop_id, name, path, hidden, _class_names(desktop_id, keys))
    return found


# ----------------------------------------------------------------------------- the allowlist


@dataclass(frozen=True)
class App:
    """One app on the list: what she may do with it, and the names the user may call it by."""

    id: str
    name: str
    aliases: tuple[str, ...] = ()
    open: bool = False
    close: bool = False
    close_asks: bool = True

    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    def view(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "aliases": list(self.aliases), "open": self.open,
                "close": self.close, "close_asks": self.close_asks}


def default_apps(installed: dict[str, Entry]) -> list[App]:
    """With no apps.toml: Spotify may be opened (not closed), when it is installed."""
    for desktop_id in SPOTIFY_IDS:
        entry = installed.get(desktop_id)
        if entry is not None:
            name = entry.name if entry.name.lower().startswith("spotify") else "Spotify"
            return [App(desktop_id, name, open=True, close=False, close_asks=True)]
    return []


def spoken(text: str) -> str:
    """A name as it is compared: lower case, words only, single spaces."""
    return " ".join(re.findall(r"[^\W_]+", (text or "").lower()))


def _bool(table: dict[str, Any], key: str, default: bool, where: str) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise AppsError(f"{where}.{key} must be true or false")
    return value


def check_app(desktop_id: Any, table: Any) -> App:
    """One `[apps."<id>"]` table as an App, or AppsError naming what does not check out."""
    if not isinstance(desktop_id, str) or not ID.match(desktop_id):
        raise AppsError(f"{json.dumps(str(desktop_id))[:80]} is not a desktop id (a .desktop file's name: letters, "
                        "digits and ._+- only, no path)")
    where = f'apps."{desktop_id}"'
    if not isinstance(table, dict):
        raise AppsError(f"{where} must be a table")
    extra = sorted(set(table) - set(KEYS))
    if extra:
        raise AppsError(f"{where}: unknown keys {extra}")
    name = table.get("name", "")
    if not isinstance(name, str):
        raise AppsError(f"{where}.name must be text")
    name = " ".join(name.split()) or desktop_id[: -len(".desktop")]
    if len(name) > NAME_CHARS or not SPOKEN.match(name):
        raise AppsError(f"{where}.name must be a short name (at most {NAME_CHARS} characters: letters, digits, "
                        "spaces and . ' & + -)")
    aliases = table.get("aliases", [])
    if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
        raise AppsError(f"{where}.aliases must be a list of names")
    cleaned: list[str] = []
    for alias in aliases:
        alias = " ".join(alias.split())
        if not alias:
            continue
        if len(alias) > ALIAS_CHARS or not SPOKEN.match(alias):
            raise AppsError(f"{where}.aliases: {json.dumps(alias)[:60]} is not a name (at most {ALIAS_CHARS} "
                            "characters: letters, digits, spaces and . ' & + -)")
        if alias.lower() not in (a.lower() for a in cleaned):
            cleaned.append(alias)
    if len(cleaned) > MAX_ALIASES:
        raise AppsError(f"{where}.aliases: at most {MAX_ALIASES}")
    return App(desktop_id, name, tuple(cleaned), _bool(table, "open", False, where), _bool(table, "close", False, where),
               _bool(table, "close_asks", True, where))


def _no_clashes(apps: list[App]) -> None:
    owners: dict[str, str] = {}
    for app in apps:
        for name in app.names():
            key = spoken(name)
            if not key:
                raise AppsError(f"{app.id}: a name needs a letter or a digit")
            if key in owners and owners[key] != app.id:
                raise AppsError(f"the name {json.dumps(name)} is used by two apps ({owners[key]} and {app.id})")
            owners[key] = app.id


def parse(text: str) -> list[App]:
    """apps.toml's text as the list, or AppsError. Syntax only: an id that is not installed (any more) is kept and
    refused when it is used."""
    if len(text) > MAX_TEXT:
        raise AppsError(f"{FILE_NAME} is too long (at most {MAX_TEXT} characters)")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise AppsError(f"{FILE_NAME} does not parse: {exc}") from None
    extra = sorted(set(data) - {"apps"})
    if extra:
        raise AppsError(f"{FILE_NAME} holds [apps.\"<desktop id>\"] tables only; not {extra}")
    tables = data.get("apps", {})
    if not isinstance(tables, dict):
        raise AppsError("[apps] must hold a table per desktop id")
    if len(tables) > MAX_APPS:
        raise AppsError(f"at most {MAX_APPS} apps")
    apps = [check_app(desktop_id, table) for desktop_id, table in tables.items()]
    _no_clashes(apps)
    return apps


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)      # a JSON string is a TOML basic string


def render(apps: list[App]) -> str:
    """The list as apps.toml says it: every key written out, in the list's order."""
    lines = [HEADER]
    for app in apps:
        lines += [f"[apps.{json.dumps(app.id)}]", f"name = {_value(app.name)}", f"aliases = {_value(app.aliases)}",
                  f"open = {_value(app.open)}", f"close = {_value(app.close)}",
                  f"close_asks = {_value(app.close_asks)}", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def from_view(data: Any, installed: dict[str, Entry], kept: tuple[str, ...] = ()) -> tuple[str, list[App]]:
    """The Apps tab's list ([{id, name, aliases, open, close, close_asks}]) as apps.toml's text, checked by `parse`.
    A new id must be installed (one already in the file, `kept`, may stay while it is not); an app with neither
    `open` nor `close` is left out. AppsError when it does not check out."""
    if not isinstance(data, list):
        raise AppsError("apps must be a list of {id, name, aliases, open, close, close_asks}")
    if len(data) > MAX_APPS * 4:
        raise AppsError("too many rows")
    apps: list[App] = []
    seen: set[str] = set()
    for row in data:
        if not isinstance(row, dict):
            raise AppsError("each app must be an object")
        desktop_id = row.get("id")
        table = {k: v for k, v in row.items() if k != "id"}
        app = check_app(desktop_id, table)
        if app.id in seen:
            raise AppsError(f"{app.id} is listed twice")
        seen.add(app.id)
        if app.id not in installed and app.id not in kept:
            raise AppsError(f"{app.id} is not installed (no such .desktop file in the application dirs)")
        if app.open or app.close:
            apps.append(app)
    if len(apps) > MAX_APPS:
        raise AppsError(f"at most {MAX_APPS} apps")
    text = render(apps)
    return text, parse(text)


def apps_file(config_path: Path | None = None) -> Path:
    """apps.toml, beside the config file (`~/.config/strawberry/apps.toml`)."""
    return (config_path or default_path()).with_name(FILE_NAME)


# ----------------------------------------------------------------------------- the daemon's side


@dataclass
class Found:
    """An app named in a sentence or a call: the list's entry, and its installed desktop entry (None: not
    installed any more)."""

    app: App
    entry: Entry | None = None


class AppStore:
    """apps.toml in the daemon: looked at once a second (`run`) and right after the Apps tab saves it, parsed
    again when its mtime or size changed. One that does not check out keeps the list she has; `status()` says
    why. With no file, the default (Spotify, when installed)."""

    EVERY_S = 1.0

    def __init__(self, path: Path, scanner: Callable[[], dict[str, Entry]] = scan,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.path = path
        self.scanner = scanner
        self.clock = clock
        self._installed: dict[str, Entry] | None = None
        self._scanned_at = -1e9
        self.seen: tuple[int, int] | None = None
        self.error = ""
        self.from_file = False
        self._apps: list[App] = []
        self.task: asyncio.Task | None = None
        self.check(force=True)

    # --- the installed entries, scanned at most every SCAN_S

    def installed(self, fresh: bool = False) -> dict[str, Entry]:
        now = self.clock()
        if self._installed is None or fresh or now - self._scanned_at >= SCAN_S:
            try:
                self._installed = self.scanner()
            except Exception as exc:   # noqa: BLE001 - a dir that cannot be read: nothing installed, said once
                log.warning("apps: the application dirs could not be read (%s)", type(exc).__name__)
                self._installed = {}
            self._scanned_at = now
        return self._installed

    # --- the file

    def _stat(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.get_running_loop().create_task(self.run())

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.EVERY_S)
            try:
                self.check()
            except Exception as exc:   # noqa: BLE001 - a stat or a parse that failed oddly: say it, keep going
                log.warning("apps: checking %s failed (%s)", FILE_NAME, type(exc).__name__)

    def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            self.task = None

    def check(self, force: bool = False) -> bool:
        """Read apps.toml again if it changed (or `force`). True when the list in use changed."""
        seen = self._stat()
        if seen == self.seen and not force:
            return False
        self.seen = seen
        if seen is None:
            was = self.from_file
            self.from_file, self.error = False, ""
            self._apps = default_apps(self.installed())
            if was:
                log.info("apps: %s is gone; the default list (%d app(s))", FILE_NAME, len(self._apps))
            return was
        try:
            apps = parse(self.path.read_text(encoding="utf-8"))
        except (AppsError, OSError, UnicodeDecodeError) as exc:
            new = str(exc) if isinstance(exc, AppsError) else f"{FILE_NAME} could not be read ({type(exc).__name__})"
            if new != self.error:
                log.warning("apps: %s does not check out; keeping the list she has (the Brain UI's Apps tab says "
                            "why)", FILE_NAME)
            self.error = new
            return False
        self._apps, self.from_file, self.error = apps, True, ""
        log.info("apps: %s applied (%d app(s): %d may open, %d may close)", FILE_NAME, len(apps),
                 sum(a.open for a in apps), sum(a.close for a in apps))
        return True

    def apps(self) -> list[App]:
        if not self.from_file and not self._apps:
            # The default waits for the scan (a Spotify installed after the start counts).
            self._apps = default_apps(self.installed())
        return list(self._apps)

    def find(self, said: Any) -> Found | None:
        """The app a name (said, or a call's argument) means: its name or an alias, or, for a call, its desktop
        id. None when it is none of the list's."""
        if not isinstance(said, str) or len(said) > 120:
            return None
        key = spoken(said)
        if not key:
            return None
        for app in self.apps():
            if said.strip() == app.id or key in (spoken(n) for n in app.names()):
                return Found(app, self.installed().get(app.id))
        return None

    def find_said(self, phrase: str) -> Found | None:
        """`find` for the words of a sentence: also without a leading "the" or "my" and a trailing "app",
        "application" or "window(s)"."""
        words = spoken(phrase).split()
        while words and words[0] in ("the", "my", "that", "this"):
            words = words[1:]
        while words and words[-1] in ("app", "application", "window", "windows", "program"):
            words = words[:-1]
        return self.find(" ".join(words)) if words else None

    def text(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    def status(self) -> dict[str, Any]:
        return {"path": str(self.path), "exists": self.seen is not None, "in_use": self.from_file,
                "source": FILE_NAME if self.from_file else "default", "error": self.error or None}


def with_entry(app: App, entry: Entry | None) -> dict[str, Any]:
    """An app as the Apps tab shows it: the list's fields, and whether it is installed."""
    return app.view() | {"installed": entry is not None}

