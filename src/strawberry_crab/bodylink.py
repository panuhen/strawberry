"""Input from bodies: what the user touches and points at (PROTOCOL.md Part 1c, WIRING.md §25).

A v2 body names the separate things it draws in its hello (`capabilities.entities`: the music orb, the
crab herself) and says what input it sends (`capabilities.sends.touch`, `sends.target`). Then it reports
what the user does to them:

    touch   {entity, kind: poke|flick|grab|drag|release|fuse, with?, strength?, t?}   a discrete touch
    target  {entity | null, via: pointer|touch|gesture, t?}                             what the user points at

Both need the bus secret (a trusted body, PROTOCOL §1.4) and the declared capability, are rate-limited per
body, and are checked strictly: entity ids the body declared, no unknown fields, numbers in range, short
strings. What this module holds is the parsing and the state; server.py refuses, daemon.py acts.

`Targets` is the daemon's view of it (`daemon.targets`): the current target, for `target_s` (8 s) after the
last `target`, and what the user is holding (a grab or a drag until its release). Its `situation()` is the
thinker's line about it ("The user is pointing at the music orb."), so "this" means something. Other parts
of the brain (the gesture watcher) read it through `current()` and `holding()`.

**What reaches a model.** A trusted body holds the bus secret; that does not make every string it sends the
user's words. A body could name an entity after a track or a notification. So nothing a model reads (the
situation line, her timeline) carries a body's free text: an entity is named there by code from its id and
its kind alone (`Entity.name`: id "music" and kind "orb" are "the music orb"), and both are checked at hello:
the id a short lowercase token (`ID`), the kind one of `ENTITY_KINDS`. The `label` is for display only (bodies,
`/health`), cleaned and cut to 40 characters (`clean_label`), and never goes into a prompt.
"""

from __future__ import annotations

import math
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from typing import Any

TOUCH_KINDS = ("poke", "flick", "grab", "drag", "release", "fuse")
VIAS = ("pointer", "touch", "gesture")
# An entity's id: a lowercase token, starting with a letter. Short, so it can sit in a config key ([touch]
# music.flick) and be how code names it to a model ("the music orb") without being anyone's sentence.
ID = re.compile(r"[a-z][a-z0-9_-]{0,23}")
# An entity's kind: one of these (a body with something else says the nearest, or `thing`).
ENTITY_KINDS = ("orb", "crab", "panel", "button", "card", "light", "thing")
MAX_ENTITIES = 16
MAX_LABEL = 40
TOUCH_PER_S = 20          # per body; more in one second are dropped and counted
TARGET_PER_S = 10
TOUCH_FIELDS = frozenset({"type", "entity", "kind", "with", "strength", "t"})
TARGET_FIELDS = frozenset({"type", "entity", "via", "t"})
MAX_T = 1e10              # `t` is brain monotonic seconds (PROTOCOL §12.1)
EARLY_S = 2.0             # a `t` up to this far in the past starts the target's time; older or later: arrival

# What a touch can be mapped to ([touch] in the config): the bare music reflexes (actions.py), which a
# configured music server's adapter does or MPRIS does with nothing configured, and the tier of the calls they
# make. A touch cannot answer a question, so only `read` and `playback` may be mapped (config._validate_touch).
TOUCH_ACTIONS = {
    "now_playing": "read",
    "skip": "playback",
    "previous": "playback",
    "pause": "playback",
    "resume": "playback",
    "volume_up": "playback",
    "volume_down": "playback",
}
ACTION_ALIASES = {"next": "skip", "play": "resume"}
# How her timeline says the touch (Daemon: the notice when one was acted on).
VERBS = {"poke": "poked", "flick": "flicked", "grab": "grabbed", "drag": "dragged", "release": "let go of",
         "fuse": "fused"}


def action_name(value: str) -> str:
    """The reflex a [touch] value names ("next" is "skip"), or "" when it names none."""
    name = ACTION_ALIASES.get(value, value)
    return name if name in TOUCH_ACTIONS else ""


@dataclass(frozen=True)
class Entity:
    id: str
    kind: str
    label: str

    def name(self) -> str:
        """Code's own name for it, from the id and the kind alone, never the label: "the music orb", "the crab".
        The only name for it that may reach a model (the situation line, her timeline)."""
        return entity_name(self.id, self.kind)

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "kind": self.kind, "label": self.label}


def entity_name(entity_id: str, kind: str) -> str:
    words = " ".join(entity_id.replace("_", " ").replace("-", " ").split())
    return f"the {kind}" if words == kind else f"the {words} {kind}"


def clean_label(value: Any) -> str:
    """A body's label as the brain keeps it, for display only: NFKC, letters, digits, spaces, hyphens and
    apostrophes (the rest becomes a space), white space collapsed, at most MAX_LABEL characters, cut at a word.
    "" when nothing is left (the caller then uses `Entity.name`)."""
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKC", value[: MAX_LABEL * 4])
    kept = "".join(ch if ch.isalnum() or ch in "-'" else " " for ch in text.replace("’", "'"))
    words = kept.split()
    out = ""
    for word in words:
        joined = f"{out} {word}" if out else word
        if len(joined) > MAX_LABEL:
            break
        out = joined
    return out if out else (words[0][:MAX_LABEL] if words else "")


def parse_entities(value: Any) -> tuple[tuple[Entity, ...], int]:
    """`capabilities.entities` from a hello: each `{id, kind, label}` with a valid id (`ID`) and kind
    (`ENTITY_KINDS`), the first of each id, at most MAX_ENTITIES; and how many entries were refused (a bad id
    or kind, a repeated id, past the cap: the server tells the body, and welcome's `accepted.entities` lists
    the ids taken). An entry's other fields are ignored."""
    if value is None:
        return (), 0
    if not isinstance(value, list):
        return (), 1
    out: dict[str, Entity] = {}
    refused = max(len(value) - 256, 0)
    for item in value[:256]:
        entity_id, kind = (item.get("id"), item.get("kind")) if isinstance(item, dict) else (None, None)
        if len(out) >= MAX_ENTITIES or not _token(entity_id) or kind not in ENTITY_KINDS or entity_id in out:
            refused += 1
            continue
        label = clean_label(item.get("label")) or entity_name(entity_id, kind)
        out[entity_id] = Entity(entity_id, kind, label)
    return tuple(out.values()), refused


def parse_kinds(value: Any) -> frozenset[str]:
    """`sends.touch`: true (every kind) or a list of the kinds it sends; anything else is none."""
    if value is True:
        return frozenset(TOUCH_KINDS)
    if isinstance(value, list):
        return frozenset(k for k in value if isinstance(k, str) and k in TOUCH_KINDS)
    return frozenset()


def _token(value: Any) -> bool:
    return isinstance(value, str) and ID.fullmatch(value) is not None


def _number(value: Any, low: float, high: float) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            and low <= value <= high)


@dataclass(frozen=True)
class Touch:
    entity: Entity
    kind: str
    with_: Entity | None = None
    strength: float | None = None
    t: float | None = None


@dataclass(frozen=True)
class Pointing:
    entity: Entity | None
    via: str
    t: float | None = None


def parse_touch(data: dict[str, Any], entities: dict[str, Entity], kinds: frozenset[str]) -> tuple[Touch | None, str]:
    """A `touch` checked against what its body declared: (the touch, "") or (None, the refusal's reason:
    `unknown_field`, `bad_value`, `unknown_entity`, `not_declared`)."""
    if set(data) - TOUCH_FIELDS:
        return None, "unknown_field"
    kind, entity_id, other = data.get("kind"), data.get("entity"), data.get("with")
    if kind not in TOUCH_KINDS or not _token(entity_id):
        return None, "bad_value"
    if ("strength" in data and not _number(data["strength"], 0.0, 1.0)) or ("t" in data and not _number(data["t"], 0.0, MAX_T)):
        return None, "bad_value"
    if (kind == "fuse") != ("with" in data) or ("with" in data and (not _token(other) or other == entity_id)):
        return None, "bad_value"
    if entity_id not in entities or (other is not None and other not in entities):
        return None, "unknown_entity"
    if kind not in kinds:
        return None, "not_declared"
    strength = float(data["strength"]) if "strength" in data else None
    return Touch(entities[entity_id], kind, entities.get(other) if other else None, strength,
                 float(data["t"]) if "t" in data else None), ""


def parse_target(data: dict[str, Any], entities: dict[str, Entity]) -> tuple[Pointing | None, str]:
    """A `target`: (it, "") or (None, the reason). `entity` must be there: an id the body declared, or null."""
    if set(data) - TARGET_FIELDS:
        return None, "unknown_field"
    entity_id = data.get("entity", ...)
    if data.get("via") not in VIAS or entity_id is ... or (entity_id is not None and not _token(entity_id)):
        return None, "bad_value"
    if "t" in data and not _number(data["t"], 0.0, MAX_T):
        return None, "bad_value"
    if entity_id is not None and entity_id not in entities:
        return None, "unknown_entity"
    return Pointing(entities[entity_id] if entity_id is not None else None, data["via"],
                    float(data["t"]) if "t" in data else None), ""


class Rate:
    """At most `per_s` events in any one second (a sliding window); `allow()` says whether this one is."""

    def __init__(self, per_s: int, clock=time.monotonic) -> None:
        self.per_s = per_s
        self.clock = clock
        self.times: deque[float] = deque()

    def allow(self) -> bool:
        now = self.clock()
        while self.times and now - self.times[0] >= 1.0:
            self.times.popleft()
        if len(self.times) >= self.per_s:
            return False
        self.times.append(now)
        return True


@dataclass(frozen=True)
class Target:
    """One body's report of an entity: what the user points at, or holds."""

    owner: Any              # the hub's Body that reported it (identity only)
    body: str               # its body id
    entity: Entity
    via: str
    at: float               # brain monotonic time it was reported (or its `t`, when a little earlier)


class Targets:
    """What the user points at and holds, as the bodies report it (`Daemon.targets`).

        targets.current()   -> Target | None     the target, until `ttl_s` after the last `target`
        targets.holding()   -> list[Target]      grabbed or dragged and not let go (for at most `hold_s`)
        targets.situation() -> str               the thinker's line about both ("" when there is none)

    A target is the user's own act reported by a trusted body, and the situation line names it by code from
    the entity's id and kind (`Entity.name`), never its label: so the line is not strangers' text."""

    def __init__(self, ttl_s: float = 8.0, hold_s: float = 30.0, clock=time.monotonic) -> None:
        self.ttl_s = ttl_s
        self.hold_s = hold_s
        self.clock = clock
        self._target: Target | None = None
        self._told: tuple[Any, str, str] | None = None      # what the other bodies were last told, and when
        self._told_at = -1e9
        self._held: dict[tuple[Any, str], Target] = {}
        self.last_touch: tuple[Target, str] | None = None
        self.counts = {"touch": 0, "target": 0}

    def _when(self, t: float | None) -> float:
        now = self.clock()
        return t if t is not None and now - EARLY_S <= t <= now else now

    def point(self, owner: Any, body: str, entity: Entity | None, via: str, t: float | None = None) -> bool:
        """A `target` from a body: it becomes the target (None clears it, if it is that body's). True when the
        other bodies should be told: it changed, or they were last told more than half the TTL ago."""
        self.counts["target"] += 1
        current = self.current()
        if entity is None:
            if current is None or current.owner is not owner:
                return False
            self._target = None
        else:
            self._target = Target(owner, body, entity, via, self._when(t))
        key = (owner, entity.id if entity else "", via)
        now = self.clock()
        if key == self._told and now - self._told_at < self.ttl_s / 2:
            return False
        self._told, self._told_at = key, now
        return True

    def touched(self, owner: Any, body: str, entity: Entity, kind: str, t: float | None = None) -> None:
        """A `touch`: a grab or drag holds the entity until its release (or a flick throws it); kept as the
        last touch either way."""
        self.counts["touch"] += 1
        found = Target(owner, body, entity, "touch", self._when(t))
        key = (owner, entity.id)
        if kind in ("grab", "drag"):
            self._held[key] = found
        elif kind in ("release", "flick"):
            self._held.pop(key, None)
        self.last_touch = (found, kind)

    def current(self) -> Target | None:
        target = self._target
        if target is not None and self.clock() - target.at > self.ttl_s:
            self._target = target = None
        return target

    def holding(self) -> list[Target]:
        now = self.clock()
        for key in [k for k, held in self._held.items() if now - held.at > self.hold_s]:
            del self._held[key]
        return list(self._held.values())

    def forget(self, owner: Any) -> bool:
        """A body went away: what it reported goes with it. True when it held the target (tell the others)."""
        self._held = {k: v for k, v in self._held.items() if v.owner is not owner}
        if self.last_touch is not None and self.last_touch[0].owner is owner:
            self.last_touch = None
        if self._target is not None and self._target.owner is owner:
            had = self.current() is not None
            self._target = None
            self._told = None
            return had
        return False

    def situation(self) -> str:
        """For the thinker: "The user is pointing at the music orb. The user is holding the calendar orb." """
        parts = []
        target = self.current()
        if target is not None:
            verb = "touching" if target.via == "touch" else "pointing at"
            parts.append(f"The user is {verb} {target.entity.name()} on the screen.")
        held = [h for h in self.holding() if target is None or (h.owner, h.entity.id) != (target.owner, target.entity.id)]
        if held:
            parts.append(f"The user is holding {' and '.join(h.entity.name() for h in held[:3])}.")
        return " ".join(parts)

    def stats(self) -> dict[str, Any]:
        """For /health (trusted): the target and what is held, by body and entity id, and the counts."""
        target = self.current()
        return {"target": None if target is None else {"body": target.body, "entity": target.entity.id,
                                                       "via": target.via,
                                                       "age_s": round(self.clock() - target.at, 1)},
                "holding": [{"body": h.body, "entity": h.entity.id} for h in self.holding()],
                "ttl_s": self.ttl_s} | self.counts
