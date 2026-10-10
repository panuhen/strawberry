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

# Shapes the recogniser names, each held for `hold_ms` to count (thumbs included), and the two swipes.
SHAPES = ("thumb_up", "thumb_down", "palm_hold", "fist_hold", "point_hold", "victory_hold", "pinch_hold")
MOTIONS = ("swipe_left", "swipe_right")
MAPPABLE = SHAPES + MOTIONS
# `arm` is the raised open palm that turns command mode on (`[gestures] arming`): never mapped.
NAMES = MAPPABLE + ("arm",)
PHASES = ("started", "progress", "done", "cancelled")

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
}
TIERS = ("read", "playback")
# What `done` may say came of it, besides an ACTIONS name: an approval answered, command mode armed.
OUTCOMES = ("yes", "no", "arm")

# The points of a hand that go on the bus: the wrist and the five fingertips (MediaPipe's 0, 4, 8, 12, 16, 20).
POINTS = ("wrist", "thumb", "index", "middle", "ring", "pinky")
HAND_NUMBERS = ("x", "y", "size", "pinch", "open")
HAND_FLAGS = ("present", "engaged")

_TARGET = re.compile(r"^[a-z0-9_-]{1,32}$")

# The model: MediaPipe's gesture recogniser (hand detector, landmarks and the seven-gesture classifier in
# one file), float16, version 1. Pinned by URL and digest; `fetch` refuses anything else.
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/"
             "float16/1/gesture_recognizer.task")
MODEL_SHA256 = "97952348cf6a6a4915c2ea1496b4b37ebabc50cbbf80571435643c455f2b0482"
MODEL_SIZE = 8373440
MODEL_NAME = "gesture_recognizer.task"


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


def fetch(dest: Path | None = None, url: str = MODEL_URL, timeout: float = 30.0,
          say: Callable[[str], None] = print, opener: Callable[..., Any] | None = None) -> Path:
    """Download the model to `dest` (the data dir's by default) and check its sha256 before it is put in place:
    a temporary file beside it, renamed over it only when the digest matches, so a failed fetch leaves
    whatever was there."""
    import urllib.error
    import urllib.request

    dest = dest or model_file()
    if model_ready(dest) and _sha256(dest) == MODEL_SHA256:
        say(f"{dest} is already there (sha256 {MODEL_SHA256[:12]}…)")
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
        if actual != MODEL_SHA256:
            raise FetchError(f"{MODEL_NAME}: sha256 mismatch (expected {MODEL_SHA256}, got {actual}); not installed")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    say(f"installed {dest} ({MODEL_SIZE / 1e6:.1f} MB, sha256 {MODEL_SHA256[:12]}…)")
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
        out[str(key)] = action
    return out


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
    zone (engaged), and the wrist and fingertips as [x, y] pairs. Numbers and fixed names only."""
    if not isinstance(data, dict):
        raise GestureError("a hand must be an object")
    present = data.get("present")
    if not isinstance(present, bool):
        raise GestureError("hand.present must be true or false")
    if not present:
        if set(data) - {"present"}:
            raise GestureError("a hand that is not present carries nothing else")
        return {"present": False}
    unknown = set(data) - set(HAND_NUMBERS) - set(HAND_FLAGS) - {"points"}
    if unknown:
        raise GestureError(f"unknown hand fields: {sorted(unknown)}")
    out: dict[str, Any] = {"present": True}
    for key in HAND_NUMBERS:
        if key not in data:
            raise GestureError(f"hand.{key} is missing")
        out[key] = _unit(data[key], f"hand.{key}")
    engaged = data.get("engaged", False)
    if not isinstance(engaged, bool):
        raise GestureError("hand.engaged must be true or false")
    out["engaged"] = engaged
    points = data.get("points", {})
    if not isinstance(points, dict) or set(points) - set(POINTS):
        raise GestureError(f"hand.points must be an object of {', '.join(POINTS)}")
    shaped: dict[str, list[float]] = {}
    for key in POINTS:
        if key not in points:
            continue
        pair = points[key]
        if not isinstance(pair, list) or len(pair) != 2:
            raise GestureError(f"hand.points.{key} must be [x, y]")
        shaped[key] = [_unit(pair[0], f"hand.points.{key}"), _unit(pair[1], f"hand.points.{key}")]
    if shaped:
        out["points"] = shaped
    return out


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


class GestureDesk:
    """The daemon's gestures: what /gesture and /hand do, the state the watcher asks for, the runs.

    `daemon` is the Daemon: its config, hub, runs, actor, approvals, ledger and report are used as they are,
    so a gesture's reflex is the same run, the same line and the same timeline entry a spoken one would be,
    with `source: "gesture"`."""

    MAX_HAND_HZ = 30.0
    DONE_GAP_S = 0.5          # a second action within this of the last is refused (the watcher's own cooldown is longer)

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

    @property
    def config(self) -> Any:
        return self.daemon.config.gestures

    # --- what the watcher asks ------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """What the camera should do now (GET /gesture, and every reply): whether gestures are on, how long
        command mode stays armed, the approval a thumb may answer, and who wants what."""
        approval = self.answerable()
        return {"enabled": self.config.enabled,
                "armed_s": round(max(self.armed_until - time.monotonic(), 0.0), 2),
                "approval": approval,
                "wanted": {"gesture": self.daemon.hub.wants_gesture("gesture"),
                           "hand": self.daemon.hub.wants_gesture("hand")}}

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
        out: dict[str, Any] = {"action": "", "run_id": ""}
        if phase == "done":
            now = time.monotonic()
            if now - self.last_done < self.DONE_GAP_S:
                self.refused += 1
                out["refused"] = "too_soon"
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
        out["sent"] = await self.daemon.hub.send_gesture(bus)
        return out

    async def done(self, name: str) -> dict[str, Any]:
        if name == "arm":
            self.arm()
            log.info("gestures: armed for %.0fs", self.config.armed_s)
            self.last = {"name": name, "action": "arm", "ok": True}
            return {"action": "arm", "run_id": ""}
        approval = self.answerable()
        if approval is not None and name in ("thumb_up", "thumb_down"):
            return self.answer(name, approval)
        action = self.action_for(name)
        if not action:
            log.info("gestures: %s is not mapped; nothing done", name)
            self.last = {"name": name, "action": None, "ok": False}
            return {"action": "", "run_id": ""}
        if self.config.arming:
            # Command mode (the watcher keeps it too; this is the second look): off, a mapped gesture does
            # nothing; on, each one keeps it on a while longer.
            if time.monotonic() >= self.armed_until:
                log.info("gestures: %s while command mode is off; nothing done", name)
                self.refused += 1
                self.last = {"name": name, "action": None, "ok": False}
                return {"action": "", "run_id": "", "refused": "not_armed"}
            self.arm()
        if action == "listen":
            result = self.daemon.listen()
            ok = bool(result.get("listening")) or "loading" in result
            log.info("gestures: %s -> listen (%s)", name, "listening" if result.get("listening") else
                     next((k for k in ("loading", "busy", "stopped", "debounced", "error") if k in result), "no"))
            self.acted += 1
            self.last = {"name": name, "action": action, "ok": ok}
            return {"action": action, "run_id": "", "ok": ok}
        found = await self.daemon.actor.named(action)
        if found is None:
            log.info("gestures: %s -> %s, but nothing here can do it (no music server or player for it)", name, action)
            self.last = {"name": name, "action": action, "ok": False}
            return {"action": action, "run_id": "", "ok": False}
        run = self.daemon.runs.start("gesture", foreground=False)
        self.daemon.background(self._run(run, name, action, found), f"gesture {name}")
        self.acted += 1
        return {"action": action, "run_id": run.run_id}

    def answer(self, name: str, approval: dict[str, bool]) -> dict[str, Any]:
        """A held thumb while an approval is open: yes or no, for the tiers `answerable` allows. A thumb never
        runs its mapped action while a question is open."""
        said = "yes" if name == "thumb_up" else "no"
        pending = self.daemon.approvals.open
        if pending is None or not approval.get(said):
            log.info("gestures: a thumbs %s cannot answer a %s approval; say it, type it or use the card",
                     "up" if said == "yes" else "down", pending.risk if pending is not None else "closed")
            self.refused += 1
            self.last = {"name": name, "action": None, "ok": False}
            return {"action": "", "run_id": "", "refused": "not_answerable"}
        reason = self.daemon.approvals.answer(pending.approval_id, said, "gesture")
        if reason is not None:
            self.refused += 1
            self.last = {"name": name, "action": said, "ok": False}
            return {"action": "", "run_id": "", "refused": reason}
        log.info("gestures: %s answered %s %s", name, pending.approval_id, said)
        self.answered += 1
        self.last = {"name": name, "action": said, "ok": True}
        return {"action": said, "run_id": pending.run_id}

    async def _run(self, run: Any, name: str, action: str, found: tuple[str, str, Any]) -> None:
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
                return
            daemon.quiet_media_until = time.monotonic() + daemon.QUIET_MEDIA_S
            if not outcome.ok:
                run.error = "tools"
            self.last = {"name": name, "action": action, "ok": outcome.ok}
            performance, _ = await daemon.report(outcome.event(f"a {name.replace('_', ' ')} gesture"), outcome.ok)
            # Her line is a reflex's fact (a track's name from the player or server): strangers' text.
            daemon.ledger.notice("reflex", f"a {name.replace('_', ' ')} gesture: {outcome.did}",
                                 performance.text or "", foreign=True)
        except asyncio.CancelledError:
            run.cancel_reason = run.cancel_reason or "shutdown"
            raise
        except Exception:
            run.error = run.error or "other"
            raise
        finally:
            runs.active.reset(token)
            daemon.runs.finish(run)

    async def hand(self, message: dict[str, Any]) -> int:
        """One parsed /hand: to the bodies that asked for it, stamped with the brain's clock."""
        self.hand_at = time.monotonic()
        return await self.daemon.hub.send_gesture({"type": "hand", "t": round(self.hand_at, 3)} | message)

    def reload(self) -> bool:
        """Read [gestures] from the config file again (the tray's Gestures row, `strawberry gestures on|off`).
        Raises ConfigError, keeping what she has, when the file does not load. Returns whether they are on."""
        from .config import default_path, load

        fresh = load(self.daemon.config.path or default_path()).gestures
        self.daemon.config.gestures = fresh
        if not fresh.enabled:
            self.armed_until = 0.0
        return fresh.enabled

    def stats(self) -> dict[str, Any]:
        """For /health: counts and the last gesture's name and action. No hand, no position, no frame."""
        return {"enabled": self.config.enabled, "received": self.received, "acted": self.acted,
                "answered": self.answered, "refused": self.refused, "last": self.last,
                "hand_age_s": None if self.hand_at < 0 else round(time.monotonic() - self.hand_at, 1)}
