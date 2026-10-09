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
    {"name": "Feeling Good", "artists": ["Nina Simone"], "album": "I Put a Spell on You",
     "uri": "spotify:track:" + "1" * 22},
    {"name": "Blue Monday", "artists": ["New Order"], "album": "Power, Corruption & Lies",
     "uri": "spotify:track:" + "2" * 22},
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
        self.played_liked: list[dict] = []

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
            named = [t for t in TRACKS if t["uri"] == arguments.get("track")]   # a URI, or "current"
            if named:
                label = f"{named[0]['name']} – {', '.join(named[0]['artists'])}"
            playlist = str(arguments.get("playlist") or "")
            playlist = "Running" if playlist.startswith("spotify:playlist:") else playlist   # find_playlist's one
            return FakeResult([FakeContent(json.dumps({"removed": label, "playlist": playlist}))])
        if name == "remove_saved_tracks":
            self.removed.append(("liked", tuple(arguments.get("track_ids") or ())))
            return FakeResult([FakeContent(json.dumps({"success": True, "message": "Removed 1 track(s) from your "
                                                       "library"}))])
        if name == "play_liked":
            self.played_liked.append(dict(arguments))
            self.playing = True
            return FakeResult([FakeContent(json.dumps({"success": True, "message": "Playing 50 of your 360 Liked Songs, "
                                                       "shuffled", "first": "Around the World – Daft Punk"}))])
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
        if name == "get_saved_tracks":
            padding = [{"name": f"Filler {i}", "artists": ["New Order"], "album": "x" * 60} for i in range(40)]  # > result_chars
            return FakeResult([FakeContent(json.dumps({"tracks": padding + [{"name": "Feeling Good", "artists": ["Nina Simone"]},
                                                                            {"name": "Last", "artists": ["Erik Satie"]}]}))])
        if name == "get_playlists":
            return FakeResult([FakeContent(json.dumps({"playlists": [{"name": "Acid Techno"}, {"name": "🥲"}]}))])
        raise KeyError(name)


TOOLS = [FakeTool(n) for n in ("next", "previous", "pause", "play", "get_current_track", "get_devices", "set_volume",
                               "get_saved_tracks", "get_playlists")]
# The by-name library tools the server gained: like the playing track, a playlist by its name, and
# play the Liked Songs (the user's favourites).
LIBRARY_TOOLS = [FakeTool(n) for n in ("like_current", "add_current_to_playlist", "find_playlist",
                                       "remove_from_playlist", "create_playlist", "play_liked")]

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
         server: dict[str, Any] | None = None, mpris: Any | None = None, library: bool = False,
         extra: list[FakeTool] | None = None):
    """A fake Spotify server under `name`, its toolbox (adapters loaded as they would be live),
    and an Actor over it. `library` adds the by-name library tools (LIBRARY_TOOLS), `extra` more."""
    spotify = FakeSpotify(broken)
    session = FakeSession(TOOLS + (LIBRARY_TOOLS if library else []) + list(extra or []), spotify.handle)
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


def serve() -> None:
    """The fake as a real stdio MCP server, for a throwaway daemon's end-to-end runs (never the user's
    player): `python -m tests.fake_spotify` from the repo root, as `[tools.servers.spotify]`."""
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    server = MCPServer("fake-spotify")
    spotify = FakeSpotify()
    reads = ToolAnnotations(read_only_hint=True)

    async def run(name: str, arguments: dict) -> str:
        return (await spotify.handle(name, arguments)).content[0].text

    @server.tool(name="next")
    async def next_track() -> str:
        """Skip to the next track."""
        return await run("next", {})

    @server.tool()
    async def previous() -> str:
        """Go back to the previous track."""
        return await run("previous", {})

    @server.tool()
    async def pause() -> str:
        """Pause playback."""
        return await run("pause", {})

    @server.tool()
    async def play(uri: str = "") -> str:
        """Resume playback, or play a track, album or playlist by its Spotify URI."""
        if uri:
            spotify.index = next((i for i, t in enumerate(TRACKS) if t["uri"] == uri), spotify.index)
        return await run("play", {})

    @server.tool(annotations=reads)
    async def search(query: str, type: str = "track") -> str:
        """Search the catalogue; returns tracks with their URIs."""
        return json.dumps({"tracks": TRACKS})

    @server.tool(annotations=reads)
    async def get_current_track() -> str:
        """What is playing now."""
        return await run("get_current_track", {})

    @server.tool()
    async def add_to_queue(uri: str) -> str:
        """Add a track to the queue by its URI."""
        return json.dumps({"success": True})

    @server.tool()
    async def set_volume(volume: int) -> str:
        """Set the volume, 0-100."""
        return await run("set_volume", {"volume": volume})

    server.run("stdio")


if __name__ == "__main__":
    serve()
