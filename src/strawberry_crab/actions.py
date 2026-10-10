"""Acting on what you said (WIRING.md §8b): the reflex tier, and the seam for the thinker.

The gate has already decided a spoken sentence is a request or a question with a topic, and,
on the same embedding, which tool it is like and whether it carries an argument. Two tiers
by cost:

    reflex   the gate is sure about the tool and there is nothing to fill in -> do it now
             (a skip is ~0.25 s end to end: no model in the loop until she phrases the result)
    thinker  everything else the user says -> Qwen with the tools, in her voice (thinker.py)

A reflex is done in one of two places. A configured server with an *adapter* (adapters/) has
its own, in that server's tool names: a Spotify adapter can name the next track before the
desktop player's metadata catches up, so it wins when it is there. With no such server the
bare music commands run over **MPRIS** (mpris.py), which every desktop player speaks, so skip,
pause, resume, volume and "what's playing" work out of the box with nothing configured.

Either way the outcome is one plain sentence of fact written by code ("Skipped. Now Blue
Monday by New Order.") and the reaction path adds a short quip in her voice after it (the
`action` event). A 1B model will not reliably carry a track name or a number from a structured
result into a sentence, so the fact never depends on it, and a failure says it failed.

A reflex never asks for a yes. So one whose tools include a call that waits for one (its tier raised
by `[approvals] risk`, or the tool on its server's confirm list; the adapter's `reflex_tools` says
which tools each reflex calls) is not run: the sentence goes to the thinker, which asks (approvals.py).
A reflex that calls such a tool all the same, one its adapter did not list, gets a refusal instead of
the call (`Guarded`) and the sentence goes to the thinker too. MPRIS's reflexes are not a configured
server's and have no tier.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .config import ActionsConfig
from .events import Event
from .logtext import line, sentence
from .systemone import Route
from .tools import Toolbox, ToolResult

log = logging.getLogger("strawberryd.actions")

# The gate topic whose bare tools MPRIS covers (mpris.TOPIC; named here so actions.py
# needs no import of it, and mpris.py can import Outcome from here).
MPRIS_TOPIC = "music"


@dataclass(frozen=True)
class Outcome:
    """What she did and what came of it: the sentence she says, and the record for /health."""

    did: str            # "skipped to the next track"
    fact: str           # "Skipped. Now Blue Monday by New Order."  (spoken as is)
    ok: bool
    calls: tuple[ToolResult, ...] = ()
    emotion: str = ""   # set by the thinker (her own line, her own mood); "" = the reaction path picks
    held: Any = None    # a confirm.Held: a call the thinker stopped at, made only after a spoken yes
    # Strangers' text was in the run that wrote it (the thinker: a foreign situation line or timeline entry, a
    # foreign result): the timeline keeps the turn as foreign (ledger.py)
    foreign: bool = False

    def event(self, asked: str) -> Event:
        """For the reaction path: it adds a quip after `fact` (brain.describe)."""
        return Event(source="action", app=self.did, title=asked, body=self.fact,
                     category="" if self.ok else "failed", urgency="normal" if self.ok else "critical")


Reflex = Callable[[Toolbox, str], Awaitable[Outcome]]
# Told about a reflex's call as it happens (Daemon: the run's tool events): (phase "started" or
# "completed", server, tool, ok, error code). Never the arguments or the result.
OnCall = Callable[..., None]
# Reflexes that only read; any other one changes the player.
READ_REFLEXES = frozenset({"now_playing"})


class Guarded:
    """The toolbox as a reflex sees it: a call that would wait for a yes (Toolbox.needs_approval) is not made,
    and `refused` names it. Everything else is the toolbox's own."""

    def __init__(self, toolbox: Toolbox) -> None:
        self._toolbox = toolbox
        self.refused = ""

    async def call(self, server: str, name: str, arguments: dict[str, Any] | None = None,
                   result_chars: int | None = None) -> ToolResult:
        needs = getattr(self._toolbox, "needs_approval", None)
        if needs is not None and needs(server, name):
            self.refused = self.refused or f"{server}.{name}"
            return ToolResult(server, name, False, f"{server}.{name} waits for the user's yes; a reflex does not make it",
                              0.0, arguments=arguments or {})
        return await self._toolbox.call(server, name, arguments, result_chars)

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._toolbox, attr)


# Words a bare "start the music again" sentence is made of. Anything else in a `resume` sentence
# ("play daft punk", "play some classical") is a name or a genre the embedding under-scored as an
# argument (live: 0.31 for "play daft punk"), and the sentence belongs to the thinker, not to a reflex
# that would answer "it's already playing".
RESUME_WORDS = frozenset((
    "play playing resume unpause start continue carry go press put turn hit keep music song track it on again back the a "
    "please ok okay yes yeah sure now then just can could you would will do that this let's lets and up her him"
).split())


def carries_argument(text: str, tool: str) -> bool:
    """A `resume` sentence with a word that is not part of a bare "play" names something to play."""
    if tool != "resume":
        return False
    return any(word not in RESUME_WORDS for word in re.findall(r"[a-z'’]+", text.lower()))


# What she says when a sentence wants particular music found (the gate's `needs_catalogue`) and no
# configured server has a catalogue to find it in: MPRIS only has the player's buttons. A fixed
# line, because a model with no tool for it either pretends ("Queued again.") or says she cannot
# touch the player at all, which is wrong (ADAPTERS.md says how to add a music server). She says
# persona.md's `no_catalogue` lines (Daemon.no_catalogue); these are the shipped ones, the fallback.
NO_CATALOGUE = (
    "I can skip, pause and change the volume, but finding particular music needs a music add-on, like the Spotify one.",
    "Picking music is beyond my claws without a music add-on, such as the Spotify one. The add-ons guide says how.",
    "I only have the player's buttons. Choosing what to play needs a music add-on, like the Spotify one.",
)


# ----------------------------------------------------------------------------- the actor


class Actor:
    """Fires the bare reflexes, and collects what the servers can tell the thinker.

    `reflexes` is server name -> the gate's tool -> a reflex, taken from the adapters the
    toolbox loaded for the configured servers. `mpris` is the fallback for the music tools no
    configured server covers; None leaves music to the servers alone.
    """

    def __init__(self, config: ActionsConfig, toolbox: Toolbox, reflexes: dict[str, dict[str, Reflex]] | None = None,
                 mpris: Any | None = None) -> None:
        self.config = config
        self.toolbox = toolbox
        self.adapters = dict(getattr(toolbox, "adapters", {}))
        self.reflexes = reflexes if reflexes is not None else {
            name: dict(adapter.reflexes) for name, adapter in self.adapters.items() if adapter.reflexes
        }
        self.mpris = mpris
        self.acted = 0
        self.failed = 0
        self.deferred = 0          # would have gone to the thinker
        self.last: dict[str, Any] | None = None

    def reflex_for(self, route: Route) -> tuple[str, Reflex] | None:
        """Who does this sentence's bare command: a configured server's adapter if one has a
        reflex for it, otherwise MPRIS for the music ones. None means the thinker's turn."""
        if not route.tool or route.tool == "other":
            return None
        if route.tool_confidence < self.config.reflex or route.has_argument >= self.config.argument:
            return None
        if carries_argument(route.text, route.tool):
            return None
        for name, server in self.toolbox.servers.items():
            if server.topic == route.topic and route.tool in self.reflexes.get(name, {}):
                return name, self.reflexes[name][route.tool]
        if self.mpris is not None and route.topic == MPRIS_TOPIC:
            reflex = self.mpris.reflexes().get(route.tool)
            if reflex is not None:
                return "mpris", reflex
        return None

    async def said_reflex_for(self, text: str, route: Route) -> tuple[str, str, Reflex] | None:
        """A whole sentence a configured server's adapter knows by its words ("I like this": the gate
        has no option for liking), for a server of the sentence's topic that lists the tool it
        needs: (server, name, reflex), or None. Asked before the gate's decision, which reads "I like
        this" as chat; never for a sentence the gate reads as not aimed at her."""
        if route.kind == "other":
            return None
        for name, server in self.toolbox.servers.items():
            adapter = self.adapters.get(name)
            if adapter is None or server.topic != route.topic or not getattr(adapter, "said_reflexes", None):
                continue
            try:
                said = adapter.said_reflex(text, route)
                needs, reflex = adapter.said_reflexes[said] if said else ("", None)
            except Exception as exc:   # an adapter must never cost the sentence its answer
                log.warning("actions: %s adapter could not read the sentence (%s)", name, type(exc).__name__)
                continue
            if reflex is None:
                continue
            try:
                await server.ensure()
                listed = {t.name for t in server.tools}
            except Exception as exc:   # not answering: the thinker says so
                log.info("actions: %s cannot do %s (%s); over to the thinker", name, said, exc)
                continue
            if needs in listed:
                return name, said, reflex
            log.info("actions: %s has no %s tool (an older server?); %s goes to the thinker", name, needs, said)
        return None

    CATALOGUE = 0.5   # p(needs_catalogue) from which a music request wants a server's catalogue

    def has_catalogue(self) -> bool:
        """A configured music server: its tools can search, play and queue. MPRIS cannot."""
        return any(server.topic == MPRIS_TOPIC for server in self.toolbox.servers.values())

    def needs_catalogue(self, route: Route) -> bool:
        """True when the sentence asks for particular music ("play daft punk", "queue one more time")
        and nothing configured could find it, so no model should be asked to pretend. A bare "play"
        is the resume button, never a catalogue request, however the embedding scored it."""
        if route.topic != MPRIS_TOPIC or route.kind not in ("request", "question") or route.catalogue < self.CATALOGUE:
            return False
        if route.tool == "resume" and not carries_argument(route.text, "resume"):
            return False
        return not self.has_catalogue()

    def asks_first(self, server: str, reflex: str) -> bool:
        """Would this reflex make a call that waits for a yes (its adapter's `reflex_tools`)? Then the thinker
        does the sentence instead, and asks. MPRIS has no tiers."""
        if server == "mpris":
            return False
        tools = (getattr(self.adapters.get(server), "reflex_tools", None) or {}).get(reflex, ())
        needs = getattr(self.toolbox, "needs_approval", None)
        return needs is not None and any(needs(server, tool) for tool in tools)

    def label(self, server: str, tool: str) -> str:
        """The reflex as the widget's step chip names it: "Spotify: skip", "Player: pause"."""
        if server == "mpris":
            return f"Player: {tool.replace('_', ' ')}"
        return self.toolbox.label(server, tool)

    async def act(self, text: str, route: Route, on_call: OnCall | None = None) -> Outcome | None:
        """Do what the sentence asks, if this tier can. Returns the outcome (its `fact` is what she
        says, `event()` lets the reaction path add a quip), or None when nothing here applies and
        the caller treats the sentence as chat. `on_call` hears the reflex start and end.

        A reflex changes the player, so once it is under way a cancel lets it finish (asyncio.shield)
        and only then stops the run: the user hears what was done instead of wondering."""
        if not self.config.enabled:
            return None
        said = await self.said_reflex_for(text, route)
        if said is not None:
            server, tool, reflex = said
        elif route.decision != "act":
            return None
        else:
            found = self.reflex_for(route)
            if found is None:
                self.deferred += 1
                log.info("actions: %s (%s/%s tool %s %.2f arg %.2f) is not a bare reflex; over to the thinker",
                         sentence(text), route.kind, route.topic, route.tool or "-", route.tool_confidence,
                         route.has_argument)
                return None
            (server, reflex), tool = found, route.tool
        if self.asks_first(server, tool):
            self.deferred += 1
            log.info("actions: %s.%s would wait for a yes ([approvals] risk, or a confirm list); over to the thinker, "
                     "which asks", server, tool)
            return None
        started = time.perf_counter()
        if on_call is not None:
            on_call("started", server, tool)
        guarded = Guarded(self.toolbox)
        work = asyncio.ensure_future(asyncio.wait_for(reflex(guarded, server), self.config.timeout_s))
        timed_out = False
        try:
            outcome = await asyncio.shield(work)
        except asyncio.TimeoutError:
            timed_out = True
            verb = tool.replace("_", " ")
            outcome = Outcome(f"tried to {verb}", f"I tried to {verb}, but {server} did not answer in time.", False)
        except asyncio.CancelledError:
            # Stopped mid-reflex: it is let finish (bounded by timeout_s), then the stop goes on.
            ok = False
            try:
                ok = (await work).ok
            except Exception:   # noqa: BLE001 - a timeout or an error: it is reported as failed
                pass
            if on_call is not None:
                on_call("completed", server, tool, ok, "" if ok else "failed")
            raise
        if guarded.refused:
            # A tool its adapter did not list for this reflex waits for a yes: nothing more is made here, and
            # the thinker takes the sentence (and asks). Whatever the reflex did before is in its calls.
            if on_call is not None:
                on_call("completed", server, tool, False, "refused")
            self.deferred += 1
            log.warning("actions: %s.%s called %s, which waits for a yes; over to the thinker (list it in the "
                        "adapter's reflex_tools)", server, tool, guarded.refused)
            return None
        if on_call is not None:
            on_call("completed", server, tool, outcome.ok, "" if outcome.ok else "timeout" if timed_out else "failed")
        ms = (time.perf_counter() - started) * 1000
        self.acted += 1
        if not outcome.ok:
            self.failed += 1
        self.last = {
            "asked": text, "tool": tool, "server": server, "did": outcome.did, "fact": outcome.fact,
            "ok": outcome.ok, "ms": round(ms, 1),
            "calls": [self.toolbox.shown(c) if hasattr(self.toolbox, "shown") else c.to_dict() | {"text": c.text[:200]}
                      for c in outcome.calls],
        }
        log.info("actions: %s -> %s.%s: %s -> %r (%.0f ms)", sentence(text), server, tool, outcome.did,
                 line(outcome.fact), ms)
        return outcome

    async def situation(self, topic: str | None = None) -> str:
        """What the thinker should know before it starts (`situation_trust`, without the flag)."""
        return (await self.situation_trust(topic))[0]

    async def situation_trust(self, topic: str | None = None) -> tuple[str, bool]:
        """What the thinker should know before it starts, from the servers that can say, and whether any of
        it is text others wrote (a track's name: the situation line is part of the trust boundary, WIRING
        §20). `topic` narrows it to one topic's servers; None (the default) asks every server that has a
        line. With nothing to say about music, MPRIS says what is playing, so "this song" still means
        something with no server configured; a player's title is always someone else's text. An adapter
        says of its own line (`Adapter.situation_is_foreign`, foreign unless it says otherwise; an
        exception there is foreign too)."""
        lines = []
        foreign = False
        for name, server in self.toolbox.servers.items():
            adapter = self.adapters.get(name)
            if adapter is None or (topic is not None and server.topic != topic):
                continue
            try:
                line = await asyncio.wait_for(adapter.situation(self.toolbox, name), 5.0)
            except asyncio.TimeoutError:
                line = ""
            if line:
                lines.append(line)
                try:
                    foreign = foreign or adapter.situation_is_foreign(line) is not False
                except Exception:   # noqa: BLE001 - when in doubt, foreign
                    foreign = True
        if not lines and self.mpris is not None and topic in (None, MPRIS_TOPIC):
            try:
                line = await asyncio.wait_for(self.mpris.situation(), 5.0)
            except asyncio.TimeoutError:
                line = ""
            if line:
                lines.append(line)
                foreign = True        # the player's title, artist and album: written by others
        return " ".join(lines), foreign

    async def vocabulary(self) -> list[str]:
        """Names from every server whose adapter can offer them, for the speech recogniser."""
        names: list[str] = []
        for name in self.toolbox.servers:
            adapter = self.adapters.get(name)
            if adapter is None:
                continue
            try:
                for word in await asyncio.wait_for(adapter.vocabulary(self.toolbox, name), 20.0):
                    if word not in names:
                        names.append(word)
            except asyncio.TimeoutError:
                log.warning("actions: %s vocabulary timed out", name)
        return names

    def stats(self) -> dict[str, Any]:
        return {"enabled": self.config.enabled, "acted": self.acted, "failed": self.failed, "deferred": self.deferred,
                "last": self.last}

    # --- a reflex by name, with no sentence: gestures (gestures.py, WIRING.md §25) -----------------------

    async def named(self, action: str) -> tuple[str, str, Reflex] | None:
        """The reflex for an action named outright (a gesture's map entry), no gate in between: a configured
        music server's adapter if it has one (a gate tool's reflex, or a said one whose tool the server lists),
        else MPRIS's. (server, action, reflex), or None when nothing here does it."""
        if not self.config.enabled:
            return None
        for name, server in self.toolbox.servers.items():
            if server.topic != MPRIS_TOPIC:
                continue
            reflex = self.reflexes.get(name, {}).get(action)
            if reflex is not None:
                return name, action, reflex
            said = (getattr(self.adapters.get(name), "said_reflexes", None) or {}).get(action)
            if said is None:
                continue
            needs, reflex = said
            try:
                await server.ensure()
                listed = {t.name for t in server.tools}
            except Exception as exc:   # not answering: MPRIS may still do it, below
                log.info("actions: %s cannot do %s (%s)", name, action, exc)
                continue
            if needs in listed:
                return name, action, reflex
        if self.mpris is not None:
            reflex = self.mpris.reflexes().get(action)
            if reflex is not None:
                return "mpris", action, reflex
        return None

    def above(self, server: str, action: str, tiers: tuple[str, ...]) -> str:
        """Why a reflex may not run for a gesture ("" when it may): one of its calls (its adapter's
        `reflex_tools`) is of a tier outside `tiers`, or waits for a yes, or the adapter does not say which
        calls it makes. MPRIS's reflexes only change what plays and how."""
        if server == "mpris":
            return ""
        tools = (getattr(self.adapters.get(server), "reflex_tools", None) or {}).get(action)
        if not tools:
            return "its adapter does not list the calls it makes (reflex_tools)"
        for tool in tools:
            tier = self.toolbox.risk(server, tool) if hasattr(self.toolbox, "risk") else "change"
            if tier not in tiers:
                return f"{server}.{tool} is of the {tier} tier"
        if self.asks_first(server, action):
            return "one of its calls waits for a yes (a confirm list)"
        return ""

    async def act_named(self, action: str, found: tuple[str, str, Reflex], on_call: OnCall | None = None,
                        tiers: tuple[str, ...] = ("read", "playback")) -> Outcome | None:
        """Do `found` (from `named`) as `act` does a reflex: shielded once under way, its calls guarded, never
        asking. None when it may not run here (`above`: a tier above `tiers`, or a call that waits for a yes)
        or a call it made was refused: a gesture hands nothing to the thinker; it does nothing, and the log
        says why."""
        server, tool, reflex = found
        why = self.above(server, action, tiers)
        if why:
            log.info("actions: a gesture's %s.%s is not run: %s", server, action, why)
            return None
        started = time.perf_counter()
        if on_call is not None:
            on_call("started", server, tool)
        guarded = Guarded(self.toolbox)
        work = asyncio.ensure_future(asyncio.wait_for(reflex(guarded, server), self.config.timeout_s))
        timed_out = False
        try:
            outcome = await asyncio.shield(work)
        except asyncio.TimeoutError:
            timed_out = True
            verb = tool.replace("_", " ")
            outcome = Outcome(f"tried to {verb}", f"I tried to {verb}, but {server} did not answer in time.", False)
        except asyncio.CancelledError:
            ok = False
            try:
                ok = (await work).ok
            except Exception:   # noqa: BLE001 - a timeout or an error: it is reported as failed
                pass
            if on_call is not None:
                on_call("completed", server, tool, ok, "" if ok else "failed")
            raise
        if guarded.refused:
            if on_call is not None:
                on_call("completed", server, tool, False, "refused")
            log.warning("actions: a gesture's %s.%s called %s, which waits for a yes; stopped there", server, tool,
                        guarded.refused)
            return None
        if on_call is not None:
            on_call("completed", server, tool, outcome.ok, "" if outcome.ok else "timeout" if timed_out else "failed")
        ms = (time.perf_counter() - started) * 1000
        self.acted += 1
        if not outcome.ok:
            self.failed += 1
        self.last = {
            "asked": "(a gesture)", "tool": tool, "server": server, "did": outcome.did, "fact": outcome.fact,
            "ok": outcome.ok, "ms": round(ms, 1),
            "calls": [self.toolbox.shown(c) if hasattr(self.toolbox, "shown") else c.to_dict() | {"text": c.text[:200]}
                      for c in outcome.calls],
        }
        log.info("actions: a gesture -> %s.%s: %s -> %r (%.0f ms)", server, tool, outcome.did, line(outcome.fact), ms)
        return outcome
