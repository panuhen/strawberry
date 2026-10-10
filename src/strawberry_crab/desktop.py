"""The desktop's own controls (WIRING.md §27): the apps on the list, the computer's volume, the screen lock.

A builtin server, `desktop` (`Toolbox.add_builtin`, like her profile and her inbox), with four tools:

    open_app       an app on the list that may be opened: `gtk-launch <desktop id>`        change
    close_app      one that may be closed: a polite close only (MPRIS Quit, then the        change; asks first
                   window's close request through wmctrl or xdotool's windowquit)             unless close_asks = false
    system_volume  the default sink's volume and mute (wpctl, else pactl), 0-100          playback
    lock_screen    `loginctl lock-session`, else GNOME's ScreenSaver.Lock                    change; never offered to
                                                                                             the thinker
Whole sentences reach them without a model, as said reflexes ("open spotify", "close firefox", "set the computer
volume to 30", "mute the computer", "lock the screen"); the thinker is offered the first three only when a
sentence asks for them (`offer = "asked"`). The lock is an explicit phrase or a gesture the user maps, nothing
else: no model can call it.

Nothing here ever builds a command line through a shell, kills or signals an app, or changes anything in the
audio setup but the default sink's volume and mute. Every program goes through `run_command` (argv, no shell),
which the tests replace (tests/conftest.py refuses it outright), and an app is named by its desktop id only
(apps.py checks it).

`SpotifyWake` is the toolbox's `recover` hook: a play that finds no Spotify device, with Spotify on the list and
allowed to open, opens it, says "Opening Spotify…", waits up to 15 s for its device and plays there.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import re
import shutil
import socket
import sys
import time
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from .actions import Outcome
from .adapters.base import Adapter
from .apps import ID, SPOTIFY_IDS, AppStore, Entry, Found
from .tools import ToolResult

log = logging.getLogger("strawberryd.desktop")

SERVER = "desktop"
TOPIC = "system"
VOLUME_STEP = 10
SINK_WPCTL = "@DEFAULT_AUDIO_SINK@"
SINK_PACTL = "@DEFAULT_SINK@"
VOLUME_ACTIONS = ("get", "up", "down", "set", "mute", "unmute")
MPRIS_PREFIX = "org.mpris.MediaPlayer2."
ROOT_IFACE = "org.mpris.MediaPlayer2"
_SESSION = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_WINDOW = re.compile(r"^(?:0x[0-9a-fA-F]{1,16}|\d{1,20})$")


class DesktopError(RuntimeError):
    """A control that cannot run here. The message is said after "but"."""


# ----------------------------------------------------------------------------- the one way a program is run


Runner = Callable[..., Awaitable[tuple[int, str]]]


async def run_command(argv: list[str], timeout_s: float = 8.0, env: dict[str, str] | None = None,
                      capture: bool = True) -> tuple[int, str]:
    """One program, by its argv (never a shell), its exit code and (with `capture`) its output. A launcher's
    output is not captured, so an app it starts never holds a pipe of ours. A helper still running after
    `timeout_s` is stopped (the helper, never an app it started) and the code is 124; a missing program is 127."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, env=env, start_new_session=True)
    except (FileNotFoundError, PermissionError):
        return 127, ""
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout_s)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return 124, ""
    return process.returncode or 0, (out or b"").decode("utf-8", "replace")[:20_000]


class Desktop:
    """The side effects, each through `run` (default `run_command`, looked up at the call so a test's
    replacement holds) and `which`; the MPRIS Quit through `bus` (an mpris.Bus)."""

    def __init__(self, run: Runner | None = None, which: Callable[[str], str | None] | None = None,
                 bus: Any = None, env: dict[str, str] | None = None, platform: str | None = None) -> None:
        self._run = run
        self.which = which or shutil.which
        self.bus = bus
        self.env = env
        self.platform = platform or sys.platform
        self._windowquit: bool | None = None

    async def _exec(self, argv: list[str], **kwargs: Any) -> tuple[int, str]:
        run = self._run or run_command
        return await run(argv, **kwargs)

    def _environ(self) -> dict[str, str]:
        return dict(os.environ if self.env is None else self.env)

    def supported(self) -> str:
        """"" here, or why not (these controls are Linux desktop ones)."""
        return "" if self.platform.startswith("linux") else "that only works on a Linux desktop for now"

    # --- open

    async def launch(self, entry: Entry) -> str:
        """Start the app of `entry` with gtk-launch: "" when it started, else why not. Inside a transient
        systemd scope when systemd-run is there, so the app is not part of her service (a restart of the
        service leaves it running)."""
        if not ID.match(entry.id):
            return "that is not a desktop id"
        gtk = self.which("gtk-launch")
        if gtk is None:
            return "gtk-launch is not installed"
        env = self._environ()
        dirs = env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
        if str(entry.data_dir) not in dirs.split(":"):
            env["XDG_DATA_DIRS"] = f"{dirs}:{entry.data_dir}"      # where gtk-launch finds this entry
        scope = self.which("systemd-run")
        code = 127
        if scope is not None:
            code, _ = await self._exec([scope, "--user", "--scope", "--collect", "--quiet", "--slice=app.slice", "--",
                                        gtk, entry.id], env=env, capture=False, timeout_s=10.0)
        if code != 0:
            code, _ = await self._exec([gtk, entry.id], env=env, capture=False, timeout_s=10.0)
        return "" if code == 0 else f"gtk-launch could not start it (exit {code})"

    # --- close: politely, or not at all

    async def close(self, entry: Entry, name: str) -> tuple[bool, str]:
        """Ask the app to close: its MPRIS player's Quit when it has one that can, else each of its windows' close
        request (wmctrl -c, or xdotool windowquit; never windowclose or windowkill, which destroy). (done, why
        not)."""
        if await self._mpris_quit(entry):
            return True, ""
        listed = False
        wmctrl = self.which("wmctrl")
        if wmctrl is not None:
            code, out = await self._exec([wmctrl, "-l", "-x"])
            if code == 0:
                listed = True
                ids = windows_of(out, entry.classes)
                closed = 0
                for window in ids:
                    code, _ = await self._exec([wmctrl, "-i", "-c", window])
                    closed += code == 0
                if closed:
                    log.info("desktop: asked %d window(s) of %s to close (wmctrl)", closed, entry.id)
                    return True, ""
                if ids:
                    return False, "its windows did not take the close request"
        xdotool = self.which("xdotool")
        if xdotool is not None and await self._has_windowquit(xdotool):
            listed = True
            ids: list[str] = []
            for cls in entry.classes:
                pattern = "^" + cls.replace(".", "\\.") + "$"
                code, out = await self._exec([xdotool, "search", "--class", pattern])
                ids += [w for w in out.split() if _WINDOW.match(w) and w not in ids]
            closed = 0
            for window in ids[:20]:
                code, _ = await self._exec([xdotool, "windowquit", window])
                closed += code == 0
            if closed:
                log.info("desktop: asked %d window(s) of %s to close (xdotool windowquit)", closed, entry.id)
                return True, ""
        if listed:
            return False, f"{name} doesn't seem to be open"
        return False, "closing a window politely needs wmctrl (or a newer xdotool) here"

    async def _has_windowquit(self, xdotool: str) -> bool:
        """xdotool's graceful close (`windowquit`, from 2021) is there: its help lists it."""
        if self._windowquit is None:
            code, out = await self._exec([xdotool, "help"])
            self._windowquit = "windowquit" in out
        return self._windowquit

    async def _mpris_quit(self, entry: Entry) -> bool:
        """The app's MPRIS player, when it has one that says CanQuit: its Quit (the player closes itself)."""
        if self.bus is None or not entry.classes:
            return False
        try:
            names = [n for n in await self.bus.list_names() if n.startswith(MPRIS_PREFIX)]
        except Exception as exc:   # noqa: BLE001 - no session bus: the windows are tried
            log.info("desktop: no media players to ask (%s)", type(exc).__name__)
            return False
        for name in names:
            try:
                props = await self.bus.get_all(name, ROOT_IFACE)
            except Exception:   # noqa: BLE001 - a player that went away between the two calls
                continue
            own = str(props.get("DesktopEntry") or "").lower()
            suffix = name[len(MPRIS_PREFIX):].split(".")[0].lower()
            if own not in entry.classes and suffix not in entry.classes:
                continue
            if props.get("CanQuit") is not True:
                continue
            try:
                await self.bus.call(name, ROOT_IFACE, "Quit")
            except Exception as exc:   # noqa: BLE001 - it said no: the windows are tried
                log.info("desktop: %s did not quit (%s)", entry.id, type(exc).__name__)
                return False
            log.info("desktop: asked %s's player to quit (MPRIS)", entry.id)
            return True
        return False

    # --- the volume: the default sink's, 0-100, never above

    async def volume(self) -> tuple[int, bool]:
        """The default sink's volume (0-100, as the system has it; it can be above 100 when boosted elsewhere) and
        whether it is muted. DesktopError when nothing here can say."""
        wpctl = self.which("wpctl")
        if wpctl is not None:
            code, out = await self._exec([wpctl, "get-volume", SINK_WPCTL])
            found = re.search(r"Volume:\s*([0-9]+(?:\.[0-9]+)?)", out)
            if code == 0 and found:
                return round(float(found.group(1)) * 100), "[MUTED]" in out
        pactl = self.which("pactl")
        if pactl is not None:
            code, out = await self._exec([pactl, "get-sink-volume", SINK_PACTL])
            found = re.search(r"(\d+)%", out)
            if code == 0 and found:
                code, mute = await self._exec([pactl, "get-sink-mute", SINK_PACTL])
                return int(found.group(1)), code == 0 and "yes" in mute.lower()
        raise DesktopError("I can't read the computer's volume here (no wpctl or pactl)")

    async def set_volume(self, level: int) -> int:
        """The default sink's volume set to `level`, clamped to 0-100 (never a boost past 100). The level set."""
        level = clamp(level)
        wpctl = self.which("wpctl")
        if wpctl is not None:
            code, _ = await self._exec([wpctl, "set-volume", SINK_WPCTL, f"{level / 100:.2f}"])
            if code == 0:
                return level
        pactl = self.which("pactl")
        if pactl is not None:
            code, _ = await self._exec([pactl, "set-sink-volume", SINK_PACTL, f"{level}%"])
            if code == 0:
                return level
        raise DesktopError("I can't set the computer's volume here (no wpctl or pactl that answers)")

    async def set_mute(self, muted: bool) -> None:
        wpctl = self.which("wpctl")
        if wpctl is not None:
            code, _ = await self._exec([wpctl, "set-mute", SINK_WPCTL, "1" if muted else "0"])
            if code == 0:
                return
        pactl = self.which("pactl")
        if pactl is not None:
            code, _ = await self._exec([pactl, "set-sink-mute", SINK_PACTL, "1" if muted else "0"])
            if code == 0:
                return
        raise DesktopError("I can't mute the computer here (no wpctl or pactl that answers)")

    # --- the lock

    async def lock(self) -> bool:
        """Lock the user's session: logind (`loginctl lock-session`, the session found from XDG_SESSION_ID or the
        user's display session), else GNOME's screensaver over D-Bus. True when one of them took it."""
        loginctl = self.which("loginctl")
        if loginctl is not None:
            env = self._environ()
            session = env.get("XDG_SESSION_ID", "")
            if not _SESSION.match(session):
                code, out = await self._exec([loginctl, "show-user", str(os.getuid()), "-p", "Display", "--value"])
                session = out.strip() if code == 0 and _SESSION.match(out.strip()) else ""
            code, _ = await self._exec([loginctl, "lock-session", *([session] if session else [])])
            if code == 0:
                return True
        gdbus = self.which("gdbus")
        if gdbus is not None:
            code, _ = await self._exec([gdbus, "call", "--session", "--dest", "org.gnome.ScreenSaver", "--object-path",
                                        "/org/gnome/ScreenSaver", "--method", "org.gnome.ScreenSaver.Lock"])
            if code == 0:
                return True
        return False


def clamp(level: Any) -> int:
    """A volume level as she sets it: a whole number from 0 to 100."""
    return max(0, min(100, int(round(float(level)))))


def windows_of(listing: str, classes: tuple[str, ...]) -> list[str]:
    """The window ids in `wmctrl -l -x` output whose WM_CLASS (instance or class) is one of `classes`."""
    found: list[str] = []
    for line in listing.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3 or not _WINDOW.match(parts[0]):
            continue
        instance, _, klass = parts[2].lower().partition(".")
        if (instance in classes or klass in classes) and parts[0] not in found:
            found.append(parts[0])
    return found[:20]


# ----------------------------------------------------------------------------- the builtin server


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> Any:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return SimpleNamespace(name=name, description=description, input_schema=schema, annotations=None)


TOOLS = [
    _tool("open_app", "Open one of the apps on the user's list, by its name (e.g. Spotify).",
          {"app": {"type": "string", "description": "The app's name as the user said it."}}, ["app"]),
    _tool("close_app", "Ask one of the apps on the user's list to close, by its name (a polite close; it may ask "
          "to save first).", {"app": {"type": "string", "description": "The app's name as the user said it."}}, ["app"]),
    _tool("system_volume", "The computer's own sound volume (all sound, not the music player's): get, up, down, "
          "set (level 0-100), mute or unmute.",
          {"action": {"type": "string", "enum": list(VOLUME_ACTIONS)},
           "level": {"type": "integer", "description": "0-100 for set; the step for up or down (default 10)."}},
          ["action"]),
    _tool("lock_screen", "Lock the screen.", {}, []),
]


def _result(text: str, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def not_done(why: str) -> dict[str, Any]:
    """A refusal or a failure, in the words both the model and a reflex use: "Not done: <why>."."""
    return _result(f"Not done: {why}.", True)


class DesktopSession:
    """The builtin server's session (tools.Server): list_tools() and call_tool(name, arguments)."""

    def __init__(self, store: AppStore, desktop: Desktop) -> None:
        self.store = store
        self.desktop = desktop

    async def list_tools(self) -> Any:
        return SimpleNamespace(tools=TOOLS)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        arguments = arguments if isinstance(arguments, dict) else {}
        why = self.desktop.supported()
        if why:
            return not_done(why)
        try:
            if name == "open_app":
                return await self.open_app(arguments.get("app"))
            if name == "close_app":
                return await self.close_app(arguments.get("app"))
            if name == "system_volume":
                return await self.system_volume(arguments.get("action"), arguments.get("level"))
            if name == "lock_screen":
                if await self.desktop.lock():
                    log.info("desktop: the screen locked")
                    return _result("Locked the screen.")
                return not_done("nothing here would lock the screen (loginctl and the screensaver both said no)")
        except DesktopError as exc:
            return not_done(str(exc))
        return _result(f"no tool named {name!r}", True)

    def _found(self, app: Any, verb: str) -> Found | dict[str, Any]:
        found = self.store.find(app)
        if found is None:
            shown = json.dumps(str(app))[:60] if app else "that"
            return not_done(f"{shown} is not on the user's list of apps (they add apps in the Brain UI's Apps tab)")
        allowed = found.app.open if verb == "open" else found.app.close
        if not allowed:
            return not_done(f"the user has not allowed me to {verb} {found.app.name} (the Brain UI's Apps tab)")
        if found.entry is None:
            return not_done(f"{found.app.name} is not installed any more")
        return found

    async def open_app(self, app: Any) -> dict[str, Any]:
        found = self._found(app, "open")
        if isinstance(found, dict):
            return found
        why = await self.desktop.launch(found.entry)
        if why:
            log.info("desktop: %s did not open (%s)", found.app.id, why)
            return not_done(why)
        log.info("desktop: opened %s", found.app.id)
        return _result(f"Opened {found.app.name}.")

    async def close_app(self, app: Any) -> dict[str, Any]:
        found = self._found(app, "close")
        if isinstance(found, dict):
            return found
        done, why = await self.desktop.close(found.entry, found.app.name)
        if not done:
            log.info("desktop: %s not closed (%s)", found.app.id, why)
            return not_done(why)
        return _result(f"Asked {found.app.name} to close.")

    async def system_volume(self, action: Any, level: Any) -> dict[str, Any]:
        if action not in VOLUME_ACTIONS:
            return not_done(f"action must be one of {', '.join(VOLUME_ACTIONS)}")
        number = level is not None and not isinstance(level, bool) and isinstance(level, (int, float)) and level == level
        if action == "set" and not number:
            return not_done("set needs a level from 0 to 100")
        if action == "mute" or action == "unmute":
            await self.desktop.set_mute(action == "mute")
            log.info("desktop: the computer %s", "muted" if action == "mute" else "unmuted")
            if action == "mute":
                return _result("Muted the computer.")
            current, _ = await self.desktop.volume()
            return _result(f"Unmuted the computer, volume {min(current, 100)}.")
        current, muted = await self.desktop.volume()
        still = " (still muted)" if muted else ""
        if action == "get":
            return _result(f"The computer's volume is {current}{', muted' if muted else ''}.")
        if action == "set":
            target = await self.desktop.set_volume(clamp(level))
            log.info("desktop: the computer's volume set to %d", target)
            return _result(f"Computer volume {target}{still}.")
        step = clamp(level) if number and clamp(level) > 0 else VOLUME_STEP
        target = await self.desktop.set_volume(min(current, 100) + (step if action == "up" else -step))
        log.info("desktop: the computer's volume %s to %d", action, target)
        return _result(f"Computer volume {action} to {target}{still}.")


# ----------------------------------------------------------------------------- whole sentences (said reflexes)

# What the said reflex read off the sentence, for the reflex that runs next in the same task (Actor.act: the
# adapter's `said_reflex`, then the reflex, whose task copies this context).
_REQUEST: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("desktop_request", default={})

_ASK = r"(?:(?:hey|ok|okay|oh)\s+)?(?:strawberry\s+)?(?:(?:can|could|will|would)\s+you\s+)?(?:please\s+)?"
_POLITE = r"(?:\s+(?:please|for\s+me|now|strawberry|thanks|thank\s+you))*"
OPEN_SENTENCE = re.compile(rf"^{_ASK}(?:open(?:\s+up)?|launch|start(?:\s+up)?|fire\s+up|bring\s+up)\s+(?P<app>.+?){_POLITE}$")
CLOSE_SENTENCE = re.compile(rf"^{_ASK}(?:close|quit|exit|shut\s+down)\s+(?P<app>.+?){_POLITE}$")
_COMPUTER = r"(?:the\s+|my\s+)?(?:computer|computer's|system|system's|desktop|pc|laptop|master|overall)"
_VOLUME = rf"{_COMPUTER}\s+(?:volume|sound)"
VOLUME_SET = re.compile(rf"^{_ASK}(?:(?:set|put|turn|change|make)\s+)?{_VOLUME}\s+(?:to|at)\s+(?P<level>\d{{1,3}})"
                        rf"(?:\s*(?:%|percent|per\s+cent))?{_POLITE}$")
VOLUME_STEP_SENTENCE = re.compile(
    rf"^{_ASK}(?:(?:turn\s+)?{_VOLUME}\s+(?P<a>up|down)|turn\s+(?P<b>up|down)\s+{_VOLUME}"
    rf"|(?P<c>raise|increase|lower|decrease|reduce)\s+{_VOLUME}|turn\s+{_VOLUME}\s+(?P<d>up|down)){_POLITE}$")
MUTE_SENTENCE = re.compile(rf"^{_ASK}(?P<verb>mute|unmute)\s+(?:{_COMPUTER}(?:\s+(?:volume|sound))?|all\s+(?:the\s+)?sound)"
                           rf"{_POLITE}$")
LOCK_SENTENCE = re.compile(
    rf"^{_ASK}lock\s+(?:the|my|this)\s+(?:screen|computer|desktop|pc|laptop|workstation|session|machine)"
    rf"(?:\s+screen)?{_POLITE}$")
# A sentence that asks for these tools at all (the thinker's offer, `wanted`): a verb for an app at its start, or
# the computer's volume named.
ASKS = re.compile(rf"^{_ASK}(?:open|launch|start|close|quit|exit|shut\s+down)\b|{_VOLUME}|\b(?:mute|unmute)\s+{_COMPUTER}")


def plain(text: str) -> str:
    """The sentence as the patterns read it: lower case, words, digits and %, single spaces."""
    text = (text or "").lower().replace("’", "'")
    return " ".join(re.sub(r"[^\w'%\s]+", " ", text).split())


def says_lock(text: str) -> bool:
    """The whole sentence asks to lock the screen ("lock the screen", "could you lock my computer please")."""
    return bool(LOCK_SENTENCE.match(plain(text)))


def read_volume(text: str) -> dict[str, Any] | None:
    """The computer's volume as the whole sentence asks for it: {"action", "level"?} or None."""
    said = plain(text)
    found = VOLUME_SET.match(said)
    if found:
        return {"action": "set", "level": clamp(int(found.group("level")))}
    found = VOLUME_STEP_SENTENCE.match(said)
    if found:
        word = next(w for w in found.groups() if w)
        return {"action": "up" if word in ("up", "raise", "increase") else "down"}
    found = MUTE_SENTENCE.match(said)
    if found:
        return {"action": found.group("verb")}
    return None


async def _call(toolbox: Any, server: str, tool: str, arguments: dict[str, Any]) -> ToolResult:
    return await toolbox.call(server, tool, arguments)


def _told(result: ToolResult) -> str:
    """A failure's reason as the end of her sentence ("…, but <why>.")."""
    text = (result.text or "").strip().removeprefix("Not done: ").rstrip(".")
    return text[:1].lower() + text[1:] if text[:1].isupper() and not text.startswith(("I ", "Spotify")) else text


async def open_reflex(toolbox: Any, server: str) -> Outcome:
    found: Found = _REQUEST.get().get("found")
    name = found.app.name
    if not found.app.open:
        return Outcome(f"did not open {name}", f"I'm not allowed to open {name}; the Apps tab in the Brain UI can "
                       "allow it.", False)
    result = await _call(toolbox, server, "open_app", {"app": found.app.id})
    if not result.ok:
        return Outcome(f"tried to open {name}", f"I tried to open {name}, but {_told(result)}.", False, (result,))
    return Outcome(f"opened {name}", result.text, True, (result,))


async def close_reflex(toolbox: Any, server: str) -> Outcome:
    found: Found = _REQUEST.get().get("found")
    name = found.app.name
    if not found.app.close:
        return Outcome(f"did not close {name}", f"I'm not allowed to close {name}; the Apps tab in the Brain UI can "
                       "allow it.", False)
    result = await _call(toolbox, server, "close_app", {"app": found.app.id})
    if not result.ok:
        return Outcome(f"tried to close {name}", f"I tried to close {name}, but {_told(result)}.", False, (result,))
    return Outcome(f"asked {name} to close", result.text, True, (result,))


async def volume_reflex(toolbox: Any, server: str) -> Outcome:
    asked = dict(_REQUEST.get().get("volume") or {})
    result = await _call(toolbox, server, "system_volume", asked)
    verb = {"mute": "mute the computer", "unmute": "unmute the computer"}.get(asked.get("action"),
                                                                             "change the computer's volume")
    if not result.ok:
        return Outcome(f"tried to {verb}", f"I tried to {verb}, but {_told(result)}.", False, (result,))
    did = {"mute": "muted the computer", "unmute": "unmuted the computer"}.get(asked.get("action"),
                                                                               "changed the computer's volume")
    return Outcome(did, result.text, True, (result,))


async def lock_reflex(toolbox: Any, server: str) -> Outcome:
    result = await _call(toolbox, server, "lock_screen", {})
    if not result.ok:
        return Outcome("tried to lock the screen", f"I tried to lock the screen, but {_told(result)}.", False, (result,))
    return Outcome("locked the screen", "Locked.", True, (result,))


# ----------------------------------------------------------------------------- the adapter


GUIDE = ("The desktop tools act on this computer. `open_app` and `close_app` take an app by the name the user "
         "said, and work only for the apps on the user's list{names}; anything else is refused, so never claim it "
         "was done without the tool. Closing asks the app politely; it may ask to save first. `system_volume` is "
         "the computer's own volume (all sound), with action up, down, set (level 0-100), mute, unmute or get; "
         "'turn it down' while music plays means the music, not this. Say what the tool said, in a few words.")


class DesktopAdapter(Adapter):
    """The desktop server: none of the trust flags (its results are code's own sentences about the user's own list;
    nothing leaves the machine), offered only to a sentence that asks for it, and known by whole sentences of any
    gate topic (`any_topic`: the gate reads "open spotify" as music and "lock the screen" as system)."""

    name = SERVER
    title = "Desktop"
    offer = "asked"
    any_topic = True
    # Her questions name an app only by the list's name for it (the user's own words), never an argument as it came.
    asks_after_foreign = True
    tools = ("open_app", "close_app", "system_volume")      # lock_screen is never the thinker's to call
    # open and close change what runs on the computer: no question on the user's own sentence, a question once
    # strangers' text is in the run (approvals.needed `foreign`). The volume is how it sounds, undone in a word.
    # The lock is `change` too; a gesture may run it all the same (gestures.EXCEPTIONS, WIRING §26 and §27).
    risks = {"open_app": "change", "close_app": "change", "system_volume": "playback", "lock_screen": "change"}
    reflex_tools = {"open": ("open_app",), "close": ("close_app",), "system_volume": ("system_volume",),
                    "lock": ("lock_screen",)}
    said_reflexes = {"open": ("open_app", open_reflex), "close": ("close_app", close_reflex),
                     "system_volume": ("system_volume", volume_reflex), "lock": ("lock_screen", lock_reflex)}
    labels = {"open_app": "opening an app…", "close_app": "closing an app…", "system_volume": "computer volume…",
              "lock_screen": "locking the screen…", "open": "opening an app…", "close": "closing an app…",
              "lock": "locking the screen…"}

    def __init__(self, store: AppStore) -> None:
        self.store = store

    def said_reflex(self, text: str, route: Any) -> str | None:
        said = plain(text)
        if says_lock(said):
            _REQUEST.set({})
            return "lock"
        volume = read_volume(said)
        if volume is not None:
            _REQUEST.set({"volume": volume})
            return "system_volume"
        for verb, pattern in (("open", OPEN_SENTENCE), ("close", CLOSE_SENTENCE)):
            match = pattern.match(said)
            if not match:
                continue
            found = self.store.find_said(match.group("app"))
            if found is None:
                return None            # not a name on the list: the thinker's (it may be anything)
            if verb == "close" and found.app.close and found.app.close_asks:
                return None            # it asks first: the thinker holds the call and her card asks (§19)
            _REQUEST.set({"found": found})
            return verb
        return None

    def wanted(self, text: str, route: Any) -> bool | None:
        return True if ASKS.search(plain(text)) else None

    def nudge(self, text: str, route: Any) -> str:
        said = plain(text)
        for verb, pattern in (("open", OPEN_SENTENCE), ("close", CLOSE_SENTENCE)):
            match = pattern.match(said)
            found = self.store.find_said(match.group("app")) if match else None
            if found is not None:
                return (f"They asked to {verb} {found.app.name}: do it with {verb}_app now, app \"{found.app.name}\". "
                        "Say it is done only if the tool did it.")
        return ("If they asked to open or close an app or set the computer's volume, use the desktop tools; only "
                "the apps on the user's list can be opened or closed.")

    def guide_for(self, tools: list[str]) -> str:
        names = [a.name for a in self.store.apps() if a.open or a.close]
        return GUIDE.format(names=f" ({', '.join(names[:12])})" if names else " (empty now)")

    def guard(self, state: dict[str, Any], name: str, arguments: dict[str, Any]) -> str | None:
        if not isinstance(arguments, dict):
            return "Not done: give the app's name as text."
        if name in ("open_app", "close_app"):
            app = arguments.get("app")
            if not isinstance(app, str) or not app.strip() or len(app) > 100:
                return "Not done: give the app's name as text."
            found = self.store.find(app)
            verb = name.removesuffix("_app")
            if found is None:
                return f"Not done: that app is not on the user's list; they add apps in the Brain UI's Apps tab."
            if not (found.app.open if verb == "open" else found.app.close):
                return f"Not done: the user has not allowed {verb}ing {found.app.name}."
        return None

    def confirm_call(self, name: str, arguments: dict[str, Any]) -> bool | None:
        """Does this very call ask first? A close asks unless its app says `close_asks = false`."""
        if name != "close_app":
            return None
        found = self.store.find(arguments.get("app")) if isinstance(arguments, dict) else None
        return True if found is None else found.app.close_asks

    async def ask(self, toolbox: Any, server: str, name: str,
                  arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        found = self.store.find(arguments.get("app")) if isinstance(arguments, dict) else None
        if name in ("open_app", "close_app") and found is not None:
            verb = "Close" if name == "close_app" else "Open"
            return f"{verb} {found.app.name}? Say yes.", {"app": found.app.id}
        return await super().ask(toolbox, server, name, arguments)

    def done(self, name: str, arguments: dict[str, Any], result: Any) -> Outcome | None:
        found = self.store.find(arguments.get("app")) if isinstance(arguments, dict) else None
        what = found.app.name if found is not None else "it"
        if not result.ok:
            verb = {"close_app": "close", "open_app": "open"}.get(name, "do that")
            return Outcome(f"tried to {verb} {what}", f"I tried to {verb} {what}, but {_told(result)}.", False, (result,))
        return Outcome({"close_app": f"asked {what} to close", "open_app": f"opened {what}"}.get(name, name),
                       result.text, True, (result,))


# ----------------------------------------------------------------------------- Spotify, opened when it is needed


def _error_code(text: str) -> str:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("code"), str):
        return data["code"]
    return "no_active_device" if "no active device" in (text or "").lower() else ""


def _devices(result: ToolResult) -> list[dict[str, Any]]:
    try:
        data = json.loads(result.text)
    except (json.JSONDecodeError, TypeError):
        return []
    devices = data.get("devices") if isinstance(data, dict) else None
    return [d for d in devices if isinstance(d, dict)] if isinstance(devices, list) else []


def _device_id(device: dict[str, Any]) -> str:
    value = device.get("id") or device.get("device_id") or ""
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) else ""


class SpotifyWake:
    """The toolbox's `recover` hook: `play` or `play_liked` on the Spotify server that failed for want of a device,
    with Spotify on the list and allowed to open: open it, say so, wait up to `wait_s` for its device in
    `get_devices`, then the same call once more with that `device_id`. A device that never shows is a short honest
    failure. Anything else is left as it was (None)."""

    TOOLS = ("play", "play_liked")
    LINE = "Opening Spotify…"

    def __init__(self, store: AppStore, desktop: Desktop, say: Callable[[str], Awaitable[Any]] | None = None,
                 wait_s: float = 15.0, every_s: float = 1.0, sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic, host: str | None = None) -> None:
        self.store = store
        self.desktop = desktop
        self.host = (host if host is not None else socket.gethostname()).strip().lower()
        self.say = say
        self.wait_s = wait_s
        self.every_s = every_s
        self.sleep = sleep
        self.clock = clock
        self.opened = 0

    def spotify(self) -> Found | None:
        for app in self.store.apps():
            if app.id in SPOTIFY_IDS or app.name.lower() == "spotify":
                if not app.open:
                    return None
                found = self.store.find(app.id)
                return found if found is not None and found.entry is not None else None
        return None

    async def __call__(self, toolbox: Any, server: str, name: str, arguments: dict[str, Any], result: ToolResult,
                       shape: bool = False) -> ToolResult | None:
        adapter = getattr(toolbox, "adapters", {}).get(server)
        if getattr(adapter, "name", "") != "spotify" or name not in self.TOOLS or result.ok:
            return None
        if "device_id" in arguments or _error_code(result.text) != "no_active_device":
            return None
        found = self.spotify()
        if found is None or self.desktop.supported():
            return None
        listed = _devices(await toolbox.call(server, "get_devices"))
        idle = self._pick(listed, set(), here_only=True)
        if idle is not None:
            # Spotify is open here and only idle: no device was active. Play there; nothing to open.
            log.info("desktop: no active Spotify device; playing on this computer's")
            return await toolbox.call(server, name, {**arguments, "device_id": idle}, shape=shape)
        before = {_device_id(d) for d in listed}
        why = await self.desktop.launch(found.entry)
        if why:
            log.info("desktop: Spotify has no device and did not open (%s)", why)
            return None
        self.opened += 1
        log.info("desktop: Spotify had no device; opened it and waiting up to %.0fs for one", self.wait_s)
        if self.say is not None:
            try:
                await self.say(self.LINE)
            except Exception as exc:   # noqa: BLE001 - her line is a courtesy; the play goes on
                log.warning("desktop: could not say %r (%s)", self.LINE, type(exc).__name__)
        deadline = self.clock() + self.wait_s
        while True:
            device = self._pick(_devices(await toolbox.call(server, "get_devices")), before)
            if device is not None:
                log.info("desktop: Spotify's device is there; playing on it")
                return await toolbox.call(server, name, {**arguments, "device_id": device}, shape=shape)
            if self.clock() >= deadline:
                break
            await self.sleep(self.every_s)
        log.info("desktop: Spotify opened but no device showed in %.0fs", self.wait_s)
        error = {"error": f"Spotify did not show up as a device within {self.wait_s:.0f} seconds of opening it",
                 "code": "device_timeout"}
        return ToolResult(server, name, False, json.dumps(error), result.ms, arguments=arguments)

    def _pick(self, devices: list[dict[str, Any]], before: set[str], here_only: bool = False) -> str | None:
        """This computer's Spotify: a Computer device that appeared since the launch, else one named as this machine
        is (Spotify names a desktop device by its host name); unless `here_only`, then any device that appeared.
        Another computer's idle Spotify is never picked from before the launch."""
        computers = [d for d in devices if str(d.get("type", "")).lower() == "computer" and _device_id(d)]
        here = [d for d in computers if str(d.get("name", "")).strip().lower() == self.host]
        if here_only:
            return _device_id(here[0]) if here else None
        fresh = [d for d in computers if _device_id(d) not in before]
        others = [d for d in devices if _device_id(d) and _device_id(d) not in before]
        for group in (fresh, here, others):
            if group:
                return _device_id(group[0])
        return None
