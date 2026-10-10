"""What a server's results and calls are, for the trust model (WIRING.md §20, ADAPTERS.md).

Each server has three flags, from its adapter (`Adapter.private`, `foreign`, `egress`) or, for a server
without one, from its config (`[tools.servers.<name>] flags = [...]`):

    private   its results are the user's own: their library, what they play, their messages or notes
    foreign   its results carry text written by others: a web page, a search snippet, a message
    egress    a call sends what it carries off this machine to someone else: a search query, a page
              address, a name others may see

    web        foreign + egress        (adapters/web.py)
    spotify    private + egress, and foreign per result (adapters/spotify.py: names reach the thinker only
               as short quoted fields; a result with free text or instruction-like wording is foreign)
    messages   private + foreign       (brain step 6, stage 6)
    recall     private + foreign + egress   (stage 4)
    a server with no adapter and no `flags`: all three, the safe default

A config may add flags to an adapter's, never take one away: what an adapter says about its server
holds. A server that is not foreign may still say one result is (`Adapter.view`, `reads_as_foreign`,
`ToolResult.foreign`, failing closed: an exception there makes the result foreign): that result counts
as a foreign server's.

What the thinker does with them (Thinker._run) cannot be switched off:

    once a foreign result is in the conversation ("tainted"):
      the ledger, the situation but its public part and every result not from a foreign server
        are taken out of the conversation, and at most three rounds remain;
      a call to a private or egress server whose results are not in it is refused;
      a call to an egress server that carries a phrase of the private context is refused;
      every call that is not `read` waits for the user's yes, with a question code writes
        (approvals.needed(..., foreign=True); confirm.hold without the adapter's wording);
      her answer is kept in the ledger as a placeholder and logged as its length.
    always: control characters are taken out of every result (`clean`), and a private, foreign or
      unknown server's results and arguments are logged as counts and sizes only (tools.Server.call).
"""

from __future__ import annotations

import re
from typing import Any

FLAGS = ("private", "foreign", "egress")
UNKNOWN = frozenset(FLAGS)        # a server nobody has said anything about

# Control characters a result may not carry into a prompt: C0 and C1 but the line break and the tab,
# the line and paragraph separators, zero-width characters and the bidirectional overrides and isolates,
# which can make text read differently from what it is.
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f  ​-‏‪-‮⁠-⁤⁦-⁩﻿]")


def flags_for(adapter: Any, config: dict[str, Any] | None = None) -> frozenset[str]:
    """A server's flags: its adapter's, plus any its config adds; the config's alone for a server without
    an adapter, and all three when that says nothing either."""
    config = config or {}
    declared = frozenset(f for f in config.get("flags", ()) if f in FLAGS) if "flags" in config else None
    if adapter is None:
        return declared if declared is not None else UNKNOWN
    own = frozenset(flag for flag in FLAGS if getattr(adapter, flag, False) is True)
    return own | (declared or frozenset())


def clean(text: str) -> str:
    """A result without control characters (each becomes a space); line breaks and tabs stay."""
    return CONTROL.sub(" ", text) if isinstance(text, str) else text
