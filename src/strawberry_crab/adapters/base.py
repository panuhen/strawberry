"""What an adapter is (ADAPTERS.md, WIRING.md §8b).

The core knows how to connect an MCP server, list its tools and hand them to the brain. That
is all a server needs to be useful. An *adapter* is the optional extra a particular server
earns once you use it every day:

    reflexes        the gate's bare commands done without a model, in that server's tool names
    said_reflexes   commands the gate has no option for, known by the sentence's own words
    situation       one line the thinker is told before it starts ("Now playing on Spotify: …")
    vocabulary      names the speech recogniser should know (artists, playlists)
    clarify_error   this server's confusing refusals, reworded before a model reads them
    gate_examples   extra phrases for the gate's questions, in that server's vocabulary
    common_tools    the tools worth keeping first when the brain's context is tight
    tools           the only tools of the server the brain is offered, and shorter schemas for them
    shape_result    a result made compact before it is cut to result_chars
    log_result      what the journal says of a call (a count and a size; the result stays out of it)
    log_detail      True: the journal may carry the first 160 characters of a result and the arguments,
                    for a server that is neither private nor foreign (off by default)
    guide           what the brain is told about these tools when they are offered (and
                    `unavailable`, when the server is configured but not answering)
    wanted          whether a sentence wants this server's tools at all, or asks for them outright
    max_calls       how many calls to its tools one sentence may make
    private, foreign, egress
                    the trust flags (trust.py, WIRING §20): its results are the user's own, its results
                    carry strangers' text, a call sends something off the machine. A foreign result
                    in a conversation brings the thinker's containment; with `guard`, `forward`,
                    `screen` and `observe`, what may follow one, and in what form it is sent
    offer           when the thinker is offered its tools: always, topic, asked (Thinker.tools)
    reflex_tools    which of its tools each reflex calls, so a tier that needs a yes hands the
                    sentence to the thinker instead (actions.Actor)
    confirm         the tools she asks about before calling, unless the config's `confirm` says
                    otherwise; `ask` words the question (and pins the call), `describe` the line
                    on her approval card, `done` the result
    risks           each tool's approval tier (read / change / sends / destructive), where it is not
                    the default: `reads` and a look-up-only server are `read`, the rest `change`

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
    #: reflexes picked by the sentence's own words (`said_reflex`), for a command the gate has no
    #: option for: name -> (the server tool it needs, the reflex). One whose tool the server does not
    #: list (an older version of it) is not used, and the sentence goes to the thinker
    said_reflexes: dict[str, tuple[str, Reflex]] = {}
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
    #: The trust flags (trust.py, WIRING §20). `private`: its results are the user's own (their library,
    #: what they play, their messages). `foreign`: its results carry text written by others (web pages,
    #: snippets, messages): once one is in a conversation, the thinker takes the user's private context
    #: out of it, refuses every private or egress server's tools but this one's, asks before anything
    #: that is not a read (Thinker._run), and `guard` checks each further call. `egress`: a call sends
    #: what it carries off the machine to someone else (a query, an address, a name others may see):
    #: once foreign text is in, a call that carries a phrase of the user's private context is refused.
    #: An adapter that sets none says its server is none of them; a server with no adapter is all three
    #: unless its config says otherwise.
    private: bool = False
    foreign: bool = False
    egress: bool = False
    #: When the thinker is offered this server's tools (Thinker.tools): "always" (every sentence; the
    #: prompt stays the same and Ollama's cache holds), "topic" (a sentence the gate reads as this
    #: server's topic, or one `wanted` says asks for it) or "asked" (only one `wanted` says asks for it,
    #: or that names the server). The config's `offer` decides over it
    offer: str = "always"
    #: True: the journal may say what this server's calls carry (the first 160 characters of a result,
    #: the arguments); only for a server that is neither private nor foreign. Off: counts and sizes
    log_detail: bool = False
    #: reflex (the gate's option, or a said reflex's name) -> the server tools it calls. A reflex whose
    #: tools include one that waits for a yes ([approvals] risk raised it, or its confirm list) is not
    #: run: the sentence goes to the thinker, which asks (actions.Actor)
    reflex_tools: dict[str, tuple[str, ...]] = {}
    #: tool names that mark a server as this adapter's whatever its name: listed by a server with
    #: no adapter (or with one that is not `foreign`), they give it this adapter (Server._run)
    claims_tools: tuple[str, ...] = ()
    #: tools she asks about first, out loud, and calls only after a spoken yes (confirm.py), when the
    #: server's config has no `confirm` list of its own
    confirm: tuple[str, ...] = ()
    #: how the widget's step chip names this server ("Spotify"); empty: the server's name, capitalised
    title: str = ""
    #: tool -> the chip's words for it while it runs ("searching the web…"); others are "<title>: <tool>"
    labels: dict[str, str] = {}
    #: tools that only read: a cancel stops waiting for them, where any other call is let finish first
    reads: tuple[str, ...] = ()
    #: tool -> its approval tier (approvals.py) when it is not what `risk` gives by default: `sends`
    #: for a tool that sends something to someone, `destructive` for one that deletes or cannot be
    #: undone. Both always wait for a yes; `[approvals] risk` in the config decides over this
    risks: dict[str, str] = {}

    def risk(self, name: str) -> str | None:
        """The tool's approval tier: `risks`, else `read` for a look-up-only server or a tool in `reads`,
        else None (the core's `change`)."""
        if name in self.risks:
            return self.risks[name]
        if self.looks_up_only or name in self.reads:
            return "read"
        return None

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
        """A result as the thinker reads it, made compact before it is cut to `result_chars` (the noise
        of a search listing would otherwise fill it): one line per hit. Only for the thinker's calls; a
        reflex, `ask`, `done` and the vocabulary get the server's own text. Returned unchanged by default."""
        return text

    def log_result(self, name: str, text: str, ok: bool) -> str | None:
        """The journal's summary of a call's result in place of its text (a count, a size), or None for
        the core's own line: the size, or with `log_detail` the first 160 characters."""
        return None

    def wanted(self, text: str, route: Any) -> bool | None:
        """Does this sentence want this server's tools? True: asked for outright (offered first,
        with `nudge`); False: left out of this sentence's offer; None: no opinion, offered as usual.
        `route` is the gate's reading, None when the gate could not read it."""
        return None

    def guide_for(self, tools: list[str]) -> str:
        """`guide`, for a server that lists these tools (all of them, not this sentence's offer, so
        the paragraph is the same from sentence to sentence): "" when it would name tools the
        server does not have."""
        return self.guide

    def said_reflex(self, text: str, route: Any) -> str | None:
        """The name of one of `said_reflexes` when the whole sentence plainly is that command ("I like
        this"), else None. Asked before the gate's tool question, for a sentence of this server's
        topic: keep it to whole sentences with nothing to fill in, and anything else to the thinker."""
        return None

    def guard(self, state: dict[str, Any], name: str, arguments: dict[str, Any]) -> str | None:
        """Before a call: None to let it run, or what the brain is told instead of running it.
        `state` is this sentence's own, kept for this server across its calls (see `observe`)."""
        return None

    def forward(self, state: dict[str, Any], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """After `guard` let a call through: the arguments to send, in the form that was checked
        (a URL in its canonical form, so what is fetched is what was compared). As they came by default."""
        return arguments

    async def screen(self, state: dict[str, Any], name: str, arguments: dict[str, Any]) -> str | None:
        """Just before the call, on the arguments `forward` returned: a check that has to wait on
        something (a DNS lookup of a page's host). None to let it run, or what the brain is told
        instead. Keep it short: the sentence is waiting."""
        return None

    def result_urls(self, name: str, text: str, ok: bool) -> list[str]:
        """The URLs a call's result names as its own (a search's results), read from the server's whole
        answer before `shape_result` and the cut to `result_chars`; they reach `observe` as `urls`."""
        return []

    def observe(self, state: dict[str, Any], name: str, arguments: dict[str, Any], text: str, ok: bool,
                urls: tuple[str, ...] = ()) -> None:
        """After a call: note what `guard` needs to know later in the same sentence. `text` is the
        result as the model got it (compacted, cut); `urls` what `result_urls` read before that."""

    async def ask(self, toolbox: Toolbox, server: str, name: str,
                  arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Before a `confirm` tool: the one line that asks about it ("Remove 'Teardrop' from Gym? Say
        yes."), and the arguments to call it with after the yes, with anything that could change by
        then pinned ("current" as the playing track). Read-only calls only. The core's line names
        the tool by default (confirm.generic_question), with the arguments as they came."""
        from ..confirm import generic_question   # local: confirm imports the tools, as this module does

        return generic_question(name), arguments

    def describe(self, name: str, arguments: dict[str, Any], question: str) -> str:
        """The one line her approval card shows (PROTOCOL §13b `prompt`): what is about to happen, for
        display, from the pinned call and the question `ask` wrote, and nothing the spoken question does
        not already say (it goes on the bus as her line anyway). The question without its "Say yes." by
        default: "Remove 'Teardrop' from Gym?". It goes on the bus to every body that shows approvals,
        cut to 160 characters (confirm.card_line, runs.clean). For a `sends` or `destructive` tool it
        must carry no free text from the arguments (a message's body, a note's text): naming the target
        is fine (the song, the playlist, the recipient's display name), quoting what is sent is not."""
        from ..confirm import card_line   # local, as in `ask`

        return card_line(question)

    def done(self, name: str, arguments: dict[str, Any], result: Any) -> Any:
        """After the yes: an Outcome saying what came of the call, or None for the core's "Done."."""
        return None

    def nudge(self, text: str, route: Any) -> str:
        """A line added under the sentence when `wanted` said True ("search first"), or ""."""
        return ""

    def flat_gate_examples(self) -> dict[str, list[str]]:
        """`gate_examples` in the gate's own key shape: {"kind.request": [...]}."""
        return {f"{question}.{option}": list(phrases)
                for question, options in self.gate_examples.items()
                for option, phrases in options.items()}
