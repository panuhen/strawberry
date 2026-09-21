"""Acting on what you said (WIRING.md §8b): the reflex tier, and the seam for the thinker.

The gate has already decided a spoken sentence is a request or a question with a topic, and,
on the same embedding, which tool it is like and whether it carries an argument. Three tiers
by cost:

    reflex   the gate is sure about the tool and there is nothing to fill in -> call it now
             (a skip is ~0.25 s end to end: no model in the loop until she phrases the result)
    thinker  an argument or several steps -> Qwen with the topic's tools (not built yet: today
             she answers as chat, and the journal says what would have gone to the thinker)
    chat     everything else, as before

Whatever tier acted, the outcome is one plain sentence of fact written by code ("Skipped.
Now Blue Monday by New Order.") and the reaction path adds a short quip in her voice after it
(the `action` event). A 1B model will not reliably carry a track name or a number from a
structured result into a sentence, so the fact never depends on it; Spotify's JSON never
reaches the bubble, and a failure says it failed.

Reflexes are per server (the tool names are the server's), keyed by the gate's tool option.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .config import ActionsConfig
from .events import Event
from .systemone import Route
from .tools import Toolbox, ToolResult

log = logging.getLogger("strawberryd.actions")


@dataclass(frozen=True)
class Outcome:
    """What she did and what came of it: the sentence she says, and the record for /health."""

    did: str            # "skipped to the next track"
    fact: str           # "Skipped. Now Blue Monday by New Order."  (spoken as is)
    ok: bool
    calls: tuple[ToolResult, ...] = ()

    def event(self, asked: str) -> Event:
        """For the reaction path: it adds a quip after `fact` (brain.describe)."""
        return Event(source="action", app=self.did, title=asked, body=self.fact,
                     category="" if self.ok else "failed", urgency="normal" if self.ok else "critical")


Reflex = Callable[[Toolbox, str], Awaitable[Outcome]]


# ----------------------------------------------------------------------------- spotify reflexes


def _json(result: ToolResult) -> dict[str, Any]:
    try:
        data = json.loads(result.text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _track(data: dict[str, Any]) -> str:
    """'Blue Monday by New Order' from a get_current_track result; '' when nothing is playing."""
    track = data.get("track") or {}
    if not track:
        return ""
    name = track.get("name", "something")
    artists = ", ".join(track.get("artists", [])) or "an unknown artist"
    return f"{name} by {artists}"


def _failed(verb: str, result: ToolResult) -> Outcome:
    detail = (_json(result).get("error") or result.text.strip().splitlines()[0][:160]) if result.text else "no answer"
    detail = str(detail).removeprefix("error: ")
    return Outcome(f"tried to {verb}", f"I tried to {verb}, but Spotify said: {detail}.", False, (result,))


async def _current(toolbox: Toolbox, server: str) -> tuple[str, dict[str, Any], ToolResult]:
    now = await toolbox.call(server, "get_current_track")
    data = _json(now) if now.ok else {}
    return _track(data), data, now


async def _spotify_now(toolbox: Toolbox, server: str) -> Outcome:
    track, data, result = await _current(toolbox, server)
    if not result.ok:
        return _failed("look at the player", result)
    if not track:
        return Outcome("looked at the player", "Nothing is playing right now.", True, (result,))
    album = (data.get("track") or {}).get("album")
    tail = f", from {album}" if album and album not in track else ""
    verb = "That's" if data.get("playing", True) else "Paused on"
    return Outcome("looked at the player", f"{verb} {track}{tail}.", True, (result,))


async def _spotify_step(toolbox: Toolbox, server: str, tool: str, verb: str, did: str, lead: str) -> Outcome:
    result = await toolbox.call(server, tool)
    if not result.ok:
        return _failed(verb, result)
    await asyncio.sleep(0.6)  # Spotify reports the old track for a moment
    track, _data, now = await _current(toolbox, server)
    return Outcome(did, f"{lead} {track}." if track else f"{lead.rstrip(':')}.", True, (result, now))


async def spotify_skip(toolbox: Toolbox, server: str) -> Outcome:
    return await _spotify_step(toolbox, server, "next", "skip", "skipped to the next track", "Skipped. Now")


async def spotify_previous(toolbox: Toolbox, server: str) -> Outcome:
    return await _spotify_step(toolbox, server, "previous", "go back", "went back to the previous track", "Back to")


async def spotify_pause(toolbox: Toolbox, server: str) -> Outcome:
    result = await toolbox.call(server, "pause")
    if not result.ok:
        return _failed("pause", result)
    return Outcome("paused the music", "Paused.", True, (result,))


async def spotify_resume(toolbox: Toolbox, server: str) -> Outcome:
    return await _spotify_step(toolbox, server, "play", "resume", "started the music again", "Playing again:")


def _volume(delta: int) -> Reflex:
    async def reflex(toolbox: Toolbox, server: str) -> Outcome:
        # Only the devices listing carries the current volume (the active device's).
        word = "down" if delta < 0 else "up"
        state = await toolbox.call(server, "get_devices")
        if not state.ok:
            return _failed(f"turn it {word}", state)
        active = [d for d in _json(state).get("devices", []) if isinstance(d, dict) and d.get("is_active")]
        current = active[0].get("volume") if active else None
        if not isinstance(current, int):
            return Outcome(f"tried to turn it {word}", f"I tried to turn it {word}, but Spotify won't say where the volume is.",
                           False, (state,))
        target = max(0, min(100, current + delta))
        result = await toolbox.call(server, "set_volume", {"volume": target})
        if not result.ok:
            return _failed(f"turn it {word}", result)
        return Outcome(f"turned the volume {word}", f"Volume {word} to {target}.", True, (state, result))

    return reflex


async def spotify_situation(toolbox: Toolbox, server: str) -> str:
    """One line of context for the thinker: 'this song' means whatever is playing now."""
    track, data, result = await _current(toolbox, server)
    if not result.ok:
        return ""
    if not track:
        return "Nothing is playing on Spotify right now."
    album = (data.get("track") or {}).get("album")
    state = "Now playing" if data.get("playing", True) else "Paused"
    return f"{state} on Spotify: {track}" + (f" (album: {album})." if album else ".")


Situation = Callable[[Toolbox, str], Awaitable[str]]
SITUATIONS: dict[str, Situation] = {"spotify": spotify_situation}


async def spotify_vocabulary(toolbox: Toolbox, server: str) -> list[str]:
    """Names whisper should know: what is playing, favourites, recent saves, playlists.

    'Daft Punk' came through as Dothpunk, Duff Punk and Dove Punk before the recogniser was
    told the names; the third one played a real artist called Dovepunk. Order matters: the
    list is cut to voice.max_hotwords, so the most likely names come first.
    """
    names: list[str] = []

    def add(*values: Any) -> None:
        for value in values:
            if isinstance(value, str) and value.strip() and value not in names and len(value) <= 40:
                names.append(value.strip())

    track, data, now = await _current(toolbox, server)
    if now.ok:
        add(*(data.get("track") or {}).get("artists", []))
    whole = 500_000  # these listings are parsed here, not read by a model: no truncation
    favourites = await toolbox.call(server, "get_favorites", result_chars=whole)
    if favourites.ok:
        for item in _json(favourites).get("favorites", []):
            add(*item.get("artists", []))
    saved = await toolbox.call(server, "get_saved_tracks", {"limit": 50}, result_chars=whole)
    if saved.ok:
        for item in _json(saved).get("tracks", []):
            add(*item.get("artists", []))
    playlists = await toolbox.call(server, "get_playlists", {"limit": 50}, result_chars=whole)
    if playlists.ok:
        for item in _json(playlists).get("playlists", []):
            name = item.get("name", "")
            if any(ch.isalpha() for ch in name):  # emoji-only playlist names help nobody
                add(name)
    return names


Vocabulary = Callable[[Toolbox, str], Awaitable[list[str]]]
VOCABULARIES: dict[str, Vocabulary] = {"spotify": spotify_vocabulary}


REFLEXES: dict[str, dict[str, Reflex]] = {
    "spotify": {
        "skip": spotify_skip,
        "previous": spotify_previous,
        "pause": spotify_pause,
        "resume": spotify_resume,
        "volume_down": _volume(-15),
        "volume_up": _volume(15),
        "now_playing": _spotify_now,
    },
}


# ----------------------------------------------------------------------------- the actor


class Actor:
    def __init__(self, config: ActionsConfig, toolbox: Toolbox, reflexes: dict[str, dict[str, Reflex]] | None = None) -> None:
        self.config = config
        self.toolbox = toolbox
        self.reflexes = REFLEXES if reflexes is None else reflexes
        self.acted = 0
        self.failed = 0
        self.deferred = 0          # would have gone to the thinker
        self.last: dict[str, Any] | None = None

    def reflex_for(self, route: Route) -> tuple[str, Reflex] | None:
        """The first configured server for the route's topic that has a reflex for its tool."""
        if not route.tool or route.tool == "other":
            return None
        if route.tool_confidence < self.config.reflex or route.has_argument >= self.config.argument:
            return None
        for name, server in self.toolbox.servers.items():
            if server.topic == route.topic and route.tool in self.reflexes.get(name, {}):
                return name, self.reflexes[name][route.tool]
        return None

    async def act(self, text: str, route: Route) -> Outcome | None:
        """Do what the sentence asks, if this tier can. Returns the outcome (its `fact` is what she
        says, `event()` lets the reaction path add a quip), or None when nothing here applies and
        the caller treats the sentence as chat."""
        if not self.config.enabled or route.decision != "act":
            return None
        found = self.reflex_for(route)
        if found is None:
            self.deferred += 1
            log.info("actions: %r (%s/%s tool %s %.2f arg %.2f) needs the thinker; answering as chat for now",
                     text, route.kind, route.topic, route.tool or "-", route.tool_confidence, route.has_argument)
            return None
        server, reflex = found
        started = time.perf_counter()
        try:
            outcome = await asyncio.wait_for(reflex(self.toolbox, server), self.config.timeout_s)
        except asyncio.TimeoutError:
            verb = route.tool.replace("_", " ")
            outcome = Outcome(f"tried to {verb}", f"I tried to {verb}, but {server} did not answer in time.", False)
        ms = (time.perf_counter() - started) * 1000
        self.acted += 1
        if not outcome.ok:
            self.failed += 1
        self.last = {
            "asked": text, "tool": route.tool, "server": server, "did": outcome.did, "fact": outcome.fact,
            "ok": outcome.ok, "ms": round(ms, 1), "calls": [c.to_dict() | {"text": c.text[:200]} for c in outcome.calls],
        }
        log.info("actions: %r -> %s.%s: %s -> %r (%.0f ms)", text, server, route.tool, outcome.did, outcome.fact, ms)
        return outcome

    async def situation(self, topic: str) -> str:
        """What the thinker should know about the topic before it starts, from the servers that can say."""
        lines = []
        for name, server in self.toolbox.servers.items():
            if server.topic == topic and name in SITUATIONS:
                try:
                    line = await asyncio.wait_for(SITUATIONS[name](self.toolbox, name), 5.0)
                except asyncio.TimeoutError:
                    line = ""
                if line:
                    lines.append(line)
        return " ".join(lines)

    async def vocabulary(self) -> list[str]:
        """Names from every server that can offer them, for the speech recogniser."""
        names: list[str] = []
        for name in self.toolbox.servers:
            if name in VOCABULARIES:
                try:
                    for word in await asyncio.wait_for(VOCABULARIES[name](self.toolbox, name), 20.0):
                        if word not in names:
                            names.append(word)
                except asyncio.TimeoutError:
                    log.warning("actions: %s vocabulary timed out", name)
        return names

    def stats(self) -> dict[str, Any]:
        return {"enabled": self.config.enabled, "acted": self.acted, "failed": self.failed, "deferred": self.deferred,
                "last": self.last}
