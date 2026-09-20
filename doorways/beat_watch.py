#!/usr/bin/env python3
"""Doorway: listen to the music player's own audio stream and tell strawberryd the beat.

    what is playing (PipeWire node of the player)  ->  beat_track.BeatTracker  ->  POST /tempo

Captures ONE PipeWire output stream with `pw-record --target <node>`: the player's, never the
microphone and never the whole mixer, so her own voice, video calls and system sounds stay
out of the analysis. The node is picked automatically (a running audio stream from a known
player, else any running stream that is not ours) or fixed with --target / [beat].target.
Every couple of seconds the current estimate goes to the daemon, which forwards it to the
widget as {"tempo": {...}}; the widget picks a dance style from it (WIRING.md §4c).

Runs on the system python3 (numpy only). Silence, a paused player or a beatless piece give a
low-confidence estimate; the widget treats that as "just sway".
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from beat_track import BeatTracker  # noqa: E402

log = logging.getLogger("beat_watch")

RATE = 22050
CHUNK_SAMPLES = 2048
LATENCY_S = 0.05
PLAYERS = ("spotify", "vlc", "mpv", "rhythmbox", "audacious", "clementine", "elisa", "lollypop", "amberol",
           "tidal", "deezer", "youtube music", "firefox", "chromium", "chrome", "brave")
OURS = ("strawberry", "strawberryd", "godot")


def pipewire_streams() -> list[dict]:
    """Running audio output streams as {id, name, app, state}."""
    try:
        dump = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        log.warning("pw-dump failed: %s", exc)
        return []
    out = []
    for obj in dump:
        info = obj.get("info") or {}
        props = info.get("props") or {}
        if props.get("media.class") != "Stream/Output/Audio":
            continue
        out.append({
            "id": obj.get("id"),
            "name": str(props.get("node.name", "")),
            "app": str(props.get("application.name", "")),
            "state": str(info.get("state", "")),
        })
    return out


def pick_target(streams: list[dict], preferred: str = "") -> dict | None:
    if preferred:
        for s in streams:
            if preferred.lower() in (s["name"].lower(), s["app"].lower()):
                return s
        return None
    candidates = [s for s in streams if s["app"].lower() not in OURS and s["name"].lower() not in OURS]
    running = [s for s in candidates if s["state"] == "running"]
    for pool in (running, candidates):
        for s in pool:
            label = (s["name"] + " " + s["app"]).lower()
            if any(p in label for p in PLAYERS):
                return s
    return running[0] if running else None


class Watcher:
    def __init__(self, daemon: str, target: str, interval_s: float) -> None:
        self.daemon = daemon.rstrip("/")
        self.target = target
        self.interval = interval_s
        self.tracker = BeatTracker(sample_rate=RATE)
        self.posts = 0
        self.failures = 0

    def run(self) -> None:
        while True:
            stream = pick_target(pipewire_streams(), self.target)
            if stream is None:
                time.sleep(3.0)
                continue
            log.info("listening to %s (%s, node %s)", stream["app"] or stream["name"], stream["name"], stream["id"])
            self.capture(stream)
            self.tracker.reset()
            self.post({"silent": True})
            time.sleep(1.0)

    def capture(self, stream: dict) -> None:
        cmd = ["pw-record", "--target", str(stream["id"]), "--rate", str(RATE), "--channels", "1",
               "--format", "s16", "--latency", f"{int(LATENCY_S * 1000)}ms", "-"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as exc:
            log.error("pw-record not runnable: %s", exc)
            time.sleep(10.0)
            return
        assert proc.stdout is not None
        next_post = time.time() + self.interval
        try:
            while True:
                raw = proc.stdout.read(CHUNK_SAMPLES * 2)
                if not raw:
                    log.info("stream %s ended", stream["name"])
                    return
                now = time.time() - LATENCY_S
                samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                self.tracker.feed(samples, now)
                if now >= next_post:
                    next_post = now + self.interval
                    self.report(now)
        finally:
            proc.kill()
            proc.wait(timeout=2)

    def report(self, now: float) -> None:
        tempo = self.tracker.estimate(now)
        if tempo is None or tempo.loudness_db < -60.0:
            self.post({"silent": True})
            return
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
                log.info("bpm %.1f conf %.2f even %.2f low %.2f dens %.1f %.0f dB -> %s widget(s)",
                         payload["bpm"], payload["confidence"], payload["evenness"], payload["low_ratio"],
                         payload["density"], payload["loudness_db"], reply.get("sent", "?"))
        except urllib.error.HTTPError as exc:
            log.warning("/tempo rejected: %s", exc.read().decode(errors="replace")[:200])
        except (urllib.error.URLError, TimeoutError) as exc:
            self.failures += 1
            if self.failures % 10 == 1:
                log.warning("strawberryd unreachable at %s (%s)", self.daemon, exc)


def settings() -> dict:
    import tomllib

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
    beat = cfg.get("beat", {}) if isinstance(cfg.get("beat"), dict) else {}
    port = (cfg.get("daemon") or {}).get("port", 8770)
    parser = argparse.ArgumentParser(description="Strawberry beat doorway (PipeWire -> /tempo)")
    parser.add_argument("--daemon", default=f"http://127.0.0.1:{port}")
    parser.add_argument("--target", default=str(beat.get("target", "")), help="PipeWire node or application name (default: auto)")
    parser.add_argument("--interval", type=float, default=float(beat.get("interval_s", 2.0)))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    if beat.get("enabled", True) is False:
        log.info("[beat] enabled = false; exiting")
        return
    Watcher(args.daemon, args.target, args.interval).run()


if __name__ == "__main__":
    main()
