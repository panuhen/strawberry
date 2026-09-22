"""Music control for any player, over MPRIS (WIRING.md §8b).

Skip, previous, pause, resume, volume and "what's playing" are things every desktop media
player already offers on the session bus as `org.mpris.MediaPlayer2.Player`, so they need no
MCP server and no account: the gate's bare music reflexes land here whenever no configured
server covers them (see `actions.Actor.reflex_for`). Spotify, VLC, Rhythmbox, mpv and a
browser tab with a video all speak it.

The outcomes are the same shape and the same sentences the server reflexes produce (an
`Outcome` whose `fact` is one plain sentence), so the daemon's report path and Gemma's quip
after it do not know which one answered.

Not every player implements everything: a `CanGoNext` of false, a missing `Volume`, a player
that refuses the call — each becomes a plain sentence saying so rather than a silent failure.

jeepney does the bus (pure Python, asyncio); the `Bus` seam below is what the unit tests
replace, so nothing in the tests touches a real session bus.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from .actions import Outcome

log = logging.getLogger("strawberryd.mpris")

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
ROOT_IFACE = "org.mpris.MediaPlayer2"
DBUS_NAME = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"

TOPIC = "music"          # the gate topic whose bare tools these are
VOLUME_STEP = 0.15       # MPRIS volume is 0.0-1.0; a sixth of the range per step
SETTLE_S = 0.4           # players report the old track for a moment after Next/Previous


class MprisError(RuntimeError):
    """A player or the bus said no. The message is what she tells the user."""


# ----------------------------------------------------------------------------- the bus seam


class Bus(Protocol):
    """What MPRIS needs from a session bus. `SessionBus` is the real one; tests pass a fake."""

    async def list_names(self) -> list[str]: ...

    async def get_all(self, bus_name: str, interface: str) -> dict[str, Any]: ...

    async def get(self, bus_name: str, interface: str, prop: str) -> Any: ...

    async def set(self, bus_name: str, interface: str, prop: str, signature: str, value: Any) -> None: ...

    async def call(self, bus_name: str, interface: str, member: str) -> None: ...

    async def close(self) -> None: ...


def unvariant(value: Any) -> Any:
    """jeepney hands variants back as ("s", "Blue Monday"); unwrap them, containers included."""
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        return unvariant(value[1])
    if isinstance(value, dict):
        return {k: unvariant(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unvariant(v) for v in value]
    return value


class SessionBus:
    """jeepney on the session bus: one connection, reopened after a failure, one call at a time."""

    def __init__(self, timeout_s: float = 4.0) -> None:
        self.timeout_s = timeout_s
        self._router: Any = None
        self._conn: Any = None
        self._lock = asyncio.Lock()

    async def _router_ready(self) -> Any:
        if self._router is not None:
            return self._router
        from jeepney.io.asyncio import DBusRouter, open_dbus_connection

        try:
            self._conn = await asyncio.wait_for(open_dbus_connection("SESSION"), self.timeout_s)
        except Exception as exc:  # no DISPLAY, no session bus, a socket that went away
            raise MprisError("I can't reach the desktop's media bus") from exc
        self._router = DBusRouter(self._conn)
        return self._router

    async def _reply(self, message: Any) -> tuple:
        from jeepney import MessageType

        async with self._lock:
            router = await self._router_ready()
            try:
                reply = await asyncio.wait_for(router.send_and_get_reply(message), self.timeout_s)
            except asyncio.TimeoutError as exc:
                await self._drop()
                raise MprisError("the player did not answer in time") from exc
            except Exception as exc:
                await self._drop()
                raise MprisError(_short(exc)) from exc
        if reply.header.message_type is MessageType.error:
            raise MprisError(_short_body(reply.body))
        return reply.body

    async def _drop(self) -> None:
        """Forget the connection; the next call opens a new one (a player restart, a bus hiccup)."""
        router, conn = self._router, self._conn
        self._router = self._conn = None
        if router is not None:
            try:
                await router.__aexit__(None, None, None)   # stops the receiver task
            except Exception:  # a connection we are throwing away anyway
                pass
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass

    def _address(self, bus_name: str, interface: str, path: str = MPRIS_PATH) -> Any:
        from jeepney import DBusAddress

        return DBusAddress(path, bus_name=bus_name, interface=interface)

    async def list_names(self) -> list[str]:
        from jeepney import new_method_call

        body = await self._reply(new_method_call(self._address(DBUS_NAME, DBUS_NAME, DBUS_PATH), "ListNames"))
        return list(body[0]) if body else []

    async def get_all(self, bus_name: str, interface: str) -> dict[str, Any]:
        from jeepney import new_method_call

        props = self._address(bus_name, "org.freedesktop.DBus.Properties")
        body = await self._reply(new_method_call(props, "GetAll", "s", (interface,)))
        return {k: unvariant(v) for k, v in (body[0] if body else {}).items()}

    async def get(self, bus_name: str, interface: str, prop: str) -> Any:
        from jeepney import new_method_call

        props = self._address(bus_name, "org.freedesktop.DBus.Properties")
        body = await self._reply(new_method_call(props, "Get", "ss", (interface, prop)))
        return unvariant(body[0]) if body else None

    async def set(self, bus_name: str, interface: str, prop: str, signature: str, value: Any) -> None:
        from jeepney import new_method_call

        props = self._address(bus_name, "org.freedesktop.DBus.Properties")
        await self._reply(new_method_call(props, "Set", "ssv", (interface, prop, (signature, value))))

    async def call(self, bus_name: str, interface: str, member: str) -> None:
        from jeepney import new_method_call

        await self._reply(new_method_call(self._address(bus_name, interface), member))

    async def close(self) -> None:
        await self._drop()


def _short(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return text[:120]


def _short_body(body: Any) -> str:
    if isinstance(body, tuple) and body and isinstance(body[0], str):
        return body[0].strip().splitlines()[0][:120]
    return "it refused"


# ----------------------------------------------------------------------------- one player


@dataclass
class Player:
    """One MPRIS player and the properties read from it in a single GetAll."""

    bus_name: str
    props: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return str(self.props.get("PlaybackStatus", "Stopped"))

    @property
    def playing(self) -> bool:
        return self.status == "Playing"

    @property
    def metadata(self) -> dict[str, Any]:
        data = self.props.get("Metadata")
        return data if isinstance(data, dict) else {}

    @property
    def title(self) -> str:
        return str(self.metadata.get("xesam:title", "")).strip()

    @property
    def artists(self) -> list[str]:
        value = self.metadata.get("xesam:artist") or self.metadata.get("xesam:albumArtist") or []
        if isinstance(value, str):
            value = [value]
        return [str(a).strip() for a in value if str(a).strip()]

    @property
    def album(self) -> str:
        return str(self.metadata.get("xesam:album", "")).strip()

    @property
    def track(self) -> str:
        """'Blue Monday by New Order'; '' when the player says nothing about a track."""
        if not self.title:
            return ""
        return f"{self.title} by {', '.join(self.artists) or 'an unknown artist'}"

    @property
    def fallback_name(self) -> str:
        """'Spotify' from org.mpris.MediaPlayer2.spotify, until Identity has been read."""
        tail = self.bus_name[len(MPRIS_PREFIX):].split(".")[0] if self.bus_name.startswith(MPRIS_PREFIX) else self.bus_name
        return tail[:1].upper() + tail[1:] if tail else "the player"

    def can(self, capability: str) -> bool:
        """A player that does not say is assumed able; one that says CanControl false is not."""
        if not bool(self.props.get("CanControl", True)):
            return False
        return bool(self.props.get(capability, True))

    @property
    def volume(self) -> float | None:
        value = self.props.get("Volume")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# ----------------------------------------------------------------------------- the reflexes


Reflex = Callable[[Any, str], Awaitable[Outcome]]


class Mpris:
    """The bare music reflexes over whichever player is active.

    Picking one: a player that is Playing wins; between several, the one she acted on last,
    then the first the bus lists. With nothing playing the same rule runs over the paused
    players, so "play" resumes what was paused rather than waking a silent browser tab.
    """

    def __init__(self, bus: Bus | None = None, timeout_s: float = 4.0, settle_s: float = SETTLE_S) -> None:
        self.bus: Bus = bus if bus is not None else SessionBus(timeout_s)
        self.settle_s = settle_s
        self.last = ""                       # bus name of the player she acted on last
        self.identities: dict[str, str] = {}  # bus name -> Identity ("Spotify", "VLC media player")
        self._reflexes: dict[str, Reflex] = {
            "skip": _bare(self.skip),
            "previous": _bare(self.previous),
            "pause": _bare(self.pause),
            "resume": _bare(self.play),
            "volume_up": _bare(lambda: self.volume_step(VOLUME_STEP)),
            "volume_down": _bare(lambda: self.volume_step(-VOLUME_STEP)),
            "now_playing": _bare(self.now_playing),
        }

    def reflexes(self) -> dict[str, Reflex]:
        """Keyed by the gate's music tool, in the shape `Actor` calls a server reflex with (the
        toolbox and server name mean nothing here and are ignored)."""
        return self._reflexes

    async def close(self) -> None:
        await self.bus.close()

    # --- reading the bus ---------------------------------------------------------

    async def players(self) -> list[Player]:
        """Every MPRIS player that answers, with its Player properties read in one call each."""
        names = [n for n in await self.bus.list_names() if n.startswith(MPRIS_PREFIX)]
        players: list[Player] = []
        for name in sorted(names):
            try:
                players.append(Player(name, await self.bus.get_all(name, PLAYER_IFACE)))
            except MprisError as exc:  # a player that quit between ListNames and the read
                log.debug("mpris: %s did not answer (%s)", name, exc)
        return players

    async def active(self) -> Player | None:
        """The player a bare command is about, or None when no player is running."""
        players = await self.players()
        if not players:
            return None
        chosen = (self._prefer([p for p in players if p.playing])
                  or self._prefer([p for p in players if p.status == "Paused"])
                  or self._prefer(players))
        if chosen:
            self.last = chosen.bus_name
        return chosen

    def _prefer(self, players: list[Player]) -> Player | None:
        """The one she acted on last if it is among these, else the first."""
        for player in players:
            if player.bus_name == self.last:
                return player
        return players[0] if players else None

    async def name_of(self, player: Player) -> str:
        """The player's own Identity ('VLC media player'), read once and remembered."""
        if player.bus_name not in self.identities:
            try:
                identity = await self.bus.get(player.bus_name, ROOT_IFACE, "Identity")
            except MprisError:
                identity = ""
            self.identities[player.bus_name] = str(identity).strip() or player.fallback_name
        return self.identities[player.bus_name]

    async def reread(self, player: Player) -> Player:
        """The same player again after acting: Next reports the old track for a moment."""
        try:
            return Player(player.bus_name, await self.bus.get_all(player.bus_name, PLAYER_IFACE))
        except MprisError:
            return player

    # --- the reflexes ------------------------------------------------------------

    async def skip(self) -> Outcome:
        return await self._step("Next", "CanGoNext", "skip", "skipped to the next track", "Skipped. Now",
                                "won't skip from here")

    async def previous(self) -> Outcome:
        return await self._step("Previous", "CanGoPrevious", "go back", "went back to the previous track", "Back to",
                                "won't go back from here")

    async def pause(self) -> Outcome:
        player = await self._player("pause")
        if isinstance(player, Outcome):
            return player
        if not player.can("CanPause"):
            return await self._cannot("pause", player, "won't pause from here")
        if not player.playing:
            return Outcome("checked the player",
                           "It's already paused." if player.status == "Paused" else "Nothing is playing right now.", True)
        try:
            await self.bus.call(player.bus_name, PLAYER_IFACE, "Pause")
        except MprisError as exc:
            return await self._refused("pause", player, exc)
        return Outcome("paused the music", "Paused.", True)

    async def play(self) -> Outcome:
        player = await self._player("resume")
        if isinstance(player, Outcome):
            return player
        if player.playing:
            track = player.track
            return Outcome("checked the player", f"It's already playing: {track}." if track else "It's already playing.", True)
        if not player.can("CanPlay"):
            return await self._cannot("resume", player, "won't start from here")
        try:
            await self.bus.call(player.bus_name, PLAYER_IFACE, "Play")
        except MprisError as exc:
            return await self._refused("resume", player, exc)
        await asyncio.sleep(self.settle_s)
        track = (await self.reread(player)).track
        return Outcome("started the music again", f"Playing again: {track}." if track else "Playing again.", True)

    async def volume_step(self, delta: float) -> Outcome:
        word = "down" if delta < 0 else "up"
        verb = f"turn it {word}"
        player = await self._player(verb)
        if isinstance(player, Outcome):
            return player
        current = player.volume
        if current is None:
            name = await self.name_of(player)
            return Outcome(f"tried to {verb}", f"I tried to {verb}, but {name} won't say where the volume is.", False)
        target = max(0.0, min(1.0, current + delta))
        try:
            await self.bus.set(player.bus_name, PLAYER_IFACE, "Volume", "d", target)
        except MprisError as exc:
            return await self._refused(verb, player, exc)
        return Outcome(f"turned the volume {word}", f"Volume {word} to {round(target * 100)}.", True)

    async def now_playing(self) -> Outcome:
        player = await self._player("look at the player")
        if isinstance(player, Outcome):
            return player
        track = player.track
        if not track or player.status == "Stopped":
            return Outcome("looked at the player", "Nothing is playing right now.", True)
        tail = f", from {player.album}" if player.album and player.album not in track else ""
        verb = "That's" if player.playing else "Paused on"
        return Outcome("looked at the player", f"{verb} {track}{tail}.", True)

    async def situation(self) -> str:
        """One line of context for the thinker, so "this song" means something without a server."""
        try:
            player = await self.active()
        except MprisError:
            return ""
        if player is None or not player.track:
            return ""
        name = await self.name_of(player)
        state = "Now playing" if player.playing else "Paused"
        album = f" (album: {player.album})." if player.album else "."
        return f"{state} on {name}: {player.track}{album}"

    # --- the shapes every reflex shares ------------------------------------------

    async def _step(self, member: str, capability: str, verb: str, did: str, lead: str, refusal: str) -> Outcome:
        player = await self._player(verb)
        if isinstance(player, Outcome):
            return player
        if not player.can(capability):
            return await self._cannot(verb, player, refusal)
        try:
            await self.bus.call(player.bus_name, PLAYER_IFACE, member)
        except MprisError as exc:
            return await self._refused(verb, player, exc)
        await asyncio.sleep(self.settle_s)  # the player reports the old track for a moment
        track = (await self.reread(player)).track
        return Outcome(did, f"{lead} {track}." if track else f"{lead.rstrip(':')}.", True)

    async def _player(self, verb: str) -> Player | Outcome:
        """The active player, or the failed outcome to return instead."""
        try:
            player = await self.active()
        except MprisError as exc:
            return Outcome(f"tried to {verb}", f"I tried to {verb}, but {exc}.", False)
        if player is None:
            return Outcome(f"tried to {verb}", f"I tried to {verb}, but no media player is running.", False)
        return player

    async def _cannot(self, verb: str, player: Player, refusal: str) -> Outcome:
        name = await self.name_of(player)
        return Outcome(f"tried to {verb}", f"I tried to {verb}, but {name} {refusal}.", False)

    async def _refused(self, verb: str, player: Player, exc: MprisError) -> Outcome:
        name = await self.name_of(player)
        return Outcome(f"tried to {verb}", f"I tried to {verb}, but {name} said: {exc}.", False)


def _bare(method: Callable[[], Awaitable[Outcome]]) -> Reflex:
    async def reflex(_toolbox: Any, _server: str) -> Outcome:
        return await method()

    return reflex
