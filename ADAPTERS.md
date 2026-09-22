# Servers and adapters

Strawberry's core knows nothing about music, calendars or notes. It connects whatever MCP servers
the config lists, gives the brain their tools, and runs the bare music commands over MPRIS. Two
levels, and the second is optional:

| | what it is | what it takes |
|---|---|---|
| **A server** | an MCP server in `[tools.servers.<name>]`; its tools reach the brain | four lines of config |
| **An adapter** | Python in `strawberry/adapters/<name>.py` that makes one server feel native | a file and a line in the registry |

Nothing ships configured. Skip, previous, pause, resume, volume and "what song is this" already
work for any desktop player over MPRIS (WIRING §8b), so the shipped install needs no server at all.

## Adding a server

One table per server in `~/.config/strawberry/config.toml`:

```toml
[tools.servers.notes]
topic = "notes"              # one of the gate's topics: music, calendar, notes, system, other
command = "my-notes-mcp"     # or a full path, e.g. ~/notes-mcp/.venv/bin/notes-mcp
# args = ["--vault", "~/notes"]
# env = { NOTES_TOKEN = "…" }
# cwd = "~/notes"
# careful = ["delete_note"]  # tools with consequences: offered only when the sentence asks for one
# adapter = "spotify"        # only when the server's own name does not name its adapter
```

`topic` matters twice: the gate reads every spoken sentence into one topic, and that topic's servers
go first when the tool list has to be cut (`[thinker] max_tools`). `careful` lists the tools that
change something the user would miss; they reach the brain only when the gate's
`wants_library_change` is ≥ 0.5 ("save this song", not "play this song").

Check it: `strawberry tools` lists everything she can reach, `strawberry tool notes search
'{"q": "garden"}'` calls one by hand, and `/health.tools` shows each server's state and which
adapter (if any) it got. A server that fails to start is skipped with a warning and retried on the
next use; it never takes the daemon down.

That is all a server needs. The brain will call its tools, and her reply about what happened is
written by the brain from the tool's own result.

## What an adapter adds

An adapter is for a server you use every day, where the generic path is not good enough:

| | why |
|---|---|
| `reflexes` | "skip this" done in one round trip instead of a 27B model's six seconds |
| `situation` | one line the brain is told before it starts, so "this song" means something |
| `vocabulary` | names the speech recogniser should know ("Daft Punk" came through as Dove Punk) |
| `clarify_error` | this server's confusing refusals, reworded before a model reads them |
| `gate_examples` | phrases that only make sense with this server behind them |
| `common_tools` | which of its tools to keep first when the brain's context is tight |

Every one is optional. An adapter is loaded **only** when a configured server matches it, so an
adapter nobody uses shapes nothing she says, and no adapter means the core's own plain behaviour.

Matching is by the server's name in the config (`[tools.servers.spotify]` → the `spotify` adapter),
or, for any other name, by an explicit `adapter = "spotify"` in that table. The explicit key decides
alone, so it can also say "this server is *not* the one that name suggests" by naming another.

## Writing one

`strawberry/adapters/base.py` is the whole interface; subclass it and set what you have.

```python
from ..actions import Outcome
from ..tools import Toolbox
from .base import Adapter


async def skip(toolbox: Toolbox, server: str) -> Outcome:
    result = await toolbox.call(server, "next")
    if not result.ok:
        return Outcome("tried to skip", "I tried to skip, but the player said no.", False, (result,))
    return Outcome("skipped to the next track", "Skipped.", True, (result,))


class NotesAdapter(Adapter):
    name = "notes"                       # what `adapter = "…"` selects
    server_names = ("notes", "obsidian")  # names it recognises on sight
    reflexes = {"skip": skip}             # keyed by the gate's tool option
    common_tools = ("search_notes", "add_note")

    async def situation(self, toolbox: Toolbox, server: str) -> str:
        result = await toolbox.call(server, "today")
        return f"Today's note: {result.text}" if result.ok else ""
```

Then add it to the registry in `strawberry/adapters/__init__.py` (the package lives under `src/` in the checkout):

```python
from .notes import NOTES

REGISTRY: tuple[Adapter, ...] = (SPOTIFY, NOTES)
```

Rules the core relies on:

- **Code writes the fact.** A reflex returns an `Outcome(did, fact, ok)` whose `fact` is one plain
  sentence spoken as it stands. A 1B model will not reliably carry a track name or a number out of a
  JSON result, so the sentence never depends on one; the reaction path adds a short quip after it,
  and a failure says what failed. Mirror the sentences already in use ("Skipped. Now X by Y.",
  "Paused.", "It's already playing: …", "Volume down to 65.") so every path sounds like her.
- **A reflex may not raise.** Turn a refusal into an `Outcome(..., ok=False)` with something the
  user can act on. The actor puts a timeout around it and reports that too.
- **`clarify_error` is for wording, not for hiding.** The body stays an error body; only the message
  a model would misread is rewritten.
- **`gate_examples` are phrases, not rules** — `{question: {option: [phrases]}}`, merged into the
  gate's examples at start, before the user's own `[gate.examples]`. Only the gate's Choice
  questions take them (`kind`, `topic`, `music_tool`). Put a phrase here when it only means
  something with this server configured; a phrase that is true of any player belongs in
  `systemone.py`, which ships for everyone.
- **`common_tools` is an order, not a filter.** The full list is offered while it fits; the order
  only decides what survives `[thinker] max_tools`.

Tests: `tests/test_adapters.py` covers matching and the routing above it, and
`tests/test_adapter_spotify.py` covers one adapter's own behaviour against a fake server. A new
adapter wants the same pair.

## The Spotify adapter

The worked example, `strawberry/adapters/spotify.py`. Its server is a separate MCP wrapper around
the Spotify Web API, not shipped with her: you install it, register a Spotify app and authorise it
once. The one this adapter was written against is
[panuhen/spotify-mcp](https://github.com/panuhen/spotify-mcp) (25 tools over the Web API). The adapter binds to the tool names that wrapper exposes (`next`, `previous`,
`pause`, `play`, `get_current_track`, `get_devices`, `set_volume`, `get_playlists`,
`get_favorites`, `get_saved_tracks`); a different wrapper with other names needs its own adapter.
Then:

```toml
[tools.servers.spotify]
topic = "music"
command = "spotify-mcp"      # or the full path into its venv
careful = ["save_tracks", "remove_saved_tracks", "add_to_playlist",
           "favorite_current", "remove_favorite", "clear_favorites"]
```

What it is worth: the Web API knows the *next* track before the desktop player's metadata catches
up, its volume is the active device's rather than the app's, and it can search, queue, and reach
playlists and saved tracks — which is where the recogniser's names come from. Its reflexes win over
MPRIS for exactly that reason. It costs a round trip: measured live, `skip` is 1.2 s through the
server against 0.4 s over MPRIS, and `what song is this` is 0.46 s against 9 ms.

Its 25 tools are 2382 prompt tokens of Qwen's 8192 context (measured with `prompt_eval_count`), so
`common_tools` names the ten a sentence about music usually needs.

With no Spotify server configured, nothing of that is loaded: music control is MPRIS, the gate never
learns "save this song", and no error is described in Spotify's words.
