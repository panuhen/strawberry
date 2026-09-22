"""Doorway: listen to the music player's own audio stream and tell strawberryd the beat.

    what is playing (PipeWire node of the player)  ->  beat_track.BeatTracker  ->  POST /tempo

Captures ONE PipeWire output stream with `pw-record --target <node>`: the player's, never the
microphone and never the whole mixer, so her own voice, video calls and system sounds stay
out of the analysis. The node is picked automatically (a running audio stream from a known
player, else any running stream that is not ours) or fixed with --target / [beat].target.
Every couple of seconds the current estimate goes to the daemon, which forwards it to the
widget as {"tempo": {...}}; the widget picks a dance style from it (WIRING.md §4c).

Runs on the package's interpreter (numpy is a normal dependency): `strawberry-doorway
beat_watch`, or `python -m strawberry.doorways.beat_watch` as the tray starts it. Silence, a
paused player or a beatless piece give a low-confidence estimate; the widget treats that as
"just sway".
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import select
import subprocess
import time
import urllib.error
import urllib.request

import numpy as np

from ..paths import config_file
from .beat_track import BeatTracker

log = logging.getLogger("beat_watch")

RATE = 22050
CHUNK_SAMPLES = 2048
LATENCY_S = 0.05
SILENT_DB = -60.0
STALE_AFTER_S = 12.0   # a running player that stays this silent has a dead capture link (seen after suspend)
NO_DATA_S = 3.0        # pw-record produced nothing: PipeWire never linked us to the target
STRATEGIES = ("serial", "name", "sink-monitor")
NODE_NAME = "strawberry-beat"   # our capture node, so the link check can find it in the graph
PLAYERS = ("spotify", "vlc", "mpv", "rhythmbox", "audacious", "clementine", "elisa", "lollypop", "amberol",
           "tidal", "deezer", "youtube music", "firefox", "chromium", "chrome", "brave")
OURS = ("strawberry", "strawberryd", "godot")


def pw_dump() -> list[dict]:
    try:
        return json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        log.warning("pw-dump failed: %s", exc)
        return []


def pipewire_streams(dump: list[dict] | None = None) -> list[dict]:
    """Audio output streams as {id, serial, name, app, state}."""
    out = []
    for obj in pw_dump() if dump is None else dump:
        info = obj.get("info") or {}
        props = info.get("props") or {}
        if props.get("media.class") != "Stream/Output/Audio":
            continue
        out.append({
            "id": obj.get("id"),
            # pw-record --target takes the object *serial* or the name, not the id. Ids and serials
            # happen to match on a fresh graph and drift apart after suspend/resume; targeting the
            # id then yields a dead object that streams zeros.
            "serial": str(props.get("object.serial", obj.get("id"))),
            "name": str(props.get("node.name", "")),
            "app": str(props.get("application.name", "")),
            "state": str(info.get("state", "")),
        })
    return out


def pick_target(streams: list[dict], preferred: str = "") -> dict | None:
    """A RUNNING stream only. Spotify keeps a second, idle stream node; asking PipeWire to capture an
    idle node gets us silently linked to the default sink's monitor instead, and from then on the
    beat follows the output device (dead across a headset profile switch) rather than the player."""
    running = [s for s in streams if s["state"] == "running"]
    if preferred:
        for s in running:
            if preferred.lower() in (s["name"].lower(), s["app"].lower()):
                return s
        return None
    candidates = [s for s in running if s["app"].lower() not in OURS and s["name"].lower() not in OURS]
    for s in candidates:
        label = (s["name"] + " " + s["app"]).lower()
        if any(p in label for p in PLAYERS):
            return s
    return candidates[0] if candidates else None


def capture_sources(dump: list[dict], node_name: str = NODE_NAME) -> list[dict]:
    """Where our capture node's inputs come from: [{id, name, class}] per linked output node."""
    nodes = {o.get("id"): ((o.get("info") or {}).get("props") or {}) for o in dump if str(o.get("type", "")).endswith("Node")}
    ours = {i for i, props in nodes.items() if props.get("node.name") == node_name}
    if not ours:
        return []
    sources: dict[int, dict] = {}
    for o in dump:
        if not str(o.get("type", "")).endswith("Link"):
            continue
        info = o.get("info") or {}
        if info.get("input-node-id") in ours:
            out_id = info.get("output-node-id")
            props = nodes.get(out_id, {})
            sources[out_id] = {"id": out_id, "name": str(props.get("node.name", "")), "class": str(props.get("media.class", ""))}
    return list(sources.values())


def linked_to_player(sources: list[dict], stream: dict) -> bool | None:
    """True: fed by the player's stream. False: fed by something else (a sink monitor). None: no links yet."""
    if not sources:
        return None
    return any(src["id"] == stream["id"] for src in sources) and not any(src["class"].startswith("Audio/Sink") for src in sources)


class Watcher:
    def __init__(self, daemon: str, target: str, interval_s: float) -> None:
        self.daemon = daemon.rstrip("/")
        self.target = target
        self.interval = interval_s
        self.tracker = BeatTracker(sample_rate=RATE)
        self.posts = 0
        self.failures = 0
        self.strategy = 0
        self.silent_since: float | None = None

    def run(self) -> None:
        while True:
            stream = pick_target(pipewire_streams(), self.target)
            if stream is None:
                time.sleep(3.0)
                continue
            how = STRATEGIES[self.strategy % len(STRATEGIES)]
            log.info("listening to %s (%s, node %s, serial %s) by %s", stream["app"] or stream["name"], stream["name"],
                     stream["id"], stream["serial"], how)
            outcome = self.capture(stream, how)
            self.tracker.reset()
            self.post({"silent": True})
            if outcome in ("nolink", "wrong-source"):
                # This way of asking PipeWire did not get us linked to the player; try the next.
                self.strategy += 1
                time.sleep(0.5)
            else:
                self.strategy = 0
                time.sleep(1.0)

    def capture(self, stream: dict, how: str) -> str:
        """Run pw-record until the stream ends. Returns 'ended', 'nolink' (no bytes arrived),
        or 'stale' (zeros for STALE_AFTER_S while the player runs)."""
        if how == "serial":
            cmd = ["pw-record", "--target", stream["serial"]]
        elif how == "name":
            cmd = ["pw-record", "--target", stream["name"]]
        else:
            # Default sink monitor: device-agnostic (follows the default sink) but hears every
            # app, her own voice included. Last resort, and the watcher says so in the log.
            cmd = ["pw-record", "-P", "stream.capture.sink=true"]
        cmd += ["-P", f"node.name={NODE_NAME}", "--rate", str(RATE), "--channels", "1", "--format", "s16",
                "--latency", f"{int(LATENCY_S * 1000)}ms", "-"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as exc:
            log.error("pw-record not runnable: %s", exc)
            time.sleep(10.0)
            return "ended"
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        started = time.time()
        last_data = started
        next_post = started + self.interval
        self.silent_since = None
        got_any = False
        checked = False
        pending = b""  # an odd byte left over from a read, so samples never go out of alignment
        try:
            while True:
                ready, _, _ = select.select([fd], [], [], 1.0)
                now = time.time() - LATENCY_S
                if ready:
                    raw = os.read(fd, CHUNK_SAMPLES * 2)
                    if not raw:
                        log.info("stream %s ended", stream["name"])
                        return "ended"
                    if not checked and how != "sink-monitor" and time.time() - started > 1.0:
                        # Data flows: make sure it is the player's, not the sink monitor PipeWire
                        # substitutes when it cannot link to the target.
                        checked = True
                        verdict = linked_to_player(capture_sources(pw_dump()), stream)
                        if verdict is False:
                            log.warning("capture by %s got linked to the output device, not to %s; retrying", how, stream["name"])
                            return "wrong-source"
                    got_any = True
                    last_data = time.time()
                    raw = pending + raw
                    cut = len(raw) - len(raw) % 2
                    pending = raw[cut:]
                    samples = np.frombuffer(raw[:cut], dtype=np.int16).astype(np.float32) / 32768.0
                    self.tracker.feed(samples, now)
                elif time.time() - last_data > NO_DATA_S:
                    log.warning("no audio from %s for %.0fs (capture by %s never linked); reconnecting",
                                stream["name"], time.time() - last_data, how)
                    return "nolink" if not got_any else "ended"
                if now >= next_post:
                    next_post = now + self.interval
                    self.report(now)
                    if self.silent_since is not None and now - self.silent_since > STALE_AFTER_S and self.player_running(stream):
                        # After suspend/resume a capture can keep delivering zeros while the
                        # player's node says "running". Reconnect rather than dance to nothing.
                        log.warning("silent for %.0fs while %s is running; reconnecting the capture", now - self.silent_since, stream["name"])
                        return "stale"
        finally:
            proc.kill()
            proc.wait(timeout=2)

    @staticmethod
    def player_running(stream: dict) -> bool:
        for s in pipewire_streams():
            if s["id"] == stream["id"]:
                return s["state"] == "running"
        return False

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

    path = config_file()
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
