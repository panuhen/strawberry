"""A fake Spotify MCP server, shared by the tests that need a server with an adapter behind it.

Not a test module: pytest collects `test_*.py` only. `make()` gives a toolbox whose one server
is called "spotify", so the registry loads the Spotify adapter for it exactly as it would live.
"""

from __future__ import annotations

import json
from typing import Any

from strawberry_crab.actions import Actor
from strawberry_crab.config import ActionsConfig, ToolsConfig
from strawberry_crab.tools import Toolbox
from tests.test_tools import FakeContent, FakeResult, FakeSession, FakeTool, make_connect

TRACKS = [
    {"name": "Feeling Good", "artists": ["Nina Simone"], "album": "I Put a Spell on You"},
    {"name": "Blue Monday", "artists": ["New Order"], "album": "Power, Corruption & Lies"},
]


class FakeSpotify:
    """Enough of the Spotify MCP server for the reflexes: a queue position, a volume, pause."""

    def __init__(self, broken: bool = False) -> None:
        self.index = 0
        self.playing = True
        self.volume = 80
        self.broken = broken
        self.log: list[str] = []
        self.liked: list[str] = []
        self.added: list[tuple] = []
        self.removed: list[tuple] = []

    async def handle(self, name: str, arguments: dict) -> FakeResult:
        self.log.append(name)
        if self.broken == "no_device":
            return FakeResult([FakeContent(json.dumps({"error": "Resource not found.", "status": 404,
                                                       "details": "http status: 404, code: -1 - .../me/player/play:\n Player command failed: No active device found"}))])
        if self.broken == "restricted":
            return FakeResult([FakeContent(json.dumps({"error": "Permission denied. Check app scopes.", "status": 403,
                                                       "details": "http status: 403 ... Player command failed: Restriction violated, reason: UNKNOWN"}))])
        if self.broken in NEW_ERRORS:
            # The server's current shape: one sentence and a code, with the MCP error flag set.
            return FakeResult([FakeContent(json.dumps(NEW_ERRORS[self.broken]))], is_error=True)
        if self.broken:
            return FakeResult([FakeContent(json.dumps({"error": "error: no active device"}))])
        label = f"{TRACKS[self.index]['name']} – {', '.join(TRACKS[self.index]['artists'])}"
        if name == "like_current":
            if label in self.liked:
                return FakeResult([FakeContent(json.dumps({"already_liked": label}))])
            self.liked.append(label)
            return FakeResult([FakeContent(json.dumps({"liked": label}))])
        if name == "add_current_to_playlist":
            self.added.append((arguments.get("playlist"), label))
            return FakeResult([FakeContent(json.dumps({"added": label, "playlist": arguments.get("playlist")}))])
        if name == "remove_from_playlist":
            self.removed.append((arguments.get("playlist"), arguments.get("track")))
            return FakeResult([FakeContent(json.dumps({"removed": label, "playlist": arguments.get("playlist")}))])
        if name == "find_playlist":
            return FakeResult([FakeContent(json.dumps({"playlists": [{"name": "Running", "id": "x" * 22,
                                                                      "uri": "spotify:playlist:" + "x" * 22,
                                                                      "owned": True, "tracks": 12}]}))])
        if name == "next":
            self.index = (self.index + 1) % len(TRACKS)
            return FakeResult([FakeContent(json.dumps({"success": True, "message": "Skipped to next track"}))])
        if name == "previous":
            self.index = (self.index - 1) % len(TRACKS)
            return FakeResult([FakeContent(json.dumps({"success": True}))])
        if name == "pause":
            self.playing = False
            return FakeResult([FakeContent(json.dumps({"success": True}))])
        if name == "play":
            self.playing = True
            return FakeResult([FakeContent(json.dumps({"success": True}))])
        if name == "get_current_track":
            return FakeResult([FakeContent(json.dumps({"playing": self.playing, "track": TRACKS[self.index]}))])
        if name == "get_devices":
            return FakeResult([FakeContent(json.dumps({"devices": [{"name": "x", "is_active": False, "volume": 100},
                                                                   {"name": "Desk-PC", "is_active": True, "volume": self.volume}]}))])
        if name == "set_volume":
            self.volume = arguments["volume"]
            return FakeResult([FakeContent(json.dumps({"success": True}))])
        if name == "get_favorites":
            return FakeResult([FakeContent(json.dumps({"favorites": [{"name": "Around the World", "artists": ["Daft Punk"]}]}))])
        if name == "get_saved_tracks":
            padding = [{"name": f"Filler {i}", "artists": ["New Order"], "album": "x" * 60} for i in range(40)]  # > result_chars
            return FakeResult([FakeContent(json.dumps({"tracks": padding + [{"name": "Feeling Good", "artists": ["Nina Simone"]},
                                                                            {"name": "Last", "artists": ["Erik Satie"]}]}))])
        if name == "get_playlists":
            return FakeResult([FakeContent(json.dumps({"playlists": [{"name": "Acid Techno"}, {"name": "🥲"}]}))])
        raise KeyError(name)


TOOLS = [FakeTool(n) for n in ("next", "previous", "pause", "play", "get_current_track", "get_devices", "set_volume",
                               "get_favorites", "get_saved_tracks", "get_playlists")]
# The by-name library tools the server gained: like the playing track, a playlist by its name.
LIBRARY_TOOLS = [FakeTool(n) for n in ("like_current", "add_current_to_playlist", "find_playlist",
                                       "remove_from_playlist", "create_playlist", "favorite_current")]

# spotify-mcp's error shape: {"error": <one speakable sentence>, "code": <category>, "status"?, "details"?}.
NEW_ERRORS = {
    "network": {"error": "Could not reach Spotify. Check the internet connection and try again.", "code": "network"},
    "auth": {"error": "The Spotify sign-in has expired. Run spotify-mcp --login in a terminal to sign in again.",
             "code": "auth"},
    "device": {"error": "No Spotify device is active. Open Spotify on a computer or phone, or name a device_id from "
                        "get_devices.", "code": "no_active_device", "status": 404,
               "details": "Player command failed: No active device found"},
    "rate_limited": {"error": "Spotify is limiting requests right now. Try again in 30 seconds.", "code": "rate_limited",
                     "status": 429},
    "nothing": {"error": "Nothing is playing right now.", "code": "not_found"},
    "premium": {"error": "That needs Spotify Premium.", "code": "premium_required", "status": 403,
                "details": "Player command failed: Premium required"},
    "several": {"error": "Several playlists match 'gym': Gym, Gym Mix (id 0123456789abcdefABCDEF), Gym Mix (id "
                         "abcdefABCDEF0123456789), Old Gym, Gym 2 and 3 more. Which one?", "code": "bad_request"},
    # An older server: the same sentence and no code.
    "old_network": {"error": "Could not reach Spotify. Check the internet connection and try again."},
}


def make(broken: bool | str = False, actions: ActionsConfig | None = None, name: str = "spotify",
         server: dict[str, Any] | None = None, mpris: Any | None = None, library: bool = False):
    """A fake Spotify server under `name`, its toolbox (adapters loaded as they would be live),
    and an Actor over it. `library` adds the by-name library tools (LIBRARY_TOOLS)."""
    spotify = FakeSpotify(broken)
    session = FakeSession(TOOLS + (LIBRARY_TOOLS if library else []), spotify.handle)
    tools = ToolsConfig(servers={name: server or {"topic": "music", "command": "spotify"}}, preconnect=False)
    toolbox = Toolbox(tools, connect=make_connect({(server or {}).get("command", "spotify"): session}))
    return spotify, toolbox, Actor(actions or ActionsConfig(), toolbox, mpris=mpris)


def fake_gate(toolbox: Toolbox):
    """The gate the daemon would build for this toolbox: a fake embedder, and the phrases the
    configured servers' adapters bring with them."""
    from strawberry_crab.adapters import gate_examples
    from strawberry_crab.config import GateConfig
    from strawberry_crab.systemone import Gate
    from tests.test_systemone import FakeEmbedder

    return Gate(GateConfig(query_prefix="", document_prefix=""), embedder=FakeEmbedder(),
                examples=gate_examples(toolbox.adapters))
