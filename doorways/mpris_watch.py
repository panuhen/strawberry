#!/usr/bin/env python3
"""Media doorway: follow every MPRIS player on the session bus and tell strawberryd.

Spotify, VLC, Rhythmbox, and browser tabs with media all register as
org.mpris.MediaPlayer2.<something>. This watches all of them at once:

    any player Playing            -> POST /perform {"state": "dancing"}
    nothing playing / player quit -> POST /perform {"state": "idle"}
    a playing player changes track -> POST /event  {"source": "media", "app": "<Identity>", "title": "Artist — Title"}

Pure stdlib + PyGObject (Gio), which Ubuntu ships; no playerctl needed. Short-lived HTTP
calls only, never a websocket: the daemon owns the widget connection (WIRING.md §0, §4b).

    doorways/mpris_watch.py [--daemon http://127.0.0.1:8770] [--only spotify,vlc] [--ignore firefox]
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import urllib.error
import urllib.request

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

log = logging.getLogger("mpris_watch")

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
ROOT_IFACE = "org.mpris.MediaPlayer2"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
DBUS_NAME = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
DEBOUNCE_MS = 400  # players emit several PropertiesChanged per track change
RETRY_MS = 2000    # re-post a dance/idle state the daemon could not take (it may be restarting)


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


class Watcher:
    def __init__(self, daemon: str, only: set[str], ignore: set[str]) -> None:
        self.daemon = daemon.rstrip("/")
        self.only = only
        self.ignore = ignore
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.players: dict[str, Player] = {}  # keyed by unique owner name (":1.494")
        self.playing_sent: bool | None = None
        self.flush_id = 0

    # --- bus lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self.bus.signal_subscribe(
            None, PROPS_IFACE, "PropertiesChanged", MPRIS_PATH, None, Gio.DBusSignalFlags.NONE, self.on_properties_changed
        )
        self.bus.signal_subscribe(
            DBUS_NAME, DBUS_NAME, "NameOwnerChanged", DBUS_PATH, None, Gio.DBusSignalFlags.NONE, self.on_name_owner_changed
        )
        names = self.bus.call_sync(DBUS_NAME, DBUS_PATH, DBUS_NAME, "ListNames", None, None, Gio.DBusCallFlags.NONE, 2000, None)
        for name in names.unpack()[0]:
            if self.wanted(name):
                owner = self.bus.call_sync(
                    DBUS_NAME, DBUS_PATH, DBUS_NAME, "GetNameOwner", GLib.Variant("(s)", (name,)), None,
                    Gio.DBusCallFlags.NONE, 2000, None,
                ).unpack()[0]
                self.add_player(name, owner)
        scope = ", ".join(sorted(self.only)) if self.only else "all MPRIS players"
        log.info("watching %s -> %s (%d present now)", scope, self.daemon, len(self.players))
        self.schedule_flush()

    def wanted(self, bus_name: str) -> bool:
        if not bus_name.startswith(MPRIS_PREFIX):
            return False
        short = bus_name[len(MPRIS_PREFIX):].split(".")[0].lower()
        if self.only and short not in self.only:
            return False
        return short not in self.ignore

    def add_player(self, bus_name: str, owner: str) -> None:
        player = Player(bus_name, owner)
        try:
            player.identity = str(self.get_property(owner, ROOT_IFACE, "Identity") or player.identity)
            player.status = str(self.get_property(owner, PLAYER_IFACE, "PlaybackStatus") or "Stopped")
            player.metadata = dict(self.get_property(owner, PLAYER_IFACE, "Metadata") or {})
        except GLib.Error as exc:
            log.warning("could not read %s: %s", bus_name, exc.message)
        # A track already playing when we start should not be re-announced later on pause/unpause.
        player.announced_track = player.track_id
        self.players[owner] = player
        log.info("%s appeared as %s, %s", player.identity, bus_name, player.status)

    def remove_player(self, owner: str) -> None:
        player = self.players.pop(owner, None)
        if player:
            log.info("%s (%s) vanished", player.identity, player.bus_name)

    def get_property(self, owner: str, iface: str, prop: str):
        reply = self.bus.call_sync(
            owner, MPRIS_PATH, PROPS_IFACE, "Get", GLib.Variant("(ss)", (iface, prop)), None,
            Gio.DBusCallFlags.NONE, 1500, None,
        )
        return reply.unpack()[0]

    def on_name_owner_changed(self, _conn, _sender, _path, _iface, _signal, params) -> None:
        name, old_owner, new_owner = params.unpack()
        if not self.wanted(name):
            return
        if old_owner:
            self.remove_player(old_owner)
        if new_owner:
            self.add_player(name, new_owner)
        self.schedule_flush()

    def on_properties_changed(self, _conn, sender: str, _path, _iface, _signal, params) -> None:
        player = self.players.get(sender)
        if player is None:
            return  # a player we are not following (filtered out, or not MPRIS-registered yet)
        iface, changed, _invalidated = params.unpack()
        if iface != PLAYER_IFACE:
            return
        if "PlaybackStatus" in changed:
            player.status = str(changed["PlaybackStatus"])
        if "Metadata" in changed:
            player.metadata = dict(changed["Metadata"])
        self.schedule_flush()

    # --- turning player state into daemon calls ------------------------------------

    def schedule_flush(self) -> None:
        if self.flush_id:
            GLib.source_remove(self.flush_id)
        self.flush_id = GLib.timeout_add(DEBOUNCE_MS, self.flush)

    def flush(self) -> bool:
        self.flush_id = 0
        playing_players = [p for p in self.players.values() if p.playing]
        playing = bool(playing_players)

        if playing != self.playing_sent:
            if self.post("/perform", {"state": "dancing" if playing else "idle"}):
                self.playing_sent = playing
            else:
                # The daemon is down or still starting (systemd restarts both of us at once).
                # Her dance state must not be lost: try again shortly. Track announcements are
                # not retried; re-announcing a song she already named would be noise.
                for player in playing_players:
                    player.announced_track = player.track_id
                self.flush_id = GLib.timeout_add(RETRY_MS, self.flush)
                return False

        # Announce a track once, only while it is actually playing. Remembering it while
        # paused means un-pausing the same song is not an announcement.
        for player in playing_players:
            track = player.track_id
            if track and track != player.announced_track:
                title = self.describe(player.metadata)
                if title:
                    self.post("/event", {"source": "media", "app": player.identity, "title": title})
                player.announced_track = track
        return False  # one-shot timeout

    @staticmethod
    def describe(metadata: dict) -> str:
        title = str(metadata.get("xesam:title", "")).strip()
        artists = metadata.get("xesam:artist") or []
        artist = ", ".join(str(a) for a in artists if str(a).strip())
        if title and artist:
            return f"{artist} — {title}"
        return title or artist

    def post(self, path: str, payload: dict) -> bool:
        """True when the daemon answered (even with a rejection); False when it was unreachable."""
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            self.daemon + path, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                reply = json.loads(response.read() or b"{}")
            log.info("%s %s -> %s widget(s)", path, payload, reply.get("sent", "?"))
            return True
        except urllib.error.HTTPError as exc:
            log.warning("%s rejected %s: %s", path, payload, exc.read().decode(errors="replace")[:200])
            return True
        except (urllib.error.URLError, TimeoutError) as exc:
            log.warning("strawberryd unreachable at %s (%s); retrying in %ds", self.daemon, exc, RETRY_MS // 1000)
            return False


def csv_set(value: str) -> set[str]:
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def settings() -> dict:
    """[media] and [daemon] from ~/.config/strawberry/config.toml, if present (WIRING.md §15)."""
    import os
    import tomllib
    from pathlib import Path

    path = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "strawberry" / "config.toml"
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("could not read %s (%s); using defaults", path, exc)
        return {}


def main() -> None:
    cfg = settings()
    media = cfg.get("media", {}) if isinstance(cfg.get("media"), dict) else {}
    port = cfg.get("daemon", {}).get("port", 8770) if isinstance(cfg.get("daemon"), dict) else 8770
    parser = argparse.ArgumentParser(description="MPRIS media players -> strawberryd doorway")
    parser.add_argument("--daemon", default=f"http://127.0.0.1:{port}")
    parser.add_argument("--only", default=",".join(media.get("only", [])), help="comma-separated player names to follow (default: all), e.g. spotify,vlc")
    parser.add_argument("--ignore", default=",".join(media.get("ignore", [])), help="comma-separated player names to skip, e.g. firefox,chromium")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")

    watcher = Watcher(args.daemon, csv_set(args.only), csv_set(args.ignore))
    watcher.start()
    loop = GLib.MainLoop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, lambda *_: (loop.quit(), False)[1])
    loop.run()


if __name__ == "__main__":
    sys.exit(main())
