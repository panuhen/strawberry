#!/usr/bin/env python3
"""Media doorway: follow every MPRIS player on the session bus and tell strawberryd.

Spotify, VLC, Rhythmbox, and browser tabs with media all register as
org.mpris.MediaPlayer2.<something>. This watches all of them at once:

    any player Playing            -> POST /perform {"state": "dancing"}
    nothing playing / player quit -> POST /perform {"state": "idle"}
    a playing player changes track -> POST /event  {"source": "media", "app": "<Identity>", "title": "Artist — Title"}

Two subscriptions carry all of that: `PropertiesChanged` on org.mpris.MediaPlayer2.Player for
what a known player is doing, and `NameOwnerChanged` for players appearing and disappearing.
jeepney speaks the bus, so this runs on the daemon's venv; the calls to the daemon are
short-lived HTTP, never a websocket (WIRING.md §0, §4b).

    python -m strawberry_crab.doorways.mpris_watch [--daemon http://127.0.0.1:8770] [--only spotify,vlc] [--ignore firefox]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

from jeepney import DBusAddress, MatchRule

from ..bus import BusClient, BusError, field, open_session_bus, plain
from ..client import DaemonClient, configure_logging, stop_on_signals

log = logging.getLogger("mpris_watch")

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
ROOT_IFACE = "org.mpris.MediaPlayer2"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
DBUS_NAME = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
DBUS = DBusAddress(DBUS_PATH, bus_name=DBUS_NAME, interface=DBUS_NAME)
DEBOUNCE_S = 0.4   # players emit several PropertiesChanged per track change
RETRY_S = 2.0      # re-post a dance/idle state the daemon could not take (it may be restarting)
QUEUE_SIZE = 256   # signals waiting to be handled; a busy player emits a handful per track

PROPERTIES_RULE = MatchRule(type="signal", interface=PROPS_IFACE, member="PropertiesChanged", path=MPRIS_PATH)
NAME_OWNER_RULE = MatchRule(type="signal", sender=DBUS_NAME, interface=DBUS_NAME, member="NameOwnerChanged",
                            path=DBUS_PATH)
NAME_OWNER_RULE.add_arg_condition(0, ROOT_IFACE, kind="namespace")


class Player:
    def __init__(self, bus_name: str, owner: str) -> None:
        self.bus_name = bus_name
        self.owner = owner
        self.identity = bus_name[len(MPRIS_PREFIX):].split(".")[0].capitalize()
        self.status = "Stopped"
        self.metadata: dict = {}
        self.announced_track: str | None = None

    @property
    def playing(self) -> bool:
        return self.status == "Playing"

    @property
    def track_id(self) -> str | None:
        return str(self.metadata.get("mpris:trackid", "")) or None


def describe(metadata: dict) -> str:
    """"Artist — Title" from the MPRIS metadata map, or whichever half of it is there."""
    title = str(metadata.get("xesam:title", "")).strip()
    artists = metadata.get("xesam:artist") or []
    if isinstance(artists, str):
        artists = [artists]
    artist = ", ".join(str(a) for a in artists if str(a).strip())
    if title and artist:
        return f"{artist} — {title}"
    return title or artist


def apply_properties(player: Player, body: tuple | list) -> bool:
    """Fold one PropertiesChanged body into the player. True if it was about playback.

    The body is (interface, changed a{sv}, invalidated as); every value in `changed` is a
    variant, and Metadata is a variant holding a whole a{sv} of its own.
    """
    iface, changed, _invalidated = body
    if iface != PLAYER_IFACE:
        return False
    changed = plain(changed)
    if "PlaybackStatus" in changed:
        player.status = str(changed["PlaybackStatus"])
    if "Metadata" in changed:
        player.metadata = dict(changed["Metadata"] or {})
    return True


def wanted(bus_name: str, only: set[str], ignore: set[str]) -> bool:
    if not bus_name.startswith(MPRIS_PREFIX):
        return False
    short = bus_name[len(MPRIS_PREFIX):].split(".")[0].lower()
    if only and short not in only:
        return False
    return short not in ignore


class Watcher:
    def __init__(self, daemon: str, only: set[str], ignore: set[str]) -> None:
        self.daemon = DaemonClient(daemon)
        self.only = only
        self.ignore = ignore
        self.players: dict[str, Player] = {}  # keyed by unique owner name (":1.494")
        self.playing_sent: bool | None = None
        self.client: BusClient | None = None
        self.flush_task: asyncio.Task | None = None

    # --- bus lifecycle -----------------------------------------------------------

    async def get_property(self, owner: str, iface: str, prop: str) -> Any:
        address = DBusAddress(MPRIS_PATH, bus_name=owner, interface=PROPS_IFACE)
        body = await self.client.call_method(address, "Get", "ss", (iface, prop), timeout=1.5)
        return plain(body[0])

    async def start(self) -> None:
        """Subscribe, then take stock of the players that are already there."""
        await self.client.add_match(PROPERTIES_RULE)
        await self.client.add_match(NAME_OWNER_RULE)
        names = (await self.client.call_method(DBUS, "ListNames"))[0]
        for name in names:
            if wanted(name, self.only, self.ignore):
                owner = (await self.client.call_method(DBUS, "GetNameOwner", "s", (name,)))[0]
                await self.add_player(name, owner)
        scope = ", ".join(sorted(self.only)) if self.only else "all MPRIS players"
        log.info("watching %s -> %s (%d present now)", scope, self.daemon.url, len(self.players))
        self.schedule_flush()

    async def add_player(self, bus_name: str, owner: str) -> None:
        player = Player(bus_name, owner)
        try:
            player.identity = str(await self.get_property(owner, ROOT_IFACE, "Identity") or player.identity)
            player.status = str(await self.get_property(owner, PLAYER_IFACE, "PlaybackStatus") or "Stopped")
            player.metadata = dict(await self.get_property(owner, PLAYER_IFACE, "Metadata") or {})
        except (BusError, OSError, asyncio.TimeoutError) as exc:
            log.warning("could not read %s: %s", bus_name, exc)
        # A track already playing when we start should not be re-announced later on pause/unpause.
        player.announced_track = player.track_id
        self.players[owner] = player
        log.info("%s appeared as %s, %s", player.identity, bus_name, player.status)

    def remove_player(self, owner: str) -> None:
        player = self.players.pop(owner, None)
        if player:
            log.info("%s (%s) vanished", player.identity, player.bus_name)

    async def on_name_owner_changed(self, body: tuple) -> None:
        name, old_owner, new_owner = body
        if not wanted(name, self.only, self.ignore):
            return
        if old_owner:
            self.remove_player(old_owner)
        if new_owner:
            await self.add_player(name, new_owner)
        self.schedule_flush()

    def on_properties_changed(self, sender: str, body: tuple) -> None:
        player = self.players.get(sender)
        if player is None:
            return  # a player we are not following (filtered out, or not MPRIS-registered yet)
        if apply_properties(player, body):
            self.schedule_flush()

    # --- turning player state into daemon calls ------------------------------------

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
                # The daemon is down or still starting (the tray starts both of us at once).
                # Her dance state must not be lost: try again shortly. Track announcements are
                # not retried; re-announcing a song she already named would be noise.
                for player in playing_players:
                    player.announced_track = player.track_id
                self.schedule_flush(RETRY_S)
                return

        # Announce a track once, only while it is actually playing. Remembering it while
        # paused means un-pausing the same song is not an announcement.
        for player in playing_players:
            track = player.track_id
            if track and track != player.announced_track:
                title = describe(player.metadata)
                if title:
                    await self.daemon.post("/event", {"source": "media", "app": player.identity, "title": title})
                player.announced_track = track

    # --- the loop ------------------------------------------------------------------

    async def run(self, stopping: asyncio.Event) -> int:
        self.client = await open_session_bus(queue_size=QUEUE_SIZE)
        reader = asyncio.ensure_future(self.client.run())
        try:
            await self.start()
            consumer = asyncio.ensure_future(self.client.serve(self.dispatch))
            stop = asyncio.ensure_future(stopping.wait())
            await asyncio.wait([reader, consumer, stop], return_when=asyncio.FIRST_COMPLETED)
            for task in (consumer, stop):
                task.cancel()
            if reader.done():
                log.error("the session bus closed the connection (%s); exiting", self.client.error)
                return 3
            return 0
        finally:
            if self.flush_task and not self.flush_task.done():
                self.flush_task.cancel()
            reader.cancel()
            await self.client.close()

    async def dispatch(self, message) -> None:
        member = field(message, "member")
        if member == "NameOwnerChanged":
            await self.on_name_owner_changed(message.body)
        elif member == "PropertiesChanged" and field(message, "path") == MPRIS_PATH:
            self.on_properties_changed(field(message, "sender"), message.body)


def csv_set(value: str) -> set[str]:
    return {item.strip().lower() for item in value.split(",") if item.strip()}


async def watch(daemon: str, only: set[str], ignore: set[str]) -> int:
    stopping = asyncio.Event()
    stop_on_signals(stopping)
    return await Watcher(daemon, only, ignore).run(stopping)


def main() -> int:
    from ..config import load

    config = load()
    parser = argparse.ArgumentParser(description="MPRIS media players -> strawberryd doorway")
    parser.add_argument("--daemon", default=f"http://{config.daemon.host}:{config.daemon.port}")
    parser.add_argument("--only", default=",".join(config.media.only),
                        help="comma-separated player names to follow (default: all), e.g. spotify,vlc")
    parser.add_argument("--ignore", default=",".join(config.media.ignore),
                        help="comma-separated player names to skip, e.g. firefox,chromium")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    configure_logging(args.log_level)
    try:
        return asyncio.run(watch(args.daemon, csv_set(args.only), csv_set(args.ignore)))
    except KeyboardInterrupt:
        return 0
    except (ConnectionError, OSError) as exc:
        log.error("no session bus (%s)", exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())
