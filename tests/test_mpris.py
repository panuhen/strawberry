"""The MPRIS reflexes (WIRING.md §8b) on a fake bus: no session bus is touched in a unit test."""

from __future__ import annotations

from typing import Any

import pytest

from strawberry.mpris import MPRIS_PREFIX, Mpris, MprisError, Player, unvariant

TRACKS = [
    {"title": "Feeling Good", "artists": ["Nina Simone"], "album": "I Put a Spell on You"},
    {"title": "Blue Monday", "artists": ["New Order"], "album": "Power, Corruption & Lies"},
]


class FakePlayer:
    """One player on the fake bus: a queue position, a status, a volume, what it admits it can do."""

    def __init__(self, name: str, status: str = "Playing", volume: float | None = 0.8, index: int = 0,
                 tracks: list[dict] | None = None, identity: str = "", **caps: bool) -> None:
        self.bus_name = MPRIS_PREFIX + name
        self.status = status
        self.volume = volume
        self.index = index
        self.tracks = TRACKS if tracks is None else tracks
        self.identity = identity or name.capitalize()
        self.caps = caps

    def props(self) -> dict[str, Any]:
        props: dict[str, Any] = {"PlaybackStatus": self.status, "CanControl": True, "CanGoNext": True,
                                 "CanGoPrevious": True, "CanPlay": True, "CanPause": True}
        props.update(self.caps)
        if self.volume is not None:
            props["Volume"] = self.volume
        if self.tracks:
            track = self.tracks[self.index % len(self.tracks)]
            props["Metadata"] = {"mpris:trackid": f"/track/{self.index}", "xesam:title": track["title"],
                                 "xesam:artist": list(track["artists"]), "xesam:album": track["album"]}
        return props


class FakeBus:
    """What `mpris.SessionBus` does, without a bus. `refuses` names members that answer with an error."""

    def __init__(self, *players: FakePlayer, others: list[str] | None = None, refuses: tuple[str, ...] = (),
                 listing: str = "") -> None:
        self.players = {p.bus_name: p for p in players}
        self.others = others if others is not None else ["org.freedesktop.Notifications", ":1.42"]
        self.refuses = refuses
        self.listing = listing          # non-empty: ListNames itself fails with this message
        self.log: list[str] = []
        self.closed = False

    async def list_names(self) -> list[str]:
        self.log.append("ListNames")
        if self.listing:
            raise MprisError(self.listing)
        return [*self.others, *self.players]

    def _player(self, bus_name: str) -> FakePlayer:
        if bus_name not in self.players:
            raise MprisError(f"The name {bus_name} was not provided by any .service files")
        return self.players[bus_name]

    async def get_all(self, bus_name: str, interface: str) -> dict[str, Any]:
        self.log.append(f"GetAll {bus_name.rsplit('.', 1)[-1]}")
        return self._player(bus_name).props()

    async def get(self, bus_name: str, interface: str, prop: str) -> Any:
        self.log.append(f"Get {prop}")
        player = self._player(bus_name)
        if prop == "Identity":
            return player.identity
        return player.props().get(prop)

    async def set(self, bus_name: str, interface: str, prop: str, signature: str, value: Any) -> None:
        self.log.append(f"Set {prop}={value:.2f}" if isinstance(value, float) else f"Set {prop}={value}")
        if prop in self.refuses:
            raise MprisError("it refused")
        if prop == "Volume":
            self._player(bus_name).volume = value

    async def call(self, bus_name: str, interface: str, member: str) -> None:
        self.log.append(member)
        player = self._player(bus_name)
        if member in self.refuses:
            raise MprisError("Player command failed")
        if member == "Next":
            player.index += 1
        elif member == "Previous":
            player.index -= 1
        elif member == "Pause":
            player.status = "Paused"
        elif member == "Play":
            player.status = "Playing"

    async def close(self) -> None:
        self.closed = True


def make(*players: FakePlayer, **kw) -> tuple[FakeBus, Mpris]:
    bus = FakeBus(*players, **kw)
    return bus, Mpris(bus, settle_s=0.0)


# ----------------------------------------------------------------------------- unit


def test_variants_are_unwrapped():
    assert unvariant(("s", "Blue Monday")) == "Blue Monday"
    assert unvariant(("a{sv}", {"xesam:artist": ("as", ["New Order"])})) == {"xesam:artist": ["New Order"]}
    assert unvariant(("d", 0.8)) == 0.8
    assert unvariant("plain") == "plain" and unvariant(42) == 42


def test_a_player_reads_its_own_metadata():
    player = Player("org.mpris.MediaPlayer2.vlc", FakePlayer("vlc").props())
    assert player.track == "Feeling Good by Nina Simone" and player.album == "I Put a Spell on You"
    assert player.playing and player.can("CanGoNext") and player.fallback_name == "Vlc"
    bare = Player("org.mpris.MediaPlayer2.mpv", {"PlaybackStatus": "Stopped"})
    assert bare.track == "" and bare.volume is None and not bare.playing
    assert bare.can("CanGoNext")   # a player that says nothing is assumed able
    assert not Player("x", {"CanControl": False}).can("CanPlay")


# ----------------------------------------------------------------------------- the reflexes


async def test_skip_and_previous_name_the_new_track():
    bus, mpris = make(FakePlayer("spotify"))
    outcome = await mpris.skip()
    assert outcome.ok and outcome.did == "skipped to the next track"
    assert outcome.fact == "Skipped. Now Blue Monday by New Order."
    assert bus.log[-2:] == ["Next", "GetAll spotify"]
    back = await mpris.previous()
    assert back.did == "went back to the previous track" and back.fact == "Back to Feeling Good by Nina Simone."


async def test_pause_and_resume():
    bus, mpris = make(FakePlayer("spotify"))
    paused = await mpris.pause()
    assert paused.ok and paused.did == "paused the music" and paused.fact == "Paused."
    again = await mpris.pause()
    assert again.ok and again.fact == "It's already paused."   # nothing to pause, and no lie about it
    resumed = await mpris.play()
    assert resumed.did == "started the music again" and resumed.fact == "Playing again: Feeling Good by Nina Simone."
    already = await mpris.play()
    assert already.ok and already.did == "checked the player"
    assert already.fact == "It's already playing: Feeling Good by Nina Simone."
    assert "Play" not in bus.log[-1]   # she looked, she did not press play again


async def test_volume_steps_by_a_sixth_and_clamps():
    bus, mpris = make(FakePlayer("spotify", volume=0.8))
    down = await mpris.volume_step(-0.15)
    assert down.ok and down.did == "turned the volume down" and down.fact == "Volume down to 65."
    up = await mpris.volume_step(0.15)
    assert up.fact == "Volume up to 80." and bus.players["org.mpris.MediaPlayer2.spotify"].volume == pytest.approx(0.8)
    for _ in range(3):
        top = await mpris.volume_step(0.15)
    assert top.fact == "Volume up to 100."   # clamped at the top, not 125
    for _ in range(8):
        bottom = await mpris.volume_step(-0.15)
    assert bottom.fact == "Volume down to 0."


async def test_now_playing_says_what_is_on():
    bus, mpris = make(FakePlayer("spotify"))
    now = await mpris.now_playing()
    assert now.ok and now.did == "looked at the player"
    assert now.fact == "That's Feeling Good by Nina Simone, from I Put a Spell on You."
    bus.players["org.mpris.MediaPlayer2.spotify"].status = "Paused"
    assert (await mpris.now_playing()).fact.startswith("Paused on Feeling Good by Nina Simone")
    bus.players["org.mpris.MediaPlayer2.spotify"].status = "Stopped"
    assert (await mpris.now_playing()).fact == "Nothing is playing right now."


async def test_a_player_that_cannot_do_it_says_so():
    bus, mpris = make(FakePlayer("firefox", CanGoNext=False, CanPause=False, volume=None, identity="Mozilla Firefox"))
    skip = await mpris.skip()
    assert not skip.ok and skip.did == "tried to skip"
    assert skip.fact == "I tried to skip, but Mozilla Firefox won't skip from here."
    pause = await mpris.pause()
    assert not pause.ok and pause.fact == "I tried to pause, but Mozilla Firefox won't pause from here."
    louder = await mpris.volume_step(0.15)
    assert not louder.ok and louder.fact == "I tried to turn it up, but Mozilla Firefox won't say where the volume is."
    assert bus.log.count("Get Identity") == 1   # asked once, then remembered


async def test_a_player_that_refuses_and_a_desktop_with_no_player():
    bus, mpris = make(FakePlayer("vlc", identity="VLC media player"), refuses=("Next",))
    outcome = await mpris.skip()
    assert not outcome.ok and outcome.fact == "I tried to skip, but VLC media player said: Player command failed."
    empty, alone = make()
    for outcome in (await alone.skip(), await alone.pause(), await alone.now_playing(), await alone.volume_step(0.15)):
        assert not outcome.ok and "no media player is running" in outcome.fact
    assert (await alone.skip()).fact == "I tried to skip, but no media player is running."
    broken, off = make(listing="I can't reach the desktop's media bus")
    assert (await off.pause()).fact == "I tried to pause, but I can't reach the desktop's media bus."


async def test_the_active_player_is_the_one_playing_then_the_one_she_used_last():
    playing = FakePlayer("spotify", status="Playing")
    paused = FakePlayer("vlc", status="Paused", index=1)
    bus, mpris = make(paused, playing)
    assert (await mpris.active()).bus_name.endswith("spotify")      # listed second, but it is playing
    playing.status = "Paused"
    assert (await mpris.active()).bus_name.endswith("spotify")      # both paused: the one she used last
    mpris.last = ""
    assert (await mpris.active()).bus_name.endswith("spotify")      # alphabetical when nothing else decides
    paused.status = "Playing"
    assert (await mpris.active()).bus_name.endswith("vlc")
    # A player that quits between ListNames and the read is skipped, not fatal.
    del bus.players[playing.bus_name]
    bus.players[playing.bus_name] = playing
    bus.others = []
    assert len(await mpris.players()) == 2


async def test_the_situation_line_for_the_thinker():
    bus, mpris = make(FakePlayer("spotify", identity="Spotify"))
    assert await mpris.situation() == ("Now playing on Spotify: Feeling Good by Nina Simone "
                                       "(album: I Put a Spell on You).")
    bus.players["org.mpris.MediaPlayer2.spotify"].status = "Paused"
    assert (await mpris.situation()).startswith("Paused on Spotify: Feeling Good by Nina Simone")
    _, alone = make()
    assert await alone.situation() == ""


async def test_the_reflexes_have_the_shape_the_actor_calls():
    bus, mpris = make(FakePlayer("spotify"))
    reflexes = mpris.reflexes()
    assert set(reflexes) == {"skip", "previous", "pause", "resume", "volume_up", "volume_down", "now_playing"}
    outcome = await reflexes["skip"](None, "mpris")   # the toolbox and server name mean nothing here
    assert outcome.fact == "Skipped. Now Blue Monday by New Order."
    await mpris.close()
    assert bus.closed
