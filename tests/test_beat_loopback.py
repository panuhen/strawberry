"""The beat's Windows capture (doorways/beat_loopback.py, WINDOWS.md step 5): from the media
session that plays to the process that renders it, on a fake session manager and a fake process
table, so it runs on any system. The one real capture is of this test's own process, which
plays nothing; tests/conftest.py refuses the backend's capture of any other process."""

from __future__ import annotations

import os
import sys

import numpy as np
import pytest

from strawberry_crab import wasapi
from strawberry_crab.doorways import beat_loopback
from strawberry_crab.doorways.beat_loopback import Backend, LoopbackStream, Target, ours, player_process, tree_root
from strawberry_crab.smtc import Sessions
from strawberry_crab.wasapi import AudioSession, Process
from tests.test_smtc import PAUSED, PLAYING, FakeManager, FakeSession

SPOTIFY_STORE = "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"
ME = 900

# explorer -> Spotify (main, with a renderer child and the audio child); explorer -> Chrome with its
# audio service; a Store player; and our own tree: the tray, its python children (daemon, us), the widget.
TABLE = {p.pid: p for p in [
    Process(4, 0, "explorer.exe"),
    Process(10, 4, "Spotify.exe"), Process(11, 10, "Spotify.exe"), Process(12, 10, "Spotify.exe"),
    Process(20, 4, "chrome.exe"), Process(21, 20, "chrome.exe"), Process(22, 20, "chrome.exe"),
    Process(30, 4, "Microsoft.Media.Player.exe"),
    Process(40, 4, "vlc.exe"),
    Process(800, 4, "strawberry-tray.exe"), Process(801, 800, "pythonw.exe"),
    Process(810, 801, "python.exe"), Process(811, 810, "python.exe"),       # the daemon, through a launcher
    Process(820, 801, "python.exe"), Process(ME, 820, "python.exe"),        # beat_watch, through a launcher
    Process(830, 801, "strawberry-widget-0.1.1-windows-x86_64.exe"),
    Process(50, 4, "python.exe"),                                           # somebody else's python
]}
PACKAGES = {30: "Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic", 12: SPOTIFY_STORE}


def package_app_id(pid: int) -> str:
    return PACKAGES.get(pid, "")


class FakeCapture:
    def __init__(self, pid, rate, reads=()):
        self.pid, self.rate = pid, rate
        self.reads = list(reads)
        self.is_alive = True
        self.closed = False
        self.asked: list[int] = []

    def alive(self):
        return self.is_alive

    def read(self, timeout_s, min_samples=0):
        self.asked.append(min_samples)
        return self.reads.pop(0) if self.reads else None

    def close(self):
        self.closed = True


class FakeSystem:
    def __init__(self, sessions, table=TABLE, packages=None):
        self.sessions = sessions
        self.table = table
        self.captures: list[FakeCapture] = []

    def audio_sessions(self):
        return list(self.sessions)

    def processes(self):
        return dict(self.table)

    def package_app_id(self, pid):
        return package_app_id(pid)

    def capture(self, pid, rate):
        capture = FakeCapture(pid, rate)
        self.captures.append(capture)
        return capture


def backend(media: list[FakeSession], audio: list[AudioSession], current=None) -> tuple[Backend, FakeSystem]:
    system = FakeSystem(audio)
    b = Backend(Sessions(FakeManager(*media, current=current)), system)
    b.me = ME
    return b, system


def test_our_own_processes_are_never_the_player():
    mine = ours(TABLE, ME)
    assert mine == {800, 801, 810, 811, 820, ME, 830}
    assert 50 not in mine and 10 not in mine
    # By hand, under uv: the climb stops at the first process that is not ours.
    by_hand = {1: Process(1, 0, "bash.exe"), 2: Process(2, 1, "uv.exe"), 3: Process(3, 2, "python.exe"),
               5: Process(5, 3, "python.exe")}
    assert ours(by_hand, 5) == {3, 5}


def test_the_tree_is_captured_from_its_topmost_same_named_process():
    assert tree_root(22, TABLE) == 20 and tree_root(12, TABLE) == 10 and tree_root(40, TABLE) == 40
    assert tree_root(ME, TABLE) == 820          # a launcher above the interpreter


def test_a_desktop_app_is_found_by_its_image_name_and_a_packaged_one_by_its_app_id():
    audio = [AudioSession(21, False), AudioSession(22, True), AudioSession(12, True), AudioSession(30, True)]
    excluded = ours(TABLE, ME)
    assert player_process("Chrome", audio, TABLE, package_app_id, excluded) == 22        # the one rendering now
    assert player_process("Spotify.exe", audio, TABLE, package_app_id, excluded) == 12
    assert player_process(SPOTIFY_STORE, audio, TABLE, package_app_id, excluded) == 12
    # Media Player's exe says "Player"; only its package id finds it.
    assert player_process("Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic", audio, TABLE, package_app_id,
                          excluded) == 30
    assert player_process("MSEdge", audio, TABLE, package_app_id, excluded) is None
    # Firefox outside its default folder: its id is a hash that names no process.
    assert player_process("6F193CCC56814779", audio, TABLE, package_app_id, excluded) is None


def test_our_python_and_the_widget_are_left_out_of_a_python_player():
    audio = [AudioSession(811, True), AudioSession(830, True), AudioSession(ME, True)]
    assert player_process("python.exe", audio, TABLE, package_app_id, ours(TABLE, ME)) is None
    audio.append(AudioSession(50, True))
    assert player_process("python.exe", audio, TABLE, package_app_id, ours(TABLE, ME)) == 50


def test_the_playing_media_session_picks_the_process_tree():
    b, _ = backend([FakeSession("Chrome", status=PAUSED), FakeSession("Spotify.exe")],
                   [AudioSession(22, True), AudioSession(11, True)])
    assert b.find("") == Target(10, "Spotify.exe", "Spotify", "Spotify.exe")
    assert b.find("chrome") is None                           # pinned, but it is paused
    b.close()


def test_a_pinned_target_and_a_player_without_a_media_session():
    b, _ = backend([FakeSession("Spotify.exe")], [AudioSession(11, True), AudioSession(22, True), AudioSession(40, True)])
    assert b.find("spotify").pid == 10
    b.sessions = Sessions(FakeManager())                      # no media session at all
    assert b.find("") == Target(10, "Spotify.exe", "Spotify")   # a known player that renders sound
    assert b.find("chrome") == Target(20, "chrome.exe", "Google Chrome")
    assert b.find("firefox") is None
    b.close()


def test_a_media_session_whose_process_renders_nothing_is_skipped_and_said_once(caplog):
    caplog.set_level("INFO", logger="beat_watch")
    b, _ = backend([FakeSession("MSEdge")], [])
    assert b.find("") is None and b.find("") is None
    assert [r.getMessage() for r in caplog.records] == ["Microsoft Edge plays, but no process of its renders sound"]
    b.close()


def test_the_media_sessions_unreadable_leaves_the_known_players():
    system = FakeSystem([AudioSession(40, True)])
    b = Backend(Sessions(), system)            # the real manager: tests/conftest.py refuses it
    b.me = ME
    assert b.find("") == Target(40, "vlc.exe", "VLC media player")
    assert b.failed
    b.close()


def test_running_and_superseded():
    spotify, chrome = FakeSession("Spotify.exe"), FakeSession("Chrome", status=PAUSED)
    b, system = backend([spotify, chrome], [AudioSession(11, True), AudioSession(22, True)])
    target = b.find("")
    assert b.running(target) and not b.superseded(target, "")
    spotify.status, chrome.status = PAUSED, PLAYING
    assert not b.running(target) and b.superseded(target, "")
    # Found without a media session: its audio session says whether it plays.
    vlc = Target(40, "vlc.exe", "VLC media player")
    assert not b.running(vlc)
    system.sessions.append(AudioSession(40, True))
    assert b.running(vlc)
    b.close()


def test_the_stream_reads_half_a_chunk_and_ends_with_the_process():
    b, system = backend([FakeSession("Spotify.exe")], [AudioSession(11, True)])
    stream = b.open(b.find(""), 22050, 2048)
    capture = system.captures[0]
    assert (capture.pid, capture.rate) == (10, 22050) and stream.how == "process loopback"
    capture.reads = [np.ones(1100, dtype=np.float32)]
    assert len(stream.read(1.0)) == 1100 and capture.asked == [1024]
    assert stream.read(1.0) is None and stream.verify(5.0) is None
    capture.is_alive = False
    with pytest.raises(EOFError):
        stream.read(1.0)
    stream.close()
    assert capture.closed
    b.close()


def test_a_refused_activation_says_what_it_needs():
    b, system = backend([FakeSession("Spotify.exe")], [AudioSession(11, True)])

    def refuse(pid, rate):
        raise OSError("[WinError -2147024809] The parameter is incorrect")

    system.capture = refuse
    with pytest.raises(OSError, match="Windows 10 2004 or later"):
        b.open(b.find(""), 22050, 2048)
    b.close()


def test_the_real_backend_may_not_capture_other_processes():
    with pytest.raises(AssertionError, match="out of bounds"):
        beat_loopback.System.capture(10, 22050)


# --- the real WASAPI calls, on this process only ------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="WASAPI")
def test_process_loopback_of_this_process_hears_its_silence():
    """Activation (the async completion handler), the format and the capture, on the one process
    that is sure to play nothing: this one."""
    try:
        capture = wasapi.LoopbackCapture(os.getpid(), 22050)
    except OSError as exc:
        pytest.skip(f"no process loopback here: {exc}")
    try:
        assert capture.alive()
        samples = capture.read(0.3, 256)
        assert samples is None or (samples.dtype == np.float32 and not samples.any())
    finally:
        capture.close()
    assert capture.client is None and capture.event is None


@pytest.mark.skipif(sys.platform != "win32", reason="WASAPI")
def test_the_process_table_and_the_audio_sessions_read():
    table = wasapi.processes()
    assert table[os.getpid()].exe.lower().startswith("python")
    assert all(s.pid for s in wasapi.audio_sessions())
    assert wasapi.package_app_id(os.getpid()) == ""           # a desktop process has no package
