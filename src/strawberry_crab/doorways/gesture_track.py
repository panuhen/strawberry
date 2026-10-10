"""The gesture doorway's reading of a hand and a head over time (WIRING.md §26): numbers in, names out.

    Hand (21 landmarks as in a mirror, the recogniser's label)  ->  Tracker.step(t, hand)  ->  [("gesture" | "hand", {...})]
    Face (yaw, pitch, the mouth: the watcher's face model)     ->  Tracker.look(t, face)  ->  [("gesture", nod | shake)]

No camera and no model here, so the tests feed it synthetic hands and faces. What it guards against: the webcam
sees the user all day, and a hand that only passes by, scratches an ear, reaches for a mug or talks along with the
mouth must do nothing.

1. Engagement: a hand counts only when it is raised into the zone (its wrist above `zone`, 0 at the top of the
   frame) and near enough (`min_size` of the frame). Anything else cancels a hold and clears a swipe.
2. Facing the screen: with the face model running (`Settings.face`) and `require_facing`, a gesture counts only
   while the user faces the screen (the head's yaw and pitch within a cone, with hysteresis) and a face was seen
   in the last `present_s`. A hand that talks along while the user looks at someone else does nothing. Without
   the face model the check is skipped, and shh, nod and shake cannot be read.
3. Hold to confirm: a shape (thumbs up, open palm, pointing, a count …) must be held still for `hold_s`; it goes out
   as `started`, `progress` and `done` (the bodies fill a ring), and a moved, changed or dropped hand cancels it.
4. Re-arm: a shape that fired must be left for `rearm_s` (or the hand leave) before it can fire again; nothing fires
   within `cooldown_s` of the last, and some gestures have a longer cooldown of their own (COOLDOWNS).
5. Arming (`arming`): command mode first, by a held open palm (`arm`), for `armed_s`; each gesture keeps it on.
   A thumb (or a nod or shake) answering the open approval needs no arming.

The shapes are read from the landmarks' geometry (`Shaper`): each digit's curl (the bends of its joints, from the
3D world landmarks when the recogniser gives them) is "extended", "curled" or neither, with an enter and an exit
threshold each (hysteresis), from the user's calibration when there is one (`Thresholds`). The recogniser's own
label decides the thumbs and helps where the geometry is unsure. The landmarks are smoothed first (`OneEuro`).

Motions: a swipe is the palm moving across at least `swipe_dist` of the frame within `swipe_s`, mostly sideways,
by a hand that had been up for `settle_s` before it started. A wave is an open palm going side to side (at least
two turns of `wave_amp` within `wave_s`). A pinch held still for `slide_hold_s` becomes a slider (`pinch_slide`):
moving the hand up or down moves it, and letting go ends it. x is as in a mirror (the user's right is +x), so
swipe_right is a move to the user's right and point_right a finger toward it.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..gestures import CALIBRATION_DEFAULTS, DIGITS, PARTNERS

# MediaPipe's 21 hand landmarks.
WRIST = 0
THUMB_CMC, THUMB_MCP, THUMB_IP, THUMB_TIP = 1, 2, 3, 4
INDEX_MCP, INDEX_PIP, INDEX_DIP, INDEX_TIP = 5, 6, 7, 8
MIDDLE_MCP, MIDDLE_PIP, MIDDLE_DIP, MIDDLE_TIP = 9, 10, 11, 12
RING_MCP, RING_PIP, RING_DIP, RING_TIP = 13, 14, 15, 16
PINKY_MCP, PINKY_PIP, PINKY_DIP, PINKY_TIP = 17, 18, 19, 20
TIPS = {"wrist": WRIST, "thumb": THUMB_TIP, "index": INDEX_TIP, "middle": MIDDLE_TIP, "ring": RING_TIP,
        "pinky": PINKY_TIP}
CHAINS = {"index": (INDEX_MCP, INDEX_PIP, INDEX_DIP, INDEX_TIP), "middle": (MIDDLE_MCP, MIDDLE_PIP, MIDDLE_DIP, MIDDLE_TIP),
          "ring": (RING_MCP, RING_PIP, RING_DIP, RING_TIP), "pinky": (PINKY_MCP, PINKY_PIP, PINKY_DIP, PINKY_TIP)}
FINGERS = ("index", "middle", "ring", "pinky")

# The recogniser's categories (MediaPipe's canned gesture classifier) -> our shapes. "None" and "ILoveYou" are none.
LABELS = {"Thumb_Up": "thumb_up", "Thumb_Down": "thumb_down", "Open_Palm": "palm_hold", "Closed_Fist": "fist_hold",
          "Pointing_Up": "point_hold", "Victory": "victory_hold"}
SWIPES = ("swipe_left", "swipe_right")
PINCHED = 0.85        # the `pinch` number from which the hand message's pinch reads as closed (the old threshold)

# A finger's curl is the sum of its three joints' bends over this many degrees: 0 straight, 1 a closed fist.
CURL_DEGREES = 240.0
# Where a pointing finger points, by its elevation (degrees above the horizontal): up from UP_IN (kept down to
# UP_OUT), sideways within SIDE_IN of the horizontal (kept up to SIDE_OUT). Between them is the dead zone: a
# diagonal finger is neither.
UP_IN, UP_OUT = 60.0, 50.0
SIDE_IN, SIDE_OUT = 25.0, 35.0
UPRIGHT = 60.0        # a count needs the hand upright: the wrist-to-knuckles line at least this far above level
# shh: the pointing finger (its last two bones) within this many face widths of the mouth's centre.
SHH_NEAR = 0.25
# A gesture's own cooldown after it fired, beyond the shared `cooldown_s`.
COOLDOWNS = {"wave": 3.0, "shh": 5.0, "nod": 1.5, "shake": 1.5}


@dataclass(frozen=True)
class Hand:
    """One hand in one frame: 21 (x, y) points in 0-1, x as in a mirror and y down, and what the recogniser called
    it (its category name and score; "" when there is none). `world` is the same 21 points in metres (x mirrored),
    when the recogniser gives them: the joints' bends are read from those. `aspect` is the frame's width over its
    height, for directions."""

    points: tuple[tuple[float, float], ...]
    label: str = ""
    score: float = 1.0
    world: tuple[tuple[float, float, float], ...] | None = None
    aspect: float = 1.0


@dataclass(frozen=True)
class Face:
    """What the watcher's face model says of one frame, for this tracker only (nothing of it is posted): the head's
    yaw and pitch in degrees (0, 0 faces the camera), the mouth's centre (0-1, x as in a mirror) and the face's
    width (of the frame's width)."""

    yaw: float
    pitch: float
    mouth: tuple[float, float] = (0.5, 0.6)
    size: float = 0.2


def head_angles(matrix: Any) -> tuple[float, float]:
    """Yaw and pitch (degrees) from the face model's 4x4 facial transformation matrix: its rotation read as a turn
    about the vertical axis, then a nod about the horizontal one. Roll is left out. The signs do not matter here
    (facing takes their size, a nod or shake their swings)."""
    r = [[float(matrix[i][j]) for j in range(3)] for i in range(3)]
    yaw = math.degrees(math.atan2(r[0][2], r[2][2]))
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, -r[1][2]))))
    return yaw, pitch


# --- smoothing --------------------------------------------------------------------------


@dataclass
class OneEuro:
    """The One Euro filter (Casiez et al., 2012): a low-pass whose cutoff rises with speed, so a still hand stops
    trembling and a fast one does not lag much."""

    min_cutoff: float = 1.0
    beta: float = 3.0
    d_cutoff: float = 1.0
    x: float | None = None
    dx: float = 0.0
    t: float = 0.0

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, t: float, x: float) -> float:
        if self.x is None or t <= self.t:
            self.x, self.t = x, t
            return x
        dt = t - self.t
        dx = (x - self.x) / dt
        a_d = self._alpha(self.d_cutoff, dt)
        self.dx = a_d * dx + (1 - a_d) * self.dx
        cutoff = self.min_cutoff + self.beta * abs(self.dx)
        a = self._alpha(cutoff, dt)
        self.x = a * x + (1 - a) * self.x
        self.t = t
        return self.x


class LandmarkFilter:
    """One Euro on each coordinate of the 21 points (and of the world points). Forgotten when the hand goes."""

    def __init__(self) -> None:
        self.filters: list[OneEuro] = []
        self.world: list[OneEuro] = []

    def reset(self) -> None:
        self.filters, self.world = [], []

    def __call__(self, t: float, hand: Hand) -> Hand:
        flat = [c for p in hand.points for c in p]
        if len(self.filters) != len(flat):
            self.filters = [OneEuro() for _ in flat]
        smooth = [f(t, c) for f, c in zip(self.filters, flat)]
        points = tuple((smooth[i], smooth[i + 1]) for i in range(0, len(smooth), 2))
        world = hand.world
        if world is not None:
            flat_w = [c for p in world for c in p]
            if len(self.world) != len(flat_w):
                self.world = [OneEuro(min_cutoff=2.0, beta=40.0) for _ in flat_w]
            smooth_w = [f(t, c) for f, c in zip(self.world, flat_w)]
            world = tuple((smooth_w[i], smooth_w[i + 1], smooth_w[i + 2]) for i in range(0, len(smooth_w), 3))
        return Hand(points, hand.label, hand.score, world, hand.aspect)


# --- the geometry ----------------------------------------------------------------------


def _dist(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return math.sqrt(sum((p - q) ** 2 for p, q in zip(a, b)))


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(max(value, low), high)


def _bend(a: tuple[float, ...], b: tuple[float, ...], c: tuple[float, ...]) -> float:
    """How far the line a-b-c turns at b, in degrees: 0 straight on, 180 folded back."""
    u = [q - p for p, q in zip(a, b)]
    v = [q - p for p, q in zip(b, c)]
    nu, nv = math.sqrt(sum(x * x for x in u)), math.sqrt(sum(x * x for x in v))
    if nu < 1e-9 or nv < 1e-9:
        return 0.0
    cos = sum(x * y for x, y in zip(u, v)) / (nu * nv)
    return math.degrees(math.acos(_clip(cos, -1.0, 1.0)))


def _segment_distance(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    ab = (b[0] - a[0], b[1] - a[1])
    length = ab[0] ** 2 + ab[1] ** 2
    s = 0.0 if length < 1e-12 else _clip(((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1]) / length)
    return _dist(p, (a[0] + s * ab[0], a[1] + s * ab[1]))


def curls(hand: Hand) -> tuple[dict[str, float], float]:
    """Each digit's curl (0 straight … 1 curled) and the gap between the thumb's and index's tips (over the palm's
    length, at most 1): the numbers the shapes are read from, and what calibration records."""
    p3 = hand.world if hand.world is not None and len(hand.world) == 21 else tuple((x, y, 0.0) for x, y in hand.points)
    out: dict[str, float] = {}
    for name, (mcp, pip, dip, tip) in CHAINS.items():
        bend = _bend(p3[WRIST], p3[mcp], p3[pip]) + _bend(p3[mcp], p3[pip], p3[dip]) + _bend(p3[pip], p3[dip], p3[tip])
        out[name] = round(_clip(bend / CURL_DEGREES), 3)
    palm = max(_dist(p3[WRIST], p3[MIDDLE_MCP]), 1e-6)
    # The thumb folds across the palm rather than at its joints: how far its tip reaches from the little finger's
    # knuckle says it (about 1.1 palms stretched out, under 0.5 tucked in).
    reach = _dist(p3[THUMB_TIP], p3[PINKY_MCP]) / palm
    out = {"thumb": round(_clip((1.0 - reach) / 0.6), 3)} | out
    gap = round(_clip(_dist(p3[THUMB_TIP], p3[INDEX_TIP]) / palm), 3)
    return out, gap


@dataclass(frozen=True)
class Thresholds:
    """Where a digit counts as extended or curled, each with an enter and an exit value (hysteresis), and where the
    thumb and index tips count as pinched: from the user's calibration (`[gestures.calibration]`, the means the
    Input tab recorded for an open hand, a fist, a relaxed hand and a pinch), else from the defaults."""

    ext_in: dict[str, float]
    ext_out: dict[str, float]
    curl_in: dict[str, float]
    curl_out: dict[str, float]
    pinch_in: float
    pinch_out: float

    @classmethod
    def from_calibration(cls, calibration: dict[str, float] | None = None) -> "Thresholds":
        cal = dict(CALIBRATION_DEFAULTS) | dict(calibration or {})
        ext_in, ext_out, curl_in, curl_out = {}, {}, {}, {}
        for digit in DIGITS:
            low, high = cal[f"{digit}_open"], cal[f"{digit}_curled"]
            span = max(high - low, 0.15)
            ext_in[digit], ext_out[digit] = low + 0.30 * span, low + 0.42 * span
            curl_in[digit], curl_out[digit] = low + 0.65 * span, low + 0.53 * span
        closed, apart = cal["gap_closed"], cal["gap_apart"]
        span = max(apart - closed, 0.1)
        return cls(ext_in, ext_out, curl_in, curl_out, closed + 0.30 * span, closed + 0.50 * span)


@dataclass(frozen=True)
class Features:
    x: float            # the palm's centre
    y: float
    size: float         # the hand's extent, of the frame
    pinch: float        # 0 apart … 1 thumb and index tips together
    open: float         # the share of the four fingers extended
    wrist_y: float
    shape: str          # what the hand is now (the canonical name: a shape, or count_N); "" for none
    curl: dict[str, float] = field(default_factory=dict)
    gap: float = 1.0
    count: int = 0      # the digits held up (the thumb counts), 0 when the hand is not a clear count


class Shaper:
    """Reads a hand's shape with hysteresis: each digit extended, curled or neither (keeping its state until it is
    past the exit threshold), where a pointing finger points, and whether the hand is pinching. One per tracker;
    `features()` uses a fresh one (no memory)."""

    def __init__(self, thresholds: Thresholds | None = None) -> None:
        self.t = thresholds or Thresholds.from_calibration()
        self.states: dict[str, str] = {d: "" for d in DIGITS}
        self.direction = ""      # "up", "side" or "" for a pointing finger
        self.pinched = False

    def reset(self) -> None:
        self.states = {d: "" for d in DIGITS}
        self.direction, self.pinched = "", False

    def _digit(self, digit: str, curl: float) -> str:
        t, previous = self.t, self.states[digit]
        if previous == "ext" and curl <= t.ext_out[digit]:
            state = "ext"
        elif previous == "curl" and curl >= t.curl_out[digit]:
            state = "curl"
        else:
            state = "ext" if curl <= t.ext_in[digit] else "curl" if curl >= t.curl_in[digit] else ""
        self.states[digit] = state
        return state

    def _aim(self, elevation: float) -> str:
        if self.direction == "up" and elevation >= UP_OUT:
            return "up"
        if self.direction == "side" and abs(elevation) <= SIDE_OUT:
            return "side"
        return "up" if elevation >= UP_IN else "side" if abs(elevation) <= SIDE_IN else ""

    def read(self, hand: Hand, face: Face | None = None, min_score: float = 0.5) -> Features:
        p = hand.points
        xs, ys = [q[0] for q in p], [q[1] for q in p]
        centre = tuple(sum(p[i][k] for i in (WRIST, INDEX_MCP, MIDDLE_MCP, PINKY_MCP)) / 4 for k in (0, 1))
        palm2 = max(_dist(p[WRIST], p[MIDDLE_MCP]), 1e-6)
        pinch = _clip((0.7 - _dist(p[THUMB_TIP], p[INDEX_TIP]) / palm2) / 0.5)
        curl, gap = curls(hand)
        states = {d: self._digit(d, curl[d]) for d in DIGITS}
        # A pinch: the tips together with the index finger not curled in (in a fist the thumb lies on the index too).
        self.pinched = gap <= (self.t.pinch_out if self.pinched else self.t.pinch_in) and states["index"] != "curl"
        label = LABELS.get(hand.label, "") if hand.score >= min_score else ""
        # Where the index finger points (its knuckle to its tip, the frame's aspect put back) and how upright the
        # hand is (the wrist to the middle knuckle).
        dx = (p[INDEX_TIP][0] - p[INDEX_MCP][0]) * hand.aspect
        dy = p[INDEX_MCP][1] - p[INDEX_TIP][1]
        elevation = math.degrees(math.atan2(dy, abs(dx))) if abs(dx) + abs(dy) > 1e-9 else 0.0
        upright = math.degrees(math.atan2(p[WRIST][1] - p[MIDDLE_MCP][1],
                                          abs(p[MIDDLE_MCP][0] - p[WRIST][0]) * hand.aspect)) >= UPRIGHT
        others_curled = all(states[d] == "curl" for d in ("middle", "ring", "pinky"))
        pointing = states["index"] == "ext" and others_curled
        aiming = pointing or (label == "point_hold" and states["index"] == "ext")
        self.direction = self._aim(elevation) if aiming else ""
        extended = sum(states[d] == "ext" for d in DIGITS)
        count = 0
        if all(states.values()) and upright and extended and (extended != 1 or states["index"] == "ext") \
                and not (pointing and self.direction != "up"):
            count = extended
        shape = self._shape(hand, label, states, pointing, dx, face)
        opened = sum(states[d] == "ext" for d in FINGERS) / 4
        return Features(_clip(centre[0]), _clip(centre[1]), _clip(max(max(xs) - min(xs), max(ys) - min(ys))),
                        round(pinch, 3), opened, p[WRIST][1], shape, curl, gap, count)

    def _shape(self, hand: Hand, label: str, states: dict[str, str], pointing: bool, dx: float,
               face: Face | None) -> str:
        p = hand.points
        if pointing and self.direction == "up" and face is not None and face.size > 0:
            # shh: the finger upright across the lips. Its last two bones pass within SHH_NEAR face widths of the
            # mouth's centre (the face's width in the frame's height units).
            mouth = (face.mouth[0] * hand.aspect, face.mouth[1])
            near = min(_segment_distance(mouth, (p[a][0] * hand.aspect, p[a][1]), (p[b][0] * hand.aspect, p[b][1]))
                       for a, b in ((INDEX_PIP, INDEX_DIP), (INDEX_DIP, INDEX_TIP)))
            if near <= SHH_NEAR * face.size * hand.aspect:
                return "shh"
        if label in ("thumb_up", "thumb_down"):
            return label
        if self.pinched:
            return "pinch_hold"
        if pointing or (label == "point_hold" and states["index"] == "ext"
                        and not any(states[d] == "ext" for d in ("middle", "ring", "pinky"))):
            if self.direction == "up":
                return "point_hold"
            if self.direction == "side":
                return "point_right" if dx > 0 else "point_left"
            return ""            # a diagonal: the dead zone between up and sideways
        fingers = [states[d] for d in FINGERS]
        if all(s == "ext" for s in fingers) and states["thumb"] != "curl":
            return "palm_hold"
        if label == "palm_hold" and fingers.count("ext") >= 3 and "curl" not in fingers:
            return "palm_hold"
        if all(s == "curl" for s in fingers) and states["thumb"] != "ext":
            return "fist_hold"
        if label == "fist_hold" and fingers.count("curl") >= 3 and "ext" not in fingers:
            return "fist_hold"
        # The victory sign keeps the thumb in: with it out, the same two fingers are a count of three.
        two = states["index"] == "ext" and states["middle"] == "ext" and states["thumb"] != "ext"
        if two and states["ring"] == "curl" and states["pinky"] == "curl":
            return "victory_hold"
        if label == "victory_hold" and two and "ext" not in (states["ring"], states["pinky"]):
            return "victory_hold"
        return ""


def features(hand: Hand, min_score: float = 0.5, face: Face | None = None) -> Features:
    """The hand's numbers and shape, read with no memory (a fresh Shaper, the default thresholds)."""
    f = Shaper().read(hand, face, min_score)
    if not f.shape and f.count:
        return Features(f.x, f.y, f.size, f.pinch, f.open, f.wrist_y, f"count_{f.count}", f.curl, f.gap, f.count)
    return f


def hand_message(hand: Hand, f: Features, engaged: bool, facing: dict[str, bool] | None = None) -> dict[str, Any]:
    """What a body gets of a hand (PROTOCOL Part 1d): the palm, its size, pinch and openness, whether it is up, the
    shape seen now (absent for none), the wrist and the five fingertips, each digit's curl and the thumb-index gap
    (numbers only; the Input tab's calibration records them), and, while the face model runs, whether the user
    faces the screen and whether a face is there (two booleans: nothing else of the face leaves the watcher)."""
    message = {"present": True, "x": round(f.x, 3), "y": round(f.y, 3), "size": round(f.size, 3),
               "pinch": round(f.pinch, 3), "open": round(f.open, 3), "engaged": engaged,
               "points": {name: [round(_clip(hand.points[i][0]), 3), round(_clip(hand.points[i][1]), 3)]
                          for name, i in TIPS.items()},
               "curl": {d: round(f.curl.get(d, 0.0), 3) for d in DIGITS}, "gap": round(f.gap, 3)}
    if f.shape:
        message["shape"] = f.shape
    if facing is not None:
        message |= facing
    return message


def turns(values: list[float], threshold: float) -> int:
    """How many times a series turned back by at least `threshold` (a zigzag's pivots): 2 for there, back and there
    again."""
    if not values:
        return 0
    start = extreme = values[0]
    direction, count = 0, 0
    for v in values[1:]:
        if direction == 0:
            if abs(v - start) >= threshold:
                direction, extreme = (1 if v > start else -1), v
        elif direction == 1:
            if v > extreme:
                extreme = v
            elif extreme - v >= threshold:
                direction, extreme, count = -1, v, count + 1
        else:
            if v < extreme:
                extreme = v
            elif v - extreme >= threshold:
                direction, extreme, count = 1, v, count + 1
    return count


# --- the tracker --------------------------------------------------------------------------


@dataclass
class Settings:
    hold_s: float = 0.4
    zone: float = 0.8
    min_size: float = 0.12
    arming: bool = False
    armed_s: float = 8.0
    names: frozenset[str] = frozenset()   # the gestures the map uses: only these show a ring or fire
    swipe_dist: float = 0.22
    swipe_s: float = 0.5
    swipe_slant: float = 0.6              # at most this much up or down per unit across
    settle_s: float = 0.3                 # up and in the zone this long before a swipe may start
    cooldown_s: float = 0.8
    move_limit: float = 0.6               # palm speed (frames a second) above which a hold does not count
    hold_drift: float = 0.04              # a hold ends when the palm wanders this far (of the frame) from its start
    flicker_s: float = 0.12               # a different shape must last this long to replace the held one
    rearm_s: float = 0.3                  # a shape that fired must be left this long before it fires again
    lost_s: float = 0.3                   # a hand missing this long is gone
    min_score: float = 0.5
    thresholds: Thresholds = field(default_factory=Thresholds.from_calibration)
    # The face (the watcher's face model): `face` says it runs. With `require_facing` a gesture counts only while
    # the head is within the cone: in at |yaw| <= facing_yaw and |pitch| <= facing_pitch, out past the *_out ones.
    face: bool = False
    require_facing: bool = True
    facing_yaw: float = 20.0
    facing_yaw_out: float = 30.0
    facing_pitch: float = 25.0
    facing_pitch_out: float = 35.0
    present_s: float = 1.0                # a face seen within this long is there
    # The slider: a pinch held still this long engages it; moving the hand by slide_span of the frame's height
    # moves it from one end to the other.
    slide_hold_s: float = 0.3
    slide_span: float = 0.3
    # A wave: at least wave_turns turns of wave_amp (of the frame) within wave_s, an open palm.
    wave_amp: float = 0.05
    wave_s: float = 1.6
    wave_turns: int = 2
    # A nod or a shake: at least head_turns turns of nod_deg (pitch) or shake_deg (yaw) within head_s.
    nod_deg: float = 8.0
    shake_deg: float = 10.0
    head_s: float = 1.6
    head_turns: int = 2

    @classmethod
    def from_config(cls, gestures: Any, face: bool = False) -> "Settings":
        """From a config.GesturesConfig: the map's gesture names (targets stripped), the calibration's thresholds,
        whether facing is needed; `face` says whether the watcher's face model runs."""
        names = frozenset(key.rpartition(":")[2] for key in gestures.map)
        return cls(hold_s=gestures.hold_ms / 1000.0, zone=gestures.zone, min_size=gestures.min_size,
                   arming=gestures.arming, armed_s=gestures.armed_s, names=names,
                   thresholds=Thresholds.from_calibration(getattr(gestures, "calibration", None)),
                   face=face, require_facing=getattr(gestures, "require_facing", True))


@dataclass
class Tracker:
    settings: Settings = field(default_factory=Settings)
    approval: dict[str, bool] | None = None   # the brain's open question a thumb may answer ({"yes", "no"})
    armed_until: float = 0.0
    held: str = ""            # the shape being held (as the bodies are told it)
    held_shape: str = ""      # …and as the hand made it
    held_since: float = 0.0
    held_at: tuple[float, float] = (0.0, 0.0)   # where the palm was when the hold began
    palm: tuple[float, float] = (0.0, 0.0)      # where it is now
    held_told: bool = False
    told: float = 0.0         # the progress the bodies were last told (a cancel says where the ring stopped)
    candidate: str = ""       # a different shape on its way in (flicker_s)
    candidate_since: float = 0.0
    latched: str = ""         # the shape that fired: no more of it until it is left for rearm_s or the hand goes
    latch_free_since: float | None = None
    cooldown_until: float = 0.0
    fired_at: dict[str, float] = field(default_factory=dict)   # a gesture's last time, for COOLDOWNS
    seen: bool = False
    last_seen: float = -1e9
    engaged_since: float | None = None
    last_engaged: float = -1e9
    track: deque = field(default_factory=lambda: deque(maxlen=64))   # (t, x, y) of the palm while engaged
    waves: deque = field(default_factory=lambda: deque(maxlen=64))   # (t, x, y) of an open palm while engaged
    # The head (look): facing with hysteresis, when a face was last seen, its recent angles (nod, shake).
    facing: bool = False
    face_at: float = -1e9
    face: Face | None = None
    head: deque = field(default_factory=lambda: deque(maxlen=64))    # (t, yaw, pitch)
    # The slider (pinch_slide): engaged, where the hand was when it was, its last position, a release on its way.
    sliding: bool = False
    slide_y0: float = 0.0
    slide_at: float = 0.5
    slide_free_since: float | None = None
    shaper: Shaper | None = None
    smooth: LandmarkFilter = field(default_factory=LandmarkFilter)

    def __post_init__(self) -> None:
        if self.shaper is None:
            self.shaper = Shaper(self.settings.thresholds)

    def retune(self, settings: Settings) -> None:
        """New settings (the config changed): the shapes are read with its thresholds from now on."""
        self.settings = settings
        self.shaper = Shaper(settings.thresholds)

    def arm(self, until: float) -> None:
        self.armed_until = max(self.armed_until, until)

    def armed(self, t: float) -> bool:
        return not self.settings.arming or t < self.armed_until

    def active(self, t: float) -> bool:
        """A hand is up or was a moment ago, or a hold or slide is going: the watcher reads at its full rate."""
        return bool(self.held) or self.sliding or t - self.last_engaged < 2.0

    def raised(self, hand: Hand | None) -> bool:
        """Is this hand up in the zone and near enough (before smoothing; the watcher asks it to decide whether
        to run the face model on this frame)?"""
        if hand is None:
            return False
        xs, ys = [q[0] for q in hand.points], [q[1] for q in hand.points]
        size = max(max(xs) - min(xs), max(ys) - min(ys))
        return hand.points[WRIST][1] <= self.settings.zone and size >= self.settings.min_size

    def wants_face(self, t: float, hand: Hand | None) -> bool:
        """Should the face model look at this frame: only while a hand is up (or a slide goes on) or a question
        is open that a nod or a shake may answer."""
        return self.settings.face and (self.approval is not None or self.sliding or self.raised(hand)
                                       or t - self.last_engaged < 0.5)

    def present(self, t: float) -> bool:
        return t - self.face_at <= self.settings.present_s

    def facing_now(self, t: float) -> bool:
        """May a gesture count now: always without the face model (or with require_facing off); with it, only
        while the user faces the screen and a face was seen lately."""
        s = self.settings
        if not s.face or not s.require_facing:
            return True
        return self.facing and self.present(t)

    def facing_report(self, t: float) -> dict[str, bool] | None:
        if not self.settings.face:
            return None
        return {"facing": bool(self.facing and self.present(t)), "face": self.present(t)}

    def allowed(self, shape: str, t: float) -> str:
        """The name a held `shape` goes out as now, or "" when it may not count: a thumb answering her question,
        the arming palm, or a mapped gesture while command mode is on and its own cooldown is over."""
        if not shape:
            return ""
        if t - self.fired_at.get(shape, -1e9) < COOLDOWNS.get(shape, 0.0):
            return ""
        if self.approval is not None and shape in ("thumb_up", "thumb_down"):
            return shape if self.approval.get("yes" if shape == "thumb_up" else "no") else ""
        if not self.armed(t):
            return "arm" if shape in ("palm_hold", "count_5") else ""
        return shape if shape in self.settings.names else ""

    def named(self, f: Features) -> str:
        """The hand's shape by the name the map uses: a shape and its partner (point_hold and count_1, victory_hold
        and count_2, palm_hold and count_5, pinch_hold and pinch_slide) are the same hand, and a map uses one of
        each pair (gestures.check_map)."""
        names = self.settings.names
        if f.shape and not f.shape.startswith("count_"):
            partner = PARTNERS.get(f.shape)
            return partner if partner in names else f.shape
        return f"count_{f.count}" if f.count else ""

    # --- the head ---------------------------------------------------------------------

    def look(self, t: float, face: Face | None) -> list[tuple[str, dict[str, Any]]]:
        """One read of the face model (None: it found no face). Keeps `facing` with hysteresis and, while a
        question is open, reads a nod or a shake from the head's recent angles."""
        out: list[tuple[str, dict[str, Any]]] = []
        if face is None:
            if not self.present(t):
                self.facing = False
                self.head.clear()
            return out
        s = self.settings
        self.face, self.face_at = face, t
        yaw, pitch = abs(face.yaw), abs(face.pitch)
        if self.facing:
            self.facing = yaw <= s.facing_yaw_out and pitch <= s.facing_pitch_out
        else:
            self.facing = yaw <= s.facing_yaw and pitch <= s.facing_pitch
        self.head.append((t, face.yaw, face.pitch))
        while self.head and t - self.head[0][0] > s.head_s:
            self.head.popleft()
        motion = self._nod_or_shake(t)
        if motion:
            out.append(("gesture", {"name": motion, "phase": "done", "progress": 1.0}))
            self.fired_at[motion] = t
            self.head.clear()
        return out

    def _nod_or_shake(self, t: float) -> str:
        """A nod (the pitch swinging back and forth) or a shake (the yaw), only while a question is open that it
        may answer, the swings mostly on the one axis, the head about facing the screen on average."""
        s = self.settings
        if self.approval is None or len(self.head) < 4:
            return ""
        yaws, pitches = [h[1] for h in self.head], [h[2] for h in self.head]
        yaw_range, pitch_range = max(yaws) - min(yaws), max(pitches) - min(pitches)
        if abs(sum(yaws) / len(yaws)) > s.facing_yaw_out or abs(sum(pitches) / len(pitches)) > s.facing_pitch_out:
            return ""
        nod = self.approval.get("yes") and t - self.fired_at.get("nod", -1e9) >= COOLDOWNS["nod"] \
            and yaw_range <= 0.6 * pitch_range and turns(pitches, s.nod_deg) >= s.head_turns
        if nod:
            return "nod"
        shake = self.approval.get("no") and t - self.fired_at.get("shake", -1e9) >= COOLDOWNS["shake"] \
            and pitch_range <= 0.6 * yaw_range and turns(yaws, s.shake_deg) >= s.head_turns
        return "shake" if shake else ""

    # --- the hand ---------------------------------------------------------------------

    def step(self, t: float, hand: Hand | None) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        if hand is None:
            if self.seen and t - self.last_seen > self.settings.lost_s:
                self.seen = False
                self._drop(t, out)
                self.smooth.reset()
                self.shaper.reset()
                out.append(("hand", {"present": False}))
            return out
        s = self.settings
        # The palm's place from the raw points (a move is not slowed down by smoothing: swipes, waves, stillness,
        # the slider); the shape and the hand the bodies draw from the smoothed ones.
        raw = tuple(sum(hand.points[i][k] for i in (WRIST, INDEX_MCP, MIDDLE_MCP, PINKY_MCP)) / 4 for k in (0, 1))
        hand = self.smooth(t, hand)
        face = self.face if self.face is not None and self.present(t) else None
        f = self.shaper.read(hand, face, s.min_score)
        if not f.shape and f.count:
            f = Features(f.x, f.y, f.size, f.pinch, f.open, f.wrist_y, f"count_{f.count}", f.curl, f.gap, f.count)
        engaged = f.wrist_y <= s.zone and f.size >= s.min_size
        self.seen, self.last_seen = True, t
        self.palm = (_clip(raw[0]), _clip(raw[1]))
        out.append(("hand", hand_message(hand, f, engaged, self.facing_report(t))))
        if self.sliding:
            self._slide(t, hand, engaged, out)
            return out
        if not engaged:
            self._drop(t, out)
            return out
        if self.engaged_since is None:
            self.engaged_since = t
        self.last_engaged = t
        self.track.append((t, *self.palm))
        while self.track and t - self.track[0][0] > s.swipe_s:
            self.track.popleft()
        if f.open >= 0.75:
            self.waves.append((t, *self.palm))
        else:
            self.waves.clear()
        while self.waves and t - self.waves[0][0] > s.wave_s:
            self.waves.popleft()
        facing = self.facing_now(t)
        motion = self._swipe(t) or self._wave(t) if facing else ""
        if motion:
            self._cancel(t, out)
            out.append(("gesture", {"name": motion, "phase": "done", "progress": 1.0}))
            self._fired(t, motion, self.named(f))
            self.track.clear()
            self.waves.clear()
            return out
        if not self._near_start():
            # The palm wandered off from where the hold began (a slow move, a wave turning at its ends): the hold
            # is over at once, with no grace for a blinking label.
            self._cancel(t, out)
            self.candidate = ""
        name = self.named(f) if facing and self._still(t) else ""
        if name == "pinch_slide" or self.held_shape == "pinch_slide":
            self._engage(t, hand, name, out)
            return out
        self._hold(t, name, out)
        return out

    # --- the parts ------------------------------------------------------------------

    def _drop(self, t: float, out: list) -> None:
        """The hand left the zone or the frame: a hold is cancelled (a slide ends), a swipe forgotten, the latch
        let go."""
        self._end_slide(t, out)
        self._cancel(t, out)
        self.track.clear()
        self.waves.clear()
        self.engaged_since = None
        self.latched, self.latch_free_since = "", None
        self.candidate = ""

    def _cancel(self, t: float, out: list) -> None:
        if self.held and self.held_told:
            out.append(("gesture", {"name": self.held, "phase": "cancelled", "progress": self.told}))
        self.held, self.held_shape, self.held_told = "", "", False

    def _progress(self, t: float) -> float:
        return round(min(max((t - self.held_since) / self.settings.hold_s, 0.0), 1.0), 3)

    def _still(self, t: float) -> bool:
        recent = [p for p in self.track if t - p[0] <= 0.2]
        if len(recent) < 2 and len(self.track) >= 2 and t - self.track[-2][0] <= 0.5:
            recent = list(self.track)[-2:]       # a slow frame rate: the last two looks
        if len(recent) < 2 or recent[-1][0] - recent[0][0] <= 0:
            return False         # one look is not enough to call a hand still: it may be passing by
        speed = math.hypot(recent[-1][1] - recent[0][1], recent[-1][2] - recent[0][2]) / (recent[-1][0] - recent[0][0])
        return speed <= self.settings.move_limit

    def _near_start(self) -> bool:
        """A hold under way stays near where it began: a hand moving slowly, or turning back at the ends of a wave,
        is still at moments, but it does not stay put."""
        if not self.held_shape:
            return True
        return math.hypot(self.palm[0] - self.held_at[0], self.palm[1] - self.held_at[1]) <= self.settings.hold_drift

    def _swipe(self, t: float) -> str:
        s = self.settings
        if t < self.cooldown_until or not self.track or self.engaged_since is None:
            return ""
        if not self.armed(t) or not any(name in s.names for name in SWIPES):
            return ""
        _, x, y = self.track[-1]
        best = None
        for start_t, sx, sy in self.track:
            if start_t - self.engaged_since < s.settle_s:
                continue    # the hand had not been up long enough before this point: it may be passing by
            dx, dy = x - sx, y - sy
            if abs(dx) >= s.swipe_dist and abs(dy) <= s.swipe_slant * abs(dx) and (best is None or abs(dx) > abs(best)):
                best = dx
        if best is None:
            return ""
        name = "swipe_right" if best > 0 else "swipe_left"
        return name if name in s.names else ""

    def _wave(self, t: float) -> str:
        """An open palm going side to side: wave_turns turns of at least wave_amp across within wave_s, mostly
        sideways. Too small a wave (a trembling hand, a hand talking along) is none."""
        s = self.settings
        if "wave" not in s.names or t < self.cooldown_until or not self.armed(t) or len(self.waves) < 4:
            return ""
        if t - self.fired_at.get("wave", -1e9) < COOLDOWNS["wave"]:
            return ""
        xs, ys = [w[1] for w in self.waves], [w[2] for w in self.waves]
        if max(ys) - min(ys) > 0.6 * (max(xs) - min(xs)):
            return ""
        return "wave" if turns(xs, s.wave_amp) >= s.wave_turns else ""

    def _engage(self, t: float, hand: Hand, name: str, out: list) -> None:
        """A pinch held still for slide_hold_s becomes the slider: `started` at 0.5, then the hand's height moves
        it. Before that nothing is shown (a ring would be the slider's level)."""
        s = self.settings
        if self._latch_holds(t, name):
            return
        if name != "pinch_slide":
            # The pinch changed, moved or opened: as for a hold, it must stay so for flicker_s to end the engaging.
            key = name or "-"
            if self.candidate != key:
                self.candidate, self.candidate_since = key, t
            if t - self.candidate_since < s.flicker_s:
                return
            self.held, self.held_shape, self.candidate = "", "", ""
            self._hold(t, name, out)
            return
        self.candidate = ""
        if self.held_shape != name:
            self._cancel(t, out)
            self.held_shape, self.held_since, self.held_told, self.held_at = name, t, False, self.palm
            self.held = self.allowed(name, t)
            return
        if not self.held or t < self.cooldown_until:
            if t < self.cooldown_until:
                self.held_since = t
            return
        if t - self.held_since < s.slide_hold_s:
            return
        self.held, self.held_shape = "", ""
        self.sliding, self.slide_y0, self.slide_at, self.slide_free_since = True, self.palm[1], 0.5, None
        out.append(("gesture", {"name": name, "phase": "started", "progress": 0.5}))

    def _slide(self, t: float, hand: Hand, engaged: bool, out: list) -> None:
        """While sliding: up is more. Letting the pinch go (for flicker_s), lowering the hand or losing it ends
        it, with its last position (`done`: the brain sets that level)."""
        s = self.settings
        if engaged:
            self.last_engaged = t
        if self.shaper.pinched and engaged:
            self.slide_free_since = None
            # The palm's height, not the fingertips': opening the pinch must not move the slider.
            position = round(_clip(0.5 + (self.slide_y0 - self.palm[1]) / (2 * s.slide_span)), 3)
            if abs(position - self.slide_at) >= 0.004:
                self.slide_at = position
                out.append(("gesture", {"name": "pinch_slide", "phase": "progress", "progress": position}))
            return
        if engaged:
            self.slide_free_since = self.slide_free_since if self.slide_free_since is not None else t
            if t - self.slide_free_since < s.flicker_s:
                return
        self._end_slide(t, out)
        if not engaged:
            self._drop(t, out)

    def _end_slide(self, t: float, out: list) -> None:
        if not self.sliding:
            return
        self.sliding = False
        out.append(("gesture", {"name": "pinch_slide", "phase": "done", "progress": self.slide_at}))
        self._fired(t, "pinch_slide", "pinch_slide")

    def _hold(self, t: float, shape: str, out: list) -> None:
        s = self.settings
        if self._latch_holds(t, shape):
            return
        if self.held_shape and shape != self.held_shape:
            # Another shape (or none, or a moving hand) must last flicker_s to end the held one: the recogniser
            # blinks for a frame now and then.
            key = shape or "-"
            if self.candidate != key:
                self.candidate, self.candidate_since = key, t
            if t - self.candidate_since < s.flicker_s:
                shape = self.held_shape
        else:
            self.candidate = ""
        if shape != self.held_shape:
            self._cancel(t, out)
            self.candidate = ""
            self.held_shape, self.held_since, self.held_at = shape, t, self.palm
            self.held = self.allowed(shape, t)
            self.held_told = False
        if not self.held:
            return
        if t < self.cooldown_until:
            self.held_since = t      # the hold starts counting once the cooldown is over
            return
        progress = self._progress(t)
        if progress >= 1.0:
            if not self.held_told:
                out.append(("gesture", {"name": self.held, "phase": "started", "progress": 0.0}))
            out.append(("gesture", {"name": self.held, "phase": "done", "progress": 1.0}))
            name = self.held
            self.held, self.held_told = "", False
            if name == "arm":
                self.arm(t + s.armed_s)
            self._fired(t, name, self.held_shape)
            self.held_shape = ""
            return
        out.append(("gesture", {"name": self.held, "phase": "progress" if self.held_told else "started",
                                "progress": progress}))
        self.held_told, self.told = True, progress

    def _latch_holds(self, t: float, shape: str) -> bool:
        """The re-arm: True while the shape that fired is still made, or was left less than rearm_s ago."""
        if not self.latched:
            return False
        if shape == self.latched:
            self.latch_free_since = None
            return True
        self.latch_free_since = self.latch_free_since if self.latch_free_since is not None else t
        if t - self.latch_free_since < self.settings.rearm_s:
            return True
        self.latched, self.latch_free_since = "", None
        return False

    def _fired(self, t: float, name: str, shape: str) -> None:
        self.cooldown_until = t + self.settings.cooldown_s
        self.fired_at[name] = t
        self.latched, self.latch_free_since = shape or "", None
        if self.settings.arming and t < self.armed_until:
            self.armed_until = t + self.settings.armed_s   # a gesture keeps command mode on
