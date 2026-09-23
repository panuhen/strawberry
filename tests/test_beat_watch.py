"""beat_watch: the shared watcher (tracker, posts, when a capture is let go) over a fake capture,
and the PipeWire backend, which picks the player's RUNNING stream and notices when PipeWire fed
it the sink monitor instead."""

from __future__ import annotations

import subprocess
import sys

import numpy as np
import pytest

from strawberry_crab.doorways import beat_loopback, beat_pipewire, beat_watch
from strawberry_crab.doorways.beat_watch import Watcher


def node(id_, name, media_class, app="", state="running", serial=None):
    return {"id": id_, "type": "PipeWire:Interface:Node",
            "info": {"state": state, "props": {"node.name": name, "media.class": media_class, "application.name": app,
                                               "object.serial": serial or id_}}}


def link(out_id, in_id):
    return {"id": 900 + out_id * 10 + in_id, "type": "PipeWire:Interface:Link", "info": {"output-node-id": out_id, "input-node-id": in_id}}


# Spotify as seen live: one running stream and one idle one, plus her own voice and a sink.
DUMP = [
    node(53, "bluez_output.50_C2", "Audio/Sink"),
    node(62, "Strawberry", "Stream/Output/Audio", app="Strawberry"),
    node(68, "spotify", "Stream/Output/Audio", app="Spotify", state="suspended", serial=521),
    node(70, "spotify", "Stream/Output/Audio", app="Spotify", state="running", serial=522),
    node(67, "strawberry-beat", "Stream/Input/Audio", app="pw-record"),
]


def test_only_a_running_player_stream_is_a_target():
    streams = beat_pipewire.pipewire_streams(DUMP)
    picked = beat_pipewire.pick_target(streams)
    assert picked["id"] == 70 and picked["serial"] == "522"
    idle_only = [s for s in streams if s["id"] != 70]
    assert beat_pipewire.pick_target(idle_only) is None          # wait, rather than capture the idle node
    assert beat_pipewire.pick_target(streams, "spotify")["id"] == 70
    assert beat_pipewire.pick_target(idle_only, "spotify") is None


def test_sink_monitor_substitution_is_detected():
    target = {"id": 70}
    fed_by_sink = beat_pipewire.capture_sources(DUMP + [link(53, 67)])
    assert fed_by_sink == [{"id": 53, "name": "bluez_output.50_C2", "class": "Audio/Sink"}]
    assert beat_pipewire.linked_to_player(fed_by_sink, target) is False
    fed_by_player = beat_pipewire.capture_sources(DUMP + [link(70, 67)])
    assert beat_pipewire.linked_to_player(fed_by_player, target) is True
    assert beat_pipewire.linked_to_player(beat_pipewire.capture_sources(DUMP), target) is None   # not linked yet
    assert beat_pipewire.capture_sources(DUMP, node_name="nobody") == []


def test_pipewire_strategies_rotate_only_while_the_link_fails():
    backend = beat_pipewire.Backend()
    assert backend.after("nolink") == 0.5 and backend.strategy == 1
    assert backend.after("wrong-source") == 0.5 and backend.strategy == 2
    assert backend.after("ended") == 1.0 and backend.strategy == 0
    assert backend.superseded({"id": 70}, "") is False


def test_each_system_has_its_capture():
    assert beat_watch.backend("linux") is beat_pipewire
    assert beat_watch.backend("win32") is beat_loopback
    for module in (beat_pipewire, beat_loopback):
        assert module.NAME and module.TARGET_HELP and hasattr(module, "Backend")


# --- the shared watcher, on a fake capture --------------------------------------------

class FakeStream:
    name = "fake player"
    how = "fake"

    def __init__(self, reads, verdict=None):
        self.reads = list(reads)
        self.verdict = verdict
        self.closed = False

    def read(self, timeout_s):
        if not self.reads:
            raise EOFError
        item = self.reads.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def verify(self, elapsed_s):
        return self.verdict

    def close(self):
        self.closed = True


class FakeBackend:
    def __init__(self, streams, players=("player",)):
        self.streams = list(streams)
        self.players = list(players)
        self.outcomes = []
        self.closed = False
        self.opened = []

    def find(self, target):
        return self.players.pop(0) if self.players else None

    def open(self, player, rate, chunk):
        self.opened.append((player, rate, chunk))
        stream = self.streams.pop(0)
        if isinstance(stream, BaseException):
            raise stream
        return stream

    def after(self, outcome):
        self.outcomes.append(outcome)
        return 0.0

    def running(self, player):
        return True

    def superseded(self, player, target):
        return False

    def close(self):
        self.closed = True


def watcher(backend) -> tuple[Watcher, list[dict]]:
    w = Watcher("http://127.0.0.1:9", "", 2.0, backend)
    posts: list[dict] = []
    w.post = posts.append
    return w, posts


def test_a_capture_that_ends_is_let_go_and_the_tracker_reset():
    samples = np.zeros(1024, dtype=np.float32)
    stream = FakeStream([samples, samples])                  # then EOFError: the stream ended
    backend = FakeBackend([stream])
    w, posts = watcher(backend)
    backend.find = lambda target, players=["player"]: players.pop(0) if players else w.stopping.set()
    w.run()
    assert backend.opened == [("player", beat_watch.RATE, beat_watch.CHUNK_SAMPLES)]
    assert backend.outcomes == ["ended"] and stream.closed and backend.closed
    assert posts == [{"silent": True}] and w.tracker.seconds == 0.0


def test_a_backend_verdict_ends_the_capture():
    stream = FakeStream([np.zeros(64, dtype=np.float32)], verdict="wrong-source")
    w, _ = watcher(FakeBackend([stream]))
    assert w.capture("player") == "wrong-source" and stream.closed


def test_no_data_at_all_is_nolink(monkeypatch):
    # Below zero, not zero: Windows' time.time() ticks every ~16 ms, so the gap it reads is 0.0.
    monkeypatch.setattr(beat_watch, "NO_DATA_S", -1.0)
    stream = FakeStream([None])
    w, _ = watcher(FakeBackend([stream]))
    assert w.capture("player") == "nolink" and stream.closed


def test_a_capture_that_cannot_start_waits_and_counts_as_ended():
    w, _ = watcher(FakeBackend([OSError("pw-record not runnable: no such file")]))
    w.stopping.set()                                         # so the 10 s wait returns at once
    assert w.capture("player") == "ended"


def test_the_stop_event_ends_a_capture_without_a_last_post():
    w, posts = watcher(FakeBackend([]))
    stream = FakeStream([np.zeros(64, dtype=np.float32)] * 1000)
    w.capture_backend.streams = [stream]
    original = stream.read

    def read(timeout_s):
        w.stopping.set()
        return original(timeout_s)

    stream.read = read
    w.capture_backend.players = ["player"]
    w.run()
    assert stream.closed and w.capture_backend.outcomes == [] and posts == []


def test_silence_is_posted_as_silent():
    w, posts = watcher(FakeBackend([]))
    w.tracker.feed(np.zeros(22050 * 5, dtype=np.float32), 100.0)
    w.report(100.0)
    assert posts == [{"silent": True}] and w.silent_since == 100.0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows stop events")
def test_the_stop_event_stops_the_watcher_on_windows():
    from strawberry_crab import winproc

    code = """
import time
import numpy as np
from strawberry_crab.doorways import beat_watch

class Stream:
    name, how = "silence", "test"
    def read(self, timeout_s): time.sleep(0.02); return np.zeros(441, dtype=np.float32)
    def verify(self, elapsed_s): return None
    def close(self): print("capture closed", flush=True)

class Backend:
    def find(self, target): return "player"
    def open(self, player, rate, chunk): print("listening", flush=True); return Stream()
    def after(self, outcome): return 0.0
    def running(self, player): return True
    def superseded(self, player, target): return False
    def close(self): pass

w = beat_watch.Watcher("http://127.0.0.1:9", "", 60.0, Backend())
beat_watch.listen_for_stop(w)
w.run()
print("stopped cleanly", flush=True)
"""
    process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                               env={**__import__("os").environ, winproc.STOP_ENV: "test-beat-watch"})
    try:
        assert process.stdout.readline().strip() == "listening"
        assert winproc.stop(process.pid, "test-beat-watch", timeout_s=10) == "stopped"
        assert process.stdout.read().split() == ["capture", "closed", "stopped", "cleanly"]
        assert process.wait(5) == 0
    finally:
        if process.poll() is None:
            process.kill()
