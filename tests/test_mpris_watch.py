"""The media doorway: real PropertiesChanged signals in, daemon calls out. No bus needed."""

import asyncio

from jeepney import DBusAddress, Parser, new_signal

from strawberry.doorways import mpris_watch
from strawberry.doorways.mpris_watch import Player, Watcher, apply_properties, describe, wanted

PLAYER = DBusAddress("/org/mpris/MediaPlayer2", bus_name="org.mpris.MediaPlayer2.spotify",
                     interface="org.freedesktop.DBus.Properties")


def properties_changed(status="Playing", title="Get Lucky", artists=("Daft Punk",), track="/track/1"):
    """A PropertiesChanged as Spotify sends it: Metadata is a variant holding a whole a{sv}."""
    metadata = {"mpris:trackid": ("s", track), "xesam:title": ("s", title),
                "xesam:artist": ("as", list(artists))}
    changed = {"PlaybackStatus": ("s", status), "Metadata": ("a{sv}", metadata)}
    message = new_signal(PLAYER, "PropertiesChanged", "sa{sv}as",
                         ("org.mpris.MediaPlayer2.Player", changed, []))
    parser = Parser()
    parser.add_data(message.serialise(serial=7))
    return parser.get_next_message()


def test_a_real_signal_becomes_status_and_a_track_line():
    player = Player("org.mpris.MediaPlayer2.spotify", ":1.494")
    assert apply_properties(player, properties_changed().body) is True
    assert player.playing and player.track_id == "/track/1"
    assert describe(player.metadata) == "Daft Punk — Get Lucky"


def test_another_interface_is_not_playback():
    message = new_signal(PLAYER, "PropertiesChanged", "sa{sv}as", ("org.mpris.MediaPlayer2", {}, []))
    parser = Parser()
    parser.add_data(message.serialise(serial=8))
    assert apply_properties(Player("org.mpris.MediaPlayer2.vlc", ":1.5"), parser.get_next_message().body) is False


def test_describe_handles_a_missing_half():
    assert describe({"xesam:title": "Solo"}) == "Solo"
    assert describe({"xesam:artist": ["Nobody"]}) == "Nobody"
    assert describe({}) == ""


def test_only_and_ignore_pick_the_players():
    assert wanted("org.mpris.MediaPlayer2.spotify", set(), set())
    assert not wanted("org.freedesktop.Notifications", set(), set())
    assert wanted("org.mpris.MediaPlayer2.spotify", {"spotify"}, set())
    assert not wanted("org.mpris.MediaPlayer2.vlc", {"spotify"}, set())
    assert not wanted("org.mpris.MediaPlayer2.firefox.instance_1", set(), {"firefox"})


class FakeDaemon:
    def __init__(self, up=True):
        self.up = up
        self.calls = []

    async def post(self, path, payload):
        self.calls.append((path, payload))
        return self.up

    url = "http://127.0.0.1:1"


def watcher_with(player_status="Playing", up=True):
    watcher = Watcher("http://127.0.0.1:1", set(), set())
    watcher.daemon = FakeDaemon(up)
    player = Player("org.mpris.MediaPlayer2.spotify", ":1.494")
    apply_properties(player, properties_changed(status=player_status).body)
    watcher.players[":1.494"] = player
    return watcher, player


def test_playing_dances_and_announces_the_track_once():
    watcher, player = watcher_with()
    asyncio.run(watcher.flush())
    assert watcher.daemon.calls == [
        ("/perform", {"state": "dancing"}),
        ("/event", {"source": "media", "app": "Spotify", "title": "Daft Punk — Get Lucky"}),
    ]
    asyncio.run(watcher.flush())          # nothing new: no second dance, no second announcement
    assert len(watcher.daemon.calls) == 2

    apply_properties(player, properties_changed(title="Instant Crush", track="/track/2").body)
    asyncio.run(watcher.flush())
    assert watcher.daemon.calls[-1] == ("/event", {"source": "media", "app": "Spotify", "title": "Daft Punk — Instant Crush"})


def test_pausing_goes_idle_and_unpausing_is_not_an_announcement():
    watcher, player = watcher_with()
    asyncio.run(watcher.flush())
    apply_properties(player, properties_changed(status="Paused").body)
    asyncio.run(watcher.flush())
    assert watcher.daemon.calls[-1] == ("/perform", {"state": "idle"})
    apply_properties(player, properties_changed(status="Playing").body)
    asyncio.run(watcher.flush())
    assert watcher.daemon.calls[-1] == ("/perform", {"state": "dancing"})   # and no /event after it


def test_a_daemon_that_is_down_keeps_the_dance_state_for_a_retry():
    watcher, _player = watcher_with(up=False)

    async def run():
        await watcher.flush()
        assert watcher.playing_sent is None          # not recorded: the post did not land
        assert watcher.flush_task is not None        # a retry is scheduled
        watcher.flush_task.cancel()
        watcher.daemon.up = True
        await watcher.flush()

    asyncio.run(run())
    assert watcher.playing_sent is True
    # The track was not announced while the daemon was away, and is not replayed afterwards.
    assert [c[0] for c in watcher.daemon.calls] == ["/perform", "/perform"]


def test_the_match_rules_are_the_ones_the_bus_needs():
    assert mpris_watch.PROPERTIES_RULE.serialise() == (
        "interface='org.freedesktop.DBus.Properties',member='PropertiesChanged',"
        "path='/org/mpris/MediaPlayer2',type='signal'")
    assert "arg0namespace='org.mpris.MediaPlayer2'" in mpris_watch.NAME_OWNER_RULE.serialise()
