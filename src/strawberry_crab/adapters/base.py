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
    tools           the only tools of the server the brain is offered, and shorter schemas for them
    shape_result    a result made compact before it is cut to result_chars
    log_result      what the journal says of a call, when the result itself must stay out of it
    guide           what the brain is told about these tools when they are offered (and
                    `unavailable`, when the server is configured but not answering)
    wanted          whether a sentence wants this server's tools at all, or asks for them outright
    max_calls       how many calls to its tools one sentence may make
    untrusted       its results are outside text; with `guard` and `observe`, what may follow one

Every one of them is optional; the base class answers "nothing to add" to all of them. An
adapter is loaded only when a configured server matches it, by name or by an explicit
`adapter = "…"` in `[tools.servers.<name>]`, so an unused adapter costs nothing and shapes
nothing she says.
"""

from __future__ import annotations

from typing import Any

from ..actions import Reflex
from ..tools import Toolbox, ToolSpec


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
    #: the only tools of this server the brain is offered; empty offers all of them. A filter,
    #: unlike common_tools: a tool left out here never reaches the brain
    tools: tuple[str, ...] = ()
    #: True when the server's tools only look things up and change nothing on the user's computer
    #: (web search): the thinker then keeps the rules for having no player to act through
    looks_up_only: bool = False
    #: one paragraph for the brain's rules when this server's tools are offered ("" = nothing)
    guide: str = ""
    #: one paragraph for the brain's rules when the server is configured but not answering
    unavailable: str = ""
    #: at most this many calls to this server's tools for one sentence (0: no limit beyond the
    #: thinker's max_rounds); a call over it is not made and the brain is told to answer
    max_calls: int = 0
    #: True when its results are text from strangers (web pages): once one is in a conversation,
    #: the thinker takes the user's private context out of it and refuses every other server's
    #: tools for the rest of that sentence (Thinker._run), and `guard` checks each further call
    untrusted: bool = False

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

    def shape_tool(self, spec: ToolSpec) -> ToolSpec:
        """The tool as the brain should see it: a shorter description or fewer parameters cost
        fewer prompt tokens. Returned unchanged by default."""
        return spec

    def shape_result(self, name: str, text: str, ok: bool) -> str:
        """A result made compact before it is cut to `result_chars` (the noise of a search listing
        would otherwise fill it). Returned unchanged by default."""
        return text

    def log_result(self, name: str, text: str, ok: bool) -> str | None:
        """The journal's summary of a call's result in place of its text, or None for the core's
        own line (the first 160 characters). For results that may carry what the user asked."""
        return None

    def wanted(self, text: str, route: Any) -> bool | None:
        """Does this sentence want this server's tools? True: asked for outright (offered first,
        with `nudge`); False: left out of this sentence's offer; None: no opinion, offered as usual.
        `route` is the gate's reading, None when the gate could not read it."""
        return None

    def guard(self, state: dict[str, Any], name: str, arguments: dict[str, Any]) -> str | None:
        """Before a call: None to let it run, or what the brain is told instead of running it.
        `state` is this sentence's own, kept for this server across its calls (see `observe`)."""
        return None

    def observe(self, state: dict[str, Any], name: str, arguments: dict[str, Any], text: str, ok: bool) -> None:
        """After a call: note what `guard` needs to know later in the same sentence."""

    def nudge(self, text: str, route: Any) -> str:
        """A line added under the sentence when `wanted` said True ("search first"), or ""."""
        return ""

    def flat_gate_examples(self) -> dict[str, list[str]]:
        """`gate_examples` in the gate's own key shape: {"kind.request": [...]}."""
        return {f"{question}.{option}": list(phrases)
                for question, options in self.gate_examples.items()
                for option, phrases in options.items()}
