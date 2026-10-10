"""Gestures v2 (doorways/gesture_track.py, gestures.py, doorways/gesture_watch.py; WIRING.md §26, PROTOCOL Part 1d):
facing the screen (the face model), steadier shapes (smoothing, hysteresis, re-arm, cooldowns), calibration, and
the new gestures: pointing left and right, the finger counts, pinch-and-slide, a wave, shh, and a nod or a shake on
her card.

No test opens a camera (conftest.py refuses it): the tracker gets synthetic hands (tests/test_gestures.py's `hand`)
and synthetic faces (yaw, pitch, the mouth), the watcher a fake camera, a fake recogniser and a fake face reader.
The near misses are here too: a diagonal finger, a half-curled hand, hands talking along while the user looks
away, a pinch that drifts, a wave too small, a hand at the mouth that is no shh, a head that only looks around.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
from pathlib import Path
from typing import Any

import pytest

from strawberry_crab import gestures, inputs
from strawberry_crab.actions import Actor, Outcome
from strawberry_crab.config import Config, ConfigError, GesturesConfig, load
from strawberry_crab.confirm import Held
from strawberry_crab.daemon import Daemon
from strawberry_crab.doorways import gesture_watch
from strawberry_crab.doorways.gesture_track import (Face, OneEuro, Settings, Shaper, Thresholds, Tracker, features,
                                                    head_angles, turns)
from strawberry_crab.events import CannedReactor
from strawberry_crab.server import create_app
from strawberry_crab.tools import Toolbox
from tests.bus import add_trusted
from tests.test_gestures import (DT, FPS, HELLO_V2, FakeMpris, assert_plain, body, done, gesture_daemon, gone, hand,
                                 held, run, typed, watcher_for, spin, write)
from tests.test_voice import Sink

FACING = Face(yaw=2.0, pitch=8.0, mouth=(0.5, 0.25), size=0.2)
AWAY = Face(yaw=48.0, pitch=4.0, mouth=(0.6, 0.25), size=0.18)
POINT = {"label": "Pointing_Up"}
FOUR = {"curl": {}, "thumb": 1.0}


def watched(tracker: Tracker, frames: list, face: Any = FACING) -> list[dict]:
    """The gesture events for these frames, the face model reading `face` (a Face, a function of the time, or None
    for no face found) before each hand."""
    out = []
    for t, h in frames:
        f = face(t) if callable(face) else face
        out += [p for kind, p in tracker.look(t, f) if kind == "gesture"]
        out += [p for kind, p in tracker.step(t, h) if kind == "gesture"]
    return out


def facing_tracker(*names: str, **settings: Any) -> Tracker:
    return Tracker(Settings(names=frozenset(names), face=True, **settings))


# --- pointing to either side --------------------------------------------------------------------------


def test_pointing_to_the_users_left_and_right_and_up():
    names = ("point_left", "point_right", "point_hold")
    # turn=90 points the finger to the user's right (x is as in a mirror: +x is the user's right).
    assert done(run(Tracker(Settings(names=frozenset(names))), held("Pointing_Up", 0.6, turn=90))) == ["point_right"]
    assert done(run(Tracker(Settings(names=frozenset(names))), held("Pointing_Up", 0.6, turn=-90))) == ["point_left"]
    assert done(run(Tracker(Settings(names=frozenset(names))), held("Pointing_Up", 0.6))) == ["point_hold"]
    # Sideways counts even when the recogniser says nothing of it.
    assert done(run(Tracker(Settings(names=frozenset(names))), held("None", 0.6, curl=POINT_CURL, thumb=1.0,
                                                                     turn=90))) == ["point_right"]


POINT_CURL = {"middle": 1.0, "ring": 1.0, "pinky": 1.0}


def test_a_diagonal_finger_is_neither_up_nor_sideways_and_the_angle_has_hysteresis():
    names = frozenset(("point_left", "point_right", "point_hold"))
    diagonal = held("Pointing_Up", 1.0, turn=45)
    assert run(Tracker(Settings(names=names)), diagonal) == []
    assert features(hand(label="Pointing_Up", turn=45)).shape == ""                      # the dead zone
    assert features(hand(label="Pointing_Up", turn=62)).shape == ""                      # ~30° above level: fresh, no
    shaper = Shaper()
    assert shaper.read(hand(label="Pointing_Up", turn=90)).shape == "point_right"
    assert shaper.read(hand(label="Pointing_Up", turn=62)).shape == "point_right"        # kept: under the exit angle
    assert shaper.read(hand(label="Pointing_Up", turn=45)).shape == ""                   # past it


# --- the finger counts -----------------------------------------------------------------------------------


def test_finger_counts_held_and_the_pairs_go_by_the_name_the_map_uses():
    three = {"curl": {"pinky": 1.0}, "thumb": 1.0}
    tracker = Tracker(Settings(names=frozenset({"count_3", "count_4"})))
    assert done(run(tracker, held("None", 0.6, **three))) == ["count_3"]
    tracker = Tracker(Settings(names=frozenset({"count_3", "count_4"})))
    assert done(run(tracker, held("None", 0.6, **FOUR))) == ["count_4"]
    # An open palm is count_5 when the map uses count_5, palm_hold when it uses that; index up likewise count_1.
    assert done(run(Tracker(Settings(names=frozenset({"count_5"}))), held("Open_Palm", 0.6))) == ["count_5"]
    assert done(run(Tracker(Settings(names=frozenset({"palm_hold"}))), held("Open_Palm", 0.6))) == ["palm_hold"]
    assert done(run(Tracker(Settings(names=frozenset({"count_1"}))), held("Pointing_Up", 0.6))) == ["count_1"]
    assert done(run(Tracker(Settings(names=frozenset({"count_2"}))), held("Victory", 0.6))) == ["count_2"]
    # The thumb counts: thumb, index and middle up is three.
    assert features(hand(curl={"ring": 1.0, "pinky": 1.0}, thumb=0.0)).shape == "count_3"
    with pytest.raises(gestures.GestureError, match="same hand"):
        gestures.check_map({"palm_hold": "pause", "count_5": "skip"})
    with pytest.raises(gestures.GestureError, match="same hand"):
        gestures.check_map({"point_hold": "listen", "music:count_1": "skip"})


def test_a_half_curled_hand_is_no_shape_and_no_count():
    names = frozenset({"count_1", "count_2", "count_3", "count_4", "count_5", "fist_hold", "point_left",
                       "point_right", "pinch_hold"})
    half = {"curl": {"index": 0.45, "middle": 0.45, "ring": 0.45, "pinky": 0.45}, "thumb": 0.5}
    assert run(Tracker(Settings(names=names)), held("None", 1.2, **half)) == []
    f = features(hand(**half))
    assert f.shape == "" and f.count == 0
    # A tilted hand is no count either: counts are held upright.
    assert features(hand(curl={"pinky": 1.0}, thumb=1.0, turn=50)).count == 0


# --- pinch and slide ---------------------------------------------------------------------------------------


def slide_frames(start: float, still_s: float, up: float, move_s: float, released_s: float) -> list:
    """A pinch held still, then moved up by `up` of the frame, then let go."""
    frames, t = [], start
    for _ in range(int(still_s * FPS)):
        frames.append((t, hand(pinch=True, label="None")))
        t += DT
    steps = max(int(move_s * FPS), 1)
    for i in range(1, steps + 1):
        frames.append((t, hand(pinch=True, label="None", y=0.45 - up * i / steps)))
        t += DT
    for _ in range(int(0.3 * FPS)):
        frames.append((t, hand(pinch=True, label="None", y=0.45 - up)))
        t += DT
    for _ in range(int(released_s * FPS)):
        frames.append((t, hand(label="None", y=0.45 - up)))
        t += DT
    return frames


def test_a_held_pinch_becomes_a_slider_moved_by_the_hand_and_let_go():
    tracker = Tracker(Settings(names=frozenset({"pinch_slide"})))
    events = run(tracker, slide_frames(0.0, 0.5, 0.15, 0.5, 0.4))
    assert [e["name"] for e in events] == ["pinch_slide"] * len(events)
    phases = [e["phase"] for e in events]
    assert phases[0] == "started" and events[0]["progress"] == 0.5 and phases[-1] == "done"
    assert phases.count("started") == 1 and phases.count("done") == 1 and "cancelled" not in phases
    progress = [e["progress"] for e in events if e["phase"] == "progress"]
    assert progress == sorted(progress) and progress[-1] > 0.7                  # up is more: 0.15 of 0.3 is +0.25
    assert events[-1]["progress"] == progress[-1]                               # let go: its last position
    # Before slide_hold_s (0.3 s) nothing is shown: no ring for the slider.
    early = run(Tracker(Settings(names=frozenset({"pinch_slide"}))), [(i * DT, hand(pinch=True)) for i in range(4)])
    assert early == []


def test_a_pinch_that_drifts_does_not_engage_and_lowering_the_hand_ends_a_slide():
    tracker = Tracker(Settings(names=frozenset({"pinch_slide"})))
    drifting = [(i * DT, hand(pinch=True, y=0.3 + 0.05 * (i % 12))) for i in range(30)]
    assert run(tracker, drifting) == []
    tracker = Tracker(Settings(names=frozenset({"pinch_slide"})))
    frames = [(i * DT, hand(pinch=True)) for i in range(8)] + [(8 * DT + i * DT, hand(pinch=True, y=0.9))
                                                                for i in range(3)]
    events = run(tracker, frames)
    assert [e["phase"] for e in events][0] == "started" and events[-1]["phase"] == "done"
    # Mapped as pinch_hold instead, a pinch is the plain held shape it always was.
    assert done(run(Tracker(Settings(names=frozenset({"pinch_hold"}))), [(i * DT, hand(pinch=True))
                                                                         for i in range(10)])) == ["pinch_hold"]


# --- wave --------------------------------------------------------------------------------------------------------


def wave_frames(start: float, amplitude: float, seconds: float = 1.4, period: float = 0.5) -> list:
    return [(start + i * DT, hand(label="Open_Palm", x=0.5 + amplitude * math.sin(2 * math.pi * i * DT / period)))
            for i in range(int(seconds * FPS))]


def test_an_open_palm_going_side_to_side_is_a_wave_once():
    tracker = Tracker(Settings(names=frozenset({"wave", "palm_hold", "swipe_left", "swipe_right"})))
    events = run(tracker, [(i * DT, hand(label="Open_Palm")) for i in range(2)] + wave_frames(2 * DT, 0.07))
    assert done(events) == ["wave"]                                             # not a swipe, not a held palm
    # Waving on: its own cooldown (3 s) keeps it to one.
    assert done(run(tracker, wave_frames(1.6, 0.07, seconds=1.2))) == []
    assert done(run(tracker, gone(0.5, 2.8) + wave_frames(5.0, 0.07))) == ["wave"]


def test_a_wave_too_small_or_a_hand_moving_one_way_is_none():
    tracker = Tracker(Settings(names=frozenset({"wave"})))
    assert run(tracker, wave_frames(0.0, 0.012, seconds=2.0)) == []            # a trembling hand
    sliding = [(3.0 + i * DT, hand(label="Open_Palm", x=0.2 + 0.02 * i)) for i in range(25)]
    assert run(tracker, sliding) == []
    closed = [(6.0 + i * DT, hand(label="Closed_Fist", size=0.3, x=0.5 + 0.07 * math.sin(2 * math.pi * i * DT / 0.5)))
              for i in range(20)]
    assert run(tracker, closed) == []                                           # a fist shaken is no wave


# --- shh ------------------------------------------------------------------------------------------------------------


def at_lips(h, dx: float = 0.0) -> Face:
    """A face whose mouth is on the pointing finger's middle joint (index DIP), moved `dx` across."""
    dip = h.points[7]
    return Face(yaw=0.0, pitch=5.0, mouth=(dip[0] + dx, dip[1]), size=0.2)


def test_a_finger_upright_across_the_lips_is_shh():
    up = hand(label="Pointing_Up")
    tracker = facing_tracker("shh", "point_hold")
    events = watched(tracker, held("Pointing_Up", 0.7), at_lips(up))
    assert done(events) == ["shh"]
    assert features(up, face=at_lips(up)).shape == "shh"


def test_a_hand_at_the_mouth_that_is_not_shh():
    names = ("shh", "point_hold", "fist_hold", "point_right")
    up = hand(label="Pointing_Up")
    beside = watched(facing_tracker(*names), held("Pointing_Up", 0.7), at_lips(up, dx=0.15))
    assert done(beside) == ["point_hold"]                                        # pointing up beside the face
    fist = hand(label="Closed_Fist", size=0.3)
    at_fist = Face(0.0, 5.0, fist.points[6], 0.2)
    assert done(watched(facing_tracker(*names), held("Closed_Fist", 0.7, size=0.3), at_fist)) == ["fist_hold"]
    across = hand(label="Pointing_Up", turn=90)
    assert done(watched(facing_tracker(*names), held("Pointing_Up", 0.7, turn=90), at_lips(across))) == ["point_right"]
    # Without the face model there is no shh at all: pointing at the mouth is pointing.
    assert done(run(Tracker(Settings(names=frozenset(names))), held("Pointing_Up", 0.7))) == ["point_hold"]


# --- facing the screen ------------------------------------------------------------------------------------------


def test_hands_talking_along_while_the_user_looks_away_do_nothing():
    names = ("thumb_up", "palm_hold", "swipe_right", "wave", "point_right")
    tracker = facing_tracker(*names)
    talking = held("Thumb_Up", 0.8) + gone(0.4, 0.8) + held("Open_Palm", 0.8, start=1.3)
    talking += [(2.2 + i * DT, hand(x=0.3)) for i in range(8)] + [(2.8 + i * DT, hand(x=0.3 + 0.1 * i)) for i in range(1, 5)]
    talking += wave_frames(3.3, 0.07) + held("Pointing_Up", 0.8, start=5.0, turn=90)
    assert watched(tracker, talking, AWAY) == []
    assert watched(facing_tracker(*names), talking, None) == []                   # no face seen at all
    # The same, facing the screen: they count.
    assert done(watched(facing_tracker(*names), held("Thumb_Up", 0.8), FACING)) == ["thumb_up"]
    # Without the face model (or require_facing off) the check is skipped, as before.
    assert done(run(Tracker(Settings(names=frozenset(names))), held("Thumb_Up", 0.8))) == ["thumb_up"]
    off = Tracker(Settings(names=frozenset(names), face=True, require_facing=False))
    assert done(watched(off, held("Thumb_Up", 0.8), AWAY)) == ["thumb_up"]


def test_facing_has_hysteresis_and_turning_away_cancels_a_hold():
    tracker = facing_tracker("thumb_up")
    tracker.look(0.0, FACING)
    assert tracker.facing
    tracker.look(0.2, Face(yaw=25.0, pitch=0.0))
    assert tracker.facing                                                       # in at 20°, out past 30°
    tracker.look(0.4, Face(yaw=33.0, pitch=0.0))
    assert not tracker.facing
    fresh = facing_tracker("thumb_up")
    fresh.look(0.0, Face(yaw=25.0, pitch=0.0))
    assert not fresh.facing
    # A hold under way is cancelled when the user turns away.
    tracker = facing_tracker("thumb_up")
    events = watched(tracker, held("Thumb_Up", 0.8), lambda t: FACING if t < 0.2 else AWAY)
    assert done(events) == [] and events[-1]["phase"] == "cancelled"
    # Present: a face seen in the last second. Gone longer, nothing counts.
    tracker = facing_tracker("thumb_up")
    tracker.look(0.0, FACING)
    assert done(watched(tracker, held("Thumb_Up", 0.8, start=2.0), None)) == []


def test_the_hand_message_says_facing_and_face_as_booleans_only():
    tracker = facing_tracker("thumb_up")
    tracker.look(0.0, Face(yaw=1.2345, pitch=6.789, mouth=(0.4321, 0.5678), size=0.2468))
    message = [p for kind, p in tracker.step(0.0, hand()) if kind == "hand"][0]
    assert message["facing"] is True and message["face"] is True
    assert_plain(message)
    text = json.dumps(message)
    for leaked in ("1.2345", "6.789", "0.4321", "0.5678", "0.2468", "yaw", "pitch", "mouth"):
        assert leaked not in text
    assert gestures.parse_hand(message) == message
    plain_message = [p for kind, p in Tracker().step(0.0, hand()) if kind == "hand"][0]
    assert "facing" not in plain_message and "face" not in plain_message       # no face model: not said


# --- nod and shake ------------------------------------------------------------------------------------------------


def head_frames(start: float, yaw_amp: float, pitch_amp: float, seconds: float = 1.5, rate: float = 10.0,
                hz: float = 1.5) -> list[tuple[float, Face]]:
    out = []
    for i in range(int(seconds * rate)):
        t = start + i / rate
        swing = math.sin(2 * math.pi * hz * t)
        out.append((t, Face(yaw=yaw_amp * swing, pitch=6.0 + pitch_amp * swing)))
    return out


def looked(tracker: Tracker, frames: list) -> list[dict]:
    return [p for t, f in frames for kind, p in tracker.look(t, f) if kind == "gesture"]


def test_a_nod_or_a_shake_answers_only_an_open_question_and_as_its_tier_allows():
    tracker = facing_tracker_with({"yes": True, "no": True})
    assert done(looked(tracker, head_frames(0.0, 1.0, 12.0))) == ["nod"]
    tracker = facing_tracker_with({"yes": True, "no": True})
    assert done(looked(tracker, head_frames(0.0, 14.0, 1.0))) == ["shake"]
    # No question open: neither.
    assert looked(facing_tracker_with(), head_frames(0.0, 1.0, 12.0)) == []
    assert looked(facing_tracker_with(), head_frames(0.0, 14.0, 1.0)) == []
    # A tier her card holds for: no yes by nodding; a shake still says no.
    held_tier = {"yes": False, "no": True}
    assert looked(facing_tracker_with(held_tier), head_frames(0.0, 1.0, 12.0)) == []
    assert done(looked(facing_tracker_with(held_tier), head_frames(0.0, 14.0, 1.0))) == ["shake"]


def facing_tracker_with(approval: dict | None = None, **settings: Any) -> Tracker:
    tracker = Tracker(Settings(face=True, **settings))
    tracker.approval = approval
    return tracker


def test_small_head_moves_and_looking_around_are_neither_nod_nor_shake():
    either = {"yes": True, "no": True}
    assert looked(facing_tracker_with(either), head_frames(0.0, 3.0, 3.0)) == []          # talking
    assert looked(facing_tracker_with(either), head_frames(0.0, 14.0, 12.0)) == []        # both axes: looking around
    single = [(i / 10, Face(yaw=0.0, pitch=6.0 + (10.0 if 3 <= i <= 6 else 0.0))) for i in range(15)]
    assert looked(facing_tracker_with(either), single) == []                              # one look down and back


# --- steadier tracking ------------------------------------------------------------------------------------------


def test_a_shape_must_be_left_for_a_moment_before_it_fires_again():
    tracker = Tracker(Settings(names=frozenset({"thumb_up"})))
    assert done(run(tracker, held("Thumb_Up", 0.6))) == ["thumb_up"]
    blink = [(0.7 + i * DT, hand(label="None", curl={}, thumb=1.0)) for i in range(3)]   # 0.2 s away: under rearm_s
    assert done(run(tracker, blink + held("Thumb_Up", 1.0, start=0.95))) == []
    away = [(2.0 + i * DT, hand(label="None", curl={}, thumb=1.0)) for i in range(8)]    # ~0.5 s away
    assert done(run(tracker, away + held("Thumb_Up", 0.7, start=2.6))) == ["thumb_up"]


def test_the_one_euro_filter_steadies_a_trembling_point_and_follows_a_move():
    import random

    rng = random.Random(3)
    f = OneEuro()
    raw = [0.5 + rng.uniform(-0.01, 0.01) for _ in range(60)]
    smooth = [f(i * DT, v) for i, v in enumerate(raw)]
    spread = lambda xs: max(xs[20:]) - min(xs[20:])   # noqa: E731
    assert spread(smooth) < 0.7 * spread(raw)
    for i in range(60, 75):
        last = f(i * DT, 0.8)
    assert abs(last - 0.8) < 0.02


def test_head_angles_from_the_transformation_matrix():
    def rotation(yaw: float, pitch: float) -> list[list[float]]:
        y, p = math.radians(yaw), math.radians(pitch)
        ry = [[math.cos(y), 0, math.sin(y)], [0, 1, 0], [-math.sin(y), 0, math.cos(y)]]
        rx = [[1, 0, 0], [0, math.cos(p), -math.sin(p)], [0, math.sin(p), math.cos(p)]]
        r = [[sum(ry[i][k] * rx[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
        return [row + [0.0] for row in r] + [[0.0, 0.0, 0.0, 1.0]]

    for yaw, pitch in ((0, 0), (25, -10), (-40, 15)):
        got = head_angles(rotation(yaw, pitch))
        assert abs(got[0] - yaw) < 0.01 and abs(got[1] - pitch) < 0.01
    assert turns([0, 10, 0, 10, 0], 8) == 3 and turns([0, 3, 0, 3], 8) == 0


# --- calibration -------------------------------------------------------------------------------------------------


def test_calibration_in_the_config_is_checked(tmp_path):
    config = load(write(tmp_path, "[gestures.calibration]\nindex_open = 0.2\nindex_curled = 0.9\ngap_closed = 0\n"),
                  env={})
    assert config.gestures.calibration == {"index_open": 0.2, "index_curled": 0.9, "gap_closed": 0.0}
    for text, words in (("knuckles_open = 0.2", "unknown keys"), ("index_open = 1.5", "from 0 to 1"),
                        ("index_open = 0.5\nindex_curled = 0.55", "at least 0.15 above"),
                        ("gap_closed = 0.6\ngap_apart = 0.62", "gap_apart"), ('index_open = "low"', "from 0 to 1")):
        with pytest.raises(ConfigError, match=words):
            load(write(tmp_path, f"[gestures.calibration]\n{text}\n"), env={})


def test_a_calibration_moves_the_thresholds_the_shapes_are_read_with():
    stiff = {"curl": {"index": 0.4, "middle": 0.4, "ring": 0.4, "pinky": 0.4}, "thumb": 0.0}
    assert features(hand(**stiff)).shape == ""                                  # not open enough by default
    cal = {f"{d}_open": 0.3 for d in gestures.DIGITS} | {f"{d}_curled": 1.0 for d in gestures.DIGITS}
    shaper = Shaper(Thresholds.from_calibration(cal))
    assert shaper.read(hand(**stiff)).shape == "palm_hold"                      # this user's open hand
    settings = Settings.from_config(GesturesConfig(calibration=cal))
    assert settings.thresholds == Thresholds.from_calibration(cal)
    assert Thresholds.from_calibration({}) == Thresholds.from_calibration(gestures.CALIBRATION_DEFAULTS)


def test_calibration_goes_through_input_toml_and_the_page(tmp_path):
    base = Config()
    cal = {"index_open": 0.12, "index_curled": 0.88, "gap_closed": 0.05, "gap_apart": 0.6}
    text, g, _ = inputs.from_view({"gestures": {"calibration": cal, "require_facing": False, "shh_minutes": 10.0},
                                   "touch": {}}, base)
    assert "[gestures.calibration]" in text and "index_curled = 0.88" in text and "require_facing = false" in text
    assert g.calibration == cal and g.require_facing is False and g.shh_minutes == 10.0
    parsed, _, present = inputs.parse(text, base)
    assert parsed.calibration == cal and "gestures" in present
    for bad in ({"index_open": True}, {"index_open": "0.1"}, {"wrist_open": 0.1}, {"index_open": 0.9}):
        with pytest.raises(ConfigError):
            inputs.from_view({"gestures": {"calibration": bad}, "touch": {}}, base)
    # Without one nothing is written for it, and the defaults are used.
    text, g, _ = inputs.from_view({"gestures": {}, "touch": {}}, base)
    assert "calibration" not in text and g.calibration == {}


def test_the_watcher_reads_its_thresholds_from_input_toml(tmp_path, monkeypatch):
    watcher, cameras, daemon, clock, path = watcher_for(tmp_path, monkeypatch, lambda t: None)
    spin(watcher, clock, 1.5)
    assert watcher.tracker.settings.thresholds == Thresholds.from_calibration({})
    cal = {"thumb_open": 0.2, "thumb_curled": 0.9}
    (tmp_path / "input.toml").write_text("[gestures]\n[gestures.map]\nthumb_up = \"like\"\n[gestures.calibration]\n"
                                         "thumb_open = 0.2\nthumb_curled = 0.9\n", encoding="utf-8")
    spin(watcher, clock, 1.5)
    assert watcher.tracker.settings.thresholds == Thresholds.from_calibration(cal)


# --- names, the map and the messages -----------------------------------------------------------------------------


def test_the_new_names_on_the_routes_and_in_the_map():
    for name in ("point_left", "count_3", "shh", "wave", "pinch_slide", "nod", "shake"):
        assert gestures.parse_gesture({"name": name, "phase": "done"})["name"] == name
    assert set(gestures.HEAD) <= set(gestures.NAMES) and not set(gestures.HEAD) & set(gestures.MAPPABLE)
    with pytest.raises(gestures.GestureError, match="not a gesture"):
        gestures.check_map({"nod": "skip"})
    with pytest.raises(gestures.GestureError, match="slider"):
        gestures.check_map({"thumb_up": "volume"})
    with pytest.raises(gestures.GestureError, match="slider"):
        gestures.check_map({"pinch_slide": "volume_up"})
    shipped = gestures.check_map(dict(GesturesConfig().map))
    assert shipped["point_left"] == "previous" and shipped["point_right"] == "skip" and shipped["shh"] == "quiet"
    assert shipped["pinch_slide"] == "volume" and shipped["wave"] == "wave_back"
    for wrong in ({"curl": {"wrist": 0.1}}, {"curl": {"index": 2}}, {"gap": -0.1}, {"facing": "yes"}, {"yaw": 3.0},
                  {"mouth": [0.5, 0.5]}):
        message = {"present": True, "x": 0.5, "y": 0.5, "size": 0.2, "pinch": 0.0, "open": 1.0} | wrong
        with pytest.raises(gestures.GestureError):
            gestures.parse_hand(message)
    assert inputs.parse_watcher("camera=open; fps=14.9; cpu=21.5; face=on") == {
        "camera": True, "fps": 14.9, "cpu": 21.5, "face": "on"}
    assert inputs.parse_watcher("camera=open; fps=14.9; cpu=21.5; face=yaw12") is None


# --- the brain: nod, shake, wave back, shh, the slider -------------------------------------------------------------


async def test_a_nod_and_a_shake_answer_her_card_per_tier_and_nothing_without_one():
    daemon, mpris = gesture_daemon()
    assert (await daemon.gestures.gesture({"name": "nod", "phase": "done"}))["refused"] == "no_question"
    daemon.gestures.last_done = -1e9
    approval = daemon.approvals.request(daemon.runs.start("typed"), Held("s", "t", {}, "Do it?", risk="change"))
    result = await daemon.gestures.gesture({"name": "nod", "phase": "done"})
    assert result["action"] == "yes" and approval.outcome == "yes" and approval.by == "gesture"
    daemon.gestures.last_done = -1e9
    pending = daemon.approvals.request(daemon.runs.start("typed"), Held("mail", "send", {}, "Send?", risk="sends"))
    assert (await daemon.gestures.gesture({"name": "nod", "phase": "done"}))["refused"] == "not_answerable"
    assert pending.open
    daemon.gestures.last_done = -1e9
    assert (await daemon.gestures.gesture({"name": "shake", "phase": "done"}))["action"] == "no"
    assert pending.outcome == "no" and mpris.pressed == []
    # Practice mode: recognised, nothing answered.
    daemon.input.practice = True
    daemon.gestures.last_done = -1e9
    again = daemon.approvals.request(daemon.runs.start("typed"), Held("s", "t", {}, "?", risk="change"))
    result = await daemon.gestures.gesture({"name": "nod", "phase": "done"})
    assert result["refused"] == "practice" and result["practice"] == "yes" and again.open
    await daemon.close()


async def test_a_wave_gets_her_wave_back_and_a_line_now_and_then():
    daemon, mpris = gesture_daemon()
    sink = add_trusted(daemon.hub, Sink())
    result = await daemon.gestures.gesture({"name": "wave", "phase": "done"})
    assert result["action"] == "wave_back"
    await asyncio.sleep(0.05)
    said = [m for m in sink.got if "state" in m]
    assert said[-1]["reaction"] == "wave" and said[-1]["text"] in gestures_lines("wave")
    daemon.gestures.last_done = -1e9
    daemon.state = "idle"
    await daemon.gestures.gesture({"name": "wave", "phase": "done"})
    await asyncio.sleep(0.05)
    again = [m for m in sink.got if "state" in m][-1]
    assert again["reaction"] == "wave" and "text" not in again                  # a line at most every 20 s
    assert daemon.ledger.recent_notices() == [] and mpris.pressed == []
    # Busy (a run going): no wave back.
    daemon.gestures.last_done = -1e9
    run_ = daemon.runs.start("typed")
    assert (await daemon.gestures.gesture({"name": "wave", "phase": "done"}))["refused"] == "busy"
    daemon.runs.finish(run_)
    await daemon.close()


def gestures_lines(key: str) -> tuple[str, ...]:
    from strawberry_crab import persona

    return persona.shipped().lines[key]


async def test_shh_turns_her_voice_off_for_a_while_with_a_trusted_notice(aiohttp_client):
    daemon, _ = gesture_daemon(shh_minutes=12.0)
    client = await aiohttp_client(create_app(daemon))
    sink = add_trusted(daemon.hub, Sink())
    result = await daemon.gestures.gesture({"name": "shh", "phase": "done"})
    assert result["action"] == "quiet"
    assert {"command": "quiet", "value": 720} in sink.got                       # the widget's own quiet
    await asyncio.sleep(0.05)
    line = [m for m in sink.got if "state" in m][-1]
    assert line["text"] in gestures_lines("shushed")
    notice = daemon.ledger.recent_notices()[-1]
    assert notice.source == "reflex" and not notice.foreign and "shushed you" in notice.about
    assert 715 < daemon.gestures.stats()["quiet_s"] <= 720
    # The tray's Quiet row (or anything sending quiet 0) lets her talk again; the brain's count follows.
    reply = await client.post("/command", json={"command": "quiet", "value": 0})
    assert reply.status == 200 and daemon.gestures.stats()["quiet_s"] == 0
    # Its time runs out by itself (the widget's quiet_until; the brain's count with it).
    daemon.gestures.quiet_until = 1.0
    assert daemon.gestures.quiet_left() == 0
    # No body to keep quiet: it says so and does nothing; in practice mode nothing is sent.
    empty, _ = gesture_daemon()
    assert (await empty.gestures.gesture({"name": "shh", "phase": "done"})).get("ok") is False
    empty.input.practice = True
    empty.gestures.last_done = -1e9
    quiet_sink = add_trusted(empty.hub, Sink())
    assert (await empty.gestures.gesture({"name": "shh", "phase": "done"}))["refused"] == "practice"
    assert quiet_sink.got == [] and empty.ledger.recent_notices() == []
    await empty.close()
    await daemon.close()


class VolumePlayer(FakeMpris):
    """MPRIS with a volume: each set is recorded with its time."""

    def __init__(self, volume: float | None = 0.5) -> None:
        super().__init__()
        self.volume = volume
        self.sets: list[tuple[float, float]] = []

    async def volume_level(self) -> float | None:
        return self.volume

    async def set_volume_level(self, level: float) -> Outcome:
        self.sets.append((asyncio.get_running_loop().time(), level))
        self.volume = level
        return Outcome("set the volume", f"Volume {round(level * 100)}.", True)


def volume_daemon(player: Any, **settings: Any) -> Daemon:
    config = Config()
    config.brain.enabled = config.speech.enabled = config.voice.enabled = config.gate.enabled = False
    config.tools.enabled = config.thinker.enabled = False
    config.gestures.enabled = True
    for key, value in settings.items():
        setattr(config.gestures, key, value)
    toolbox = Toolbox(config.tools)
    return Daemon(reactor=CannedReactor(), config=config, toolbox=toolbox,
                  actor=Actor(config.actions, toolbox, mpris=player))


async def slide(daemon: Daemon, positions: list[float], end: str = "done") -> None:
    desk = daemon.gestures
    await desk.gesture({"name": "pinch_slide", "phase": "started", "progress": 0.5})
    for p in positions:
        await desk.gesture({"name": "pinch_slide", "phase": "progress", "progress": p})
        await asyncio.sleep(1 / 15)
    await desk.gesture({"name": "pinch_slide", "phase": end, "progress": positions[-1]})
    for _ in range(100):
        if desk.slider is None:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the slide did not end")


async def test_the_slider_sets_the_volume_at_most_four_times_a_second_and_the_last_level_on_release():
    player = VolumePlayer(0.4)
    daemon = volume_daemon(player)
    sink = body(daemon)
    positions = [round(0.5 + 0.2 * i / 30, 3) for i in range(1, 31)]          # 2 s, up to 0.7: the volume to 0.8
    await slide(daemon, positions)
    levels = [level for _, level in player.sets]
    assert levels[-1] == 0.8 and player.volume == 0.8                           # the last level, on release
    times = [t for t, _ in player.sets]
    assert len(times) <= 2 * 4 + 2
    assert all(b - a >= 0.24 for a, b in zip(times, times[1:]))                 # ≤ 4 a second
    bus = typed(sink, "gesture")
    assert bus[0]["phase"] == "started" and bus[0]["progress"] == 0.4           # the level it started from
    assert all(m["name"] == "pinch_slide" for m in bus)
    progress = [m["progress"] for m in bus if m["phase"] == "progress"]
    assert progress == sorted(progress) and 0.4 < progress[0] < progress[-1] <= 0.8
    last = bus[-1]
    assert last["phase"] == "done" and last["progress"] == 0.8 and last["action"] == "volume" and last["run_id"]
    notice = daemon.ledger.recent_notices()[-1]
    assert notice.about.endswith("volume to 80") and not notice.foreign
    entry = daemon.input.recent.view()[0]
    assert entry["name"] == "pinch_slide" and entry["outcome"] == "did" and entry["action"] == "volume"
    await daemon.close()


async def test_the_slider_in_practice_mode_shows_the_level_and_sets_nothing():
    player = VolumePlayer(0.5)
    daemon = volume_daemon(player)
    daemon.input.practice = True
    sink = body(daemon)
    await slide(daemon, [0.55, 0.6, 0.65])
    assert player.sets == []
    bus = typed(sink, "gesture")
    assert bus[-1]["phase"] == "done" and "action" not in bus[-1] and bus[-1]["progress"] == 0.8
    assert daemon.input.recent.view()[0]["outcome"] == "practice"
    await daemon.close()


async def test_a_slider_with_nothing_to_set_or_no_volume_to_start_from_does_nothing():
    daemon, mpris = gesture_daemon()                                            # FakeMpris: no volume at all
    result = await daemon.gestures.gesture({"name": "pinch_slide", "phase": "started", "progress": 0.5})
    assert result["action"] == "volume" and result["ok"] is False and daemon.gestures.slider is None
    await daemon.close()
    silent = VolumePlayer(None)                                                 # a player that won't say
    daemon = volume_daemon(silent)
    sink = body(daemon)
    await slide(daemon, [0.6])
    assert silent.sets == [] and typed(sink, "gesture")[-1]["phase"] == "cancelled"
    assert daemon.input.recent.view()[0]["why"] == "no_volume"
    await daemon.close()
    # Not mapped: nothing.
    daemon = volume_daemon(VolumePlayer(), map={"thumb_up": "like"})
    assert (await daemon.gestures.gesture({"name": "pinch_slide", "phase": "started"}))["action"] == ""
    await daemon.close()


async def test_spotify_reads_and_sets_the_volume_as_a_level():
    from tests.fake_spotify import make

    spotify, toolbox, actor = make()
    control = actor.volume_control()
    assert control is not None and control[0] == "spotify"
    server, get, put = control
    assert actor.above(server, "volume", gestures.TIERS) == ""
    spotify.volume = 35
    assert await get(toolbox, server) == 0.35
    outcome = await put(toolbox, server, 0.62)
    assert outcome.ok and spotify.volume == 62
    toolbox.risks = {"spotify.set_volume": "change"}
    assert "change tier" in actor.above(server, "volume", gestures.TIERS)
    await toolbox.close()


# --- the watcher: a fake face reader -------------------------------------------------------------------------------


class FakeFaceReader:
    """Stands in for the face model: what it returns has numbers that must never leave the watcher."""

    def __init__(self, face: Any = None) -> None:
        self.face = face or Face(yaw=1.2345, pitch=6.789, mouth=(0.4321, 0.5678), size=0.2468)
        self.reads: list[float] = []

    def read(self, frame, t):
        self.reads.append(t)
        return self.face(t) if callable(self.face) else self.face


def face_watcher(tmp_path, monkeypatch, script, reader: FakeFaceReader, state: dict | None = None,
                 gestures_toml: str = "enabled = true"):
    monkeypatch.setattr(gestures, "face_model_ready", lambda path=None: True)
    watcher, cameras, daemon, clock, path = watcher_for(tmp_path, monkeypatch, script, gestures_toml, state)
    watcher.make_face = lambda model: reader
    return watcher, cameras, daemon, clock, path


def test_the_face_model_runs_only_while_a_hand_is_up_or_her_card_is_open(tmp_path, monkeypatch):
    reader = FakeFaceReader()
    watcher, cameras, daemon, clock, _ = face_watcher(tmp_path, monkeypatch, lambda t: None, reader)
    spin(watcher, clock, 3.0)
    assert cameras and reader.reads == []                                       # no hand: never
    up = FakeFaceReader()
    watcher, cameras, daemon, clock, _ = face_watcher(
        tmp_path, monkeypatch, lambda t: hand(label="Thumb_Up") if t >= 100.5 else None, up)
    spin(watcher, clock, 2.5)
    rate = len(up.reads) / 2.0
    assert 3 <= rate <= 6                                                        # about face_fps (5) a second
    card = FakeFaceReader()
    state = {"enabled": True, "armed_s": 0.0, "approval": {"yes": True, "no": True},
             "wanted": {"gesture": True, "hand": False}}
    watcher, cameras, daemon, clock, _ = face_watcher(tmp_path, monkeypatch, lambda t: None, card, state)
    spin(watcher, clock, 2.0)
    assert len(card.reads) / 2.0 >= 6                                           # ~10 a second for a nod or shake


def test_nothing_of_the_face_leaves_the_watcher(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    reader = FakeFaceReader()

    def script(t):
        return hand(label="Thumb_Up") if 100.3 <= t < 101.5 else None

    watcher, cameras, daemon, clock, _ = face_watcher(tmp_path, monkeypatch, script, reader)
    spin(watcher, clock, 2.5)
    assert reader.reads and watcher.face_state == "on"
    posts = [payload for method, path, payload in daemon.calls if method == "POST"]
    assert [p["name"] for p in posts if p.get("phase") == "done"] == ["thumb_up"]
    hands = [p for p in posts if p.get("present")]
    assert hands and all(set(p) - {"facing", "face"} <= {"present", "x", "y", "size", "pinch", "open", "engaged",
                                                         "shape", "points", "curl", "gap"} for p in hands)
    assert all(p["facing"] is True for p in hands)
    text = json.dumps(posts) + caplog.text + watcher.report
    for leaked in ("1.2345", "6.789", "0.4321", "0.5678", "0.2468", "yaw", "pitch", "mouth"):
        assert leaked not in text
    for payload in posts:
        assert_plain(payload)


def test_turned_away_the_watcher_posts_no_gesture(tmp_path, monkeypatch):
    reader = FakeFaceReader(AWAY)
    watcher, cameras, daemon, clock, _ = face_watcher(
        tmp_path, monkeypatch, lambda t: hand(label="Thumb_Up") if t >= 100.3 else None, reader)
    spin(watcher, clock, 2.0)
    assert reader.reads and not [c for c in daemon.calls if c[:2] == ("POST", "/gesture")]


def test_without_the_face_model_the_watcher_works_as_before_and_says_so(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(gestures, "face_model_ready", lambda path=None: False)
    watcher, cameras, daemon, clock, _ = watcher_for(
        tmp_path, monkeypatch, lambda t: hand(label="Thumb_Up") if 100.3 <= t < 101.5 else None)
    spin(watcher, clock, 2.5)
    assert watcher.face is None and watcher.face_state == "missing" and not watcher.tracker.settings.face
    assert [c[2]["name"] for c in daemon.calls if c[:2] == ("POST", "/gesture") and c[2]["phase"] == "done"] == ["thumb_up"]
    assert watcher.report.endswith("; face=missing") and inputs.parse_watcher(watcher.report)["face"] == "missing"
    assert caplog.text.count("the face model is missing") == 1


def test_a_nod_goes_from_the_watcher_while_her_card_is_open(tmp_path, monkeypatch):
    reader = FakeFaceReader(lambda t: Face(yaw=0.5, pitch=6.0 + 12.0 * math.sin(2 * math.pi * 1.5 * t)))
    state = {"enabled": True, "armed_s": 0.0, "approval": {"yes": True, "no": True},
             "wanted": {"gesture": True, "hand": False}}
    watcher, cameras, daemon, clock, _ = face_watcher(tmp_path, monkeypatch, lambda t: None, reader, state)
    spin(watcher, clock, 2.0)
    sent = [c[2] for c in daemon.calls if c[:2] == ("POST", "/gesture")]
    assert sent and sent[0] == {"name": "nod", "phase": "done", "progress": 1.0}


# --- fetching the models -----------------------------------------------------------------------------------------


class Response:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size: int = -1) -> bytes:
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk


def test_fetch_leaves_a_model_that_is_there_and_gets_only_the_missing_one(tmp_path, monkeypatch):
    hand_bytes, face_bytes = b"the hand model", b"the face model!"
    models = (gestures.Model("hand.task", "https://example.invalid/hand", hashlib.sha256(hand_bytes).hexdigest(),
                             len(hand_bytes), "the hand model"),
              gestures.Model("face.task", "https://example.invalid/face", hashlib.sha256(face_bytes).hexdigest(),
                             len(face_bytes), "the face model"))
    monkeypatch.setattr(gestures, "MODELS", models)
    (tmp_path / "hand.task").write_bytes(hand_bytes)                            # already installed
    asked: list[str] = []

    def opener(url, timeout):
        asked.append(url)
        return Response(face_bytes)

    said: list[str] = []
    gestures.fetch_all(say=said.append, opener=opener, models_dir=tmp_path)
    assert asked == ["https://example.invalid/face"]
    assert any("hand.task is already there" in line and "not downloaded" in line for line in said)
    assert any(line.startswith("installed") and "face.task" in line for line in said)
    assert (tmp_path / "face.task").read_bytes() == face_bytes
    said.clear()
    gestures.fetch_all(say=said.append, opener=opener, models_dir=tmp_path)    # again: nothing to do
    assert asked == ["https://example.invalid/face"] and len(said) == 2 and all("already there" in s for s in said)
    assert gestures.FACE_MODEL_URL.endswith("/float16/1/face_landmarker.task")  # pinned, not "latest"


# --- the real face model, when it is installed and given ------------------------------------------------------------

FACE_MODEL = os.environ.get("STRAWBERRY_TEST_FACE_MODEL", "")


@pytest.mark.skipif(not FACE_MODEL or gestures.mediapipe_missing() != "", reason="needs the gestures group and "
                    "STRAWBERRY_TEST_FACE_MODEL (a fetched face_landmarker.task)")
def test_the_real_face_model_finds_no_face_in_a_blank_frame():
    import numpy as np

    reader = gesture_watch.FaceReader(Path(FACE_MODEL))
    try:
        assert reader.read(np.zeros((480, 640, 3), np.uint8), 0.0) is None
        assert reader.read(np.full((480, 640, 3), 255, np.uint8), 0.1) is None
    finally:
        reader.close()


@pytest.mark.skipif(gestures.mediapipe_missing() != "", reason="needs the gestures group (MediaPipe)")
def test_the_recognizer_mirrors_x_and_passes_the_world_points_on():
    """The real Recognizer's reading of a result (MediaPipe's shape, faked): x as in a mirror, so a finger toward
    the camera's left is toward the user's right."""
    import numpy as np

    class P:
        def __init__(self, x, y, z=0.0):
            self.x, self.y, self.z = x, y, z

    class Category:
        category_name, score = "Pointing_Up", 0.9

    class Result:
        hand_landmarks = [[P(0.3, 0.5)] + [P(0.3 - 0.01 * i, 0.5) for i in range(1, 21)]]
        hand_world_landmarks = [[P(0.01 * i, 0.0, 0.002) for i in range(21)]]
        gestures = [[Category()]]

    class Fake:
        def recognize_for_video(self, image, ms):
            return Result()

    recognizer = gesture_watch.Recognizer.__new__(gesture_watch.Recognizer)
    recognizer.recognizer, recognizer.last_ms = Fake(), -1
    h = recognizer.recognize(np.zeros((480, 640, 3), np.uint8), 0.0)
    assert h.points[0] == (0.7, 0.5) and h.points[8][0] > h.points[0][0]       # the camera's left is the user's right
    assert h.world[1] == (-0.01, 0.0, 0.002) and abs(h.aspect - 640 / 480) < 1e-9


async def test_untrusted_sockets_get_none_of_the_new_gestures():
    daemon, _ = gesture_daemon()
    asked = body(daemon)
    untrusted = body(daemon, {k: v for k, v in HELLO_V2.items() if k != "secret"})
    v1 = add_trusted(daemon.hub, Sink())
    for name in ("point_right", "count_4", "wave", "nod"):
        daemon.gestures.last_done = -1e9
        await daemon.gestures.gesture({"name": name, "phase": "done"})
    await daemon.gestures.hand(gestures.parse_hand({"present": True, "x": 0.5, "y": 0.5, "size": 0.2, "pinch": 0.0,
                                                    "open": 1.0, "facing": True, "face": True,
                                                    "curl": {"index": 0.1}, "gap": 0.5}))
    assert [m["name"] for m in typed(asked, "gesture")] == ["point_right", "count_4", "wave", "nod"]
    assert typed(asked, "hand")[0]["facing"] is True
    for sink in (untrusted, v1):
        assert not [m for m in sink.got if m.get("type") in ("gesture", "hand")]
    await daemon.close()

