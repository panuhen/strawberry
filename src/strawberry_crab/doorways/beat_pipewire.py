"""The beat's capture on Linux: the player's own PipeWire output stream through `pw-record`
(WIRING.md §4c). beat_watch.py drives it; beat_loopback.py is the Windows counterpart.

Captures ONE PipeWire output stream with `pw-record --target <node>`: the player's, never the
microphone and never the whole mixer, so her own voice, video calls and system sounds stay
out of the analysis. The node is picked automatically (a running audio stream from a known
player, else any running stream that is not ours) or fixed with --target / [beat].target.
After the first second of data the graph is read again to make sure the bytes come from that
stream and not from the sink monitor PipeWire links instead when it cannot link the target.
"""

from __future__ import annotations

import json
import logging
import os
import select
import subprocess

import numpy as np

log = logging.getLogger("beat_watch")

NAME = "PipeWire"
TARGET_HELP = "PipeWire node or application name (default: auto)"
LATENCY_S = 0.05
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


class Stream:
    """One `pw-record` run: 16-bit mono at `rate` on its stdout, as float32 samples."""

    def __init__(self, stream: dict, how: str, rate: int, chunk_samples: int) -> None:
        self.stream = stream
        self.name = stream["name"]
        self.how = how
        self.chunk_samples = chunk_samples
        self.pending = b""  # an odd byte left over from a read, so samples never go out of alignment
        self.checked = how == "sink-monitor"
        if how == "serial":
            cmd = ["pw-record", "--target", stream["serial"]]
        elif how == "name":
            cmd = ["pw-record", "--target", stream["name"]]
        else:
            # Default sink monitor: device-agnostic (follows the default sink) but hears every
            # app, her own voice included. Last resort, and the watcher says so in the log.
            cmd = ["pw-record", "-P", "stream.capture.sink=true"]
        cmd += ["-P", f"node.name={NODE_NAME}", "--rate", str(rate), "--channels", "1", "--format", "s16",
                "--latency", f"{int(LATENCY_S * 1000)}ms", "-"]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise OSError(f"pw-record not runnable: {exc}") from exc
        assert self.proc.stdout is not None
        self.fd = self.proc.stdout.fileno()

    def read(self, timeout_s: float) -> np.ndarray | None:
        """What pw-record wrote, or None when nothing came within `timeout_s`. EOFError once it ends."""
        ready, _, _ = select.select([self.fd], [], [], timeout_s)
        if not ready:
            return None
        raw = os.read(self.fd, self.chunk_samples * 2)
        if not raw:
            raise EOFError
        raw = self.pending + raw
        cut = len(raw) - len(raw) % 2
        self.pending = raw[cut:]
        return np.frombuffer(raw[:cut], dtype=np.int16).astype(np.float32) / 32768.0

    def verify(self, elapsed_s: float) -> str | None:
        """Once data has flowed for a second: make sure it is the player's, not the sink monitor
        PipeWire substitutes when it cannot link to the target."""
        if self.checked or elapsed_s <= 1.0:
            return None
        self.checked = True
        if linked_to_player(capture_sources(pw_dump()), self.stream) is False:
            log.warning("capture by %s got linked to the output device, not to %s; retrying", self.how, self.name)
            return "wrong-source"
        return None

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=2)


class Backend:
    """What beat_watch.Watcher asks of a capture: find the player, open a capture of it, and
    whether the player still runs. Rotates through STRATEGIES while PipeWire will not link us."""

    name = NAME

    def __init__(self) -> None:
        self.strategy = 0

    def find(self, target: str) -> dict | None:
        return pick_target(pipewire_streams(), target)

    def open(self, stream: dict, rate: int, chunk_samples: int) -> Stream:
        how = STRATEGIES[self.strategy % len(STRATEGIES)]
        log.info("listening to %s (%s, node %s, serial %s) by %s", stream["app"] or stream["name"], stream["name"],
                 stream["id"], stream["serial"], how)
        return Stream(stream, how, rate, chunk_samples)

    def after(self, outcome: str) -> float:
        """How long to wait before looking for the player again."""
        if outcome in ("nolink", "wrong-source"):
            # This way of asking PipeWire did not get us linked to the player; try the next.
            self.strategy += 1
            return 0.5
        self.strategy = 0
        return 1.0

    @staticmethod
    def running(stream: dict) -> bool:
        for s in pipewire_streams():
            if s["id"] == stream["id"]:
                return s["state"] == "running"
        return False

    def superseded(self, _stream: dict, _target: str) -> bool:
        """Another player took over (Windows asks its media sessions); a PipeWire capture runs
        until its stream ends."""
        return False

    def close(self) -> None:
        pass
