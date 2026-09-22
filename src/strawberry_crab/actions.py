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

    def event(self, asked: str) -> Event:
        """For the reaction path: it adds a quip after `fact` (brain.describe)."""
        return Event(source="action", app=self.did, title=asked, body=self.fact,
                     category="" if self.ok else "failed", urgency="normal" if self.ok else "critical")


Reflex = Callable[[Toolbox, str], Awaitable[Outcome]]


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
# touch the player at all, which is wrong (ADAPTERS.md says how to add a music server).
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

    async def act(self, text: str, route: Route) -> Outcome | None:
        """Do what the sentence asks, if this tier can. Returns the outcome (its `fact` is what she
        says, `event()` lets the reaction path add a quip), or None when nothing here applies and
        the caller treats the sentence as chat."""
        if not self.config.enabled or route.decision != "act":
            return None
        found = self.reflex_for(route)
        if found is None:
            self.deferred += 1
            log.info("actions: %r (%s/%s tool %s %.2f arg %.2f) is not a bare reflex; over to the thinker",
                     text, route.kind, route.topic, route.tool or "-", route.tool_confidence, route.has_argument)
            return None
        server, reflex = found
        started = time.perf_counter()
        try:
            outcome = await asyncio.wait_for(reflex(self.toolbox, server), self.config.timeout_s)
        except asyncio.TimeoutError:
            verb = route.tool.replace("_", " ")
            outcome = Outcome(f"tried to {verb}", f"I tried to {verb}, but {server} did not answer in time.", False)
        ms = (time.perf_counter() - started) * 1000
        self.acted += 1
        if not outcome.ok:
            self.failed += 1
        self.last = {
            "asked": text, "tool": route.tool, "server": server, "did": outcome.did, "fact": outcome.fact,
            "ok": outcome.ok, "ms": round(ms, 1), "calls": [c.to_dict() | {"text": c.text[:200]} for c in outcome.calls],
        }
        log.info("actions: %r -> %s.%s: %s -> %r (%.0f ms)", text, server, route.tool, outcome.did, outcome.fact, ms)
        return outcome

    async def situation(self, topic: str | None = None) -> str:
        """What the thinker should know before it starts, from the servers that can say. `topic`
        narrows it to one topic's servers; None (the default) asks every server that has a line.
        With nothing to say about music, MPRIS says what is playing, so "this song" still means
        something with no server configured."""
        lines = []
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
        if not lines and self.mpris is not None and topic in (None, MPRIS_TOPIC):
            try:
                line = await asyncio.wait_for(self.mpris.situation(), 5.0)
            except asyncio.TimeoutError:
                line = ""
            if line:
                lines.append(line)
        return " ".join(lines)

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
