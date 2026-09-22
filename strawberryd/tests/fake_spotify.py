"""A fake Spotify MCP server, shared by the tests that need a server with an adapter behind it.

Not a test module: pytest collects `test_*.py` only. `make()` gives a toolbox whose one server
is called "spotify", so the registry loads the Spotify adapter for it exactly as it would live.
"""

from __future__ import annotations

import json
from typing import Any

from strawberryd.actions import Actor
from strawberryd.config import ActionsConfig, ToolsConfig
from strawberryd.tools import Toolbox
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

    async def handle(self, name: str, arguments: dict) -> FakeResult:
        self.log.append(name)
        if self.broken == "no_device":
            return FakeResult([FakeContent(json.dumps({"error": "Resource not found.", "status": 404,
                                                       "details": "http status: 404, code: -1 - .../me/player/play:\n Player command failed: No active device found"}))])
        if self.broken == "restricted":
            return FakeResult([FakeContent(json.dumps({"error": "Permission denied. Check app scopes.", "status": 403,
                                                       "details": "http status: 403 ... Player command failed: Restriction violated, reason: UNKNOWN"}))])
        if self.broken:
            return FakeResult([FakeContent(json.dumps({"error": "error: no active device"}))])
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


def make(broken: bool = False, actions: ActionsConfig | None = None, name: str = "spotify",
         server: dict[str, Any] | None = None, mpris: Any | None = None):
    """A fake Spotify server under `name`, its toolbox (adapters loaded as they would be live),
    and an Actor over it."""
    spotify = FakeSpotify(broken)
    session = FakeSession(TOOLS, spotify.handle)
    tools = ToolsConfig(servers={name: server or {"topic": "music", "command": "spotify"}}, preconnect=False)
    toolbox = Toolbox(tools, connect=make_connect({(server or {}).get("command", "spotify"): session}))
    return spotify, toolbox, Actor(actions or ActionsConfig(), toolbox, mpris=mpris)


def fake_gate(toolbox: Toolbox):
    """The gate the daemon would build for this toolbox: a fake embedder, and the phrases the
    configured servers' adapters bring with them."""
    from strawberryd.adapters import gate_examples
    from strawberryd.config import GateConfig
    from strawberryd.systemone import Gate
    from tests.test_systemone import FakeEmbedder

    return Gate(GateConfig(query_prefix="", document_prefix=""), embedder=FakeEmbedder(),
                examples=gate_examples(toolbox.adapters))
