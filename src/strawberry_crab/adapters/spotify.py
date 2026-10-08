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
    said_reflexes   "I like this", "save this song", "add this to my favourites": the playing track
                    into Liked Songs; "play my favourites": the Liked Songs, shuffled. Read off the
                    sentence's words (the gate has no option for either), in about a second.
    clarify_error   its refusals by their `code`, in words a model reads right; several playlists
                    matching a name becomes a question back, naming at most three.
    gate_examples   the phrases only a library server can act on ("add this to my favourites").
    common_tools    the fourteen of its twenty-six worth keeping when the context is tight.
    guide           which of its tools a "like", a favourites or a playlist sentence means.
    nudge           a line under a playlist or favourites sentence the reflexes did not take.

The user's favourites are Spotify's Liked Songs: the server once kept a separate local list
with tools of its own, and that list is gone.

The facts are written here, in one plain sentence each: a 1B model will not reliably carry a
track name or a number out of a JSON result, so what she says never depends on it (§8b).
"""

from __future__ import annotations

import asyncio
import json
import re
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


# Her own words for the failures that have one, by the server's `code` (spotify-mcp's errors are
# {"error": <one sentence>, "code": <category>, "status"?, "details"?}). The rest say the server's
# own sentence, tidied (`_spoken`).
SAID = {
    "network": "I can't reach Spotify right now",
    "auth": "Spotify needs signing in again",
    "no_active_device": "Spotify has no active device; open Spotify on the computer or phone first",
    "restricted": "Spotify won't do that right now (already doing it, or the device refuses)",
}
# A sentence of the server's that is an instruction for a terminal or a model, not for the user's ears.
NOT_SPOKEN = re.compile(r"--login|device_id|get_devices|force=|\bpass\b|\bID\b", re.IGNORECASE)
SEVERAL = re.compile(r"^Several playlists match (?:'(?P<asked>.*?)')?:? ?(?P<names>.*?)(?: and \d+ more)?\. Which one\?$")
# The same refusal once `clarify_error` has put it in words for a model (a reflex reads it after).
SEVERAL_TOLD = re.compile(r"^Several of the user's playlists match that name: (?P<names>.*?)\. Nothing was done\.")


def code_of(data: dict[str, Any]) -> str:
    """The error's category: the server's `code`, or for an older server without one, read off its
    wording the way it used to be (Spotify's own `details`, then the sentence)."""
    code = data.get("code")
    if isinstance(code, str) and code:
        return code
    details, error = str(data.get("details", "")), str(data.get("error", "")).lower()
    if "No active device" in details:
        return "no_active_device"
    if "Restriction violated" in details:
        return "restricted"
    if data.get("status") == 403:
        return "forbidden_403"
    if any(w in error for w in ("could not reach spotify", "check the internet", "did not answer in time")):
        return "network"
    if any(w in error for w in ("--login", "not signed in", "sign-in has expired", "rejected the sign-in")):
        return "auth"
    return ""


def several(text: str) -> list[str]:
    """The playlist names of a "Several playlists match 'x': A, B, C. Which one?" refusal (ids off)."""
    text = " ".join(str(text).split())
    told = SEVERAL_TOLD.match(text)
    if told:
        return [n for n in re.split(r", | or ", told.group("names")) if n]
    match = SEVERAL.match(text)
    if not match:
        return []
    names = [re.sub(r" \(id \w+\)$", "", n).strip() for n in match.group("names").split(", ")]
    return [n for n in names if n]


def one_of(names: list[str]) -> str:
    """'A, B or C': at most three, each once, for a question she can say in one breath."""
    names = list(dict.fromkeys(names))[:3]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _spoken(text: str) -> str:
    """The server's sentence as the end of hers: one line, without the sentences meant for a
    terminal or a model, no final stop (hers adds one), and lower case after her 'but'."""
    text = " ".join(str(text).split()).removeprefix("error: ").removeprefix("Error: ")
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept = [one for one in sentences if not NOT_SPOKEN.search(one)] or sentences[:1]
    text = " ".join(kept).rstrip(" .")
    first = text.split(" ", 1)[0]
    if first not in ("Spotify", "I", "Premium") and text[1:2].islower():
        text = text[:1].lower() + text[1:]
    return text[:160]


def _failed(verb: str, result: ToolResult) -> Outcome:
    data = _json(result)
    detail = (data.get("error") or result.text.strip().splitlines()[0][:160]) if result.text else "no answer"
    code = code_of(data)
    names = several(str(data.get("error", "")))
    if names:
        said = f"several playlists match: {one_of(names)}. Which one?"
    elif code in SAID:
        said = SAID[code]
    elif code == "forbidden_403":
        said = "Spotify says that's not allowed for this app"
    elif code or "spotify" in str(detail).lower():
        said = _spoken(detail)       # the server's own sentence: it names Spotify, or says plainly what failed
    else:
        said = f"Spotify said: {_spoken(detail)[:120]}"
    end = "" if said.endswith(("?", "!", ".")) else "."
    return Outcome(f"tried to {verb}", f"I tried to {verb}, but {said}{end}", False, (result,))


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


def _label(text: Any) -> str:
    """spotify-mcp's "Blue Monday – New Order" as she says it: "Blue Monday by New Order"."""
    name, dash, artists = str(text or "").rpartition(" – ")
    return f"{name} by {artists}" if dash and name and artists else str(text or "it")


async def like(toolbox: Toolbox, server: str) -> Outcome:
    """'I like this', 'save this song', 'add this to my favourites': the playing track into Liked
    Songs, once (the server skips a track that is already there). Easy to undo, so not a careful tool."""
    result = await toolbox.call(server, "like_current")
    if not result.ok:
        return _failed("like it", result)
    data = _json(result)
    if data.get("already_liked"):
        return Outcome("checked the liked songs", f"Already in your Liked Songs: {_label(data['already_liked'])}.",
                       True, (result,))
    fact = f"Liked: {_label(data['liked'])}." if data.get("liked") else "Liked."
    return Outcome("liked the track", fact, True, (result,))


async def play_liked(toolbox: Toolbox, server: str) -> Outcome:
    """'Play my favourites': the Liked Songs, shuffled. The server starts a page of them as a track
    list (Spotify will not start Liked Songs as a context); the first one is the one it sent first."""
    result = await toolbox.call(server, "play_liked", {"shuffle": True})
    if not result.ok:
        return _failed("play your Liked Songs", result)
    first = _json(result).get("first")
    fact = f"Playing your Liked Songs, shuffled, starting with {_label(first)}." if first else (
        "Playing your Liked Songs, shuffled.")
    return Outcome("played the liked songs", fact, True, (result,))


# Whole sentences that mean "like the song that is playing", in the words people use for it. The
# gate has no option for it (a new one would mean retraining its head), and these are plain enough
# to read by their words: the gate reads "I like this" as chat and "save this song" as a library
# request, and either way the user wants Liked Songs (`Adapter.said_reflexes`). Favourites are Liked
# Songs too: "add this to my favourites" was answered "added" with no call when it went to the
# thinker. Anything longer or with a name in it ("I like this better", "save this to my gym
# playlist", "add this to my favourites playlist") goes to the thinker.
_LIKED = r"(?:liked\s+songs|library|favou?rites)"
# "Hey, could you please …", "… for later, please": the wrapping that leaves the request the same.
# Qwen took "for later" as a reason not to call: "hey could you save this song to my favourites for
# later" was answered "Saved" with no call 4 times in 6.
_ASK = r"(?:(?:hey|ok|okay|oh)\s+)?(?:strawberry\s+)?(?:(?:can|could|will)\s+you\s+)?(?:please\s+)?"
_POLITE = r"(?:\s+(?:please|for\s+me|for\s+later|strawberry|now|thanks|thank\s+you))*"
LIKE_SENTENCE = re.compile(
    rf"""^{_ASK}(?:
        i\s+(?:really\s+|do\s+)?like\s+(?:this|that|it)(?:\s+(?:one|song|track|tune))?
      | like\s+(?:this|that|it)(?:\s+(?:one|song|track|tune))?
      | favou?rite\s+(?:this|that|it)(?:\s+(?:one|song|track|tune))?
      | (?:save|keep)\s+(?:this|that|it)(?:\s+(?:one|song|track|tune))?(?:\s+(?:to|in)\s+(?:my\s+)?{_LIKED})?
      | (?:add|put)\s+(?:this|that|it)(?:\s+(?:one|song|track|tune))?\s+(?:to|in|into)\s+(?:my\s+)?{_LIKED}
    ){_POLITE}$""",
    re.IGNORECASE | re.VERBOSE,
)
# "Play my favourites": the Liked Songs, shuffled. Whole sentences only, as above: "play my
# favourites playlist" is a playlist, and "play my favourite songs from last month" goes to the thinker.
PLAY_LIKED_SENTENCE = re.compile(
    rf"""^{_ASK}
        (?:play|put\s+on|shuffle|start)\s+(?:some\s+(?:of\s+)?)?(?:my|the)\s+
        (?:favou?rites|favou?rite\s+(?:songs|tracks|music)|liked\s+(?:songs|tracks)|saved\s+(?:songs|tracks))
    {_POLITE}$""",
    re.IGNORECASE | re.VERBOSE,
)


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[^\w'\s]+", " ", text or "").split())


def says_like(text: str) -> bool:
    """The whole sentence asks to like what is playing ("I like this", "save this song, please")."""
    return bool(LIKE_SENTENCE.match(_plain(text)))


def says_play_liked(text: str) -> bool:
    """The whole sentence asks to play the user's favourites ("play my favourites", "play my liked songs")."""
    return bool(PLAY_LIKED_SENTENCE.match(_plain(text)))


# A sentence about one of the user's playlists by name. Measured live (qwen3.8:27b, a fake server with
# the real schemas): with the guide alone, "take this off my gym playlist" got a reply that it was
# done and no call 4/5, "play my running playlist" 3/6. A line under the sentence (per sentence, so
# the cached prompt above it stays the same) says what it asks for and that only a call does it.
_THIS = r"(?:this|it|that)(?:\s+(?:one|song|track|tune))?"
_NAME = r"(?:my|the)\s+(?:playlist\s+(?:called\s+)?(?P<called>[\w' &-]+?)(?:\s+(?:please|for\s+me|now))*$|(?P<name>[\w' &-]+?)\s+playlist)"
ADD_TO_PLAYLIST = re.compile(rf"\b(?:add|put|save|stick|throw)\s+{_THIS}\s+(?:on|in|into|to|onto)\s+{_NAME}\b",
                             re.IGNORECASE)
OFF_PLAYLIST = re.compile(rf"\b(?:take|remove|delete|drop|get)\s+{_THIS}\s+(?:off|out\s+of|from)\s+{_NAME}\b",
                          re.IGNORECASE)
PLAY_PLAYLIST = re.compile(rf"\b(?:play|put\s+on|start|shuffle)\s+{_NAME}\b", re.IGNORECASE)
# The favourites in a sentence the reflexes did not take ("could you add this one to my favourites").
_FAVOURITES = r"(?:my|the)\s+(?:favou?rites|favou?rite\s+(?:songs|tracks|music)|liked\s+(?:songs|tracks))\b(?!\s+playlist)"
ADD_TO_FAVOURITES = re.compile(rf"\b(?:add|put|save|stick|throw)\s+{_THIS}\s+(?:on|in|into|to|onto)\s+{_FAVOURITES}",
                               re.IGNORECASE)
PLAY_FAVOURITES = re.compile(rf"\b(?:play|put\s+on|start|shuffle)\s+(?:some\s+(?:of\s+)?)?{_FAVOURITES}", re.IGNORECASE)
FAVOURITES_LINES = (
    (ADD_TO_FAVOURITES, "They asked to save the playing track to their favourites, which are their Liked Songs: do "
                        "it with a tool call now. Say it is done only if a tool did it."),
    (PLAY_FAVOURITES, "They asked to play their favourites, which are their Liked Songs: do it with a tool call now. "
                      "Say it is playing only if a tool started it."),
)


def playlist_request(text: str, route: Any = None) -> str:
    """The line under a playlist or favourites sentence, or "": what it asks for, with the name as heard."""
    plain = " ".join(re.sub(r"[^\w'&\s-]+", " ", text or "").split())
    for pattern, asks in ((OFF_PLAYLIST, "take the playing track off their playlist"),
                          (ADD_TO_PLAYLIST, "add the playing track to their playlist"),
                          (PLAY_PLAYLIST, "play their playlist")):
        match = pattern.search(plain)
        if not match:
            continue
        if pattern is OFF_PLAYLIST and route is not None and getattr(route, "library_change", 1.0) < 0.5:
            return ""     # the removal is not offered to this sentence; nothing to point at
        name = (match.group("called") or match.group("name") or "").strip()
        return (f"They asked to {asks} '{name}': do it with a tool call now, passing the name as heard. Say it is "
                "done only if a tool did it.")
    for pattern, line in FAVOURITES_LINES:
        if pattern.search(plain):
            return line
    return ""


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
    """Names whisper should know: what is playing, playlists, recent saves.

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
    saved = await toolbox.call(server, "get_saved_tracks", {"limit": 50}, result_chars=whole)
    if saved.ok:
        for item in _json(saved).get("tracks", []):
            add(*item.get("artists", []))
    return names


def clarify_error(text: str) -> str:
    """The server's refusals in words a model reads right, by its `code` (from an older server
    without one, by Spotify's own `details`). An older server said "Permission denied. Check app
    scopes." for a 403 that is really "Restriction violated": already playing, already paused, or
    the device does not allow the command, and a model reading that reported a permissions problem.
    Several playlists matching a name is the one time she asks a question back. The body stays an
    error body; only `error` is rewritten."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(data, dict):
        return text
    code = code_of(data)
    names = several(str(data.get("error", "")))
    if names:
        data["error"] = (f"Several of the user's playlists match that name: {one_of(names)}. Nothing was done. Ask the "
                         "user which one they mean, naming these; this is the one question you may ask. Do not pick one.")
    elif code == "restricted":
        data["error"] = ("Spotify refused the command: restriction violated. Usually the player is already in that "
                         "state (already playing or paused) or the active device does not allow it. Not a permissions problem.")
    elif code == "no_active_device":
        data["error"] = ("Spotify has no active device: no Spotify app is open and active on any of the user's devices, so "
                         "nothing can be played from here until they open one. Do not retry; tell the user.")
    elif code == "network":
        data["error"] = "Spotify cannot be reached right now (a network problem). Do not retry; tell the user."
    elif code == "auth":
        data["error"] = ("Spotify needs signing in again, which the user does on the computer. Do not retry; tell the "
                         "user Spotify needs signing in again.")
    elif code == "forbidden_403" and "Forbidden" in str(data.get("details", "")):
        data["error"] = "Spotify forbids this for the app (403 Forbidden); it cannot be done from here."
    else:
        return text
    # The category stays with the body: a reflex reads this rewritten error after the toolbox (_failed).
    data.setdefault("code", code or "bad_request")
    return json.dumps(data, ensure_ascii=False)


# What the brain is told about this server's tools, once, in the system prompt (the same for every
# sentence: Ollama's prompt cache, §8b). The playlist tools take a name as the user said it and the
# server matches it, misheard words included, so nothing needs looking up first.
# Worded as calls to make, and with no clause of its own for "play my X playlist": a version that
# described the tools ("'play my <name> playlist' is play with playlist set to the name") had Qwen
# answer that sentence with no call at all 3/3, claiming it was on, and one with "for 'play my X
# playlist' call play with playlist X" still 3/6; play's own schema says it takes a playlist (6/6).
# The user's favourites are their Liked Songs: the clause naming play_liked is left out for a server
# without it (`guide_for`).
PLAY_LIKED_CLAUSE = " For 'play my favourites' call play_liked."
GUIDE = (
    "Spotify tools: the user's favourites are their Liked Songs. For 'I like this', 'save this song' or 'add this to "
    "my favourites', call like_current." + PLAY_LIKED_CLAUSE + " For 'add this to my X playlist' call "
    "add_current_to_playlist with playlist X; for 'take this off my X playlist' call remove_from_playlist with "
    "playlist X and track 'current'. Pass a playlist name as the user said it, also to play; the server finds the "
    "playlist, misheard names included."
)


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
    said_reflexes: dict[str, tuple[str, Reflex]] = {"like": ("like_current", like),
                                                     "play_liked": ("play_liked", play_liked)}
    # Phrases only a library server can act on. The generic music examples stay in systemone.py:
    # skipping, pausing and asking what is playing are core now, over MPRIS, with no server at all.
    gate_examples = {
        "kind": {"request": ["save this song", "like this track", "add this to my favourites",
                             "put this on my running playlist"]},
        "music_tool": {"other": ["add this to my favourites", "play my running playlist", "save this song",
                                 "remove this from my liked songs"]},
    }
    # Twenty-six tools are ~2300 prompt tokens (§8b, ADAPTERS.md); these fourteen (~1250) are the ones a
    # sentence about music usually needs, and they go first when [thinker] max_tools has to cut the list.
    common_tools = ("search", "play", "pause", "next", "previous", "get_current_track", "like_current",
                    "add_current_to_playlist", "find_playlist", "play_liked", "add_to_queue", "get_playlists",
                    "set_volume", "shuffle")
    guide = GUIDE

    def guide_for(self, tools: list[str]) -> str:
        # An older server without the by-name tools gets no paragraph about them, nor about play_liked.
        if "like_current" not in tools:
            return ""
        return GUIDE if "play_liked" in tools else GUIDE.replace(PLAY_LIKED_CLAUSE, "")

    def wanted(self, text: str, route: Any) -> bool | None:
        # A playlist or favourites sentence gets a line under it (`nudge`); every other one is offered as usual.
        return True if playlist_request(text, route) else None

    def nudge(self, text: str, route: Any) -> str:
        return playlist_request(text, route)

    def said_reflex(self, text: str, route: Any) -> str | None:
        if says_like(text):
            return "like"
        return "play_liked" if says_play_liked(text) else None

    async def situation(self, toolbox: Toolbox, server: str) -> str:
        return await situation(toolbox, server)

    async def vocabulary(self, toolbox: Toolbox, server: str) -> list[str]:
        return await vocabulary(toolbox, server)

    def clarify_error(self, text: str) -> str:
        return clarify_error(text)


SPOTIFY = SpotifyAdapter()
