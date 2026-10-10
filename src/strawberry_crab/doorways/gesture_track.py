"""The gesture doorway's reading of a hand over time (WIRING.md §25): numbers in, names out.

    Hand (21 landmarks as in a mirror, the recogniser's label)  ->  Tracker.step(t, hand)  ->  [("gesture" | "hand", {...})]

No camera and no model here, so the tests feed it synthetic hands. What it guards against: the webcam sees
the user all day, and a hand that only passes by, scratches an ear or reaches for a mug must do nothing.

1. Engagement: a hand counts only when it is raised into the zone (its wrist above `zone`, 0 at the top of the
   frame) and near enough (`min_size` of the frame). Anything else cancels a hold and clears a swipe.
2. Hold to confirm: a shape (thumbs up, open palm, …) must be held still for `hold_s`; it goes out as `started`,
   `progress` and `done` (the bodies fill a ring), and a moved, changed or dropped hand cancels it. A shape that
   fired must change, or the hand leave, before it can fire again, and nothing fires within `cooldown_s` of the
   last.
3. Arming (`arming`): command mode first, by a held open palm (`arm`), for `armed_s`; each gesture keeps it on.
   A thumb answering the open approval needs no arming.

A swipe is the palm moving across at least `swipe_dist` of the frame within `swipe_s`, mostly sideways, by a hand
that had been up for `settle_s` before it started: a hand that wanders through is not a swipe. x is as in a
mirror (the user's right is +x), so swipe_right is a move to the user's right.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# MediaPipe's 21 hand landmarks: the ones read here.
WRIST, THUMB_TIP = 0, 4
INDEX_MCP, INDEX_PIP, INDEX_TIP = 5, 6, 8
MIDDLE_MCP, MIDDLE_PIP, MIDDLE_TIP = 9, 10, 12
RING_PIP, RING_TIP = 14, 16
PINKY_MCP, PINKY_PIP, PINKY_TIP = 17, 18, 20
TIPS = {"wrist": WRIST, "thumb": THUMB_TIP, "index": INDEX_TIP, "middle": MIDDLE_TIP, "ring": RING_TIP,
        "pinky": PINKY_TIP}
FINGERS = ((INDEX_PIP, INDEX_TIP), (MIDDLE_PIP, MIDDLE_TIP), (RING_PIP, RING_TIP), (PINKY_PIP, PINKY_TIP))

# The recogniser's categories (MediaPipe's canned gesture classifier) -> our shapes. "None" and "ILoveYou" are none.
LABELS = {"Thumb_Up": "thumb_up", "Thumb_Down": "thumb_down", "Open_Palm": "palm_hold", "Closed_Fist": "fist_hold",
          "Pointing_Up": "point_hold", "Victory": "victory_hold"}
SWIPES = ("swipe_left", "swipe_right")
PINCHED = 0.85        # pinch strength from which the hand is a pinch (a geometry shape; the recogniser has none)


@dataclass(frozen=True)
class Hand:
    """One hand in one frame: 21 (x, y) points in 0-1, x as in a mirror and y down, and what the recogniser called
    it (its category name and score; "" when there is none)."""

    points: tuple[tuple[float, float], ...]
    label: str = ""
    score: float = 1.0


@dataclass(frozen=True)
class Features:
    x: float            # the palm's centre
    y: float
    size: float         # the hand's extent, of the frame
    pinch: float        # 0 apart … 1 thumb and index tips together
    open: float         # the share of the four fingers stretched
    wrist_y: float
    shape: str          # LABELS' names or pinch_hold; "" for none


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _clip(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def features(hand: Hand, min_score: float = 0.5) -> Features:
    p = hand.points
    xs, ys = [q[0] for q in p], [q[1] for q in p]
    centre = tuple(sum(p[i][k] for i in (WRIST, INDEX_MCP, MIDDLE_MCP, PINKY_MCP)) / 4 for k in (0, 1))
    palm = max(_dist(p[WRIST], p[MIDDLE_MCP]), 1e-6)
    pinch = _clip((0.7 - _dist(p[THUMB_TIP], p[INDEX_TIP]) / palm) / 0.5)
    stretched = sum(_dist(p[WRIST], p[tip]) > 1.1 * _dist(p[WRIST], p[pip]) for pip, tip in FINGERS)
    shape = LABELS.get(hand.label, "") if hand.score >= min_score else ""
    if pinch >= PINCHED and shape not in ("thumb_up", "thumb_down"):
        shape = "pinch_hold"
    return Features(_clip(centre[0]), _clip(centre[1]), _clip(max(max(xs) - min(xs), max(ys) - min(ys))),
                    round(pinch, 3), stretched / 4, p[WRIST][1], shape)


def hand_message(hand: Hand, f: Features, engaged: bool) -> dict[str, Any]:
    """What a body gets of a hand (PROTOCOL Part 1c): the palm, its size, pinch and openness, whether it is up, the
    wrist and the five fingertips. Numbers and fixed names only."""
    return {"present": True, "x": round(f.x, 3), "y": round(f.y, 3), "size": round(f.size, 3),
            "pinch": round(f.pinch, 3), "open": round(f.open, 3), "engaged": engaged,
            "points": {name: [round(_clip(hand.points[i][0]), 3), round(_clip(hand.points[i][1]), 3)]
                       for name, i in TIPS.items()}}


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
    flicker_s: float = 0.12               # a different shape must last this long to replace the held one
    lost_s: float = 0.3                   # a hand missing this long is gone
    min_score: float = 0.5

    @classmethod
    def from_config(cls, gestures: Any) -> "Settings":
        """From a config.GesturesConfig: the map's gesture names, targets stripped."""
        names = frozenset(key.rpartition(":")[2] for key in gestures.map)
        return cls(hold_s=gestures.hold_ms / 1000.0, zone=gestures.zone, min_size=gestures.min_size,
                   arming=gestures.arming, armed_s=gestures.armed_s, names=names)


@dataclass
class Tracker:
    settings: Settings = field(default_factory=Settings)
    approval: dict[str, bool] | None = None   # the brain's open question a thumb may answer ({"yes", "no"})
    armed_until: float = 0.0
    held: str = ""            # the shape being held (as the bodies are told it)
    held_shape: str = ""      # …and as the hand made it
    held_since: float = 0.0
    held_told: bool = False
    told: float = 0.0         # the progress the bodies were last told (a cancel says where the ring stopped)
    candidate: str = ""       # a different shape on its way in (flicker_s)
    candidate_since: float = 0.0
    latched: str = ""         # the shape that fired: no more of it until it changes or the hand goes
    latch_free_since: float | None = None
    cooldown_until: float = 0.0
    seen: bool = False
    last_seen: float = -1e9
    engaged_since: float | None = None
    last_engaged: float = -1e9
    track: deque = field(default_factory=lambda: deque(maxlen=64))   # (t, x, y) of the palm while engaged

    def arm(self, until: float) -> None:
        self.armed_until = max(self.armed_until, until)

    def armed(self, t: float) -> bool:
        return not self.settings.arming or t < self.armed_until

    def active(self, t: float) -> bool:
        """A hand is up or was a moment ago, or a hold is going: the watcher reads at its full rate."""
        return bool(self.held) or t - self.last_engaged < 2.0

    def allowed(self, shape: str, t: float) -> str:
        """The name a held `shape` goes out as now, or "" when it may not count: a thumb answering her question,
        the arming palm, or a mapped gesture while command mode is on."""
        if not shape:
            return ""
        if self.approval is not None and shape in ("thumb_up", "thumb_down"):
            return shape if self.approval.get("yes" if shape == "thumb_up" else "no") else ""
        if not self.armed(t):
            return "arm" if shape == "palm_hold" else ""
        return shape if shape in self.settings.names else ""

    def step(self, t: float, hand: Hand | None) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        if hand is None:
            if self.seen and t - self.last_seen > self.settings.lost_s:
                self.seen = False
                self._drop(t, out)
                out.append(("hand", {"present": False}))
            return out
        s = self.settings
        f = features(hand, s.min_score)
        engaged = f.wrist_y <= s.zone and f.size >= s.min_size
        self.seen, self.last_seen = True, t
        out.append(("hand", hand_message(hand, f, engaged)))
        if not engaged:
            self._drop(t, out)
            return out
        if self.engaged_since is None:
            self.engaged_since = t
        self.last_engaged = t
        self.track.append((t, f.x, f.y))
        while self.track and t - self.track[0][0] > s.swipe_s:
            self.track.popleft()
        swipe = self._swipe(t)
        if swipe:
            self._cancel(t, out)
            out.append(("gesture", {"name": swipe, "phase": "done", "progress": 1.0}))
            self._fired(t, f.shape)
            self.track.clear()
            return out
        self._hold(t, f.shape if self._still(t) else "", out)
        return out

    # --- the parts ------------------------------------------------------------------

    def _drop(self, t: float, out: list) -> None:
        """The hand left the zone or the frame: a hold is cancelled, a swipe forgotten, the latch let go."""
        self._cancel(t, out)
        self.track.clear()
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

    def _hold(self, t: float, shape: str, out: list) -> None:
        s = self.settings
        # The latch: the shape that fired does nothing more until another shape (or none) has lasted a moment.
        if self.latched:
            if shape == self.latched:
                self.latch_free_since = None
                return
            self.latch_free_since = self.latch_free_since if self.latch_free_since is not None else t
            if t - self.latch_free_since < s.flicker_s:
                return
            self.latched, self.latch_free_since = "", None
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
            self.held_shape, self.held_since = shape, t
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
            self._fired(t, self.held_shape)
            self.held_shape = ""
            return
        out.append(("gesture", {"name": self.held, "phase": "progress" if self.held_told else "started",
                                "progress": progress}))
        self.held_told, self.told = True, progress

    def _fired(self, t: float, shape: str) -> None:
        self.cooldown_until = t + self.settings.cooldown_s
        self.latched, self.latch_free_since = shape or "", None
        if self.settings.arming and t < self.armed_until:
            self.armed_until = t + self.settings.armed_s   # a gesture keeps command mode on
