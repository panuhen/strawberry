"""Hand gestures (gestures.py, doorways/gesture_track.py, doorways/gesture_watch.py; WIRING.md §26, PROTOCOL Part 1d).

No test opens a camera (conftest.py refuses it): the tracker is fed synthetic hands, the watcher a fake camera
and a fake recogniser, and the brain gets the posts the watcher would make. What is held: the guards (the zone,
the hold, the latch, arming, a hand that only passes by, jitter), the tier refusal when the config loads, the
/gesture and /hand routes (secret, schema, rate), who gets `gesture` and `hand`, a gesture's run and its line,
a thumb answering her card for the tiers it may, and that nothing but numbers and names leaves the watcher.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from strawberry_crab import gestures
from strawberry_crab.actions import Actor, Outcome
from strawberry_crab.config import DEFAULT_GESTURE_MAP, Config, ConfigError, GesturesConfig, load
from strawberry_crab.confirm import Held
from strawberry_crab.daemon import Daemon
from strawberry_crab.doorways import gesture_watch
from strawberry_crab.doorways.gesture_track import Hand, Settings, Tracker, features
from strawberry_crab.events import CannedReactor
from strawberry_crab.server import create_app
from strawberry_crab.tools import Toolbox
from tests.bus import BUS_SECRET, add_trusted
from tests.test_voice import Sink

FPS = 15.0
DT = 1.0 / FPS

# An upright hand, its points relative to its centre in units of its height (x right as in a mirror, y down).
SHAPE = [(0.0, 0.5),
         (-0.15, 0.35), (-0.25, 0.2), (-0.32, 0.08), (-0.38, -0.02),
         (-0.1, 0.0), (-0.12, -0.2), (-0.13, -0.32), (-0.14, -0.45),
         (0.0, -0.02), (0.0, -0.24), (0.0, -0.37), (0.0, -0.5),
         (0.1, 0.0), (0.11, -0.2), (0.12, -0.32), (0.13, -0.43),
         (0.19, 0.05), (0.22, -0.1), (0.24, -0.2), (0.26, -0.3)]


def hand(x: float = 0.5, y: float = 0.45, size: float = 0.25, label: str = "None", score: float = 0.9,
         pinch: bool = False) -> Hand:
    shape = list(SHAPE)
    if pinch:
        shape[4] = (-0.13, -0.42)        # the thumb's tip on the index's
    return Hand(tuple((x + dx * size, y + dy * size) for dx, dy in shape), label, score)


def run(tracker: Tracker, frames: list[tuple[float, Hand | None]]) -> list[dict]:
    out = []
    for t, h in frames:
        out += [payload for kind, payload in tracker.step(t, h) if kind == "gesture"]
    return out


def held(label: str, seconds: float, start: float = 0.0, **kw) -> list[tuple[float, Hand]]:
    return [(start + i * DT, hand(label=label, **kw)) for i in range(int(seconds * FPS) + 1)]


def gone(seconds: float, start: float) -> list[tuple[float, None]]:
    return [(start + i * DT, None) for i in range(1, int(seconds * FPS) + 1)]


def done(events: list[dict]) -> list[str]:
    return [e["name"] for e in events if e["phase"] == "done"]


# --- the tracker: synthetic hands ---------------------------------------------------------------------


def test_a_thumbs_up_held_600_ms_fires_once_with_its_ring_filling():
    tracker = Tracker(Settings(names=frozenset({"thumb_up"})))
    events = run(tracker, held("Thumb_Up", 0.6))
    assert events[0] == {"name": "thumb_up", "phase": "started", "progress": 0.0}
    progress = [e["progress"] for e in events if e["phase"] == "progress"]
    assert progress == sorted(progress) and 0 < progress[0] < progress[-1] < 1.0
    assert done(events) == ["thumb_up"]
    fired_at = next(i for i, e in enumerate(events) if e["phase"] == "done")
    assert all(e["phase"] != "done" for e in events[fired_at + 1:])
    # Held on: the latch keeps it from firing again; it needs a change or the hand gone first.
    assert run(tracker, held("Thumb_Up", 1.5, start=0.7)) == []
    later = run(tracker, gone(0.5, 2.3) + held("Thumb_Up", 0.6, start=3.0))
    assert done(later) == ["thumb_up"]


def test_a_hold_dropped_early_is_cancelled_and_does_nothing():
    tracker = Tracker(Settings(names=frozenset({"thumb_up"})))
    events = run(tracker, held("Thumb_Up", 0.25) + gone(0.5, 0.25))
    assert [e["phase"] for e in events][-1] == "cancelled" and done(events) == []
    assert 0 < events[-1]["progress"] < 1


def test_a_swipe_to_the_users_right_after_the_hand_settles():
    tracker = Tracker(Settings(names=frozenset({"swipe_left", "swipe_right"})))
    frames = [(i * DT, hand(x=0.3)) for i in range(8)]                         # up and still for ~0.5 s
    t0 = 8 * DT
    frames += [(t0 + i * DT, hand(x=0.3 + 0.1 * i)) for i in range(1, 5)]      # 0.4 across in 0.27 s
    events = run(tracker, frames)
    assert done(events) == ["swipe_right"]
    # Swinging back at once is the cooldown's, not a swipe_left.
    back = run(tracker, [(t0 + 0.3 + i * DT, hand(x=0.7 - 0.1 * i)) for i in range(1, 5)])
    assert done(back) == []
    # After the cooldown, settled again, a move to the user's left is swipe_left.
    start = t0 + 2.0
    frames = [(start + i * DT, hand(x=0.7)) for i in range(8)]
    frames += [(start + 8 * DT + i * DT, hand(x=0.7 - 0.1 * i)) for i in range(1, 5)]
    assert done(run(tracker, frames)) == ["swipe_left"]


def test_arming_with_a_palm_then_pointing():
    tracker = Tracker(Settings(names=frozenset({"point_hold"}), arming=True, armed_s=6.0))
    assert run(tracker, held("Pointing_Up", 0.8)) == []                         # command mode is off
    events = run(tracker, gone(0.4, 0.8) + held("Open_Palm", 0.6, start=1.3))
    assert done(events) == ["arm"] and tracker.armed_until > 1.3
    events = run(tracker, held("Pointing_Up", 1.0, start=2.0))     # after the cooldown, held long enough
    assert done(events) == ["point_hold"]
    # Past armed_s with nothing: off again.
    assert run(tracker, gone(0.5, 2.7) + held("Pointing_Up", 0.8, start=10.0)) == []


def test_a_hand_that_wanders_through_without_raising_does_nothing():
    tracker = Tracker(Settings(names=frozenset({"thumb_up", "palm_hold", "swipe_left", "swipe_right"})))
    low = held("Thumb_Up", 1.0, y=0.85)                                          # wrist below the zone
    far = held("Thumb_Up", 1.0, start=1.2, size=0.06)                            # too small: across the room
    events = run(tracker, low + far)
    assert events == []
    # Up in the zone but passing straight through, palm open: no swipe (it never settled) and no hold (it moved).
    passing = [(3.0 + i * DT, hand(x=0.1 + 0.12 * i, label="Open_Palm")) for i in range(7)]
    assert run(tracker, passing + gone(0.5, 3.0 + 7 * DT)) == []
    messages = [p for kind, p in Tracker().step(0.0, hand(y=0.85)) if kind == "hand"]
    assert messages[0]["engaged"] is False


def test_jitter_and_a_blinking_label_still_fire_once():
    rng = random.Random(7)
    tracker = Tracker(Settings(names=frozenset({"thumb_up", "swipe_left", "swipe_right"})))
    frames = []
    for i in range(int(1.2 * FPS)):
        label = "None" if i % 5 == 4 else "Thumb_Up"                            # the recogniser blinks
        frames.append((i * DT, hand(x=0.5 + rng.uniform(-0.004, 0.004), y=0.45 + rng.uniform(-0.004, 0.004),
                                    label=label)))
    assert done(run(tracker, frames)) == ["thumb_up"]


def test_shapes_the_map_does_not_use_show_nothing():
    tracker = Tracker(Settings(names=frozenset({"thumb_up"})))
    assert run(tracker, held("Victory", 1.0) + gone(0.5, 1.0) + held("Closed_Fist", 1.0, start=2.0)) == []


def test_a_pinch_is_read_from_the_geometry():
    assert features(hand(pinch=True)).shape == "pinch_hold" and features(hand(pinch=True)).pinch == 1.0
    assert features(hand()).pinch == 0.0 and features(hand(label="Open_Palm")).open == 1.0
    tracker = Tracker(Settings(names=frozenset({"pinch_hold"})))
    assert done(run(tracker, [(i * DT, hand(pinch=True)) for i in range(10)])) == ["pinch_hold"]


def test_thumbs_answer_only_as_the_open_question_allows():
    tracker = Tracker(Settings(names=frozenset()), approval={"yes": False, "no": True})
    assert run(tracker, held("Thumb_Up", 0.8)) == []                              # a hold tier: no yes by thumb
    events = run(tracker, gone(0.4, 0.8) + held("Thumb_Down", 0.6, start=1.3))
    assert done(events) == ["thumb_down"]
    # A thumb answering needs no arming.
    armed = Tracker(Settings(names=frozenset(), arming=True), approval={"yes": True, "no": True})
    assert done(run(armed, held("Thumb_Up", 0.6))) == ["thumb_up"]


def test_hand_messages_are_numbers_and_fixed_names_only():
    messages = [p for kind, p in Tracker().step(0.0, hand(label="Open_Palm")) if kind == "hand"]
    assert len(messages) == 1
    message = messages[0]
    assert set(message) == {"present", "x", "y", "size", "pinch", "open", "engaged", "points"}
    assert set(message["points"]) == set(gestures.POINTS)
    assert gestures.parse_hand(message) == message                              # the brain takes it as it is
    assert_plain(message)
    tracker = Tracker()
    tracker.step(0.0, hand())
    assert [p for k, p in tracker.step(1.0, None) if k == "hand"] == [{"present": False}]


def assert_plain(value: Any, depth: int = 0) -> None:
    """Numbers, booleans and short fixed names: nothing that could be a frame, an image or text."""
    assert depth < 4
    if isinstance(value, dict):
        for key, inner in value.items():
            assert isinstance(key, str) and len(key) <= 16 and key.replace("_", "").isalnum()
            assert_plain(inner, depth + 1)
    elif isinstance(value, list):
        assert len(value) <= 2
        for inner in value:
            assert_plain(inner, depth + 1)
    elif isinstance(value, str):
        assert len(value) <= 16 and value.replace("_", "").replace("-", "").isalnum()
    else:
        assert isinstance(value, (bool, int, float)) and (isinstance(value, bool) or math.isfinite(value))


# --- the config: the tier refusal ---------------------------------------------------------------------


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("action", ["save_tracks", "remove_saved_tracks", "spotify.next", "send_message", "delete"])
def test_a_map_naming_more_than_read_or_playback_is_refused_when_the_config_loads(tmp_path, action):
    path = write(tmp_path, f'[gestures.map]\nthumb_up = "{action}"\n')
    with pytest.raises(ConfigError, match="a gesture runs only read and playback actions"):
        load(path, env={})


def test_the_map_keys_are_gestures_optionally_for_a_target(tmp_path):
    with pytest.raises(ConfigError, match="is not a gesture"):
        load(write(tmp_path, '[gestures.map]\nwave = "skip"\n'), env={})
    with pytest.raises(ConfigError, match="entity id"):
        load(write(tmp_path, '[gestures.map]\n"Music Orb:thumb_up" = "skip"\n'), env={})
    config = load(write(tmp_path, '[gestures]\nenabled = true\n[gestures.map]\n"music:thumb_up" = "like"\n'
                                  'swipe_right = "skip"\n'), env={})
    assert config.gestures.enabled and config.gestures.map == {"music:thumb_up": "like", "swipe_right": "skip"}
    with pytest.raises(ConfigError, match="gestures.watch"):
        load(write(tmp_path, '[gestures]\nwatch = "sometimes"\n'), env={})


def test_the_shipped_map_is_read_and_playback_only():
    assert gestures.check_map(DEFAULT_GESTURE_MAP) == DEFAULT_GESTURE_MAP
    assert set(gestures.ACTIONS.values()) <= set(gestures.TIERS)
    assert GesturesConfig().enabled is False                                    # opt-in


# --- the brain: /gesture, /hand, who gets what -------------------------------------------------------


class FakeMpris:
    """The bare music reflexes, recorded instead of pressed."""

    def __init__(self) -> None:
        self.pressed: list[str] = []

    def reflexes(self):
        def reflex(name: str, fact: str):
            async def run(toolbox, server):
                self.pressed.append(name)
                return Outcome(f"did {name}", fact, True)
            return run

        return {"skip": reflex("skip", "Skipped. Now Blue Monday by New Order."),
                "pause": reflex("pause", "Paused."), "previous": reflex("previous", "Back one.")}

    async def situation(self) -> str:
        return ""


def gesture_daemon(enabled: bool = True, **settings: Any) -> tuple[Daemon, FakeMpris]:
    config = Config()
    config.brain.enabled = False
    config.speech.enabled = False
    config.voice.enabled = False
    config.gate.enabled = False
    config.tools.enabled = False
    config.thinker.enabled = False
    config.gestures.enabled = enabled
    for key, value in settings.items():
        setattr(config.gestures, key, value)
    mpris = FakeMpris()
    toolbox = Toolbox(config.tools)
    daemon = Daemon(reactor=CannedReactor(), config=config, toolbox=toolbox,
                    actor=Actor(config.actions, toolbox, mpris=mpris))
    return daemon, mpris


HELLO_V2 = {"type": "hello", "client": "test-body", "version": "dev", "protocol": 2, "body": {"id": "orbs"},
            "capabilities": {"phases": ["run"], "gestures": ["gesture", "hand"]}, "secret": BUS_SECRET}


def body(daemon: Daemon, hello: dict | None = None) -> Sink:
    sink = Sink()
    daemon.hub.add(sink)   # type: ignore[arg-type]
    daemon.hub.hello(sink, hello or HELLO_V2, daemon.bus_secret())   # type: ignore[arg-type]
    return sink


def typed(sink: Sink, kind: str) -> list[dict]:
    return [m for m in sink.got if m.get("type") == kind]


async def test_gesture_and_hand_go_only_to_trusted_v2_bodies_that_asked():
    daemon, _ = gesture_daemon()
    asked = body(daemon)
    untrusted = body(daemon, {k: v for k, v in HELLO_V2.items() if k != "secret"})
    only_gestures = body(daemon, HELLO_V2 | {"capabilities": {"gestures": ["gesture"]}})
    not_asked = body(daemon, HELLO_V2 | {"capabilities": {"phases": ["run"]}})
    v1 = add_trusted(daemon.hub, Sink())
    await daemon.gestures.gesture({"name": "thumb_up", "phase": "progress", "progress": 0.5})
    await daemon.gestures.hand(gestures.parse_hand({"present": False}))
    assert typed(asked, "gesture") == [{"type": "gesture", "t": typed(asked, "gesture")[0]["t"], "name": "thumb_up",
                                        "phase": "progress", "progress": 0.5}]
    assert [m["present"] for m in typed(asked, "hand")] == [False]
    assert typed(only_gestures, "gesture") and not typed(only_gestures, "hand")
    for sink in (untrusted, not_asked, v1):
        assert sink.got == []
    state = daemon.gestures.state()
    assert state["wanted"] == {"gesture": True, "hand": True} and state["enabled"] is True
    # What welcome says each got, and /health's row.
    from strawberry_crab.hub import accepted_gestures

    assert accepted_gestures(daemon.hub.body(asked)) == {"gestures": ["gesture", "hand"]}   # type: ignore[arg-type]
    assert accepted_gestures(daemon.hub.body(untrusted)) == {"gestures": []}               # type: ignore[arg-type]
    assert accepted_gestures(daemon.hub.body(not_asked)) == {}                             # type: ignore[arg-type]
    await daemon.close()


async def test_the_routes_need_the_secret_a_schema_and_gestures_on(aiohttp_client):
    daemon, _ = gesture_daemon()
    client = await aiohttp_client(create_app(daemon))
    bare = await aiohttp_client(create_app(gesture_daemon()[0]), headers={})
    ok = {"name": "thumb_up", "phase": "progress", "progress": 0.4}
    assert (await bare.post("/gesture", json=ok)).status == 403
    assert (await bare.post("/hand", json={"present": False})).status == 403
    assert (await bare.get("/gesture")).status == 403
    for wrong in ({"name": "wave", "phase": "done"}, {"name": "thumb_up", "phase": "held"},
                  {"name": "thumb_up", "phase": "progress"}, {"name": "thumb_up", "phase": "progress", "progress": 1.5},
                  {"name": "thumb_up", "phase": "done", "frame": "iVBORw0KGgo="}, ["thumb_up"]):
        response = await client.post("/gesture", json=wrong)
        assert response.status == 400, wrong
    for wrong in ({"present": True}, {"present": False, "x": 0.5}, {"present": True, "x": 0.5, "y": 0.5, "size": 0.2,
                  "pinch": 0, "open": 1, "image": "AAAA"}, {"present": True, "x": 2, "y": 0.5, "size": 0.2, "pinch": 0,
                  "open": 1}, {"present": True, "x": 0.5, "y": 0.5, "size": 0.2, "pinch": 0, "open": 1,
                               "points": {"wrist": [0.5, 0.5, 0.1]}}):
        assert (await client.post("/hand", json=wrong)).status == 400, wrong
    reply = await client.post("/gesture", json=ok)
    assert reply.status == 200
    body_ = await reply.json()
    assert body_["state"]["enabled"] is True and body_["action"] == ""
    assert (await (await client.get("/gesture")).json())["approval"] is None
    daemon.config.gestures.enabled = False
    assert (await client.post("/gesture", json=ok)).status == 409
    assert (await client.post("/hand", json={"present": False})).status == 409


async def test_the_routes_are_rate_limited(aiohttp_client):
    daemon, mpris = gesture_daemon()
    client = await aiohttp_client(create_app(daemon))
    statuses = [(await client.post("/gesture", json={"name": "thumb_up", "phase": "progress", "progress": 0.1})).status
                for _ in range(40)]
    assert statuses.count(429) >= 5 and statuses[:30] == [200] * 30
    daemon.gestures.gesture_limit.tokens = 30.0
    first = await client.post("/gesture", json={"name": "swipe_right", "phase": "done"})
    second = await client.post("/gesture", json={"name": "swipe_right", "phase": "done"})
    assert first.status == 200 and second.status == 429                          # one action per half second
    replies = [await (await client.post("/hand", json={"present": False})).json() for _ in range(25)]
    assert any(r.get("dropped") for r in replies)                                 # past the burst: dropped, not queued
    await asyncio.sleep(0.05)


async def test_a_mapped_gesture_runs_its_reflex_as_a_run_with_a_line_and_a_notice():
    daemon, mpris = gesture_daemon()
    sink = body(daemon, HELLO_V2 | {"capabilities": {"phases": ["tool", "speaking", "run"],
                                                     "gestures": ["gesture"]}})
    await daemon.start()
    try:
        result = await daemon.gestures.gesture({"name": "swipe_right", "phase": "done", "progress": 1.0})
        assert result["action"] == "skip" and result["run_id"].startswith("r-")
        run = daemon.runs.get(result["run_id"])
        await asyncio.wait_for(run.finished.wait(), 5)
        await asyncio.sleep(0.1)
        assert mpris.pressed == ["skip"] and run.source == "gesture" and not run.foreground
        assert run.outcome == "completed" and run.tools == ["mpris.skip"]
        said = [m for m in sink.got if "state" in m]
        assert said and said[-1]["text"].startswith("Skipped. Now Blue Monday") and said[-1]["source"] == "gesture"
        phases = [m["type"] for m in sink.got if m.get("run_id") == run.run_id and "state" not in m
                  and m["type"] != "gesture"]
        assert phases == ["tool.started", "tool.completed", "speaking", "run.completed"]
        bus = typed(sink, "gesture")[0]
        assert bus["action"] == "skip" and bus["run_id"] == run.run_id and bus["phase"] == "done"
        notices = daemon.ledger.recent_notices()
        assert len(notices) == 1 and "swipe right gesture" in notices[0].about and notices[0].foreign
        # A gesture that is not mapped, or that nothing can do, takes no action and writes no notice.
        assert (await daemon.gestures.gesture({"name": "victory_hold", "phase": "done"}))["action"] == ""
        daemon.gestures.last_done = -1e9
        assert (await daemon.gestures.gesture({"name": "thumb_up", "phase": "done"})) | {} == \
            {"action": "like", "run_id": "", "ok": False, "sent": 1}                # like needs a music server
        assert len(daemon.ledger.recent_notices()) == 1
    finally:
        await daemon.close()


async def test_arming_in_the_brain_too():
    daemon, mpris = gesture_daemon(arming=True)
    assert (await daemon.gestures.gesture({"name": "palm_hold", "phase": "done"}))["refused"] == "not_armed"
    assert mpris.pressed == []
    daemon.gestures.last_done = -1e9
    assert (await daemon.gestures.gesture({"name": "arm", "phase": "done"}))["action"] == "arm"
    assert daemon.gestures.state()["armed_s"] > 7
    daemon.gestures.last_done = -1e9
    assert (await daemon.gestures.gesture({"name": "palm_hold", "phase": "done"}))["action"] == "pause"
    await asyncio.sleep(0.2)
    assert mpris.pressed == ["pause"]
    await daemon.close()


async def test_a_target_picks_its_own_map_entry(monkeypatch):
    daemon, mpris = gesture_daemon(map={"thumb_up": "pause", "music:thumb_up": "skip"})
    assert daemon.gestures.action_for("thumb_up") == "pause"
    monkeypatch.setattr(daemon.gestures, "target", lambda: "music")
    assert daemon.gestures.action_for("thumb_up") == "skip"
    monkeypatch.setattr(daemon.gestures, "target", lambda: "calendar")
    assert daemon.gestures.action_for("thumb_up") == "pause"
    await daemon.close()


async def test_the_body_links_target_picks_the_map_entry():
    """What a body says the user points at (bodylink.Targets) is the target a map entry names."""
    from strawberry_crab.bodylink import Entity

    daemon, mpris = gesture_daemon(map={"thumb_up": "pause", "music:thumb_up": "skip"})
    assert daemon.gestures.target() == ""
    orbs = object()
    daemon.targets.point(orbs, "orbs", Entity("music", "orb", "Music"), "pointer")
    assert daemon.gestures.target() == "music" and daemon.gestures.action_for("thumb_up") == "skip"
    daemon.targets.point(orbs, "orbs", None, "pointer")
    assert daemon.gestures.action_for("thumb_up") == "pause"
    await daemon.close()


async def test_a_thumb_answers_her_card_only_for_a_tap_tier():
    daemon, mpris = gesture_daemon()
    run = daemon.runs.start("typed")
    approval = daemon.approvals.request(run, Held("spotify", "remove_from_playlist", {}, "Remove it?", risk="change"))
    assert daemon.gestures.state()["approval"] == {"yes": True, "no": True}
    result = await daemon.gestures.gesture({"name": "thumb_up", "phase": "done"})
    assert result["action"] == "yes" and approval.outcome == "yes" and approval.by == "gesture"
    assert mpris.pressed == []                                                   # not its mapped like
    # A sends or destructive question (a hold on the card): no yes by thumb; a no is fine.
    for risk in ("sends", "destructive"):
        daemon.gestures.last_done = -1e9
        pending = daemon.approvals.request(daemon.runs.start("typed"), Held("mail", "send", {}, "Send it?", risk=risk))
        assert daemon.gestures.state()["approval"] == {"yes": False, "no": True}
        refused = await daemon.gestures.gesture({"name": "thumb_up", "phase": "done"})
        assert refused["refused"] == "not_answerable" and pending.open
        daemon.gestures.last_done = -1e9
        assert (await daemon.gestures.gesture({"name": "thumb_down", "phase": "done"}))["action"] == "no"
        assert pending.outcome == "no" and pending.by == "gesture"
    # [gestures] approvals = false: thumbs never answer.
    daemon.config.gestures.approvals = False
    daemon.approvals.request(daemon.runs.start("typed"), Held("s", "t", {}, "?", risk="change"))
    assert daemon.gestures.state()["approval"] is None
    await daemon.close()


async def test_a_reflex_whose_calls_are_above_playback_is_not_run_by_a_gesture():
    class Server:
        topic = "music"

    class Adapter:
        reflexes = {}
        said_reflexes = {}
        reflex_tools = {"skip": ("next",), "like": ("like_current",)}

    pressed = []

    async def skip(toolbox, server):
        pressed.append("skip")
        return Outcome("skipped", "Skipped.", True)

    class Box:
        servers = {"spotify": Server()}
        adapters = {"spotify": Adapter()}
        risks = {"spotify.next": "sends"}

        def risk(self, server, tool):
            return self.risks.get(f"{server}.{tool}", "playback")

        def needs_approval(self, server, tool):
            return self.risk(server, tool) in ("sends", "destructive")

        def label(self, server, tool):
            return tool

    config = Config()
    actor = Actor(config.actions, Box(), reflexes={"spotify": {"skip": skip}})   # type: ignore[arg-type]
    found = await actor.named("skip")
    assert found is not None and found[0] == "spotify"
    assert "sends tier" in actor.above("spotify", "skip", gestures.TIERS)
    assert await actor.act_named("skip", found) is None and pressed == []
    Box.risks = {}
    assert (await actor.act_named("skip", found)).fact == "Skipped." and pressed == ["skip"]
    assert actor.above("spotify", "previous", gestures.TIERS) != ""             # unlisted calls: not run


async def test_spotify_likes_skips_and_pauses_by_gesture_but_a_raised_tier_stops_it():
    """The real Spotify adapter over the fake server: its like (like_current, playback) and skip run by name; with
    `[approvals] risk` raising the like to change, the gesture does nothing and calls nothing."""
    from tests.fake_spotify import make

    spotify, toolbox, actor = make(library=True)
    found = await actor.named("like")
    assert found is not None and found[0] == "spotify"
    outcome = await actor.act_named("like", found)
    assert outcome is not None and outcome.ok and spotify.liked
    for action in ("skip", "pause"):
        assert (await actor.act_named(action, await actor.named(action))).ok
    assert spotify.log.count("like_current") == 1 and "next" in spotify.log and "pause" in spotify.log
    toolbox.risks = {"spotify.like_current": "change"}
    calls = len(spotify.log)
    assert "change tier" in actor.above("spotify", "like", gestures.TIERS)
    assert await actor.act_named("like", await actor.named("like")) is None and len(spotify.log) == calls
    await toolbox.close()


async def test_health_says_counts_and_names_only(aiohttp_client):
    daemon, _ = gesture_daemon()
    client = await aiohttp_client(create_app(daemon))
    await client.post("/hand", json={"present": True, "x": 0.5, "y": 0.4, "size": 0.2, "pinch": 0.1, "open": 1.0,
                                     "engaged": True, "points": {"wrist": [0.5, 0.6]}})
    stats = (await (await client.get("/health")).json())["gestures"]
    assert set(stats) == {"enabled", "received", "acted", "answered", "refused", "last", "hand_age_s"}
    assert "0.4" not in json.dumps(stats) and "points" not in json.dumps(stats)


# --- the watcher: fake camera, fake model, fake daemon ------------------------------------------------


class FrameCanary:
    """Stands in for a frame: it must never reach a post or a log line."""

    def __repr__(self) -> str:
        return "FRAME-CANARY"

    __str__ = __repr__


class FakeCamera:
    def __init__(self) -> None:
        self.reads = 0
        self.closed = False

    def read(self):
        self.reads += 1
        return FrameCanary()

    def close(self) -> None:
        self.closed = True


class ScriptedRecognizer:
    def __init__(self, script) -> None:
        self.script = script

    def recognize(self, frame, t):
        assert isinstance(frame, FrameCanary)
        return self.script(t)


class FakeDaemon:
    def __init__(self, state: dict | None = None) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.state = state or {"enabled": True, "armed_s": 0.0, "approval": None,
                               "wanted": {"gesture": True, "hand": True}}

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "GET":
            return 200, dict(self.state)
        return 200, {"sent": 1, "action": "like" if payload.get("phase") == "done" else "", "state": dict(self.state)}

    def close(self) -> None:
        pass


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def watcher_for(tmp_path, monkeypatch, script, gestures_toml: str = "enabled = true", state: dict | None = None):
    monkeypatch.setattr(gestures, "mediapipe_missing", lambda: "")
    monkeypatch.setattr(gestures, "model_ready", lambda path=None: True)
    path = write(tmp_path, f"[gestures]\n{gestures_toml}\n")
    cameras: list[FakeCamera] = []

    def opener(spec, fps):
        cameras.append(FakeCamera())
        return cameras[-1]

    clock = Clock()
    daemon = FakeDaemon(state)
    watcher = gesture_watch.Watcher("http://127.0.0.1:1", path, camera_opener=opener,
                                    recognizer_factory=lambda model: ScriptedRecognizer(script), poster=daemon,
                                    clock=clock)
    return watcher, cameras, daemon, clock, path


def spin(watcher, clock, seconds: float) -> None:
    end = clock.now + seconds
    while clock.now < end:
        wait = watcher.tick()
        clock.now += max(wait, 0.01)


def test_the_watcher_never_opens_the_camera_while_gestures_are_off(tmp_path, monkeypatch):
    watcher, cameras, daemon, clock, _ = watcher_for(tmp_path, monkeypatch, lambda t: None, "enabled = false")
    spin(watcher, clock, 10.0)
    assert cameras == [] and daemon.calls == []


def test_the_watcher_posts_names_and_numbers_and_never_a_frame(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    def script(t):
        return hand(label="Thumb_Up") if 100.5 <= t < 102.0 else None

    watcher, cameras, daemon, clock, path = watcher_for(tmp_path, monkeypatch, script)
    spin(watcher, clock, 3.0)
    assert len(cameras) == 1 and cameras[0].reads > 0
    posts = [(p, payload) for method, p, payload in daemon.calls if method == "POST"]
    sent = [payload for p, payload in posts if p == "/gesture"]
    assert [g["name"] for g in sent if g["phase"] == "done"] == ["thumb_up"]
    hands = [payload for p, payload in posts if p == "/hand"]
    assert hands and hands[-1] == {"present": False}
    for _, payload in posts:
        assert_plain(payload)
        assert "FRAME-CANARY" not in json.dumps(payload)
    assert "FRAME-CANARY" not in caplog.text
    # Rate: a hand while it is up goes out at most hand_hz (15) a second.
    present = [c for c in daemon.calls if c[1] == "/hand" and c[2]["present"]]
    assert len(present) <= 15 * 1.6 + 1
    # Turned off in the file (the tray's row): the camera is released within a second or two.
    path.write_text("[gestures]\nenabled = false\n")
    os.utime(path, None)
    spin(watcher, clock, 2.5)
    assert cameras[0].closed and watcher.camera is None


def test_the_hand_goes_out_only_when_a_body_wants_it(tmp_path, monkeypatch):
    state = {"enabled": True, "armed_s": 0.0, "approval": None, "wanted": {"gesture": True, "hand": False}}
    watcher, cameras, daemon, clock, _ = watcher_for(tmp_path, monkeypatch, lambda t: hand(), state=state)
    spin(watcher, clock, 2.0)
    assert not [c for c in daemon.calls if c[1] == "/hand"]


def test_watch_armed_keeps_the_camera_closed_until_the_brain_arms_it(tmp_path, monkeypatch):
    state = {"enabled": True, "armed_s": 0.0, "approval": None, "wanted": {"gesture": True, "hand": False}}
    watcher, cameras, daemon, clock, _ = watcher_for(tmp_path, monkeypatch, lambda t: None,
                                                     'enabled = true\nwatch = "armed"\narmed_s = 4.0', state)
    spin(watcher, clock, 5.0)
    assert cameras == [] and any(c[:2] == ("GET", "/gesture") for c in daemon.calls)
    daemon.state["armed_s"] = 4.0                                               # strawberry gestures arm
    spin(watcher, clock, 1.5)
    daemon.state["armed_s"] = 0.0
    assert len(cameras) == 1 and not cameras[0].closed
    spin(watcher, clock, 5.0)
    assert cameras[0].closed and watcher.camera is None                         # no hand: released after armed_s
    daemon.state["approval"] = {"yes": True, "no": True}                         # her card: the camera opens for it
    spin(watcher, clock, 1.5)
    assert len(cameras) == 2 and not cameras[1].closed


def test_without_mediapipe_or_the_model_the_camera_stays_closed(tmp_path, monkeypatch, caplog):
    watcher, cameras, daemon, clock, _ = watcher_for(tmp_path, monkeypatch, lambda t: None)
    monkeypatch.setattr(gestures, "mediapipe_missing", lambda: "mediapipe not installed: uv sync …")
    spin(watcher, clock, 12.0)
    monkeypatch.setattr(gestures, "mediapipe_missing", lambda: "")
    monkeypatch.setattr(gestures, "model_ready", lambda path=None: False)
    spin(watcher, clock, 12.0)
    assert cameras == []
    assert caplog.text.count("mediapipe not installed") == 1 and "strawberry gestures fetch" in caplog.text


def test_the_real_camera_opener_is_refused_in_tests():
    with pytest.raises(AssertionError, match="out of bounds"):
        gesture_watch.open_camera("", 15.0)


def test_the_core_runs_without_mediapipe_or_opencv(tmp_path):
    code = (
        "import sys\nsys.modules['mediapipe'] = None\nsys.modules['cv2'] = None\n"
        "from strawberry_crab import server, daemon, gestures, gesturecmd, doctor\n"
        "from strawberry_crab.doorways import gesture_watch, gesture_track\n"
        "from strawberry_crab.config import load\n"
        "load(env={})\n"
        "assert gestures.mediapipe_missing().startswith('mediapipe and cv2 not installed'), gestures.mediapipe_missing()\n"
    )
    env = dict(os.environ, XDG_CONFIG_HOME=str(tmp_path), XDG_DATA_HOME=str(tmp_path), XDG_STATE_HOME=str(tmp_path))
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, env=env)
    assert result.returncode == 0, result.stderr


def test_fetch_checks_the_digest(tmp_path):
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

    dest = tmp_path / "models" / gestures.MODEL_NAME
    with pytest.raises(gestures.FetchError, match="sha256 mismatch"):
        gestures.fetch(dest, opener=lambda url, timeout: Response(b"not the model"), say=lambda line: None)
    assert not dest.exists() and list(dest.parent.iterdir()) == []
    assert gestures.MODEL_URL.endswith("/float16/1/gesture_recognizer.task")    # pinned, not "latest"


# --- the real recogniser, when it is installed and its model given -----------------------------------

MODEL = os.environ.get("STRAWBERRY_TEST_GESTURE_MODEL", "")


@pytest.mark.skipif(not MODEL or gestures.mediapipe_missing() != "", reason="needs the gestures group and "
                    "STRAWBERRY_TEST_GESTURE_MODEL (a fetched gesture_recognizer.task)")
def test_the_real_recognizer_finds_no_hand_in_a_blank_frame():
    import numpy as np

    recognizer = gesture_watch.Recognizer(Path(MODEL))
    try:
        assert recognizer.recognize(np.zeros((480, 640, 3), np.uint8), 0.0) is None
        assert recognizer.recognize(np.full((480, 640, 3), 255, np.uint8), 0.1) is None
    finally:
        recognizer.close()
