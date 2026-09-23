"""Doorway: listen to the music player's own audio and tell strawberryd the beat.

    the player's audio (one capture backend per system)  ->  beat_track.BeatTracker  ->  POST /tempo

The capture is the player's alone, never the microphone and never the whole mixer, so her own
voice, video calls and system sounds stay out of the analysis:

    Linux    beat_pipewire.py   the player's PipeWire output stream, through `pw-record`
    Windows  beat_loopback.py   WASAPI process loopback of the player's processes, found through
                                its media session

`backend()` picks one at runtime; this module never imports either directly. What they share is
here: the tracker driving, the posts to the daemon, and when to let a capture go and look for
the player again (the stream ended, no data came, a player that plays gave only silence for too
long, or another player took over). Every couple of seconds the current estimate goes to the
daemon, which forwards it to the widget as {"tempo": {...}}; the widget picks a dance style from
it (WIRING.md §4c).

Runs on the package's interpreter (numpy is a normal dependency): `strawberry-doorway
beat_watch`, or `python -m strawberry_crab.doorways.beat_watch` as the tray starts it. Silence, a
paused player or a beatless piece give a low-confidence estimate, and a tempo that has not held
for a few estimates yet goes out with "steady": false; the widget treats both as "just sway".
On Windows the tray and `strawberry stop` stop it through its stop event (winproc.py).
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from ..paths import config_file
from .beat_track import BeatTracker

log = logging.getLogger("beat_watch")

RATE = 22050
CHUNK_SAMPLES = 2048
LATENCY_S = 0.05
SILENT_DB = -60.0
STALE_AFTER_S = 12.0   # a running player that stays this silent has a dead capture link (seen after suspend)
SWITCH_AFTER_S = 4.0   # silent this long: ask whether another player has taken over
NO_DATA_S = 3.0        # the capture produced nothing: it never got linked to the target


def backend(platform: str | None = None) -> Any:
    """This system's capture: beat_pipewire on Linux, beat_loopback on Windows."""
    platform = platform or sys.platform
    name = "beat_loopback" if platform == "win32" else "beat_pipewire"
    return importlib.import_module(f"{__package__}.{name}")


class Watcher:
    def __init__(self, daemon: str, target: str, interval_s: float, capture: Any = None) -> None:
        self.daemon = daemon.rstrip("/")
        self.target = target
        self.interval = interval_s
        self.capture_backend = capture if capture is not None else backend().Backend()
        self.tracker = BeatTracker(sample_rate=RATE)
        self.posts = 0
        self.failures = 0
        self.silent_since: float | None = None
        self.stopping = threading.Event()   # set by the stop event on Windows; Linux stops by SIGTERM

    def run(self) -> None:
        try:
            while not self.stopping.is_set():
                player = self.capture_backend.find(self.target)
                if player is None:
                    self.stopping.wait(3.0)
                    continue
                outcome = self.capture(player)
                if self.stopping.is_set():
                    break
                self.tracker.reset()
                self.post({"silent": True})
                self.stopping.wait(self.capture_backend.after(outcome))
        finally:
            self.capture_backend.close()

    def capture(self, player: Any) -> str:
        """Feed the tracker from one capture until it ends. Returns 'ended', 'nolink' (no data
        arrived), 'stale' (silence for STALE_AFTER_S while the player plays), a backend's own
        verdict ('wrong-source'), or 'stopped'."""
        try:
            stream = self.capture_backend.open(player, RATE, CHUNK_SAMPLES)
        except OSError as exc:
            log.error("%s", exc)
            self.stopping.wait(10.0)
            return "ended"
        started = time.time()
        last_data = started
        next_post = started + self.interval
        self.silent_since = None
        got_any = False
        try:
            while not self.stopping.is_set():
                try:
                    samples = stream.read(1.0)
                except EOFError:
                    log.info("stream %s ended", stream.name)
                    return "ended"
                now = time.time() - LATENCY_S
                if samples is not None:
                    verdict = stream.verify(time.time() - started)
                    if verdict:
                        return verdict
                    got_any = True
                    last_data = time.time()
                    self.tracker.feed(samples, now)
                elif time.time() - last_data > NO_DATA_S:
                    log.warning("no audio from %s for %.0fs (capture by %s never linked); reconnecting",
                                stream.name, time.time() - last_data, stream.how)
                    return "nolink" if not got_any else "ended"
                if now >= next_post:
                    next_post = now + self.interval
                    self.report(now)
                    silent_for = now - self.silent_since if self.silent_since is not None else 0.0
                    if silent_for > STALE_AFTER_S and self.capture_backend.running(player):
                        # After suspend/resume a capture can keep delivering zeros while the
                        # player says it is running. Reconnect rather than dance to nothing.
                        log.warning("silent for %.0fs while %s is running; reconnecting the capture", silent_for, stream.name)
                        return "stale"
                    if silent_for > SWITCH_AFTER_S and self.capture_backend.superseded(player, self.target):
                        log.info("another player is playing now; leaving %s", stream.name)
                        return "ended"
            return "stopped"
        finally:
            stream.close()

    def report(self, now: float) -> None:
        tempo = self.tracker.estimate(now)
        if tempo is None or tempo.loudness_db < SILENT_DB:
            if self.silent_since is None:
                self.silent_since = now
            self.post({"silent": True})
            return
        self.silent_since = None
        payload = tempo.to_dict()
        payload["next_beat"] = round(tempo.next_beat, 3)
        self.post(payload)

    def post(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        request = urllib.request.Request(self.daemon + "/tempo", data=body,
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=1.5) as response:
                reply = json.loads(response.read() or b"{}")
            self.posts += 1
            if payload.get("silent"):
                log.info("silent -> %s widget(s)", reply.get("sent", "?"))
            else:
                log.info("bpm %.1f conf %.2f %s even %.2f low %.2f dens %.1f %.0f dB -> %s widget(s)",
                         payload["bpm"], payload["confidence"], "steady" if payload.get("steady") else "unsteady",
                         payload["evenness"], payload["low_ratio"], payload["density"], payload["loudness_db"],
                         reply.get("sent", "?"))
        except urllib.error.HTTPError as exc:
            log.warning("/tempo rejected: %s", exc.read().decode(errors="replace")[:200])
        except (urllib.error.URLError, TimeoutError) as exc:
            self.failures += 1
            if self.failures % 10 == 1:
                log.warning("strawberryd unreachable at %s (%s)", self.daemon, exc)


def settings() -> dict:
    import tomllib

    path = config_file()
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("could not read %s (%s); using defaults", path, exc)
        return {}


def listen_for_stop(watcher: Watcher) -> None:
    """Windows: the tray and `strawberry stop` set our named stop event (winproc.py), which is
    what SIGTERM is on Linux; the watcher then lets its capture go and returns."""
    if sys.platform != "win32":
        return
    from .. import winproc

    try:
        winproc.listen_for_stop(watcher.stopping.set)
    except OSError as exc:
        log.warning("no stop event (%s); only TerminateProcess can stop this process", exc)


def main() -> None:
    cfg = settings()
    beat = cfg.get("beat", {}) if isinstance(cfg.get("beat"), dict) else {}
    port = (cfg.get("daemon") or {}).get("port", 8770)
    capture = backend()
    parser = argparse.ArgumentParser(description=f"Strawberry beat doorway ({capture.NAME} -> /tempo)")
    parser.add_argument("--daemon", default=f"http://127.0.0.1:{port}")
    parser.add_argument("--target", default=str(beat.get("target", "")), help=capture.TARGET_HELP)
    parser.add_argument("--interval", type=float, default=float(beat.get("interval_s", 2.0)))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    if beat.get("enabled", True) is False:
        log.info("[beat] enabled = false; exiting")
        return
    watcher = Watcher(args.daemon, args.target, args.interval, capture.Backend())
    listen_for_stop(watcher)
    try:
        watcher.run()
    except KeyboardInterrupt:
        pass
    log.info("stopped")


if __name__ == "__main__":
    main()
