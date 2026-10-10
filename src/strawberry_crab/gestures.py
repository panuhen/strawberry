"""Gestures (WIRING.md §26, PROTOCOL.md Part 1d): the brain's side of the camera doorway.

Strawberry owns the camera: one watcher (doorways/gesture_watch.py, a process of its own, MediaPipe on
the CPU) turns frames into hand landmarks and gesture names and posts only those. Frames never leave it.

    gesture_watch  --POST /gesture {name, phase, progress}-->  GestureDesk  --> a reflex (actions.py), as a run
                   --POST /hand {x, y, pinch, open, points}-->              --> `gesture` / `hand` to the bodies

A recognised gesture is already typed, so it goes neither to the gate nor to a model: the map in
`[gestures.map]` names one of the reflex actions voice has (`ACTIONS`), and only those of the `read` and
`playback` tiers (what plays and how; undone in a second). Anything else is refused when the config loads.
While an approval is open, a thumbs up or down held for the hold time answers it, for the tiers a card
answers with a tap (not one in `[approvals] hold`, never `destructive`); a thumbs down may refuse any.

Only trusted v2 bodies that ask for them (`capabilities.gestures`) get `gesture` and `hand`; no other socket
gets either. A timeline notice is written only when a gesture made something happen.

The model file (`gesture_recognizer.task`, Apache-2.0, ~8 MB) is fetched by `strawberry gestures fetch` into
the data dir, from a pinned URL, and checked against its sha256 (`fetch`).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import paths

log = logging.getLogger("strawberryd.gestures")

# Shapes, each held for `hold_ms` to count (thumbs included): the recogniser's seven, pointing to either side
# (the user's own left and right), the finger counts and shh (a finger to the lips: the face model finds the
# lips). Then the motions (swipes and a wave, `done` alone) and the slider (a pinch held, then moved up or down).
SHAPES = ("thumb_up", "thumb_down", "palm_hold", "fist_hold", "point_hold", "victory_hold", "pinch_hold",
          "point_left", "point_right", "count_1", "count_2", "count_3", "count_4", "count_5", "shh")
MOTIONS = ("swipe_left", "swipe_right", "wave")
SLIDES = ("pinch_slide",)
MAPPABLE = SHAPES + MOTIONS + SLIDES
# `arm` is the raised open palm that turns command mode on (`[gestures] arming`); `nod` and `shake` answer her card
# (yes, no) while one is open, and nothing else: none of the three is mapped.
HEAD = ("nod", "shake")
NAMES = MAPPABLE + ("arm",) + HEAD
PHASES = ("started", "progress", "done", "cancelled")
# A shape and the count or slide that is the same hand (index up is count_1, the victory sign count_2, an open palm
# count_5; a held pinch is the slider's start): a map uses one name of each pair, and the hand goes by that one.
PARTNERS = {"point_hold": "count_1", "victory_hold": "count_2", "palm_hold": "count_5", "pinch_hold": "pinch_slide"}
# The digits, thumb first: their curls are in `hand` and in the calibration.
DIGITS = ("thumb", "index", "middle", "ring", "pinky")
# `[gestures.calibration]` (the Input tab records it): each digit's curl (0 straight … 1 curled) with the hand open
# and in a fist, and the thumb-index gap (over the palm's length) in a pinch and in a relaxed hand. The tracker
# puts its thresholds between them (gesture_track.Thresholds); a key left out takes its default.
CALIBRATION_DEFAULTS = {**{f"{d}_open": 0.05 for d in DIGITS}, **{f"{d}_curled": 0.95 for d in DIGITS},
                        "gap_closed": 0.1, "gap_apart": 0.7}
CALIBRATION_KEYS = tuple(CALIBRATION_DEFAULTS)

# What a gesture may run: the reflexes voice has (actions.py: a configured server's adapter, else MPRIS), by
# the gate's tool names and the adapters' said reflexes, each with the highest tier its calls have. A gesture
# runs `read` and `playback` only (WIRING §20: undone in a second, so a misread hand costs nothing).
ACTIONS = {
    "now_playing": "read",
    "pause": "playback",
    "resume": "playback",
    "skip": "playback",
    "previous": "playback",
    "volume_up": "playback",
    "volume_down": "playback",
    "like": "playback",          # Spotify's like_current: the track already playing, one tap undoes it
    "play_liked": "playback",
    # Push to talk without the hotkey: the same as `strawberry listen` (POST /listen). It changes nothing;
    # what the user then says is a sentence like any other, with its own tiers.
    "listen": "read",
    # The slider's: the player's volume set to where the hand has it (pinch_slide only, and it only this).
    "volume": "playback",
    # She waves back (her `wave` reaction and a line from persona.md's `wave`): changes nothing.
    "wave_back": "read",
    # Her voice off for `[gestures] shh_minutes` (the widget's quiet: the bubble stays): what plays, her own voice.
    "quiet": "playback",
}
TIERS = ("read", "playback")
# What `done` may say came of it, besides an ACTIONS name: an approval answered, command mode armed.
OUTCOMES = ("yes", "no", "arm")
# The gestures that answer her card, and with what: a thumb or the head.
ANSWERS = {"thumb_up": "yes", "nod": "yes", "thumb_down": "no", "shake": "no"}

# The points of a hand that go on the bus: the wrist and the five fingertips (MediaPipe's 0, 4, 8, 12, 16, 20).
POINTS = ("wrist", "thumb", "index", "middle", "ring", "pinky")
HAND_NUMBERS = ("x", "y", "size", "pinch", "open")
HAND_FLAGS = ("present", "engaged")
# Only while the watcher's face model runs: whether the user faces the screen, and whether a face is there.
FACE_FLAGS = ("facing", "face")
# Two hands (since 2026-10-11): `hands`, each hand seen now (the fields above but `present` and the face's, and
# these), and with two of them `pair`.
HAND_ENTRY = ("id", "handedness", "primary", "pinched")
HANDEDNESS = ("left", "right")
PAIR_FIELDS = ("distance", "angle", "both_pinched")
MAX_HANDS = 2

_TARGET = re.compile(r"^[a-z0-9_-]{1,32}$")

# The model: MediaPipe's gesture recogniser (hand detector, landmarks and the seven-gesture classifier in
# one file), float16, version 1. Pinned by URL and digest; `fetch` refuses anything else.
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/"
             "float16/1/gesture_recognizer.task")
MODEL_SHA256 = "97952348cf6a6a4915c2ea1496b4b37ebabc50cbbf80571435643c455f2b0482"
MODEL_SIZE = 8373440
MODEL_NAME = "gesture_recognizer.task"
# The face model: MediaPipe's face landmarker (a face detector and 478 landmarks, with the head's pose), float16,
# version 1. Only the head's yaw and pitch and where the mouth is are read from it, in the watcher: it is not face
# recognition (no identity, no embedding), and nothing of the face leaves the watcher but two booleans.
FACE_MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/"
                  "float16/1/face_landmarker.task")
FACE_MODEL_SHA256 = "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"
FACE_MODEL_SIZE = 3758596
FACE_MODEL_NAME = "face_landmarker.task"


@dataclass(frozen=True)
class Model:
    name: str
    url: str
    sha256: str
    size: int
    what: str


MODELS = (Model(MODEL_NAME, MODEL_URL, MODEL_SHA256, MODEL_SIZE, "the hand model"),
          Model(FACE_MODEL_NAME, FACE_MODEL_URL, FACE_MODEL_SHA256, FACE_MODEL_SIZE, "the face model"))


class GestureError(ValueError):
    """A /gesture or /hand body that is not one, or a map that cannot be used. The message says why."""


class FetchError(RuntimeError):
    pass


def model_file() -> Path:
    """Where the recogniser's model is kept: the data dir's models/, beside the gate's."""
    return paths.models_dir() / MODEL_NAME


def model_ready(path: Path | None = None) -> bool:
    path = path or model_file()
    try:
        return path.stat().st_size == MODEL_SIZE
    except OSError:
        return False


def face_model_file() -> Path:
    """The face model, beside the hand model."""
    return paths.models_dir() / FACE_MODEL_NAME


def face_model_ready(path: Path | None = None) -> bool:
    path = path or face_model_file()
    try:
        return path.stat().st_size == FACE_MODEL_SIZE
    except OSError:
        return False


def fetch_all(say: Callable[[str], None] = print, opener: Callable[..., Any] | None = None,
              models_dir: Path | None = None) -> list[Path]:
    """Both models into the data dir's models/: one already there with the right sha256 is left alone (and said
    so), the others are downloaded and checked. Stops at the first that fails (FetchError, OSError)."""
    folder = models_dir or paths.models_dir()
    return [fetch(folder / m.name, m.url, say=say, opener=opener, sha256=m.sha256, size=m.size, name=m.name)
            for m in MODELS]


def fetch(dest: Path | None = None, url: str = MODEL_URL, timeout: float = 30.0,
          say: Callable[[str], None] = print, opener: Callable[..., Any] | None = None,
          sha256: str = MODEL_SHA256, size: int = MODEL_SIZE, name: str = MODEL_NAME) -> Path:
    """Download a model (the hand model by default) to `dest` (the data dir's) and check its sha256 before it is
    put in place: a temporary file beside it, renamed over it only when the digest matches, so a failed fetch
    leaves whatever was there. One already there with the right digest is not downloaded again."""
    import urllib.error
    import urllib.request

    dest = dest or model_file()
    try:
        there = dest.stat().st_size == size
    except OSError:
        there = False
    if there and _sha256(dest) == sha256:
        say(f"{dest} is already there (sha256 {sha256[:12]}…); not downloaded")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    say(f"downloading {url}")
    digest = hashlib.sha256()
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", dir=dest.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out:
            try:
                with (opener or urllib.request.urlopen)(url, timeout=timeout) as response:
                    while chunk := response.read(1 << 16):
                        digest.update(chunk)
                        out.write(chunk)
            except urllib.error.HTTPError as exc:
                raise FetchError(f"{url}: HTTP {exc.code}") from None
            except (urllib.error.URLError, OSError) as exc:
                raise FetchError(f"{url}: {getattr(exc, 'reason', exc)}") from None
        actual = digest.hexdigest()
        if actual != sha256:
            raise FetchError(f"{name}: sha256 mismatch (expected {sha256}, got {actual}); not installed")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    say(f"installed {dest} ({size / 1e6:.1f} MB, sha256 {sha256[:12]}…)")
    return dest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def mediapipe_missing() -> str:
    """"" when MediaPipe and OpenCV import; else what to run to get them (the doctor, the watcher)."""
    import importlib.util

    missing = [name for name in ("mediapipe", "cv2") if importlib.util.find_spec(name) is None]
    if not missing:
        return ""
    return (f"{' and '.join(missing)} not installed: in a checkout `uv sync --inexact --group gpu --group gestures`, "
            "installed `uv tool install --force 'strawberry-crab[gestures]'`")


# --- the map ------------------------------------------------------------------------------


def split_key(key: str) -> tuple[str, str]:
    """"music:thumb_up" -> ("music", "thumb_up"); "thumb_up" -> ("", "thumb_up")."""
    target, _, name = key.rpartition(":")
    return target, name


def check_map(mapping: dict[str, Any]) -> dict[str, str]:
    """The `[gestures.map]` table as it will be used, or GestureError naming the first entry that cannot be:
    a key is a gesture (MAPPABLE), optionally after the target it is meant for ("music:thumb_up", for when the
    user points at something; WIRING §26); a value one of ACTIONS. A value that is not (a tool, a removal, a
    message) is refused here, not when the hand comes up: a gesture runs only `read` and `playback` actions."""
    out: dict[str, str] = {}
    for key, action in mapping.items():
        target, name = split_key(str(key))
        if name not in MAPPABLE:
            raise GestureError(f"gestures.map.{key}: {name!r} is not a gesture; one of {', '.join(MAPPABLE)}")
        if target and not _TARGET.match(target):
            raise GestureError(f"gestures.map.{key}: the target before ':' must be an entity id like \"music\"")
        if not isinstance(action, str):
            raise GestureError(f"gestures.map.{key} must be an action name (a string)")
        if action not in ACTIONS:
            raise GestureError(
                f"gestures.map.{key} = {action!r}: a gesture runs only read and playback actions, which are "
                f"{', '.join(ACTIONS)}. Saving, playlists, messages and removals are said or typed, where she "
                "can ask first.")
        if ACTIONS[action] not in TIERS:     # a table edit that adds a higher action trips here, not at runtime
            raise GestureError(f"gestures.map.{key} = {action!r} is of the {ACTIONS[action]} tier; gestures run "
                               f"{' and '.join(TIERS)} only")
        if (name in SLIDES) != (action == "volume"):
            raise GestureError(f"gestures.map.{key} = {action!r}: the slider (pinch_slide) sets the volume, and only "
                               "it does (volume_up and volume_down are steps for the other gestures)")
        out[str(key)] = action
    used = {split_key(key)[1] for key in out}
    for shape, partner in PARTNERS.items():
        if shape in used and partner in used:
            raise GestureError(f"gestures.map: {shape} and {partner} are the same hand; map one of them")
    return out


def check_calibration(values: Any) -> dict[str, float]:
    """`[gestures.calibration]` as the tracker will use it, or GestureError: known keys, numbers from 0 to 1, each
    digit's curl in a fist well above the open hand's, the gap in a pinch below the relaxed one."""
    if not isinstance(values, dict):
        raise GestureError("gestures.calibration must be a table of numbers")
    unknown = sorted(set(values) - set(CALIBRATION_KEYS))
    if unknown:
        raise GestureError(f"gestures.calibration: unknown keys {unknown}; known are {', '.join(CALIBRATION_KEYS)}")
    out: dict[str, float] = {}
    for key, value in values.items():
        out[key] = _unit(value, f"gestures.calibration.{key}")
    merged = dict(CALIBRATION_DEFAULTS) | out
    for digit in DIGITS:
        if merged[f"{digit}_curled"] - merged[f"{digit}_open"] < 0.15:
            raise GestureError(f"gestures.calibration: {digit}_curled must be at least 0.15 above {digit}_open "
                               "(record the fist and the open hand again)")
    if merged["gap_apart"] - merged["gap_closed"] < 0.1:
        raise GestureError("gestures.calibration: gap_apart must be at least 0.1 above gap_closed (record the pinch "
                           "and the relaxed hand again)")
    return {key: out[key] for key in CALIBRATION_KEYS if key in out}


# --- what comes in ----------------------------------------------------------------------


def _unit(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not 0.0 <= value <= 1.0:
        raise GestureError(f"{name} must be a number from 0 to 1")
    return round(float(value), 3)


def parse_gesture(data: Any) -> dict[str, Any]:
    """A POST /gesture body: {name, phase, progress}. Names and phases from fixed lists, progress 0-1 (needed
    with `progress`); nothing else. GestureError says what is wrong."""
    if not isinstance(data, dict):
        raise GestureError("a gesture must be an object")
    unknown = set(data) - {"name", "phase", "progress"}
    if unknown:
        raise GestureError(f"unknown gesture fields: {sorted(unknown)}")
    name, phase = data.get("name"), data.get("phase")
    if name not in NAMES:
        raise GestureError(f"gesture.name must be one of {', '.join(NAMES)}")
    if phase not in PHASES:
        raise GestureError(f"gesture.phase must be one of {', '.join(PHASES)}")
    if phase == "progress" and "progress" not in data:
        raise GestureError("a progress phase needs progress (0-1)")
    out: dict[str, Any] = {"name": name, "phase": phase}
    if "progress" in data:
        out["progress"] = _unit(data["progress"], "gesture.progress")
    elif phase == "done":
        out["progress"] = 1.0
    return out


def parse_hand(data: Any) -> dict[str, Any]:
    """A POST /hand body: {"present": false}, or where the hand is (x, y: its palm, 0-1 as in a mirror, y
    down), how big (size, 0-1 of the frame), how pinched and how open (0-1), whether it is raised into the
    zone (engaged), the shape the recogniser sees now (`shape`, one of SHAPES; absent for none) and the wrist and
    fingertips as [x, y] pairs: the primary hand. `hands` is each hand seen now with the same fields and its id,
    handedness, whether it is the primary and whether it pinches; `pair` the two hands' distance and angle.
    Numbers and fixed names only."""
    if not isinstance(data, dict):
        raise GestureError("a hand must be an object")
    present = data.get("present")
    if not isinstance(present, bool):
        raise GestureError("hand.present must be true or false")
    if not present:
        if set(data) - {"present"}:
            raise GestureError("a hand that is not present carries nothing else")
        return {"present": False}
    unknown = set(data) - set(HAND_NUMBERS) - set(HAND_FLAGS) - set(FACE_FLAGS) - {"points", "shape", "curl", "gap",
                                                                                  "hands", "pair"}
    if unknown:
        raise GestureError(f"unknown hand fields: {sorted(unknown)}")
    out: dict[str, Any] = {"present": True} | _one_hand(data, "hand")
    for key in FACE_FLAGS:
        if key in data:
            out[key] = _flag(data, key, "hand")
    if "hands" in data:
        hands = data["hands"]
        if not isinstance(hands, list) or not 1 <= len(hands) <= MAX_HANDS:
            raise GestureError(f"hand.hands must be a list of 1 to {MAX_HANDS} hands")
        out["hands"] = [_hand_entry(entry, f"hand.hands[{i}]") for i, entry in enumerate(hands)]
    if "pair" in data:
        if len(out.get("hands", ())) != 2:
            raise GestureError("hand.pair comes only with two hands")
        out["pair"] = _pair(data["pair"])
    return out


def _flag(data: dict[str, Any], key: str, name: str) -> bool:
    if not isinstance(data.get(key), bool):
        raise GestureError(f"{name}.{key} must be true or false")
    return data[key]


def _one_hand(data: dict[str, Any], name: str) -> dict[str, Any]:
    """One hand's fields: the hand message's top level, or an entry of `hands`."""
    out: dict[str, Any] = {}
    for key in HAND_NUMBERS:
        if key not in data:
            raise GestureError(f"{name}.{key} is missing")
        out[key] = _unit(data[key], f"{name}.{key}")
    out["engaged"] = _flag(data, "engaged", name) if "engaged" in data else False
    if "curl" in data:
        curl = data["curl"]
        if not isinstance(curl, dict) or set(curl) - set(DIGITS):
            raise GestureError(f"{name}.curl must be an object of {', '.join(DIGITS)}")
        out["curl"] = {d: _unit(curl[d], f"{name}.curl.{d}") for d in DIGITS if d in curl}
    if "gap" in data:
        out["gap"] = _unit(data["gap"], f"{name}.gap")
    if "shape" in data:
        if data["shape"] not in SHAPES:
            raise GestureError(f"{name}.shape must be one of {', '.join(SHAPES)}")
        out["shape"] = data["shape"]
    points = data.get("points", {})
    if not isinstance(points, dict) or set(points) - set(POINTS):
        raise GestureError(f"{name}.points must be an object of {', '.join(POINTS)}")
    shaped: dict[str, list[float]] = {}
    for key in POINTS:
        if key not in points:
            continue
        pair = points[key]
        if not isinstance(pair, list) or len(pair) != 2:
            raise GestureError(f"{name}.points.{key} must be [x, y]")
        shaped[key] = [_unit(pair[0], f"{name}.points.{key}"), _unit(pair[1], f"{name}.points.{key}")]
    if shaped:
        out["points"] = shaped
    return out


def _hand_entry(data: Any, name: str) -> dict[str, Any]:
    """An entry of `hands`: its id (a positive integer, the same while that hand stays), its handedness (the user's
    own `left` or `right`; absent when not known), whether it is the primary and whether it pinches, and one
    hand's fields."""
    if not isinstance(data, dict):
        raise GestureError(f"{name} must be an object")
    unknown = set(data) - set(HAND_NUMBERS) - {"engaged", "points", "shape", "curl", "gap"} - set(HAND_ENTRY)
    if unknown:
        raise GestureError(f"unknown {name} fields: {sorted(unknown)}")
    slot = data.get("id")
    if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot < 2 ** 31:
        raise GestureError(f"{name}.id must be a positive integer")
    out: dict[str, Any] = {"id": slot}
    if "handedness" in data:
        if data["handedness"] not in HANDEDNESS:
            raise GestureError(f"{name}.handedness must be left or right")
        out["handedness"] = data["handedness"]
    out["primary"] = _flag(data, "primary", name)
    out["pinched"] = _flag(data, "pinched", name) if "pinched" in data else False
    return out | _one_hand(data, name)


def _pair(data: Any) -> dict[str, Any]:
    """`pair`: the palms' distance (in x and y's own units, 0 up to 1.5), the angle of the line from the hand on the
    screen's left to the one on its right (degrees on the picture, -90 to 90, positive when the right one is
    higher) and whether both pinch."""
    if not isinstance(data, dict) or set(data) != set(PAIR_FIELDS):
        raise GestureError(f"hand.pair must be an object of {', '.join(PAIR_FIELDS)}")
    for key, low, high in (("distance", 0.0, 1.5), ("angle", -90.0, 90.0)):
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                or not low <= value <= high:
            raise GestureError(f"hand.pair.{key} must be a number from {low:g} to {high:g}")
    return {"distance": round(float(data["distance"]), 3), "angle": round(float(data["angle"]), 1),
            "both_pinched": _flag(data, "both_pinched", "hand.pair")}


# --- the brain's desk -------------------------------------------------------------------


@dataclass
class Limit:
    """At most `rate` a second, `burst` at once (a token bucket)."""

    rate: float
    burst: float
    tokens: float = -1.0
    at: float = field(default_factory=time.monotonic)

    def take(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if self.tokens < 0:
            self.tokens = self.burst
        self.tokens = min(self.burst, self.tokens + (now - self.at) * self.rate)
        self.at = now
        if self.tokens < 1.0:
            return False
        self.tokens -= 1.0
        return True


@dataclass
class Slide:
    """One pinch-and-slide: the volume when it began (`base`, 0-1, read in the background), where the hand has the
    slider now (`position`: 0.5 where it began, up is more), and what was last set and when."""

    action: str
    practice: bool
    run_id: str = ""
    position: float = 0.5
    base: float | None = None
    sent: float | None = None
    sent_at: float = -1e9
    told: float | None = None
    ended: bool = False
    calls: int = 0
    entry: dict[str, Any] | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def level(self) -> float | None:
        """The volume the slider stands at: from where it began, the whole range for the slider's whole travel."""
        if self.base is None:
            return None
        return round(min(max(self.base + 2.0 * (self.position - 0.5), 0.0), 1.0), 3)


class GestureDesk:
    """The daemon's gestures: what /gesture and /hand do, the state the watcher asks for, the runs.

    `daemon` is the Daemon: its config, hub, runs, actor, approvals, ledger and report are used as they are,
    so a gesture's reflex is the same run, the same line and the same timeline entry a spoken one would be,
    with `source: "gesture"`."""

    MAX_HAND_HZ = 30.0
    DONE_GAP_S = 0.5          # a second action within this of the last is refused (the watcher's own cooldown is longer)
    SLIDE_GAP_S = 0.25        # the slider sets the volume at most four times a second, and its last level at the end
    SLIDE_IDLE_S = 10.0       # a slide the watcher stopped telling about ends here, at its last level
    WAVE_LINE_S = 20.0        # she says a line when she waves back at most this often (the wave itself every time)

    def __init__(self, daemon: Any) -> None:
        self.daemon = daemon
        self.armed_until = 0.0
        self.received = 0
        self.acted = 0
        self.answered = 0
        self.refused = 0
        self.last_done = -1e9
        self.last: dict[str, Any] | None = None    # name, action, ok: no frame, no hand, nothing said
        self.hand_at = -1e9
        self.gesture_limit = Limit(30.0, 30.0)
        self.hand_limit = Limit(self.MAX_HAND_HZ, 10.0)
        self.slider: Slide | None = None
        self.waved_at = -1e9
        self.quiet_until = 0.0    # shh: her voice is off (the widget's quiet) until then, by the brain's clock

    @property
    def config(self) -> Any:
        return self.daemon.config.gestures

    # --- what the watcher asks ------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """What the camera should do now (GET /gesture, and every reply): whether gestures are on, how long
        command mode stays armed, the approval a thumb may answer, and who wants what."""
        approval = self.answerable()
        viewing = self.live is not None and self.live.watching      # the Brain UI's Input tab is open
        return {"enabled": self.config.enabled,
                "armed_s": round(max(self.armed_until - time.monotonic(), 0.0), 2),
                "approval": approval,
                "wanted": {"gesture": viewing or self.daemon.hub.wants_gesture("gesture"),
                           "hand": viewing or self.daemon.hub.wants_gesture("hand")}}

    @property
    def live(self) -> Any:
        desk = getattr(self.daemon, "input", None)
        return desk.live if desk is not None else None

    @property
    def practice(self) -> bool:
        desk = getattr(self.daemon, "input", None)
        return bool(desk is not None and desk.practice)

    def note(self, name: str, outcome: str, action: str = "", why: str = "") -> dict[str, Any] | None:
        """A `done` in the Input tab's recent list: its name, what it did or why not (no target, no hand)."""
        desk = getattr(self.daemon, "input", None)
        return desk.recent.add("gesture", name, outcome, action, why) if desk is not None else None

    def _finish(self, entry: dict[str, Any] | None, outcome: str, why: str = "") -> None:
        desk = getattr(self.daemon, "input", None)
        if entry is not None and desk is not None and entry.get("outcome") == "running":
            desk.recent.finish(entry, outcome, why)

    def answerable(self) -> dict[str, bool] | None:
        """The open approval as a thumb sees it: may a thumbs up say yes (a tier a card answers with a tap: not
        in `[approvals] hold`, never destructive), and a thumbs down no (any tier). None: none open, or
        `[gestures] approvals` is off."""
        pending = self.daemon.approvals.open
        if pending is None or not self.config.approvals:
            return None
        yes = pending.risk != "destructive" and pending.risk not in self.daemon.config.approvals.hold
        return {"yes": yes, "no": True}

    def arm(self, seconds: float | None = None) -> float:
        """Command mode on for `seconds` (`[gestures] armed_s`): the arming palm, or `strawberry gestures arm`."""
        self.armed_until = time.monotonic() + (self.config.armed_s if seconds is None else seconds)
        return self.config.armed_s if seconds is None else seconds

    def target(self) -> str:
        """What the user is pointing at, for a map entry like "music:thumb_up": the entity id of the body link's
        current target (bodylink.Targets, WIRING §25), or "" for none, and then the plain entry is used."""
        targets = getattr(self.daemon, "targets", None)
        current = targets.current() if targets is not None else None
        return current.entity.id if current is not None else ""

    def action_for(self, name: str) -> str:
        target = self.target()
        mapping = self.config.map
        if target and f"{target}:{name}" in mapping:
            return mapping[f"{target}:{name}"]
        return mapping.get(name, "")

    # --- POST /gesture and /hand -----------------------------------------------------

    def limited(self, kind: str) -> bool:
        return not (self.hand_limit if kind == "hand" else self.gesture_limit).take()

    async def gesture(self, message: dict[str, Any]) -> dict[str, Any]:
        """One parsed /gesture: broadcast it, and for `done` decide what it does (an approval answer, arming, a
        reflex as a run of its own in the background). Returns the reply's `action` and `run_id` ("" none)."""
        self.received += 1
        name, phase = message["name"], message["phase"]
        if name in SLIDES:
            return await self.slide(message)
        out: dict[str, Any] = {"action": "", "run_id": ""}
        if phase == "done":
            now = time.monotonic()
            if now - self.last_done < self.DONE_GAP_S:
                self.refused += 1
                out["refused"] = "too_soon"
                self.note(name, "ignored", why="too_soon")
                return out
            self.last_done = now
            out = await self.done(name)
        bus = {"type": "gesture", "t": round(time.monotonic(), 3), "name": name, "phase": phase}
        if "progress" in message:
            bus["progress"] = message["progress"]
        if phase == "done" and out.get("action") and out.get("ok", True):
            bus["action"] = out["action"]          # what was done: absent when nothing was
        if phase == "done" and out.get("run_id"):
            bus["run_id"] = out["run_id"]
        live = self.live
        if live is not None:
            shown = {k: v for k, v in bus.items() if k not in ("type", "t", "run_id")}
            live.publish("gesture", shown | ({"practice": out["practice"]} if out.get("practice") else {}))
        out["sent"] = await self.daemon.hub.send_gesture(bus)
        return out

    async def done(self, name: str) -> dict[str, Any]:
        approval = self.answerable()
        if self.practice:
            # Practice mode (the Brain UI's Input tab, never saved): recognised and shown, nothing done.
            if name == "arm":
                would = "arm"
            elif name in HEAD or (approval is not None and name in ("thumb_up", "thumb_down")):
                would = ANSWERS[name] if approval is not None else ""
            else:
                would = self.action_for(name)
            log.info("gestures: %s in practice mode; nothing done", name)
            self.last = {"name": name, "action": None, "ok": False}
            self.note(name, "practice", would)
            return {"action": "", "run_id": "", "refused": "practice", "practice": would or "nothing"}
        if name == "arm":
            self.arm()
            log.info("gestures: armed for %.0fs", self.config.armed_s)
            self.last = {"name": name, "action": "arm", "ok": True}
            self.note(name, "did", "arm")
            return {"action": "arm", "run_id": ""}
        if name in HEAD and approval is None:
            log.info("gestures: a %s with no question open; nothing done", name)
            self.refused += 1
            self.last = {"name": name, "action": None, "ok": False}
            self.note(name, "ignored", why="no_question")
            return {"action": "", "run_id": "", "refused": "no_question"}
        if approval is not None and name in ANSWERS:
            return self.answer(name, approval)
        action = self.action_for(name)
        if not action:
            log.info("gestures: %s is not mapped; nothing done", name)
            self.last = {"name": name, "action": None, "ok": False}
            self.note(name, "ignored", why="not_mapped")
            return {"action": "", "run_id": ""}
        if self.config.arming:
            # Command mode (the watcher keeps it too; this is the second look): off, a mapped gesture does
            # nothing; on, each one keeps it on a while longer.
            if time.monotonic() >= self.armed_until:
                log.info("gestures: %s while command mode is off; nothing done", name)
                self.refused += 1
                self.last = {"name": name, "action": None, "ok": False}
                self.note(name, "ignored", action, "not_armed")
                return {"action": "", "run_id": "", "refused": "not_armed"}
            self.arm()
        if action == "listen":
            result = self.daemon.listen()
            ok = bool(result.get("listening")) or "loading" in result
            log.info("gestures: %s -> listen (%s)", name, "listening" if result.get("listening") else
                     next((k for k in ("loading", "busy", "stopped", "debounced", "error") if k in result), "no"))
            self.acted += 1
            self.last = {"name": name, "action": action, "ok": ok}
            self.note(name, "did" if ok else "failed", action, "" if ok else "not_listening")
            return {"action": action, "run_id": "", "ok": ok}
        if action == "wave_back":
            return self.wave_back(name)
        if action == "quiet":
            return await self.quiet(name)
        found = await self.daemon.actor.named(action)
        if found is None:
            log.info("gestures: %s -> %s, but nothing here can do it (no music server or player for it)", name, action)
            self.last = {"name": name, "action": action, "ok": False}
            self.note(name, "failed", action, "nothing_can_do_it")
            return {"action": action, "run_id": "", "ok": False}
        run = self.daemon.runs.start("gesture", foreground=False)
        entry = self.note(name, "running", action)
        self.daemon.background(self._run(run, name, action, found, entry), f"gesture {name}")
        self.acted += 1
        return {"action": action, "run_id": run.run_id}

    def answer(self, name: str, approval: dict[str, bool]) -> dict[str, Any]:
        """A held thumb, a nod or a shake while an approval is open: yes or no, for the tiers `answerable` allows
        (a yes never for a tier her card holds for, a no for any). A thumb never runs its mapped action while a
        question is open."""
        said = ANSWERS[name]
        pending = self.daemon.approvals.open
        if pending is None or not approval.get(said):
            log.info("gestures: a %s cannot answer a %s approval; say it, type it or use the card",
                     name.replace("_", " "), pending.risk if pending is not None else "closed")
            self.refused += 1
            self.last = {"name": name, "action": None, "ok": False}
            self.note(name, "ignored", said, "not_answerable")
            return {"action": "", "run_id": "", "refused": "not_answerable"}
        reason = self.daemon.approvals.answer(pending.approval_id, said, "gesture")
        if reason is not None:
            self.refused += 1
            self.last = {"name": name, "action": said, "ok": False}
            self.note(name, "ignored", said, str(reason))
            return {"action": "", "run_id": "", "refused": reason}
        log.info("gestures: %s answered %s %s", name, pending.approval_id, said)
        self.answered += 1
        self.last = {"name": name, "action": said, "ok": True}
        self.note(name, "did", said)
        return {"action": said, "run_id": pending.run_id}

    async def _run(self, run: Any, name: str, action: str, found: tuple[str, str, Any],
                   entry: dict[str, Any] | None = None) -> None:
        """The reflex as a run (`source: "gesture"`, never the foreground one: a sentence does not stop it):
        its tool events, her line (the fact and a quip, as for a spoken reflex) and a timeline notice."""
        from . import runs

        daemon = self.daemon
        token = runs.active.set(run)
        try:
            daemon.quiet_media_until = time.monotonic() + daemon.QUIET_MEDIA_S
            outcome = await daemon.actor.act_named(action, found,
                                                   on_call=daemon._reflex_events(run, daemon._routing(run, None)),
                                                   tiers=TIERS)
            if outcome is None:
                run.error = "tools"
                self.last = {"name": name, "action": action, "ok": False}
                self._finish(entry, "failed", "refused")
                return
            daemon.quiet_media_until = time.monotonic() + daemon.QUIET_MEDIA_S
            if not outcome.ok:
                run.error = "tools"
            self.last = {"name": name, "action": action, "ok": outcome.ok}
            self._finish(entry, "did" if outcome.ok else "failed", "" if outcome.ok else "tool_failed")
            performance, _ = await daemon.report(outcome.event(f"a {name.replace('_', ' ')} gesture"), outcome.ok)
            # Her line is a reflex's fact (a track's name from the player or server): strangers' text.
            daemon.ledger.notice("reflex", f"a {name.replace('_', ' ')} gesture: {outcome.did}",
                                 performance.text or "", foreign=True)
        except asyncio.CancelledError:
            run.cancel_reason = run.cancel_reason or "shutdown"
            raise
        except Exception:
            run.error = run.error or "other"
            self._finish(entry, "failed", "error")
            raise
        finally:
            runs.active.reset(token)
            daemon.runs.finish(run)

    # --- wave and shh ------------------------------------------------------------------

    def wave_back(self, name: str) -> dict[str, Any]:
        """She waves back: her `wave` reaction and, unless she said one lately, a line of persona.md's `wave` (no
        model). Not while she is busy (a run, a line, listening); nothing changes and nothing goes in the
        timeline."""
        import random

        from .contract import Performance

        daemon = self.daemon
        if daemon.runs.busy() is not None or daemon.current_state() not in ("idle", "dancing"):
            log.info("gestures: %s while she is busy; no wave back", name)
            self.last = {"name": name, "action": "wave_back", "ok": False}
            self.note(name, "ignored", "wave_back", "busy")
            return {"action": "", "run_id": "", "refused": "busy"}
        now = time.monotonic()
        line = ""
        if now - self.waved_at >= self.WAVE_LINE_S:
            line = daemon.persona.line("wave", random) or ""
            self.waved_at = now
        if line:
            performance = Performance(state="talking", text=line, emotion="happy", reaction="wave")
        else:
            performance = Performance(state=daemon.current_state(), emotion="happy", reaction="wave")
        daemon.background(daemon.perform(performance), "wave back")
        log.info("gestures: %s -> wave_back%s", name, " with a line" if line else "")
        self.acted += 1
        self.last = {"name": name, "action": "wave_back", "ok": True}
        self.note(name, "did", "wave_back")
        return {"action": "wave_back", "run_id": ""}

    async def quiet(self, name: str) -> dict[str, Any]:
        """shh: her voice off for `[gestures] shh_minutes`, the bubble kept. It is the widget's own quiet (the
        `quiet` command, as the tray's Quiet row sends it), so the crab's menu and the tray show the minutes left
        and either one turns it off again. She says so in the bubble, and the timeline gets a notice (the user's
        own act: not foreign)."""
        import random

        from .contract import Performance

        daemon = self.daemon
        seconds = max(1, round(self.config.shh_minutes * 60))
        sent = await daemon.hub.send({"command": "quiet", "value": seconds})
        if not sent:
            log.info("gestures: %s, but no body is connected to keep quiet", name)
            self.last = {"name": name, "action": "quiet", "ok": False}
            self.note(name, "failed", "quiet", "no_body")
            return {"action": "quiet", "run_id": "", "ok": False}
        self.quiet_until = time.monotonic() + seconds
        minutes = round(seconds / 60)
        log.info("gestures: %s -> quiet for %d min (%d body/bodies)", name, minutes, sent)
        line = daemon.persona.line("shushed", random)
        if line:
            daemon.background(daemon.perform(Performance(state="talking", text=line, emotion="neutral")), "shushed")
        daemon.ledger.notice("reflex", f"the user shushed you with a finger to the lips, so your voice is off for "
                             f"{minutes} minutes (the bubble stays)", "", foreign=False)
        self.acted += 1
        self.last = {"name": name, "action": "quiet", "ok": True}
        self.note(name, "did", "quiet")
        return {"action": "quiet", "run_id": ""}

    def quiet_set(self, seconds: float) -> None:
        """The widget's quiet set from elsewhere (the tray's Quiet row, POST /command quiet): 0 lets her talk
        again."""
        self.quiet_until = time.monotonic() + seconds if seconds > 0 else 0.0

    def quiet_left(self) -> float:
        return round(max(self.quiet_until - time.monotonic(), 0.0), 1)

    # --- the slider ---------------------------------------------------------------------

    async def slide(self, message: dict[str, Any]) -> dict[str, Any]:
        """pinch_slide's phases from the watcher. `started` begins a slide (the volume is read in the background;
        the bodies get `started` with it as the progress once it is known), `progress` moves it (the watcher's
        position, 0.5 where it began), `done` or `cancelled` ends it at its last level. The volume is set at most
        four times a second while it moves, and to the last level when it ends (`_slide_run`)."""
        phase = message["phase"]
        slide = self.slider
        if phase == "started":
            if slide is not None and not slide.ended:
                slide.ended = True
                slide.changed.set()
            return await self._start_slide()
        if slide is None or slide.ended:
            return {"action": "", "run_id": ""}
        slide.position = message.get("progress", slide.position)
        if phase in ("done", "cancelled"):
            slide.ended = True
        slide.changed.set()
        return {"action": "" if slide.practice else slide.action, "run_id": slide.run_id}

    async def _start_slide(self) -> dict[str, Any]:
        from .actions import Guarded

        name = "pinch_slide"
        action = self.action_for(name)
        if not action:
            log.info("gestures: %s is not mapped; nothing done", name)
            self.note(name, "ignored", why="not_mapped")
            return {"action": "", "run_id": ""}
        if self.config.arming and not self.practice:
            if time.monotonic() >= self.armed_until:
                self.refused += 1
                self.note(name, "ignored", action, "not_armed")
                return {"action": "", "run_id": "", "refused": "not_armed"}
            self.arm()
        actor = self.daemon.actor
        control = actor.volume_control()
        if control is None:
            log.info("gestures: %s -> volume, but nothing here can set it (no music server or player for it)", name)
            self.last = {"name": name, "action": action, "ok": False}
            self.note(name, "failed", action, "nothing_can_do_it")
            return {"action": action, "run_id": "", "ok": False}
        why = actor.above(control[0], "volume", TIERS)
        if why:
            log.info("gestures: %s -> volume is not run: %s", name, why)
            self.refused += 1
            self.last = {"name": name, "action": action, "ok": False}
            self.note(name, "failed", action, "refused")
            return {"action": "", "run_id": "", "refused": "would_ask"}
        slide = Slide(action, self.practice)
        run = None if slide.practice else self.daemon.runs.start("gesture", foreground=False)
        slide.run_id = run.run_id if run is not None else ""
        slide.entry = self.note(name, "practice" if slide.practice else "running", action)
        self.slider = slide
        self.daemon.background(self._slide_run(slide, run, control, Guarded(actor.toolbox)), "gesture pinch_slide")
        if slide.practice:
            return {"action": "", "run_id": "", "refused": "practice", "practice": action}
        self.acted += 1
        return {"action": action, "run_id": slide.run_id}

    async def _slide_bus(self, phase: str, level: float, action: str = "", run_id: str = "",
                         practice: str = "") -> None:
        bus = {"type": "gesture", "t": round(time.monotonic(), 3), "name": "pinch_slide", "phase": phase,
               "progress": level}
        if action:
            bus["action"] = action
        if run_id and phase == "done":
            bus["run_id"] = run_id
        live = self.live
        if live is not None:
            shown = {k: v for k, v in bus.items() if k not in ("type", "t", "run_id")}
            live.publish("gesture", shown | ({"practice": practice} if practice else {}))
        await self.daemon.hub.send_gesture(bus)

    async def _slide_run(self, slide: Slide, run: Any, control: tuple[str, Any, Any], guarded: Any) -> None:
        """The slide's volume calls, one at a time and at most four a second, and what the bodies see of it: the
        level as the progress (`started`, `progress`, then `done` with the action), so they can draw a slider."""
        from . import runs

        daemon = self.daemon
        server, get, put = control
        token = runs.active.set(run) if run is not None else None
        on_call = daemon._reflex_events(run, daemon._routing(run, None)) if run is not None else None
        called, ok = False, True
        try:
            try:
                base = await asyncio.wait_for(get(guarded, server), 3.0)
            except Exception as exc:   # noqa: BLE001 - a player that does not answer: the slide does nothing
                log.info("gestures: the slider could not read the volume (%s)", type(exc).__name__)
                base = None
            if base is None:
                slide.ended = True
                await self._slide_bus("cancelled", 0.0)
                self.last = {"name": "pinch_slide", "action": slide.action, "ok": False}
                self._finish(slide.entry, "failed", "no_volume")
                if run is not None:
                    run.error = "tools"
                return
            slide.base = min(max(float(base), 0.0), 1.0)
            practice = slide.action if slide.practice else ""
            slide.told = round(slide.base, 3)        # the level it starts from; where the hand has it comes next
            await self._slide_bus("started", slide.told, practice=practice)
            while True:
                level = slide.level()
                if not slide.ended and abs(level - slide.told) >= 0.005:
                    slide.told = level
                    await self._slide_bus("progress", level, practice=practice)
                last = slide.base if slide.sent is None else slide.sent
                if not slide.practice and abs(level - last) >= 0.01:
                    wait = self.SLIDE_GAP_S - (time.monotonic() - slide.sent_at)
                    if wait > 0:
                        await asyncio.sleep(wait)
                        continue
                    if not called and on_call is not None:
                        on_call("started", server, "volume")
                    called = True
                    outcome = await asyncio.wait_for(put(guarded, server, level), daemon.actor.config.timeout_s)
                    slide.calls += 1
                    slide.sent, slide.sent_at = level, time.monotonic()
                    if not outcome.ok:
                        ok = False
                        log.info("gestures: the slider's volume call failed: %s", outcome.did)
                        slide.ended = True
                        break
                    continue
                if slide.ended:
                    break
                slide.changed.clear()
                try:
                    await asyncio.wait_for(slide.changed.wait(), self.SLIDE_IDLE_S)
                except asyncio.TimeoutError:
                    slide.ended = True
            level = slide.sent if slide.sent is not None else slide.level()
            done = slide.action if ok and not slide.practice else ""
            await self._slide_bus("done", level, done, slide.run_id, practice)
            if called and on_call is not None:
                on_call("completed", server, "volume", ok, "" if ok else "failed")
            self.last = {"name": "pinch_slide", "action": slide.action, "ok": ok and not slide.practice}
            if slide.practice:
                return
            self._finish(slide.entry, "did" if ok else "failed", "" if ok else "tool_failed")
            if not ok and run is not None:
                run.error = "tools"
            if slide.sent is not None:
                log.info("gestures: pinch_slide -> volume %d%% (%d call(s))", round(level * 100), slide.calls)
                daemon.ledger.notice("reflex", f"a pinch slide gesture: you set the volume to {round(level * 100)}",
                                     "", foreign=False)
        except asyncio.CancelledError:
            if run is not None:
                run.cancel_reason = run.cancel_reason or "shutdown"
            raise
        except Exception:
            if run is not None:
                run.error = run.error or "other"
            self._finish(slide.entry, "failed", "error")
            raise
        finally:
            if token is not None:
                runs.active.reset(token)
            if run is not None:
                daemon.runs.finish(run)
            if self.slider is slide:
                self.slider = None

    async def hand(self, message: dict[str, Any]) -> int:
        """One parsed /hand: to the bodies that asked for it, stamped with the brain's clock."""
        self.hand_at = time.monotonic()
        live = self.live
        if live is not None:
            live.publish("hand", message)
        return await self.daemon.hub.send_gesture({"type": "hand", "t": round(self.hand_at, 3)} | message)

    def reload(self) -> bool:
        """Read [gestures] from the config file again (the tray's Gestures row, `strawberry gestures on|off`),
        with input.toml over it (inputs.py). Raises ConfigError, keeping what she has, when config.toml does not
        load; an input.toml that does not check out keeps the rest she has and takes only `enabled`. Returns
        whether they are on."""
        from .config import default_path, load

        loaded = load(self.daemon.config.path or default_path())
        fresh = loaded.gestures
        if loaded.input_error:
            self.daemon.config.gestures.enabled = fresh.enabled
        else:
            self.daemon.config.gestures = fresh
        if not fresh.enabled:
            self.armed_until = 0.0
        return fresh.enabled

    def stats(self) -> dict[str, Any]:
        """For /health: counts and the last gesture's name and action. No hand, no position, no frame."""
        return {"enabled": self.config.enabled, "received": self.received, "acted": self.acted,
                "answered": self.answered, "refused": self.refused, "last": self.last,
                "hand_age_s": None if self.hand_at < 0 else round(time.monotonic() - self.hand_at, 1),
                "quiet_s": self.quiet_left(), "sliding": self.slider is not None}
