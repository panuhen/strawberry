"""strawberryd: the always-running half of the Strawberry mascot.

Short-lived event scripts POST to it over HTTP; it holds the one websocket to the
Godot widget and turns every event into one performance message (WIRING.md §0).
"""

__version__ = "0.1.0"
