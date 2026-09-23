"""The Windows media doorway (doorways/smtc_watch.py) on a fake session manager: session events
in, the same daemon calls out as the MPRIS doorway makes. No WinRT, so it runs on any system."""

from __future__ import annotations

import asyncio
import threading

from strawberry_crab.doorways import smtc_watch
from strawberry_crab.doorways.smtc_watch import Watcher, describe
from strawberry_crab.smtc import Sessions
from tests.test_smtc import CHANGING, PAUSED, PLAYING, TRACKS, FakeManager, FakeSession


class FakeDaemon:
    def __init__(self, up=True):
        self.up = up
        self.calls = []

    async def post(self, path, payload):
        self.calls.append((path, payload))
        return self.up

    url = "http://127.0.0.1:1"


def watcher_with(*sessions: FakeSession, up: bool = True, only=(), ignore=()) -> tuple[Watcher, FakeManager]:
    manager = FakeManager(*sessions)
    watcher = Watcher("http://127.0.0.1:1", set(only), set(ignore), Sessions(manager))
    watcher.daemon = FakeDaemon(up)
    return watcher, manager


def test_describe_matches_the_mpris_doorway():
    assert describe("Daft Punk", "Get Lucky") == "Daft Punk — Get Lucky"
    assert describe("", "Solo") == "Solo" and describe("Nobody", "") == "Nobody" and describe("", "") == ""


async def test_a_player_already_playing_dances_but_is_not_announced():
    watcher, _ = watcher_with(FakeSession("Spotify.exe"))
    await watcher.refresh()
    await watcher.flush()
    assert watcher.daemon.calls == [("/perform", {"state": "dancing"})]


async def test_a_track_change_is_announced_once_with_the_apps_name():
    session = FakeSession("Spotify.exe")
    watcher, _ = watcher_with(session)
    await watcher.refresh()
    await watcher.flush()
    session.index = 1
    await watcher.refresh()
    await watcher.flush()
    await watcher.flush()                 # nothing new: no second announcement
    assert watcher.daemon.calls == [
        ("/perform", {"state": "dancing"}),
        ("/event", {"source": "media", "app": "Spotify", "title": "New Order — Blue Monday"}),
    ]


async def test_pausing_goes_idle_and_unpausing_is_not_an_announcement():
    session = FakeSession("Spotify.exe")
    watcher, _ = watcher_with(session)
    await watcher.refresh()
    await watcher.flush()
    session.status = PAUSED
    await watcher.refresh()
    await watcher.flush()
    assert watcher.daemon.calls[-1] == ("/perform", {"state": "idle"})
    session.status = CHANGING             # between two tracks: still paused as far as she knows
    await watcher.refresh()
    await watcher.flush()
    assert watcher.daemon.calls[-1] == ("/perform", {"state": "idle"}) and len(watcher.daemon.calls) == 2
    session.status = PLAYING
    await watcher.refresh()
    await watcher.flush()
    assert watcher.daemon.calls[-1] == ("/perform", {"state": "dancing"}) and len(watcher.daemon.calls) == 3


async def test_a_player_that_quits_goes_idle_and_a_new_one_is_followed():
    session = FakeSession("Spotify.exe")
    watcher, manager = watcher_with(session)
    await watcher.refresh()
    await watcher.flush()
    manager.sessions.clear()
    await watcher.refresh()
    await watcher.flush()
    assert watcher.players == {} and watcher.daemon.calls[-1] == ("/perform", {"state": "idle"})
    browser = FakeSession("Chrome", status=PAUSED, tracks=[TRACKS[1]])
    manager.sessions.append(browser)
    await watcher.refresh()
    browser.status = PLAYING
    browser.tracks = [TRACKS[0]]
    await watcher.refresh()
    await watcher.flush()
    assert watcher.daemon.calls[-2:] == [
        ("/perform", {"state": "dancing"}),
        ("/event", {"source": "media", "app": "Google Chrome", "title": "Nina Simone — Feeling Good"}),
    ]


async def test_only_and_ignore_narrow_the_sessions():
    watcher, _ = watcher_with(FakeSession("Chrome"), FakeSession("Spotify.exe", status=PAUSED), ignore={"chromium"})
    await watcher.refresh()
    assert list(watcher.players) == ["Spotify.exe"]
    await watcher.flush()
    assert watcher.daemon.calls == [("/perform", {"state": "idle"})]   # the playing browser is ignored
    only, _ = watcher_with(FakeSession("Chrome"), FakeSession("Spotify.exe"), only={"spotify"})
    await only.refresh()
    assert list(only.players) == ["Spotify.exe"]


async def test_a_daemon_that_is_down_keeps_the_dance_state_for_a_retry():
    session = FakeSession("Spotify.exe")
    watcher, _ = watcher_with(session, up=False)
    await watcher.refresh()
    watcher.players["Spotify.exe"].announced_track = None      # as if it had just started
    await watcher.flush()
    assert watcher.playing_sent is None and watcher.flush_task is not None   # a retry is scheduled
    watcher.flush_task.cancel()
    watcher.daemon.up = True
    await watcher.flush()
    assert watcher.playing_sent is True
    # The track was not announced while the daemon was away, and is not replayed afterwards.
    assert [c[0] for c in watcher.daemon.calls] == ["/perform", "/perform"]


async def test_run_follows_events_from_another_thread_and_cleans_up(monkeypatch):
    monkeypatch.setattr(smtc_watch, "DEBOUNCE_S", 0.01)
    monkeypatch.setattr(smtc_watch, "POLL_S", 60.0)
    session = FakeSession("Spotify.exe", status=PAUSED)
    watcher, manager = watcher_with(session)
    stopping = asyncio.Event()
    task = asyncio.ensure_future(watcher.run(stopping))

    async def until(condition, timeout: float = 2.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not condition():
            assert asyncio.get_running_loop().time() < deadline, watcher.daemon.calls
            await asyncio.sleep(0.01)

    await until(lambda: watcher.daemon.calls == [("/perform", {"state": "idle"})])
    assert len(manager.handlers) == 1 and len(session.handlers["playback_info_changed"]) == 1
    # WinRT raises its events on a thread of its own.
    session.status = PLAYING
    session.index = 1
    thread = threading.Thread(target=session.fire)
    thread.start()
    thread.join()
    await until(lambda: len(watcher.daemon.calls) == 3)
    assert watcher.daemon.calls[1:] == [
        ("/perform", {"state": "dancing"}),
        ("/event", {"source": "media", "app": "Spotify", "title": "New Order — Blue Monday"}),
    ]
    # A new player: SessionsChanged, then its own events are followed too.
    browser = FakeSession("Chrome", status=PAUSED)
    manager.sessions.append(browser)
    thread = threading.Thread(target=manager.fire)
    thread.start()
    thread.join()
    await until(lambda: len(browser.handlers["media_properties_changed"]) == 1)
    stopping.set()
    assert await task == 0
    assert manager.handlers == {} and session.handlers["playback_info_changed"] == {}
    assert browser.handlers["media_properties_changed"] == {}


async def test_run_without_media_sessions_exits_for_a_restart():
    watcher = Watcher("http://127.0.0.1:1", set(), set())    # the real manager: refused in tests
    watcher.daemon = FakeDaemon()
    assert await watcher.run(asyncio.Event()) == 3
    assert watcher.daemon.calls == []
