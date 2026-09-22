"""The Spotify adapter: the worked example of ADAPTERS.md.

It lights up when the config lists a Spotify MCP server — `[tools.servers.spotify]`, or any
name with `adapter = "spotify"`. The server itself is a community wrapper around the Spotify
Web API that the user installs and authorises; nothing here ships it and nothing here is in
the shipped config.

What it adds over the plain tool list:

    reflexes        skip/previous/pause/resume/volume/now playing, done in about a second.
                    They beat the MPRIS ones (actions.Actor.reflex_for prefers a configured
                    server) because the Web API can name the next track before the desktop
                    player's metadata catches up, and because volume here is the *device's*.
    situation       "Now playing on Spotify: …", so "this song" means something to the thinker.
    vocabulary      artists and playlist names for whisper: 'Daft Punk' came through as
                    Dothpunk, Duff Punk and Dove Punk before the recogniser was told the names.
    clarify_error   Spotify labels several refusals "Permission denied. Check app scopes."
    gate_examples   the phrases only a library server can act on ("add this to my favourites").
    common_tools    the ten of its twenty-five worth keeping when the context is tight.

The facts are written here, in one plain sentence each: a 1B model will not reliably carry a
track name or a number out of a JSON result, so what she says never depends on it (§8b).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ..actions import Outcome, Reflex
from ..tools import Toolbox, ToolResult
from .base import Adapter


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
    data = _json(result)
    detail = (data.get("error") or result.text.strip().splitlines()[0][:160]) if result.text else "no answer"
    detail = str(detail).removeprefix("error: ")
    if "No active device" in str(data.get("details", "")):
        said = "Spotify has no active device; open Spotify on the computer or phone first"
    elif "Restriction violated" in str(data.get("details", "")):
        said = "Spotify won't do that right now (already doing it, or the device refuses)"
    elif data.get("status") == 403:
        said = "Spotify says that's not allowed for this app"
    else:
        said = f"Spotify said: {detail[:120]}"
    return Outcome(f"tried to {verb}", f"I tried to {verb}, but {said}.", False, (result,))


async def _current(toolbox: Toolbox, server: str) -> tuple[str, dict[str, Any], ToolResult]:
    now = await toolbox.call(server, "get_current_track")
    data = _json(now) if now.ok else {}
    return _track(data), data, now


async def now_playing(toolbox: Toolbox, server: str) -> Outcome:
    track, data, result = await _current(toolbox, server)
    if not result.ok:
        return _failed("look at the player", result)
    if not track:
        return Outcome("looked at the player", "Nothing is playing right now.", True, (result,))
    album = (data.get("track") or {}).get("album")
    tail = f", from {album}" if album and album not in track else ""
    verb = "That's" if data.get("playing", True) else "Paused on"
    return Outcome("looked at the player", f"{verb} {track}{tail}.", True, (result,))


async def _step(toolbox: Toolbox, server: str, tool: str, verb: str, did: str, lead: str) -> Outcome:
    result = await toolbox.call(server, tool)
    if not result.ok:
        return _failed(verb, result)
    await asyncio.sleep(0.6)  # Spotify reports the old track for a moment
    track, _data, now = await _current(toolbox, server)
    return Outcome(did, f"{lead} {track}." if track else f"{lead.rstrip(':')}.", True, (result, now))


async def skip(toolbox: Toolbox, server: str) -> Outcome:
    return await _step(toolbox, server, "next", "skip", "skipped to the next track", "Skipped. Now")


async def previous(toolbox: Toolbox, server: str) -> Outcome:
    return await _step(toolbox, server, "previous", "go back", "went back to the previous track", "Back to")


async def pause(toolbox: Toolbox, server: str) -> Outcome:
    result = await toolbox.call(server, "pause")
    if not result.ok:
        return _failed("pause", result)
    return Outcome("paused the music", "Paused.", True, (result,))


async def resume(toolbox: Toolbox, server: str) -> Outcome:
    track, data, now = await _current(toolbox, server)
    if now.ok and track and data.get("playing", False):
        return Outcome("checked the player", f"It's already playing: {track}.", True, (now,))  # play() would 403
    return await _step(toolbox, server, "play", "resume", "started the music again", "Playing again:")


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


async def situation(toolbox: Toolbox, server: str) -> str:
    """One line of context for the thinker: 'this song' means whatever is playing now."""
    track, data, result = await _current(toolbox, server)
    if not result.ok:
        return ""
    if not track:
        return "Nothing is playing on Spotify right now."
    album = (data.get("track") or {}).get("album")
    state = "Now playing" if data.get("playing", True) else "Paused"
    return f"{state} on Spotify: {track}" + (f" (album: {album})." if album else ".")


async def vocabulary(toolbox: Toolbox, server: str) -> list[str]:
    """Names whisper should know: what is playing, playlists, favourites, recent saves.

    Order matters: the list is cut to voice.max_hotwords, so the most likely names come first.
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
    # Playlists next: you ask for them by name ("play Acid Techno") and they are few.
    playlists = await toolbox.call(server, "get_playlists", {"limit": 50}, result_chars=whole)
    if playlists.ok:
        for item in _json(playlists).get("playlists", []):
            name = item.get("name", "")
            if any(ch.isalpha() for ch in name):  # emoji-only playlist names help nobody
                add(name)
    favourites = await toolbox.call(server, "get_favorites", result_chars=whole)
    if favourites.ok:
        for item in _json(favourites).get("favorites", []):
            add(*item.get("artists", []))
    saved = await toolbox.call(server, "get_saved_tracks", {"limit": 50}, result_chars=whole)
    if saved.ok:
        for item in _json(saved).get("tracks", []):
            add(*item.get("artists", []))
    return names


def clarify_error(text: str) -> str:
    """Spotify's server says "Permission denied. Check app scopes." for a 403 that is really
    "Restriction violated": already playing, already paused, or the device does not allow the
    command. A model reading that would report a permissions problem to the user, so the body is
    rewritten before anyone reads it (it stays an error body)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(data, dict):
        return text
    details = str(data.get("details", ""))
    if "Restriction violated" in details:
        data["error"] = ("Spotify refused the command: restriction violated. Usually the player is already in that "
                         "state (already playing or paused) or the active device does not allow it. Not a permissions problem.")
        return json.dumps(data, ensure_ascii=False)
    if "No active device" in details:
        data["error"] = ("Spotify has no active device: no Spotify app is open and active on any of the user's devices, so "
                         "nothing can be played from here until they open one. Do not retry; tell the user.")
        return json.dumps(data, ensure_ascii=False)
    if data.get("status") == 403 and "Forbidden" in details:
        data["error"] = "Spotify forbids this for the app (403 Forbidden); it cannot be done from here."
        return json.dumps(data, ensure_ascii=False)
    return text


class SpotifyAdapter(Adapter):
    name = "spotify"
    server_names = ("spotify",)
    reflexes: dict[str, Reflex] = {
        "skip": skip,
        "previous": previous,
        "pause": pause,
        "resume": resume,
        "volume_down": _volume(-15),
        "volume_up": _volume(15),
        "now_playing": now_playing,
    }
    # Phrases only a library server can act on. The generic music examples stay in systemone.py:
    # skipping, pausing and asking what is playing are core now, over MPRIS, with no server at all.
    gate_examples = {
        "kind": {"request": ["save this song", "like this track", "add this to my favourites",
                             "put this on my running playlist"]},
        "music_tool": {"other": ["add this to my favourites", "play my running playlist", "save this song",
                                 "remove this from my liked songs"]},
    }
    # Twenty-five tools is ~360 prompt tokens (§8b); these ten are the ones a sentence about
    # music usually needs, and they go first when [thinker] max_tools has to cut the list.
    common_tools = ("search", "play", "pause", "next", "previous", "get_current_track", "add_to_queue",
                    "get_playlists", "set_volume", "shuffle")

    async def situation(self, toolbox: Toolbox, server: str) -> str:
        return await situation(toolbox, server)

    async def vocabulary(self, toolbox: Toolbox, server: str) -> list[str]:
        return await vocabulary(toolbox, server)

    def clarify_error(self, text: str) -> str:
        return clarify_error(text)


SPOTIFY = SpotifyAdapter()
