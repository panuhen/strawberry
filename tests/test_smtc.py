"""The SMTC reflexes (smtc.py, WINDOWS.md step 2) on a fake session manager: no real WinRT, so
this runs on any system, and no test reaches the user's own players."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from strawberry_crab import media, smtc
from strawberry_crab.mpris import Mpris, MprisError
from strawberry_crab.smtc import Sessions, Smtc, app_key, app_name, wanted

REAL_REQUEST = smtc.request_manager   # tests/conftest.py swaps it for a refusal in every test

TRACKS = [
    {"title": "Feeling Good", "artist": "Nina Simone", "album": "I Put a Spell on You"},
    {"title": "Blue Monday", "artist": "New Order", "album": "Power, Corruption & Lies"},
]
PLAYING, PAUSED, STOPPED, CHANGING = 4, 5, 3, 2


class FakeSession:
    """One session shaped like WinRT's GlobalSystemMediaTransportControlsSession: the same
    attribute and method names, an int for the playback status, coroutines for the *_async calls."""

    def __init__(self, app_id: str, status: int = PLAYING, index: int = 0, tracks: list[dict] | None = None,
                 refuses: tuple[str, ...] = (), **buttons: bool) -> None:
        self.source_app_user_model_id = app_id
        self.status = status
        self.index = index
        self.tracks = TRACKS if tracks is None else tracks
        self.refuses = refuses
        self.buttons = {"next": True, "previous": True, "play": True, "pause": True, "toggle": True, **buttons}
        self.log: list[str] = []
        self.handlers: dict[str, dict[int, Any]] = {"playback_info_changed": {}, "media_properties_changed": {}}
        self.tokens = 0

    def get_playback_info(self) -> Any:
        b = self.buttons
        # Like a real player: the play button is off while playing, the pause button while paused.
        controls = SimpleNamespace(is_next_enabled=b["next"], is_previous_enabled=b["previous"],
                                   is_play_enabled=b["play"] and self.status != PLAYING,
                                   is_pause_enabled=b["pause"] and self.status == PLAYING,
                                   is_play_pause_toggle_enabled=b["toggle"])
        return SimpleNamespace(playback_status=self.status, controls=controls)

    async def try_get_media_properties_async(self) -> Any:
        if not self.tracks:
            return SimpleNamespace(title="", artist="", album_title="", album_artist="")
        track = self.tracks[self.index % len(self.tracks)]
        return SimpleNamespace(title=track["title"], artist=track["artist"], album_title=track["album"],
                               album_artist="")

    async def _press(self, name: str) -> bool:
        self.log.append(name)
        if name in self.refuses:
            return False
        if name == "next":
            self.index += 1
        elif name == "previous":
            self.index -= 1
        elif name == "pause":
            self.status = PAUSED
        elif name == "play":
            self.status = PLAYING
        elif name == "toggle":
            self.status = PAUSED if self.status == PLAYING else PLAYING
        return True

    def try_skip_next_async(self):
        return self._press("next")

    def try_skip_previous_async(self):
        return self._press("previous")

    def try_play_async(self):
        return self._press("play")

    def try_pause_async(self):
        return self._press("pause")

    def try_toggle_play_pause_async(self):
        return self._press("toggle")

    def _add(self, event: str, handler: Any) -> int:
        self.tokens += 1
        self.handlers[event][self.tokens] = handler
        return self.tokens

    def add_playback_info_changed(self, handler):
        return self._add("playback_info_changed", handler)

    def remove_playback_info_changed(self, token):
        self.handlers["playback_info_changed"].pop(token)

    def add_media_properties_changed(self, handler):
        return self._add("media_properties_changed", handler)

    def remove_media_properties_changed(self, token):
        self.handlers["media_properties_changed"].pop(token)

    def fire(self, event: str = "playback_info_changed") -> None:
        for handler in list(self.handlers[event].values()):
            handler(self, None)


class FakeManager:
    """GlobalSystemMediaTransportControlsSessionManager's shape: sessions, the current one, and
    the SessionsChanged event."""

    def __init__(self, *sessions: FakeSession, current: FakeSession | None = None) -> None:
        self.sessions = list(sessions)
        self.current = current
        self.handlers: dict[int, Any] = {}

    def get_current_session(self):
        return self.current

    def get_sessions(self):
        return list(self.sessions)

    def add_sessions_changed(self, handler):
        token = len(self.handlers) + 1
        self.handlers[token] = handler
        return token

    def remove_sessions_changed(self, token):
        self.handlers.pop(token)

    def fire(self) -> None:
        for handler in list(self.handlers.values()):
            handler(self, None)


def make(*sessions: FakeSession, current: FakeSession | None = None) -> tuple[FakeManager, Smtc]:
    manager = FakeManager(*sessions, current=current)
    return manager, Smtc(Sessions(manager), settle_s=0.0)


# ----------------------------------------------------------------------------- app identity


@pytest.mark.parametrize("app_id, key, name", [
    ("Spotify.exe", "spotify", "Spotify"),
    ("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify", "spotify", "Spotify"),
    ("Chrome", "chrome", "Google Chrome"),
    ("MSEdge", "msedge", "Microsoft Edge"),
    ("308046B0AF4A39CB", "firefox", "Firefox"),
    ("Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic", "zunemusic", "Media Player"),
    (r"C:\Program Files\VideoLAN\VLC\vlc.exe", "vlc", "VLC media player"),
    ("Tidal.exe", "tidal", "Tidal"),
    ("", "", "the player"),
])
def test_an_app_id_becomes_a_short_key_and_a_name(app_id, key, name):
    assert app_key(app_id) == key and app_name(app_id) == name


def test_only_and_ignore_pick_the_sessions():
    assert wanted("Spotify.exe", set(), set())
    assert wanted("Spotify.exe", {"spotify"}, set())
    assert not wanted("Chrome", {"spotify"}, set())
    assert not wanted("308046B0AF4A39CB", set(), {"firefox"})
    # The Chromium-based browsers also answer to the name they share on MPRIS.
    assert not wanted("MSEdge", set(), {"chromium"}) and wanted("Chrome", {"chromium"}, set())


# ----------------------------------------------------------------------------- the reflexes


async def test_skip_and_previous_name_the_new_track():
    manager, reflexes = make(FakeSession("Spotify.exe"))
    outcome = await reflexes.skip()
    assert outcome.ok and outcome.did == "skipped to the next track"
    assert outcome.fact == "Skipped. Now Blue Monday by New Order."
    back = await reflexes.previous()
    assert back.fact == "Back to Feeling Good by Nina Simone."
    assert manager.sessions[0].log == ["next", "previous"]


async def test_pause_and_resume_and_a_paused_player_is_not_a_refusal():
    manager, reflexes = make(FakeSession("Spotify.exe"))
    assert (await reflexes.pause()).fact == "Paused."
    again = await reflexes.pause()          # its pause button is off now; that is not "won't pause"
    assert again.ok and again.fact == "It's already paused."
    assert (await reflexes.play()).fact == "Playing again: Feeling Good by Nina Simone."
    already = await reflexes.play()
    assert already.ok and already.fact == "It's already playing: Feeling Good by Nina Simone."
    assert manager.sessions[0].log == ["pause", "play"]


async def test_a_player_with_only_the_toggle_gets_the_toggle():
    session = FakeSession("Chrome", play=False, pause=False)
    _, reflexes = make(session)
    assert (await reflexes.pause()).fact == "Paused."
    assert (await reflexes.play()).ok
    assert session.log == ["toggle", "toggle"]


async def test_a_player_that_cannot_or_will_not_says_so():
    _, reflexes = make(FakeSession("Chrome", next=False, pause=False, toggle=False))
    assert (await reflexes.skip()).fact == "I tried to skip, but Google Chrome won't skip from here."
    assert (await reflexes.pause()).fact == "I tried to pause, but Google Chrome won't pause from here."
    # SMTC has no volume at all.
    louder = await reflexes.volume_step(0.15)
    assert not louder.ok and louder.fact == "I tried to turn it up, but Google Chrome won't say where the volume is."
    _, stubborn = make(FakeSession("Spotify.exe", refuses=("next",)))
    assert (await stubborn.skip()).fact == "I tried to skip, but Spotify said: it refused."


async def test_now_playing_and_the_situation_line():
    manager, reflexes = make(FakeSession("Spotify.exe"))
    assert (await reflexes.now_playing()).fact == "That's Feeling Good by Nina Simone, from I Put a Spell on You."
    assert await reflexes.situation() == "Now playing on Spotify: Feeling Good by Nina Simone (album: I Put a Spell on You)."
    manager.sessions[0].status = PAUSED
    assert (await reflexes.now_playing()).fact.startswith("Paused on Feeling Good")
    manager.sessions[0].status = STOPPED
    assert (await reflexes.now_playing()).fact == "Nothing is playing right now."


async def test_no_session_and_no_manager():
    _, alone = make()
    assert (await alone.skip()).fact == "I tried to skip, but no media player is running."
    assert await alone.situation() == ""
    # The real manager is out of bounds in tests (conftest), as it is off Windows.
    unreachable = Smtc(settle_s=0.0)
    outcome = await unreachable.pause()
    assert not outcome.ok and outcome.fact.startswith("I tried to pause, but ")


async def test_the_current_session_wins_among_players_that_play():
    browser, spotify = FakeSession("Chrome", tracks=[TRACKS[1]]), FakeSession("Spotify.exe")
    _, reflexes = make(browser, spotify, current=spotify)
    assert (await reflexes.active()).bus_name == "Spotify.exe"
    browser.status = PAUSED
    spotify.status = PAUSED
    assert (await reflexes.active()).bus_name == "Spotify.exe"   # the one she used last
    browser.status = PLAYING
    assert (await reflexes.active()).bus_name == "Chrome"


async def test_two_sessions_of_one_app_are_two_players():
    one, two = FakeSession("Chrome", status=PAUSED), FakeSession("Chrome", index=1)
    _, reflexes = make(one, two)
    assert [p.bus_name for p in await reflexes.players()] == ["Chrome", "Chrome#2"]
    assert (await reflexes.skip()).fact == "Skipped. Now Feeling Good by Nina Simone."
    assert two.log == ["next"] and one.log == []
    assert await reflexes.name_of((await reflexes.players())[1]) == "Google Chrome"


async def test_a_session_that_fails_to_read_is_skipped():
    class Gone(FakeSession):
        def get_playback_info(self):
            raise OSError("the object has disconnected")

    _, reflexes = make(Gone("Chrome"), FakeSession("Spotify.exe"))
    assert [p.bus_name for p in await reflexes.players()] == ["Spotify.exe"]


async def test_the_reflexes_have_the_shape_the_actor_calls():
    _, reflexes = make(FakeSession("Spotify.exe"))
    assert set(reflexes.reflexes()) == {"skip", "previous", "pause", "resume", "volume_up", "volume_down",
                                        "now_playing"}
    assert (await reflexes.reflexes()["skip"](None, "mpris")).fact == "Skipped. Now Blue Monday by New Order."
    await reflexes.close()


def test_the_system_picks_the_controls():
    assert isinstance(media.controls("win32"), Smtc)
    linux = media.controls("linux")
    assert isinstance(linux, Mpris) and not isinstance(linux, Smtc)


async def test_without_winrt_the_manager_is_a_plain_sentence(monkeypatch):
    monkeypatch.setitem(sys.modules, "winrt.windows.media.control", None)   # as on Linux
    with pytest.raises(MprisError, match="winrt"):
        await REAL_REQUEST()
