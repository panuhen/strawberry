"""The adapter registry (ADAPTERS.md).

The core connects whatever MCP servers the config lists and knows nothing else about them.
An adapter is loaded only when one of those servers matches it — by its name in
`[tools.servers.<name>]`, or by an explicit `adapter = "spotify"` in that table — so adding a
server is enough to use it, and an adapter is a bonus, never a requirement.

    [tools.servers.spotify]           # the name matches the Spotify adapter
    topic = "music"
    command = "spotify-mcp"

    [tools.servers.music-at-home]     # any name, told which adapter to use
    topic = "music"
    command = "…"
    adapter = "spotify"

To add one: write `adapters/<name>.py` with an `Adapter` subclass and list it in `REGISTRY`.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import Adapter
from .spotify import SPOTIFY

log = logging.getLogger("strawberryd.adapters")

#: every adapter that ships. One line per adapter; nothing else registers them.
REGISTRY: tuple[Adapter, ...] = (SPOTIFY,)


def adapter_for(server_name: str, server_config: dict[str, Any]) -> Adapter | None:
    """The adapter for one configured server, or None when it is a plain server of tools."""
    for adapter in REGISTRY:
        if adapter.matches(server_name, server_config):
            return adapter
    wanted = str(server_config.get("adapter", "")).strip()
    if wanted:
        log.warning("adapters: %s asks for adapter %r, which does not exist (known: %s)",
                    server_name, wanted, ", ".join(a.name for a in REGISTRY))
    return None


def load(servers: dict[str, dict[str, Any]]) -> dict[str, Adapter]:
    """Server name -> its adapter, for the configured servers that have one."""
    found: dict[str, Adapter] = {}
    for name, server in servers.items():
        adapter = adapter_for(name, server if isinstance(server, dict) else {})
        if adapter is not None:
            found[name] = adapter
            log.info("adapters: %s for server %s", adapter.name, name)
    return found


def gate_examples(adapters: dict[str, Adapter]) -> dict[str, list[str]]:
    """The loaded adapters' extra gate phrases, in the gate's own key shape ("kind.request")."""
    merged: dict[str, list[str]] = {}
    for adapter in dict.fromkeys(adapters.values()):   # one adapter used twice contributes once
        for key, phrases in adapter.flat_gate_examples().items():
            for phrase in phrases:
                if phrase not in merged.setdefault(key, []):
                    merged[key].append(phrase)
    return merged
