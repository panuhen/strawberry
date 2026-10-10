"""Approvals (WIRING.md §19, PROTOCOL.md §13b): a call that waits for the user's yes, bound to that call.

confirm.py decides what she says and keeps the call as it would be made (`Held`); this module keeps
the one open question and who may answer it. A call waits for a yes when

    it is on its server's `confirm` list           (Spotify's two removals; [tools.servers.<name>] confirm)
    its tier is `sends` or `destructive`           (Toolbox.risk: [approvals] risk, the adapter, MCP's
                                                    destructiveHint, which only ever raises a tier)

and, from stage 3 of brain step 6, every call that is not `read` once text from strangers is in the
conversation (`needed(..., foreign=True)`; nothing passes it yet).

    approval = book.request(run, held)      # approval.request on the run: awaiting_approval
    book.answer(approval_id, "yes", "body", hold=True)   # first answer wins; later ones are refused
    outcome = await book.wait(approval)     # yes | no | timeout | cancelled | superseded
    book.verify(approval)                   # the stored call is still the one asked about

One approval is open at a time; a newer request supersedes the older one. Only the call stored at
the request is ever made: a deep copy of it, copied again just before the call and checked against
its digest (sha256 of the server, the tool and the arguments; confirm.run). Timeout is no:
[approvals] change_s (10 s), sends_s and destructive_s (30 s); while the user is speaking at the
deadline (the answer on its way) it waits once more, for at most [approvals] grace_s (10 s). Ids are
`a-<boot>-<n>`, the boot part random at each start. There is no "always allow".

Who answers: the user's own sentence, said or typed (Daemon.handle_voice); a v2 body that declared
`sends.approval` in its hello, only for the open id, and for a tier in `[approvals] hold` only with
`hold: true` (the widget enforces the ~1 s press; the brain checks the flag); the Brain UI (session
and CSRF). A body can only answer: nothing here asks for an action. The bus gets the ids, the tier,
the adapter's `describe` line, the times and the outcome (runs.emit whitelists them); never the
arguments as they came, a result or the user's sentence. The card line can carry names an adapter
took from the call (a song, a playlist, a recipient), as her spoken question does; for `sends` and
`destructive` tools never the free text being sent (Adapter.describe). The log names the tool, never
its arguments.

What this protects against: the model's mistakes and misheard speech. Asking for a call (ws `heard`,
POST /event) and answering one need the bus secret (bussecret.py), so a process that cannot read the
user's files can do neither; local code running as the user can read it, ask, say yes and claim
`hold: true` (WIRING §19).
"""

from __future__ import annotations

import asyncio
import copy
import itertools
import secrets
import logging
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from .config import RISKS, ApprovalsConfig
from .confirm import Held, digest
from .runs import Run, emit

log = logging.getLogger("strawberryd.approvals")

ALWAYS = frozenset({"sends", "destructive"})       # these wait for a yes whatever the confirm lists say
OUTCOMES = ("yes", "no", "timeout", "cancelled", "superseded")
BY = ("voice", "typed", "body", "ui")
HISTORY = 50
TICK_S = 0.25          # how often a wait past its expiry looks again while the user is speaking


def needed(listed: bool, risk: str, foreign: bool = False) -> bool:
    """Does a call wait for a yes? On its server's `confirm` list, or of the `sends` or `destructive`
    tier. `foreign` is stage 3's rule (WIRING §19): once foreign text is in the conversation, every call
    that is not `read`. No caller passes it yet."""
    return listed or risk in ALWAYS or (foreign and risk != "read")


@dataclass(eq=False)
class Approval:
    approval_id: str
    run_id: str
    held: Held                     # the stored call, a deep copy: the only one ever made after a yes
    digest: str
    risk: str
    prompt: str
    timeout_s: float
    hold: bool                     # a body's yes must say it was a press-and-hold
    asked: float = field(default_factory=time.monotonic)
    expires: float = 0.0
    at: float = field(default_factory=time.time)
    outcome: str = ""
    by: str = ""
    text: str = ""                 # the user's own sentence that answered (the ledger only; never the bus)
    resolved_at: float | None = None
    future: asyncio.Future | None = None
    request: dict[str, Any] | None = None   # the approval.request as it went out, for a body that connects later
    reply: Any = None              # (performance, sent) of what the run did after the answer
    made: bool = False             # the call was started after a yes (a yes cancelled before it: False)
    run: Run | None = None         # the run that asked: approval.resolved goes out on it

    @property
    def open(self) -> bool:
        return not self.outcome

    def view(self, prompt: bool = False) -> dict[str, Any]:
        """For the Brain UI and /health: ids, tier, tool, outcome, times. The card line only for the open
        one (`prompt`), which the page shows with Yes and No; never the arguments."""
        out = {"approval_id": self.approval_id, "run_id": self.run_id, "risk": self.risk, "tool": self.held.key,
               "at": round(self.at, 3), "timeout_s": self.timeout_s, "hold": self.hold,
               "outcome": self.outcome or None, "by": self.by or None, "made": self.made,
               "waited": round((self.resolved_at or time.monotonic()) - self.asked, 3)}
        if prompt and self.open:
            out["prompt"] = self.prompt
            out["expires_in"] = round(max(self.expires - time.monotonic(), 0.0), 3)
        return out


class ApprovalBook:
    """The open approval, the recent ones, and the rules for answering."""

    def __init__(self, config: ApprovalsConfig | None = None) -> None:
        self.config = config or ApprovalsConfig()
        # a-<boot>-<n>: the boot part is new at each start, so a card left over from before a restart
        # can never answer a new question that happens to have the same number.
        self.boot = secrets.token_hex(3)
        self.ids = itertools.count(1)
        self.current: Approval | None = None
        self.history: deque[Approval] = deque(maxlen=HISTORY)
        self.asked = 0
        self.refused = 0

    @property
    def open(self) -> Approval | None:
        found = self.current
        return found if found is not None and found.open else None

    def timeout_for(self, risk: str) -> float:
        return {"sends": self.config.sends_s, "destructive": self.config.destructive_s}.get(risk, self.config.change_s)

    def request(self, run: Run | None, held: Held) -> Approval:
        """Open an approval for `held` on `run`: approval.request goes out and the run awaits it. An older
        one still open is superseded first (one at a time)."""
        older = self.open
        if older is not None:
            self.resolve(older, "superseded")
        risk = held.risk if held.risk in RISKS else "change"
        stored = replace(held, arguments=copy.deepcopy(held.arguments))
        timeout = self.timeout_for(risk)
        approval = Approval(f"a-{self.boot}-{next(self.ids)}", run.run_id if run is not None else "", stored,
                            digest(stored.server, stored.name, stored.arguments), risk, held.prompt or held.question,
                            timeout, risk in self.config.hold, run=run)
        approval.expires = approval.asked + timeout
        approval.future = asyncio.get_running_loop().create_future()
        self.current = approval
        self.asked += 1
        approval.request = emit(run, "approval.request", approval_id=approval.approval_id, risk=risk,
                                prompt=approval.prompt, timeout_s=timeout, expires_t=approval.expires, hold=approval.hold)
        log.info("approvals: %s (%s, %s) waits up to %.0fs for a yes%s", approval.approval_id, risk, stored.key, timeout,
                 "; a body's yes must be a hold" if approval.hold else "")
        return approval

    def get(self, approval_id: str) -> Approval | None:
        if self.current is not None and self.current.approval_id == approval_id:
            return self.current
        return next((a for a in self.history if a.approval_id == approval_id), None)

    def answer(self, approval_id: Any, answer: Any, by: str, hold: bool = False, text: str = "") -> str | None:
        """A yes or no to the open approval. None when it decided it; else why not (the code a body gets in
        `input.refused`): `not_open` (no such id, or not the open one), `resolved` (already answered:
        the first answer won), `bad_answer`, `hold_required` (a body's yes to a hold tier without
        `hold: true`; the approval stays open)."""
        found = self.get(approval_id) if isinstance(approval_id, str) else None
        reason = None
        if found is None or (found.open and found is not self.open):
            reason = "not_open"
        elif not found.open:
            reason = "resolved"
        elif answer not in ("yes", "no"):
            reason = "bad_answer"
        elif by == "body" and answer == "yes" and found.hold and hold is not True:
            reason = "hold_required"
        if reason is not None:
            self.refused += 1
            log.info("approvals: an answer by %s refused (%s)", by, reason)
            return reason
        found.text = text
        self.resolve(found, answer, by)
        return None

    def resolve(self, approval: Approval, outcome: str, by: str = "") -> bool:
        """End it: approval.resolved on its run, and whoever waits wakes. A second call does nothing."""
        if not approval.open or outcome not in OUTCOMES:
            return False
        approval.outcome, approval.by = outcome, by if by in BY else ""
        approval.resolved_at = time.monotonic()
        if self.current is approval:
            self.current = None
        self.history.append(approval)
        emit(approval.run, "approval.resolved", approval_id=approval.approval_id, answer=outcome, by=approval.by or None)
        if approval.future is not None and not approval.future.done():
            approval.future.set_result(outcome)
        log.info("approvals: %s %s%s after %.1fs", approval.approval_id, outcome, f" by {approval.by}" if approval.by else "",
                 approval.resolved_at - approval.asked)
        return True

    async def wait(self, approval: Approval, busy: Callable[[], bool] = lambda: False) -> str:
        """Its outcome: an answer, or `timeout` once it has expired and the user is not speaking (the
        answer on its way decides). The wait for a speaking user is one extension of at most
        `[approvals] grace_s` past the expiry: a stuck listener or a noisy microphone cannot keep it open.
        A cancel of the waiting task leaves it open for the caller to resolve."""
        assert approval.future is not None
        last = approval.expires + max(self.config.grace_s, 0.0)
        while not approval.future.done():
            now = time.monotonic()
            left = approval.expires - now
            if left <= 0 and (now >= last or not busy()):
                self.resolve(approval, "timeout")
                break
            await asyncio.wait({approval.future}, timeout=left if left > 0 else min(TICK_S, max(last - now, 0.01)))
        return approval.future.result()

    def verify(self, approval: Approval) -> bool:
        """Is the stored call still the one that was asked about?"""
        held = approval.held
        return digest(held.server, held.name, held.arguments) == approval.digest

    def views(self) -> dict[str, Any]:
        """The Brain UI's: the open one with its card line, and the history, newest first, without it."""
        current = self.open
        return {"open": current.view(prompt=True) if current is not None else None,
                "history": [a.view() for a in reversed(self.history)]}

    def stats(self) -> dict[str, Any]:
        current = self.open
        return {"open": current.approval_id if current is not None else None, "risk": current.risk if current else None,
                "asked": self.asked, "refused": self.refused,
                "outcomes": {o: sum(1 for a in self.history if a.outcome == o) for o in OUTCOMES}}
