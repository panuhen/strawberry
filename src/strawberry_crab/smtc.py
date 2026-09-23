"""Music control on Windows, over the System Media Transport Controls (WINDOWS.md step 2).

The counterpart of mpris.py. Spotify, the browsers, Media Player and most other players register
a session with Windows' `GlobalSystemMediaTransportControlsSessionManager` (the media card in
the volume flyout shows the same sessions), with play, pause, next, previous and what is
playing. `Smtc` is `mpris.Mpris` with the bus swapped for those sessions: the choice of player,
the sentences and the outcomes are the same, so the daemon and `actions.Actor` do not know which
system answered. `media.controls()` picks one at runtime.

Each session reads as an MPRIS-shaped `mpris.Player`: its app id stands where the bus name does,
the playback status and the media properties fill `PlaybackStatus` and `Metadata`, the enabled
buttons fill `CanGoNext` and friends. SMTC has no volume, so "turn it up" gets "... won't say
where the volume is".

Which app a session belongs to is its `SourceAppUserModelId`: `Spotify.exe` for a desktop app,
`<package family>!<app>` for a Store app, `Chrome` or `MSEdge` for the browsers, and for Firefox
a hash of its install folder. `app_key` makes the short lowercase name `[media] only`/`ignore`
match (`spotify`, `chrome`, `firefox`), `app_name` the name she says ("Spotify", "Google
Chrome"). The Chromium-based browsers also answer to `chromium`, the name they share on MPRIS.

winrt (the `winrt-*` packages, Windows only) is imported in `request_manager` alone, so this
module imports on any system. The unit tests hand `Sessions` a fake manager, and
tests/conftest.py makes the real one unreachable, so no test can touch the user's players.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .mpris import SETTLE_S, Mpris, MprisError, Player

log = logging.getLogger("strawberryd.smtc")

# GlobalSystemMediaTransportControlsSessionPlaybackStatus, in MPRIS words. Changing is the moment
# between two tracks; the watcher keeps the status it had before it.
STATUS = {0: "Stopped", 1: "Stopped", 2: "Changing", 3: "Stopped", 4: "Playing", 5: "Paused"}

# The four Player methods Mpris calls, and the session's method for each.
COMMANDS = {"Next": "try_skip_next_async", "Previous": "try_skip_previous_async",
            "Play": "try_play_async", "Pause": "try_pause_async"}
TOGGLE = "try_toggle_play_pause_async"

# app_key -> the name she says. An app not listed is its own id, capitalised ("Musicbee").
KNOWN_APPS = {
    "spotify": "Spotify",
    "chrome": "Google Chrome",
    "msedge": "Microsoft Edge",
    "firefox": "Firefox",
    "brave": "Brave",
    "opera": "Opera",
    "vivaldi": "Vivaldi",
    "vlc": "VLC media player",
    "zunemusic": "Media Player",      # Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic
    "zunevideo": "Films & TV",
    "foobar2000": "foobar2000",
    "musicbee": "MusicBee",
    "itunes": "iTunes",
}
# Ids that do not say their app's name: Firefox's is a hash of its install folder, this one the
# default (C:\Program Files\Mozilla Firefox). A Firefox installed elsewhere is its hash.
ALIASES = {"308046b0af4a39cb": "firefox"}
CHROMIUM = {"chrome", "msedge", "brave", "opera", "vivaldi"}


def _segment(app_id: str) -> str:
    """The part of a SourceAppUserModelId that names the app, in its own case."""
    name = (app_id or "").strip()
    if "!" in name:                                  # a Store app: <package family>!<app id>
        name = name.split("!", 1)[1]
    name = name.replace("\\", "/").rsplit("/", 1)[-1]  # some desktop apps give a whole path
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name.rsplit(".", 1)[-1] if "." in name else name   # Microsoft.ZuneMusic -> ZuneMusic


def app_key(app_id: str) -> str:
    """'spotify' from Spotify.exe or SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify."""
    key = _segment(app_id).lower()
    return ALIASES.get(key, key)


def app_name(app_id: str) -> str:
    """The name she says: 'Spotify', 'Google Chrome', or the id's own name for an unknown app."""
    key = app_key(app_id)
    if key in KNOWN_APPS:
        return KNOWN_APPS[key]
    segment = _segment(app_id)
    return segment[:1].upper() + segment[1:] if segment else "the player"


def app_names(app_id: str) -> set[str]:
    """Every name `[media] only`/`ignore` may use for this app."""
    key = app_key(app_id)
    return {key, "chromium"} if key in CHROMIUM else {key}


def wanted(app_id: str, only: set[str], ignore: set[str]) -> bool:
    names = app_names(app_id)
    if only and not names & only:
        return False
    return not names & ignore


# ----------------------------------------------------------------------------- the sessions


async def request_manager(timeout_s: float = 4.0) -> Any:
    """Windows' session manager. MprisError where there is none (not Windows, winrt missing)."""
    try:
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager
    except ImportError as exc:
        raise MprisError("the media controls need Windows and its winrt packages") from exc
    try:
        return await asyncio.wait_for(GlobalSystemMediaTransportControlsSessionManager.request_async(), timeout_s)
    except Exception as exc:  # noqa: BLE001 - a timeout, a COM error: the same sentence either way
        raise MprisError("I can't reach Windows' media controls") from exc


@dataclass
class Snapshot:
    """One session as read at one moment. `address` is its app id, with `#2`, `#3` added for a
    second and third session of the same app (two browser windows)."""

    address: str
    app_id: str
    status: str = "Stopped"
    title: str = ""
    artist: str = ""
    album: str = ""
    buttons: dict[str, bool] = field(default_factory=dict)   # next, previous, play, pause, toggle
    session: Any = field(default=None, repr=False, compare=False)

    @property
    def track_id(self) -> str | None:
        """What identifies the track (SMTC has no track id): None while it names no title."""
        return "\n".join((self.title, self.artist, self.album)) if self.title else None

    def player(self) -> Player:
        """The same session in the MPRIS words `Mpris` reads."""
        playing = self.status == "Playing"
        b = self.buttons
        props: dict[str, Any] = {
            "PlaybackStatus": self.status if self.status in ("Playing", "Paused") else "Stopped",
            "CanGoNext": b.get("next", False),
            "CanGoPrevious": b.get("previous", False),
            # A paused player's pause button is off, which is not a refusal: pause() then says
            # "It's already paused." (and play() "It's already playing." the other way round).
            "CanPlay": b.get("play", False) or b.get("toggle", False) or playing,
            "CanPause": b.get("pause", False) or b.get("toggle", False) or not playing,
            "smtc:play": b.get("play", False),
            "smtc:pause": b.get("pause", False),
            "smtc:toggle": b.get("toggle", False),
        }
        if self.title:
            props["Metadata"] = {"xesam:title": self.title, "xesam:artist": [self.artist] if self.artist else [],
                                 "xesam:album": self.album}
        return Player(self.address, props)


def _text(obj: Any, name: str) -> str:
    return str(getattr(obj, name, "") or "").strip()


class Sessions:
    """The session manager, requested once and kept: what the unit tests replace with a fake
    manager (`Sessions(manager=...)`)."""

    def __init__(self, manager: Any = None, timeout_s: float = 4.0) -> None:
        self._manager = manager
        self.timeout_s = timeout_s

    async def manager(self) -> Any:
        if self._manager is None:
            self._manager = await request_manager(self.timeout_s)
        return self._manager

    async def read(self) -> list[Snapshot]:
        """Every session, Windows' current one first (it is the one the media keys drive)."""
        manager = await self.manager()
        try:
            current = manager.get_current_session()
            current_id = current.source_app_user_model_id if current is not None else ""
            sessions = list(manager.get_sessions())
        except Exception as exc:  # noqa: BLE001 - a COM error from the manager itself
            raise MprisError(_short(exc)) from exc
        seen: dict[str, int] = {}
        snapshots: list[Snapshot] = []
        for session in sessions:
            try:
                app_id = str(session.source_app_user_model_id or "")
                seen[app_id] = seen.get(app_id, 0) + 1
                address = app_id if seen[app_id] == 1 else f"{app_id}#{seen[app_id]}"
                snapshots.append(await self._snapshot(address, app_id, session))
            except Exception as exc:  # noqa: BLE001 - a player that quit between the list and the read
                log.debug("smtc: a session did not answer (%s)", _short(exc))
        snapshots.sort(key=lambda s: s.app_id != current_id)   # stable: otherwise Windows' order
        return snapshots

    async def _snapshot(self, address: str, app_id: str, session: Any) -> Snapshot:
        snap = Snapshot(address, app_id, session=session)
        info = session.get_playback_info()
        if info is not None:
            snap.status = STATUS.get(int(info.playback_status), "Stopped")
            controls = info.controls
            if controls is not None:
                snap.buttons = {"next": bool(controls.is_next_enabled), "previous": bool(controls.is_previous_enabled),
                                "play": bool(controls.is_play_enabled), "pause": bool(controls.is_pause_enabled),
                                "toggle": bool(controls.is_play_pause_toggle_enabled)}
        try:
            props = await asyncio.wait_for(session.try_get_media_properties_async(), self.timeout_s)
        except Exception as exc:  # noqa: BLE001 - some sessions refuse it for a moment after opening
            log.debug("smtc: no media properties from %s (%s)", app_id, _short(exc))
            props = None
        if props is not None:
            snap.title = _text(props, "title")
            snap.artist = _text(props, "artist") or _text(props, "album_artist")
            snap.album = _text(props, "album_title")
        return snap

    async def command(self, session: Any, method: str) -> None:
        """One of the session's try_*_async methods; MprisError when it fails or says no."""
        try:
            done = await asyncio.wait_for(getattr(session, method)(), self.timeout_s)
        except asyncio.TimeoutError as exc:
            raise MprisError("the player did not answer in time") from exc
        except Exception as exc:  # noqa: BLE001
            raise MprisError(_short(exc)) from exc
        if not done:
            raise MprisError("it refused")

    async def close(self) -> None:
        """Nothing to close: the manager is a WinRT object the runtime releases with us."""


def _short(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return text[:120]


# ----------------------------------------------------------------------------- the reflexes


class Smtc(Mpris):
    """The bare music reflexes over Windows' media sessions (see mpris.Mpris for the rules)."""

    bus: Sessions

    def __init__(self, sessions: Sessions | None = None, timeout_s: float = 4.0, settle_s: float = SETTLE_S) -> None:
        super().__init__(sessions if sessions is not None else Sessions(timeout_s=timeout_s), timeout_s, settle_s)
        self.found: dict[str, Any] = {}   # address -> its session object, from the last read

    async def players(self) -> list[Player]:
        snapshots = await self.bus.read()
        self.found = {s.address: s.session for s in snapshots}
        return [s.player() for s in snapshots]

    async def name_of(self, player: Player) -> str:
        return app_name(player.bus_name.split("#", 1)[0])

    async def reread(self, player: Player) -> Player:
        try:
            players = await self.players()
        except MprisError:
            return player
        return next((p for p in players if p.bus_name == player.bus_name), player)

    async def _command(self, player: Player, member: str) -> None:
        session = self.found.get(player.bus_name)
        if session is None:
            raise MprisError("it has gone away")
        method = COMMANDS[member]
        # A player with only the one play/pause button (some browsers) gets that button.
        if member in ("Play", "Pause") and not player.props.get(f"smtc:{member.lower()}") \
                and player.props.get("smtc:toggle"):
            method = TOGGLE
        await self.bus.command(session, method)

    async def _set_volume(self, player: Player, volume: float) -> None:
        raise MprisError("Windows' media controls have no volume")
