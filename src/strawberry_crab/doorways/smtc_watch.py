#!/usr/bin/env python3
"""Media doorway on Windows: follow every System Media Transport Controls session and tell strawberryd.

The counterpart of mpris_watch.py, with the same calls to the daemon (WIRING.md §4b):

    any player Playing            -> POST /perform {"state": "dancing"}
    nothing playing / player quit -> POST /perform {"state": "idle"}
    a playing player changes track -> POST /event  {"source": "media", "app": "<name>", "title": "Artist — Title"}

Spotify, the browsers and most players register a session with Windows' session manager
(smtc.py). The manager raises SessionsChanged when a player comes or goes, and each session
raises PlaybackInfoChanged and MediaPropertiesChanged. Those arrive on a WinRT thread, so a
handler only pokes the event loop; the loop then re-reads every session after a 400 ms debounce
and folds that into what it knows. A slow poll (POLL_S) re-reads as well, for a player that does
not raise every event. "<name>" is the app's name from its SourceAppUserModelId (smtc.app_name:
"Spotify", "Google Chrome"), and `--only`/`--ignore` take the short names (smtc.app_key:
spotify, chrome, firefox; the Chromium-based browsers also answer to chromium).

    python -m strawberry_crab.doorways.smtc_watch [--daemon http://127.0.0.1:8770] [--only spotify] [--ignore firefox]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

from ..client import DaemonClient, configure_logging, stop_on_signals
from ..mpris import MprisError
from ..smtc import Sessions, Snapshot, app_name, wanted

log = logging.getLogger("smtc_watch")

DEBOUNCE_S = 0.4   # a track change raises several events at once
RETRY_S = 2.0      # re-post a dance/idle state the daemon could not take (it may be restarting)
POLL_S = 5.0       # re-read every session this often even without an event


class Player:
    """What the watcher knows of one session."""

    def __init__(self, snapshot: Snapshot) -> None:
        self.address = snapshot.address
        self.identity = app_name(snapshot.app_id)
        self.status = "Stopped"
        self.title = self.artist = self.album = ""
        self.update(snapshot)
        # A track already playing when we first see the player is not announced later on pause/unpause.
        self.announced_track = self.track_id

    def update(self, snapshot: Snapshot) -> None:
        if snapshot.status != "Changing":     # between two tracks: keep what it was doing
            self.status = snapshot.status
        self.title, self.artist, self.album = snapshot.title, snapshot.artist, snapshot.album

    @property
    def playing(self) -> bool:
        return self.status == "Playing"

    @property
    def track_id(self) -> str | None:
        return "\n".join((self.title, self.artist, self.album)) if self.title else None


def describe(artist: str, title: str) -> str:
    """"Artist — Title", or whichever half of it is there (as mpris_watch.describe)."""
    if title and artist:
        return f"{artist} — {title}"
    return title or artist


class Watcher:
    def __init__(self, daemon: str, only: set[str], ignore: set[str], sessions: Sessions | None = None) -> None:
        self.daemon = DaemonClient(daemon)
        self.only = only
        self.ignore = ignore
        self.sessions = sessions or Sessions()
        self.players: dict[str, Player] = {}  # keyed by Snapshot.address ("Spotify.exe", "Chrome#2")
        self.playing_sent: bool | None = None
        self.flush_task: asyncio.Task | None = None
        self.refresh_task: asyncio.Task | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.subscriptions: list[tuple[Any, str, Any]] = []   # (source, event name, token)
        self.subscribed: tuple[str, ...] = ()                  # the session addresses they cover

    # --- reading the sessions ----------------------------------------------------

    async def refresh(self) -> None:
        """Read every session and fold it in: players that appeared, changed or went away."""
        try:
            snapshots = [s for s in await self.sessions.read() if wanted(s.app_id, self.only, self.ignore)]
        except MprisError as exc:
            log.warning("could not read the media sessions: %s", exc)
            return
        self.apply(snapshots)
        addresses = tuple(s.address for s in snapshots)
        if addresses != self.subscribed:
            self.subscribe_sessions(snapshots)

    def apply(self, snapshots: list[Snapshot]) -> None:
        present = {s.address for s in snapshots}
        for address in [a for a in self.players if a not in present]:
            player = self.players.pop(address)
            log.info("%s (%s) vanished", player.identity, address)
        for snapshot in snapshots:
            player = self.players.get(snapshot.address)
            if player is None:
                player = self.players[snapshot.address] = Player(snapshot)
                log.info("%s appeared as %s, %s", player.identity, snapshot.address, player.status)
            else:
                player.update(snapshot)

    # --- events from Windows --------------------------------------------------------

    def on_event(self, _sender: Any = None, _args: Any = None) -> None:
        """A WinRT handler: runs on a WinRT thread, so it only hands the poke to the loop."""
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self.schedule_refresh)
        except RuntimeError:  # the loop closed between the check and the call (shutting down)
            pass

    def subscribe_manager(self, manager: Any) -> None:
        self.subscriptions.append((manager, "sessions_changed", manager.add_sessions_changed(self.on_event)))

    def subscribe_sessions(self, snapshots: list[Snapshot]) -> None:
        """Follow the sessions of this read (a new list after every SessionsChanged)."""
        self.unsubscribe(keep_manager=True)
        for snapshot in snapshots:
            session = snapshot.session
            if session is None:
                continue
            for event in ("playback_info_changed", "media_properties_changed"):
                try:
                    token = getattr(session, f"add_{event}")(self.on_event)
                except Exception as exc:  # noqa: BLE001 - a session that closed meanwhile; the poll covers it
                    log.debug("could not follow %s of %s: %s", event, snapshot.address, exc)
                    continue
                self.subscriptions.append((session, event, token))
        self.subscribed = tuple(s.address for s in snapshots)

    def unsubscribe(self, keep_manager: bool = False) -> None:
        kept = []
        for source, event, token in self.subscriptions:
            if keep_manager and event == "sessions_changed":
                kept.append((source, event, token))
                continue
            try:
                getattr(source, f"remove_{event}")(token)
            except Exception:  # noqa: BLE001 - a session that is gone has nothing to remove
                pass
        self.subscriptions = kept
        if not keep_manager:
            self.subscribed = ()

    # --- turning player state into daemon calls ------------------------------------

    def schedule_refresh(self, delay: float = DEBOUNCE_S) -> None:
        if self.refresh_task and not self.refresh_task.done():
            self.refresh_task.cancel()
        self.refresh_task = asyncio.ensure_future(self._refresh_after(delay))

    async def _refresh_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self.refresh()
        self.schedule_flush(0.0)     # through flush_task, so one flush (or its retry) runs at a time

    def schedule_flush(self, delay: float = DEBOUNCE_S) -> None:
        if self.flush_task and not self.flush_task.done():
            self.flush_task.cancel()
        self.flush_task = asyncio.ensure_future(self._flush_after(delay))

    async def _flush_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self.flush()

    async def flush(self) -> None:
        playing_players = [p for p in self.players.values() if p.playing]
        playing = bool(playing_players)

        if playing != self.playing_sent:
            if await self.daemon.post("/perform", {"state": "dancing" if playing else "idle"}):
                self.playing_sent = playing
            else:
                # The daemon is down or still starting: her dance state must not be lost, so try
                # again shortly. Track announcements are not retried (mpris_watch does the same).
                for player in playing_players:
                    player.announced_track = player.track_id
                self.schedule_flush(RETRY_S)
                return

        # Announce a track once, only while it is actually playing. Remembering it while
        # paused means un-pausing the same song is not an announcement.
        for player in playing_players:
            track = player.track_id
            if track and track != player.announced_track:
                # Marked before the post: a flush cancelled mid-post by the next one must not
                # announce the same song twice.
                player.announced_track = track
                title = describe(player.artist, player.title)
                if title:
                    await self.daemon.post("/event", {"source": "media", "app": player.identity, "title": title})

    # --- the loop ------------------------------------------------------------------

    async def poll(self) -> None:
        while True:
            await asyncio.sleep(POLL_S)
            self.schedule_refresh(0.0)

    async def run(self, stopping: asyncio.Event) -> int:
        self.loop = asyncio.get_running_loop()
        try:
            manager = await self.sessions.manager()
        except MprisError as exc:
            log.error("no media sessions to follow (%s); exiting", exc)
            return 3
        poller: asyncio.Future | None = None
        try:
            self.subscribe_manager(manager)
            await self.refresh()
            scope = ", ".join(sorted(self.only)) if self.only else "all media sessions"
            log.info("watching %s -> %s (%d present now)", scope, self.daemon.url, len(self.players))
            self.schedule_flush()
            poller = asyncio.ensure_future(self.poll())
            await stopping.wait()
            return 0
        finally:
            for task in (poller, self.refresh_task, self.flush_task):
                if task is not None and not task.done():
                    task.cancel()
            self.unsubscribe()
            self.loop = None


def csv_set(value: str) -> set[str]:
    return {item.strip().lower() for item in value.split(",") if item.strip()}


async def watch(daemon: str, only: set[str], ignore: set[str]) -> int:
    stopping = asyncio.Event()
    stop_on_signals(stopping)
    return await Watcher(daemon, only, ignore).run(stopping)


def main() -> int:
    from ..config import load

    config = load()
    parser = argparse.ArgumentParser(description="Windows media sessions (SMTC) -> strawberryd doorway")
    parser.add_argument("--daemon", default=f"http://{config.daemon.host}:{config.daemon.port}")
    parser.add_argument("--only", default=",".join(config.media.only),
                        help="comma-separated player names to follow (default: all), e.g. spotify,chrome")
    parser.add_argument("--ignore", default=",".join(config.media.ignore),
                        help="comma-separated player names to skip, e.g. firefox,chromium")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    configure_logging(args.log_level)
    try:
        return asyncio.run(watch(args.daemon, csv_set(args.only), csv_set(args.ignore)))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
