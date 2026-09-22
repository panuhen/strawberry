"""What an adapter is (ADAPTERS.md, WIRING.md §8b).

The core knows how to connect an MCP server, list its tools and hand them to the brain. That
is all a server needs to be useful. An *adapter* is the optional extra a particular server
earns once you use it every day:

    reflexes        the gate's bare commands done without a model, in that server's tool names
    situation       one line the thinker is told before it starts ("Now playing on Spotify: …")
    vocabulary      names the speech recogniser should know (artists, playlists)
    clarify_error   this server's confusing refusals, reworded before a model reads them
    gate_examples   extra phrases for the gate's questions, in that server's vocabulary
    common_tools    the tools worth keeping first when the brain's context is tight

Every one of them is optional; the base class answers "nothing to add" to all of them. An
adapter is loaded only when a configured server matches it, by name or by an explicit
`adapter = "…"` in `[tools.servers.<name>]`, so an unused adapter costs nothing and shapes
nothing she says.
"""

from __future__ import annotations

from typing import Any

from ..actions import Reflex
from ..tools import Toolbox


class Adapter:
    """Subclass, set what you have, leave the rest. See `spotify.py` for a worked example."""

    #: the adapter's own name, and what `adapter = "…"` in the config selects
    name: str = ""
    #: server names (as written in `[tools.servers.<name>]`) this adapter recognises on sight
    server_names: tuple[str, ...] = ()
    #: the gate's tool option ("skip", "pause", …) -> a reflex over this server's tools
    reflexes: dict[str, Reflex] = {}
    #: question -> option -> extra phrases, merged into the gate's examples at start
    gate_examples: dict[str, dict[str, list[str]]] = {}
    #: the tools the brain should keep first when there are more than `[thinker] max_tools`
    common_tools: tuple[str, ...] = ()

    def matches(self, server_name: str, server_config: dict[str, Any]) -> bool:
        """Is this adapter for that configured server? An explicit `adapter` key decides alone."""
        wanted = str(server_config.get("adapter", "")).strip().lower()
        if wanted:
            return wanted == self.name
        return server_name.strip().lower() in self.server_names

    async def situation(self, toolbox: Toolbox, server: str) -> str:
        """One plain line of what is going on, or "" when there is nothing to say."""
        return ""

    async def vocabulary(self, toolbox: Toolbox, server: str) -> list[str]:
        """Names for the speech recogniser, most likely first (the list is cut to max_hotwords)."""
        return []

    def clarify_error(self, text: str) -> str:
        """Rewrite this server's error body so a model reads it right; return it unchanged if not."""
        return text

    def flat_gate_examples(self) -> dict[str, list[str]]:
        """`gate_examples` in the gate's own key shape: {"kind.request": [...]}."""
        return {f"{question}.{option}": list(phrases)
                for question, options in self.gate_examples.items()
                for option, phrases in options.items()}
