"""strawberry: the desktop mascot's Python half (the daemon, the doorways, the tray and the CLI).

The daemon (`strawberryd`) is the always-running part: event scripts POST to it over HTTP; it holds the one websocket to the
Godot widget and turns every event into one performance message (WIRING.md §0).
"""

__version__ = "0.1.0"
