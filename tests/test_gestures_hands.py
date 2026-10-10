"""Two hands (doorways/gesture_track.py `Tracker.step_hands`, doorways/gesture_watch.py, gestures.parse_hand;
WIRING.md §26, PROTOCOL Part 1d): the primary (the raised, higher hand, with hysteresis), the hand message's
`hands` and `pair`, gestures from the primary only, no gesture out of a hand coming or going, handedness, the
`[gestures] hands` setting.

No test opens a camera (conftest.py refuses it): the tracker gets synthetic hands (tests/test_gestures.py's `hand`),
the watcher a fake camera and a fake recogniser, the Recognizer a faked MediaPipe result.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import random
from typing import Any

import pytest

from strawberry_crab import gestures, inputs
from strawberry_crab.config import Config, ConfigError, load
from strawberry_crab.doorways import gesture_watch
from strawberry_crab.doorways.gesture_track import (PRIMARY_DWELL_S, PRIMARY_MARGIN, Hand, Settings, Slot, Shaper,
                                                    Tracker)
from tests.test_gestures import (DT, assert_plain, body, gesture_daemon, hand, spin, typed, watcher_for, write)

SWIPES = frozenset({"swipe_left", "swipe_right"})


def sided(h: Hand, side: str) -> Hand:
    return dataclasses.replace(h, handedness=side)


def left(**kw: Any) -> Hand:
    return sided(hand(**({"x": 0.3, "y": 0.45} | kw)), "left")


def right(**kw: Any) -> Hand:
    return sided(hand(**({"x": 0.7, "y": 0.45} | kw)), "right")


def frames(seconds: float, make, start: float = 0.0) -> list[tuple[float, list[Hand]]]:
    return [(start + i * DT, make(start + i * DT)) for i in range(int(round(seconds / DT)) + 1)]


def stepped(tracker: Tracker, frames_: list) -> tuple[list[dict], list[dict]]:
    """The gesture events and the hand messages."""
    shown, told = [], []
    for t, hands in frames_:
        for kind, payload in tracker.step_hands(t, hands):
            (shown if kind == "gesture" else told).append(payload)
    return shown, told


def primary_of(message: dict) -> dict:
    return next(h for h in message["hands"] if h["primary"])


def done(events: list[dict]) -> list[str]:
    return [e["name"] for e in events if e["phase"] == "done"]


# --- the primary -------------------------------------------------------------------------------------------------


def test_the_higher_hand_is_the_primary_and_its_message_is_as_one_hands_was():
    two, one = Tracker(Settings()), Tracker(Settings())
    low, high = left(y=0.5), right(y=0.3)
    for i in range(12):
        t = i * DT
        both = [p for k, p in two.step_hands(t, [low, high]) if k == "hand"]
        alone = [p for k, p in one.step(t, high) if k == "hand"]
        assert len(both) == len(alone) == 1
        message = both[0]
        # Backward compatible: the top level is exactly what the higher hand alone gives.
        assert {k: v for k, v in message.items() if k not in ("hands", "pair")} == alone[0]
    assert [h["handedness"] for h in message["hands"]] == ["left", "right"]           # left to right on the screen
    assert [h["primary"] for h in message["hands"]] == [False, True]
    assert primary_of(message)["x"] == message["x"] and primary_of(message)["y"] == message["y"]
    assert len({h["id"] for h in message["hands"]}) == 2
    for entry in message["hands"]:
        assert set(entry) >= {"id", "handedness", "primary", "pinched", "x", "y", "size", "pinch", "open", "engaged",
                              "points", "curl", "gap"}
        assert "present" not in entry and "facing" not in entry
    assert_plain(message)


def test_one_hand_goes_as_before_with_a_hands_list_of_one():
    two, one = Tracker(Settings(names=frozenset({"thumb_up"}))), Tracker(Settings(names=frozenset({"thumb_up"})))
    got_two, got_one = [], []
    for i in range(20):
        t = i * DT
        h = hand(label="Thumb_Up") if i < 15 else None
        got_two += two.step_hands(t, [h] if h else [])
        got_one += one.step(t, h)
    got_one += [(k, p) for k, p in one.step(20 * DT + 0.5, None)]
    got_two += [(k, p) for k, p in two.step_hands(20 * DT + 0.5, [])]
    stripped = [(k, {key: v for key, v in p.items() if key not in ("hands", "pair")}) for k, p in got_two]
    assert stripped == got_one and ("hand", {"present": False}) in got_two
    present = [p for k, p in got_two if k == "hand" and p["present"]]
    assert all(len(p["hands"]) == 1 and "pair" not in p and p["hands"][0]["primary"] for p in present)
    assert "handedness" not in present[0]["hands"][0]              # not known (the stand-in gives none): absent


def test_the_other_hand_must_be_clearly_higher_for_a_while_to_take_over():
    tracker = Tracker(Settings())
    shown, told = stepped(tracker, frames(1.0, lambda t: [left(y=0.42), right(y=0.5)]))
    first = primary_of(told[-1])["id"]
    assert primary_of(told[-1])["handedness"] == "left"
    # A little higher (under the margin): no change, however long.
    _, told = stepped(tracker, frames(2.0, lambda t: [left(y=0.42), right(y=0.42 - PRIMARY_MARGIN / 2)], 1.1))
    assert {primary_of(m)["id"] for m in told} == {first}
    # Clearly higher, but only briefly: no change.
    _, told = stepped(tracker, frames(PRIMARY_DWELL_S * 0.6, lambda t: [left(y=0.42), right(y=0.25)], 3.2))
    _, back = stepped(tracker, frames(0.5, lambda t: [left(y=0.42), right(y=0.5)], 3.6))
    assert {primary_of(m)["id"] for m in told + back} == {first}
    # Clearly higher and staying so: it takes over after the dwell, and keeps its id.
    _, told = stepped(tracker, frames(1.0, lambda t: [left(y=0.42), right(y=0.25)], 4.2))
    switched = [i for i, m in enumerate(told) if primary_of(m)["id"] != first]
    assert switched and primary_of(told[-1])["handedness"] == "right"
    assert switched[0] >= int(PRIMARY_DWELL_S / DT) - 1
    assert {h["id"] for h in told[-1]["hands"]} == {h["id"] for h in told[0]["hands"]}


def test_hands_at_about_the_same_height_do_not_flip_flop():
    random.seed(7)
    tracker = Tracker(Settings())

    def jittery(t):
        return [left(y=0.42 + random.uniform(-0.03, 0.03)), right(y=0.42 + random.uniform(-0.03, 0.03))]

    _, told = stepped(tracker, frames(5.0, jittery))
    ids = [primary_of(m)["id"] for m in told]
    assert len(set(ids)) == 1


def test_a_raised_hand_beats_a_higher_one_that_is_not():
    tracker = Tracker(Settings())
    far = left(y=0.05, size=0.08)               # high on the screen but small: across the room, not raised
    near = right(y=0.4)
    _, told = stepped(tracker, frames(0.6, lambda t: [far, near]))
    assert primary_of(told[-1])["handedness"] == "right" and told[-1]["engaged"] is True
    assert [h["engaged"] for h in told[-1]["hands"]] == [False, True]


def test_ids_stay_while_a_hand_stays_and_a_returning_hand_gets_a_new_one():
    tracker = Tracker(Settings())

    def moving(t):
        return [left(x=0.25 + 0.1 * t, y=0.45), right(x=0.75 - 0.1 * t, y=0.4)]

    _, told = stepped(tracker, frames(1.0, moving))
    by_side = {h["handedness"]: h["id"] for h in told[0]["hands"]}
    assert all({h["handedness"]: h["id"] for h in m["hands"]} == by_side for m in told)
    # The left hand goes for longer than lost_s and comes back: a new id; the right keeps its own.
    stepped(tracker, frames(0.6, lambda t: [right(y=0.4)], 1.1))
    _, told = stepped(tracker, frames(0.3, lambda t: [left(y=0.45), right(y=0.4)], 1.8))
    ids = {h["handedness"]: h["id"] for h in told[-1]["hands"]}
    assert ids["right"] == by_side["right"] and ids["left"] not in by_side.values()


def test_a_hand_found_twice_counts_once():
    tracker = Tracker(Settings())
    h = right(y=0.4)
    twin = dataclasses.replace(h, points=tuple((x + 0.004, y + 0.003) for x, y in h.points))
    _, told = stepped(tracker, frames(0.3, lambda t: [h, twin]))
    assert all(len(m["hands"]) == 1 and "pair" not in m for m in told)


# --- two-hand features -------------------------------------------------------------------------------------------


def test_the_pair_says_how_far_apart_and_at_what_angle_and_both_pinches():
    tracker = Tracker(Settings())
    _, told = stepped(tracker, frames(0.5, lambda t: [left(x=0.25, y=0.5), right(x=0.65, y=0.3)]))
    m = told[-1]
    a, b = m["hands"]
    assert m["pair"]["distance"] == pytest.approx(math.hypot(b["x"] - a["x"], b["y"] - a["y"]), abs=0.002)
    angle = math.degrees(math.atan2(a["y"] - b["y"], b["x"] - a["x"]))
    assert m["pair"]["angle"] == pytest.approx(angle, abs=0.2) and m["pair"]["angle"] > 0    # the right one higher
    assert m["pair"]["both_pinched"] is False and not a["pinched"] and not b["pinched"]
    # Tilted the other way: negative.
    _, told = stepped(Tracker(Settings()), frames(0.5, lambda t: [left(x=0.25, y=0.3), right(x=0.65, y=0.5)]))
    assert told[-1]["pair"]["angle"] < 0
    # Both pinching, then one.
    tracker = Tracker(Settings())
    _, told = stepped(tracker, frames(0.5, lambda t: [left(pinch=True), right(pinch=True, y=0.4)]))
    assert told[-1]["pair"]["both_pinched"] is True and all(h["pinched"] for h in told[-1]["hands"])
    _, told = stepped(tracker, frames(0.5, lambda t: [left(), right(pinch=True, y=0.4)], 0.6))
    assert told[-1]["pair"]["both_pinched"] is False
    assert [h["pinched"] for h in told[-1]["hands"]] == [False, True]
    assert all(h["pinch"] > 0.8 for h in told[-1]["hands"] if h["pinched"])
    # The aspect is put back for the angle: a wide frame flattens it.
    wide = Tracker(Settings())
    _, told = stepped(wide, frames(0.5, lambda t: [dataclasses.replace(left(x=0.25, y=0.5), aspect=16 / 9),
                                                   dataclasses.replace(right(x=0.65, y=0.3), aspect=16 / 9)]))
    assert 0 < told[-1]["pair"]["angle"] < angle


# --- gestures from the primary only ------------------------------------------------------------------------------


def test_the_other_hand_makes_no_gesture():
    tracker = Tracker(Settings(names=frozenset({"thumb_up", "palm_hold"})))
    shown, told = stepped(tracker, frames(1.5, lambda t: [left(y=0.3, label="None", curl={"index": 0.5}),
                                                          right(y=0.5, label="Thumb_Up")]))
    assert shown == [] and told[-1]["hands"][1]["shape"] == "thumb_up"     # seen and said, never a gesture


def test_two_thumbs_up_fire_once_even_when_the_hand_that_fired_is_lowered():
    tracker = Tracker(Settings())
    tracker.approval = {"yes": True, "no": True}
    shown, _ = stepped(tracker, frames(1.5, lambda t: [left(y=0.4, label="Thumb_Up"), right(y=0.45, label="Thumb_Up")]))
    assert done(shown) == ["thumb_up"]
    # The left hand drops out of the zone; the right, still thumbs up, takes over and does not fire again.
    more, told = stepped(tracker, frames(2.0, lambda t: [left(y=0.9, label="Thumb_Up"),
                                                         right(y=0.45, label="Thumb_Up")], 1.6))
    assert done(more) == [] and primary_of(told[-1])["handedness"] == "right"
    # It leaves the shape for a moment and makes it again: that one counts.
    again, _ = stepped(tracker, frames(0.6, lambda t: [right(y=0.45, label="None")], 3.7))
    again2, _ = stepped(tracker, frames(1.0, lambda t: [right(y=0.45, label="Thumb_Up")], 4.4))
    assert done(again + again2) == ["thumb_up"]


def test_a_hand_coming_or_going_makes_no_swipe_and_no_hand_gone():
    tracker = Tracker(Settings(names=SWIPES))
    a, b = left(x=0.15, y=0.38), right(x=0.8, y=0.46)
    shown, _ = stepped(tracker, frames(1.0, lambda t: [a, b]))
    # The primary (left) goes: the right one takes over (a jump of 0.65 across) without a swipe or a "present: false".
    gone, told = stepped(tracker, frames(1.5, lambda t: [b], 1.1))
    assert shown == [] and gone == []
    assert all(m["present"] for m in told) and primary_of(told[-1])["handedness"] == "right"
    # A hand appears on the other side, higher: it takes over, and that is no swipe either.
    came, told = stepped(tracker, frames(1.5, lambda t: [left(x=0.1, y=0.25), b], 2.7))
    assert came == [] and primary_of(told[-1])["handedness"] == "left"
    # The new primary swipes for real after it settled: that one counts.
    swipe, _ = stepped(tracker, frames(0.35, lambda t: [left(x=0.1 + (t - 4.3) * 1.2, y=0.25), b], 4.3))
    assert done(swipe) == ["swipe_right"]


def test_a_hold_under_way_is_not_taken_by_a_hand_coming_in_higher():
    tracker = Tracker(Settings(names=frozenset({"palm_hold"}), hold_s=0.8))
    shown, _ = stepped(tracker, frames(0.2, lambda t: [right(y=0.45, label="Open_Palm")]))
    more, told = stepped(tracker, frames(1.5, lambda t: [left(y=0.2), right(y=0.45, label="Open_Palm")], 0.25))
    events = shown + more
    assert done(events) == ["palm_hold"] and not [e for e in events if e["phase"] == "cancelled"]
    assert primary_of(told[-1])["handedness"] == "left"          # once the hold is done, the higher hand takes over


def test_a_slide_is_not_taken_by_the_other_hand():
    tracker = Tracker(Settings(names=frozenset({"pinch_slide"})))
    shown, _ = stepped(tracker, frames(0.8, lambda t: [right(y=0.45, pinch=True)]))
    assert [e["phase"] for e in shown if e["name"] == "pinch_slide"][:1] == ["started"]
    more, told = stepped(tracker, frames(1.0, lambda t: [left(y=0.15), right(y=0.45 - (t - 0.9) * 0.1, pinch=True)],
                                         0.9))
    assert tracker.sliding and primary_of(told[-1])["handedness"] == "right"
    assert not [e for e in more if e["phase"] in ("done", "cancelled")]


# --- handedness --------------------------------------------------------------------------------------------------


@pytest.mark.skipif(gestures.mediapipe_missing() != "", reason="needs the gestures group (MediaPipe)")
def test_the_recognizer_gives_each_hand_its_handedness_and_mirrors_x():
    """MediaPipe's label on the camera's own frame is the hand itself (its picture of two right hands reads
    "Right"): it goes on lowercased; x is mirrored for the user as before."""
    import numpy as np

    class P:
        def __init__(self, x, y, z=0.0):
            self.x, self.y, self.z = x, y, z

    class Category:
        def __init__(self, name, score=0.9):
            self.category_name, self.score = name, score

    class Result:
        hand_landmarks = [[P(0.2 + 0.001 * i, 0.5) for i in range(21)], [P(0.7 + 0.001 * i, 0.4) for i in range(21)]]
        hand_world_landmarks = [[P(0.01 * i, 0.0) for i in range(21)], [P(0.02 * i, 0.0) for i in range(21)]]
        gestures = [[Category("Thumb_Up")], [Category("None", 0.6)]]
        handedness = [[Category("Right")], [Category("Left")]]

    class Fake:
        def recognize_for_video(self, image, ms):
            return Result()

    recognizer = gesture_watch.Recognizer.__new__(gesture_watch.Recognizer)
    recognizer.recognizer, recognizer.last_ms = Fake(), -1
    hands = recognizer.recognize_hands(np.zeros((480, 640, 3), np.uint8), 0.0)
    assert [h.handedness for h in hands] == ["right", "left"]
    assert hands[0].points[0] == pytest.approx((0.8, 0.5)) and hands[1].points[0] == pytest.approx((0.3, 0.4))
    assert hands[0].label == "Thumb_Up" and hands[1].world[2] == (-0.04, 0.0, 0.0)
    assert recognizer.recognize(np.zeros((480, 640, 3), np.uint8), 0.1).handedness == "right"   # the first, as before

    class Bare:          # an older result with no handedness: unknown, not a guess
        hand_landmarks = Result.hand_landmarks[:1]

    Fake.recognize_for_video = lambda self, image, ms: Bare()
    assert recognizer.recognize_hands(np.zeros((480, 640, 3), np.uint8), 0.2)[0].handedness == ""


def test_a_hands_handedness_is_what_most_of_its_recent_labels_say():
    slot = Slot(1, (0.5, 0.5), 0.0, Shaper())
    slot.votes.extend(["right"] * 4 + ["left"])
    assert slot.handedness == "right"
    slot.votes.extend(["", "left"])
    assert slot.handedness == "right"          # 3 right, 2 left in the last seven
    assert Slot(2, (0.5, 0.5), 0.0, Shaper()).handedness == ""


def test_a_label_that_blinks_does_not_swap_the_hands():
    tracker = Tracker(Settings())

    def blink(t):
        i = round(t / DT)
        return [sided(left(y=0.45), "right") if i % 5 == 4 else left(y=0.45), right(y=0.4)]

    _, told = stepped(tracker, frames(1.5, blink))
    assert all([h["handedness"] for h in m["hands"]] == ["left", "right"] for m in told)
    assert len({tuple(h["id"] for h in m["hands"]) for m in told}) == 1


# --- what goes to the brain and the bodies -----------------------------------------------------------------------


def test_parse_hand_takes_hands_and_pair_and_refuses_anything_else():
    tracker = Tracker(Settings())
    _, told = stepped(tracker, frames(0.4, lambda t: [left(pinch=True), right(y=0.4)]))
    message = told[-1]
    assert gestures.parse_hand(json.loads(json.dumps(message))) == message
    base = {k: v for k, v in message.items() if k not in ("hands", "pair")}
    assert gestures.parse_hand(base) == base                                    # the old shape, as before
    entry = message["hands"][0]
    for wrong in (base | {"hands": []}, base | {"hands": [entry] * 3},
                  base | {"hands": [entry | {"frame": "AAAA"}]}, base | {"hands": [entry | {"id": 0}]},
                  base | {"hands": [entry | {"id": True}]}, base | {"hands": [entry | {"handedness": "both"}]},
                  base | {"hands": [{k: v for k, v in entry.items() if k != "primary"}]},
                  base | {"hands": [entry | {"x": 1.5}]}, base | {"hands": [entry | {"facing": True}]},
                  base | {"hands": [entry], "pair": message["pair"]},
                  message | {"pair": message["pair"] | {"angle": 120.0}},
                  message | {"pair": message["pair"] | {"distance": 2.0}},
                  message | {"pair": {"distance": 0.3, "angle": 0.0}},
                  message | {"pair": message["pair"] | {"image": 1}}):
        with pytest.raises(gestures.GestureError):
            gestures.parse_hand(wrong)


async def test_a_body_gets_both_hands_and_the_pair():
    daemon, _ = gesture_daemon()
    asked = body(daemon)
    tracker = Tracker(Settings())
    _, told = stepped(tracker, frames(0.4, lambda t: [left(), right(y=0.4)]))
    await daemon.gestures.hand(gestures.parse_hand(told[-1]))
    got = typed(asked, "hand")[-1]
    assert len(got["hands"]) == 2 and got["pair"] == told[-1]["pair"] and got["x"] == told[-1]["x"]
    assert len(json.dumps(got)) < 2000                         # two hands stay well under 2 KB a message
    await daemon.close()


def test_the_watcher_posts_both_hands_and_each_post_checks_out(tmp_path, monkeypatch):
    def script(t):
        return [left(), right(y=0.4)] if 100.3 <= t < 101.5 else None

    watcher, cameras, daemon, clock, _ = watcher_for(tmp_path, monkeypatch, script)
    spin(watcher, clock, 2.5)
    posts = [payload for method, path, payload in daemon.calls if method == "POST" and path == "/hand"]
    present = [p for p in posts if p["present"]]
    assert present and posts[-1] == {"present": False}
    for p in posts:
        assert gestures.parse_hand(p) == p
        assert_plain(p)
    assert all(len(p["hands"]) == 2 and "pair" in p for p in present[1:])
    assert len(present) <= 15 * 1.3 + 1                                          # the rate is the same: hand_hz


# --- the setting -------------------------------------------------------------------------------------------------


def test_hands_is_one_or_two_in_the_config_and_in_input_toml(tmp_path):
    assert Config().gestures.hands == 2
    assert load(write(tmp_path, "[gestures]\nhands = 1\n"), env={}).gestures.hands == 1
    for wrong in ("hands = 3", "hands = 0", 'hands = "two"', "hands = 1.5"):
        with pytest.raises(ConfigError):
            load(write(tmp_path, f"[gestures]\n{wrong}\n"), env={})
    gestures_, _, present = inputs.parse("[gestures]\nhands = 1\n", Config())
    assert gestures_.hands == 1 and "gestures" in present
    assert "hands = 2" in inputs.render(Config().gestures, Config().touch)


def test_the_watcher_asks_the_model_for_the_hands_the_setting_says(tmp_path, monkeypatch):
    made: list[int] = []

    class FakeRecognizer:
        def __init__(self, model, hands=2):
            made.append(hands)

        def recognize_hands(self, rgb, t):
            return []

        def close(self):
            made.append(-1)

    monkeypatch.setattr(gesture_watch, "Recognizer", FakeRecognizer)
    monkeypatch.setattr(gestures, "mediapipe_missing", lambda: "")
    monkeypatch.setattr(gestures, "model_ready", lambda path=None: True)
    monkeypatch.setattr(gestures, "face_model_ready", lambda path=None: False)
    path = write(tmp_path, "[gestures]\nenabled = true\nhands = 1\n")
    from tests.test_gestures import Clock, FakeCamera, FakeDaemon

    clock = Clock()
    watcher = gesture_watch.Watcher("http://127.0.0.1:1", path, camera_opener=lambda spec, fps: FakeCamera(),
                                    poster=FakeDaemon(), clock=clock)
    spin(watcher, clock, 1.0)
    assert made == [1] and watcher.camera is not None
    path.write_text("[gestures]\nenabled = true\nhands = 2\n")
    os.utime(path, (clock.now + 5, clock.now + 5))
    spin(watcher, clock, 2.0)
    assert made == [1, -1, 2] and watcher.camera is not None
