"""The beat's capture on Windows: WASAPI process loopback of the player's own processes
(WIRING.md §4c, WINDOWS.md step 5). beat_watch.py drives it; beat_pipewire.py is the Linux one.

    the media session that plays (SMTC)  ->  the process that renders its sound  ->  process loopback

Which player: the System Media Transport Controls session that is Playing (Windows' current one
first), or the one `[beat] target` names (`spotify`, `chrome`: the short names of smtc.app_key).
A session names its app, not its process (`SourceAppUserModelId`), so the process is found
among the audio sessions of the output devices, which do name their process:

- a packaged app (`<family>!<app>`, Spotify from the Store, Media Player): the process whose own
  AppUserModelId (GetApplicationUserModelId) is the session's;
- a desktop app (`Spotify.exe`, `Chrome`, `MSEdge`, `python.exe`): the process whose image name
  gives the same app key (`spotify.exe` -> spotify, `chrome.exe` -> chrome);
- an audio session that is rendering right now wins over one that is only open.

From that process we climb to the topmost ancestor with the same image name (a browser's audio
service is a child of the browser) and capture that whole tree. Firefox installed outside its
default folder has an id that is a hash of the folder and matches no process; set `[beat]
target = "firefox"` for it. A known player (PLAYERS, or the target) that renders sound and has
no media session at all is taken too; one whose media session says paused is not. Our own
processes (this one's tree, the tray's children, the widget) are never a target.

The capture itself is wasapi.LoopbackCapture: mono float32 at the tracker's rate, event driven,
converted by the audio engine. It follows the session volume and mute: what the player's
session plays at zero volume reaches us as silence (seen live, WINDOWS.md step 5).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from .. import wasapi
from ..mpris import MprisError
from ..smtc import Sessions, Snapshot, app_key, app_name, app_names

log = logging.getLogger("beat_watch")

NAME = "WASAPI process loopback"
TARGET_HELP = "the player's short name, e.g. spotify or chrome (default: the media session that plays)"
# Players that may not register a media session, taken when none plays (app keys of their image names).
PLAYERS = ("spotify", "vlc", "foobar2000", "musicbee", "aimp", "winamp", "itunes", "tidal", "deezer", "qobuz",
           "mpv", "mpc-hc64", "mpc-hc", "mpc-be64")
OURS = ("python", "pythonw", "strawberry", "strawberryd", "strawberry-tray", "strawberry-doorway")
WIDGET = ("godot", "strawberry-widget")   # her own voice plays here


@dataclass(frozen=True)
class Target:
    pid: int            # the root of the process tree we capture
    exe: str            # its image name
    app: str            # the name she would say ("Spotify")
    app_id: str = ""    # the media session's SourceAppUserModelId; "" when found without one

    @property
    def name(self) -> str:
        return f"{self.app} ({self.exe}, pid {self.pid})"


def exe_key(exe: str) -> str:
    """'spotify' from Spotify.exe, 'strawberry-widget-0.1.1-windows-x86_64' from its binary."""
    name = exe.lower()
    return name[:-4] if name.endswith(".exe") else name


def ours(table: dict[int, wasapi.Process], me: int) -> set[int]:
    """This process, the processes above it that are ours too (a venv launcher, the tray, uv's
    script launchers), and everything under the topmost of them: the daemon, the other doorways."""
    top, seen = me, {me}
    while True:
        parent = table.get(top)
        if parent is None or parent.parent in seen or parent.parent not in table:
            break
        if exe_key(table[parent.parent].exe) not in OURS:
            break
        top = parent.parent
        seen.add(top)
    found = {me, top}
    frontier = [top]
    while frontier:
        pid = frontier.pop()
        for proc in table.values():
            if proc.parent == pid and proc.pid not in found and proc.pid != pid:
                found.add(proc.pid)
                frontier.append(proc.pid)
    return found


def tree_root(pid: int, table: dict[int, wasapi.Process]) -> int:
    """The topmost ancestor with the same image name: the browser above its audio service."""
    exe = table[pid].exe.lower()
    seen = {pid}
    while True:
        parent = table[pid].parent
        if parent in seen or parent not in table or table[parent].exe.lower() != exe:
            return pid
        seen.add(parent)
        pid = parent


def player_process(app_id: str, sessions: list[wasapi.AudioSession], table: dict[int, wasapi.Process],
                   package_app_id: Callable[[int], str], excluded: set[int]) -> int | None:
    """The pid of a process that renders `app_id`'s sound (see the module doc), or None."""
    packaged = "!" in app_id
    key = app_key(app_id)

    def belongs(pid: int) -> bool:
        proc = table.get(pid)
        if proc is None or pid in excluded or not pid:
            return False
        if any(exe_key(proc.exe).startswith(w) for w in WIDGET):
            return False
        if packaged and package_app_id(pid).lower() == app_id.lower():
            return True
        return app_key(proc.exe) == key

    for active in (True, False):
        for session in sessions:
            if session.active == active and belongs(session.pid):
                return session.pid
    return None


class LoopbackStream:
    """One process loopback capture, in the shape beat_watch.Watcher reads."""

    how = "process loopback"

    def __init__(self, target: Target, capture: Any, chunk_samples: int) -> None:
        self.target = target
        self.name = target.name
        self.capture = capture
        self.min_samples = chunk_samples // 2   # about 46 ms at 22050 Hz, as pw-record's reads on Linux

    def read(self, timeout_s: float) -> np.ndarray | None:
        """The samples since the last read, None when no packet came in `timeout_s`, EOFError once
        the player's process has ended."""
        if not self.capture.alive():
            raise EOFError
        samples = self.capture.read(timeout_s, self.min_samples)
        if samples is None and not self.capture.alive():
            raise EOFError
        return samples

    def verify(self, _elapsed_s: float) -> str | None:
        """Nothing to check: a process loopback hears that process tree or nothing."""
        return None

    def close(self) -> None:
        self.capture.close()


class System:
    """What the backend asks Windows; the unit tests hand it a fake."""

    audio_sessions = staticmethod(wasapi.audio_sessions)
    processes = staticmethod(wasapi.processes)
    package_app_id = staticmethod(wasapi.package_app_id)
    capture = staticmethod(wasapi.LoopbackCapture)


class Backend:
    """What beat_watch.Watcher asks of a capture (as beat_pipewire.Backend): find the player,
    open a capture of it, and whether the player still plays."""

    name = NAME

    def __init__(self, sessions: Sessions | None = None, system: Any = None) -> None:
        self.sessions = sessions or Sessions()
        self.system = system or System()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.me = os.getpid()
        self.unmatched: set[str] = set()   # media sessions already logged as "no process found"
        self.failed = False                # the media sessions could not be read (logged once)

    # --- the media sessions ----------------------------------------------------------

    def media_sessions(self) -> list[Snapshot]:
        if self.loop is None:
            self.loop = asyncio.new_event_loop()
        try:
            snapshots = self.loop.run_until_complete(self.sessions.read())
        except MprisError as exc:
            if not self.failed:
                log.warning("could not read the media sessions (%s); only a known player's audio is followed", exc)
                self.failed = True
            return []
        self.failed = False
        return snapshots

    def find(self, target: str) -> Target | None:
        want = target.strip().lower()
        try:
            table = self.system.processes()
            sessions = self.system.audio_sessions()
        except OSError as exc:
            log.warning("could not list the audio sessions: %s", exc)
            return None
        excluded = ours(table, self.me)
        snapshots = self.media_sessions()
        for snapshot in snapshots:
            if snapshot.status != "Playing" or (want and want not in app_names(snapshot.app_id)):
                continue
            pid = player_process(snapshot.app_id, sessions, table, self.system.package_app_id, excluded)
            if pid is None:
                if snapshot.app_id not in self.unmatched:
                    self.unmatched.add(snapshot.app_id)
                    log.info("%s plays, but no process of its renders sound", app_name(snapshot.app_id))
                continue
            root = tree_root(pid, table)
            return Target(root, table[root].exe, app_name(snapshot.app_id), snapshot.app_id)
        # A known player that renders sound and has no media session to say whether it plays.
        keys = {want} if want else set(PLAYERS)
        keys -= {app_key(s.app_id) for s in snapshots}
        for session in sessions:
            proc = table.get(session.pid)
            if session.active and proc and session.pid not in excluded and app_key(proc.exe) in keys:
                root = tree_root(session.pid, table)
                return Target(root, table[root].exe, app_name(proc.exe))
        return None

    # --- the capture ---------------------------------------------------------------

    def open(self, target: Target, rate: int, chunk_samples: int) -> LoopbackStream:
        log.info("listening to %s by process loopback", target.name)
        try:
            capture = self.system.capture(target.pid, rate)
        except OSError as exc:
            raise OSError(f"process loopback of {target.name} refused: {exc} (it needs Windows 10 2004 or later)") from exc
        return LoopbackStream(target, capture, chunk_samples)

    def after(self, _outcome: str) -> float:
        return 1.0

    def running(self, target: Target) -> bool:
        """The player still says it plays (its media session, or its audio session when it has none)."""
        if target.app_id:
            return any(s.app_id == target.app_id and s.status == "Playing" for s in self.media_sessions())
        try:
            return any(s.pid == target.pid and s.active for s in self.system.audio_sessions())
        except OSError:
            return False

    def superseded(self, target: Target, setting: str) -> bool:
        """While ours is silent: does another player play now? Then the watcher lets this one go."""
        found = self.find(setting)
        return found is not None and found.pid != target.pid

    def close(self) -> None:
        if self.loop is not None:
            self.loop.close()
            self.loop = None
