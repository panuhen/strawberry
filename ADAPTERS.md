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
Finding particular music does not: with no music server, "play daft punk" or "put on my liked songs" gets her line that choosing music needs a music add-on, like the Spotify one (below).

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
| `said_reflexes`, `said_reflex` | a command the gate has no option for ("I like this"), known by the whole sentence's words |
| `situation` | one line the brain is told before it starts, so "this song" means something |
| `vocabulary` | names the speech recogniser should know ("Daft Punk" came through as Dove Punk) |
| `clarify_error` | this server's confusing refusals, reworded before a model reads them |
| `gate_examples` | phrases that only make sense with this server behind them |
| `common_tools` | which of its tools to keep first when the brain's context is tight |
| `tools`, `shape_tool` | the only tools of the server the brain sees, and shorter schemas for them |
| `shape_result` | a result made compact before it is cut to `result_chars` |
| `log_result` | what the journal says of a call when the result must stay out of it |
| `guide`, `guide_for`, `unavailable` | a paragraph for the brain's rules when its tools are offered (`guide_for`: only for a server that lists the tools it names), or when the server is down |
| `wanted`, `nudge` | whether a sentence asks for this server outright, and the line added under it |
| `max_calls` | how many calls to its tools one sentence may make |
| `untrusted`, `guard`, `forward`, `screen`, `observe` | its results are strangers' text: what may follow one, in what form a call is sent, and a last check that may wait on the network |
| `claims_tools` | tool names that give a server this adapter whatever it is called |

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
  only decides what survives `[thinker] max_tools`. `tools` is the filter: a tool left out of it
  never reaches the brain, and a call to it is refused.
- **Keep the prompt the same from sentence to sentence.** Ollama reuses its cached prompt up to the
  first token that differs, and the tool schemas come right after the system prompt: a tool list
  or a `guide` that changed with the sentence re-read ~2700 tokens, 2.1-2.7 s on the 27B model.
  Say per-sentence things in a `nudge`, which goes under the sentence.
- **An `untrusted` server's results are never trusted.** Once one is in a conversation, the
  thinker takes the ledger, the situation (but the date) and the other servers' results out of
  it, refuses every other server's tool, and leaves three rounds; the adapter's `guard` decides
  each further call, `forward` sends it in the form that was checked, and `screen` may still refuse
  it on a check that waits on the network (a DNS lookup).

Tests: `tests/test_adapters.py` covers matching and the routing above it, and
`tests/test_adapter_spotify.py` covers one adapter's own behaviour against a fake server. A new
adapter wants the same pair.

## The Spotify adapter

The worked example, `strawberry/adapters/spotify.py`. Its server is a separate MCP wrapper around
the Spotify Web API, not shipped with her: you install it, register a Spotify app and authorise it
once. The one this adapter was written against is
[panuhen/spotify-mcp](https://github.com/panuhen/spotify-mcp) (26 tools over the Web API). The adapter binds to the tool names that wrapper exposes (`next`, `previous`,
`pause`, `play`, `get_current_track`, `get_devices`, `set_volume`, `get_playlists`,
`get_saved_tracks`, `like_current`, `play_liked`); a different wrapper with other names needs its own adapter.
Then:

```toml
[tools.servers.spotify]
topic = "music"
command = "spotify-mcp"      # or the full path into its venv
careful = ["save_tracks", "remove_saved_tracks", "add_to_playlist",
           "remove_from_playlist", "create_playlist"]
```

`like_current` and `add_current_to_playlist` are not in that list on purpose: each is undone in a
word and the server skips a track that is already there, so they are offered with every sentence.
Nor is `play_liked`, which only changes what is playing, like `play`.
Removing a track from a playlist and making a new playlist are offered only when the gate reads the
sentence as asking for a library change (`wants_library_change` ≥ 0.5).

The library by name (spotify-mcp's `like_current`, `add_current_to_playlist`, `find_playlist`,
`remove_from_playlist`, `create_playlist`, `play_liked`, and `play` with `playlist`):

- **"I like this", "save this song", "add this to my favourites"** is a reflex on the sentence's own
  words (`said_reflexes`): the gate has no option for liking and reads "I like this" as chat, so the
  adapter recognises the whole sentence ("I like this", "like this track", "save this song please",
  "add this to my liked songs", "favourite this track", "could you add this one to my favourites
  for later"; nothing longer, nothing with a name in it) and calls `like_current`: "Liked: Blue
  Monday by New Order." The user's favourites are Spotify's Liked Songs: the server once kept a
  local favourites list with five tools of its own, and that list is gone. With an older server
  that has no `like_current`, the sentence goes to the thinker.
- **"Play my favourites"** ("play my liked songs", "can you shuffle my favourites") is a reflex the
  same way, on `play_liked`: "Playing your Liked Songs, shuffled, starting with Around the World by
  Daft Punk." The server starts a random page of the Liked Songs as a track list, because Spotify
  does not start Liked Songs as a context. "Play my favourites playlist" is a playlist. Without
  `play_liked` the sentence goes to the thinker.
- **A favourites sentence the reflexes do not take** ("I think you should add this one to my
  favourites", "put on some of my favourite songs while I work") goes to the thinker with a line
  under it, as a playlist sentence does: "They asked to save the playing track to their favourites,
  which are their Liked Songs: do it with a tool call now. Say it is done only if a tool did it."
  The line names no tool: naming `like_current` or `play_liked` in it made Qwen skip the call 11
  times in 12.
- **"Add this to my gym playlist"**, **"play my running playlist"** and **"take this off my gym
  playlist"** carry a name, so the thinker does them: `add_current_to_playlist(playlist=…)`,
  `play(playlist=…)` and `remove_from_playlist(playlist=…, track="current")`, with the name as it
  was heard; the server matches it, speech-to-text errors included. The adapter's `guide` names the
  first and the last (a clause for `play` made Qwen skip the call), and its `nudge` puts a line
  under a playlist sentence: "They asked to play their playlist 'running': do it with a tool call
  now … Say it is done only if a tool did it." Without that line Qwen said "Blue Monday is out of
  your gym playlist" with no call 4 times in 5.
- **Several playlists match** is the one question she asks back: `clarify_error` tells the model to
  ask which one, naming at most three, and a reflex says "several playlists match: Gym, Gym Mix or
  Old Gym. Which one?"

Failures are worded by the server's `code`: `network` is "I can't reach Spotify right now",
`auth` "Spotify needs signing in again", `no_active_device` and `restricted` keep their own lines,
and the rest say the server's sentence without its hints for a terminal ("Run spotify-mcp
--login…", "name a device_id…"). An older server's errors, without a `code`, are read by Spotify's
own `details` and their wording as before.

What it is worth: the Web API knows the *next* track before the desktop player's metadata catches
up, its volume is the active device's rather than the app's, and it can search, queue, and reach
playlists and saved tracks — which is where the recogniser's names come from. Its reflexes win over
MPRIS for exactly that reason. It costs a round trip: measured live, `skip` is 1.2 s through the
server against 0.4 s over MPRIS, and `what song is this` is 0.46 s against 9 ms.

Its 25 tools were 2382 prompt tokens of Qwen's 8192 context (measured with `prompt_eval_count`).
The server's schemas are shorter now; by their size at ~3.7 characters a token (estimated, not
measured live) all 26 are ~2300 tokens plus ~200 for the template, and the fourteen
`common_tools` a sentence about music usually needs ~1250 (`like_current`,
`add_current_to_playlist`, `find_playlist` and `play_liked` among them). With the careful ones held
back an ordinary sentence is offered 21 of them (~1775), and with the web server's two that is under
`max_tools`. Its `guide` is ~160 tokens more, once, in the system prompt.

With no Spotify server configured, nothing of that is loaded: music control is MPRIS, the gate never
learns "save this song", and no error is described in Spotify's words.

## The web adapter

`strawberry/adapters/web.py`, for a SearXNG instance behind an MCP server: `[tools.servers.web]`,
or any name with `adapter = "web"`, or any server that lists one of its tools. It binds to
[mcp-searxng](https://www.npmjs.com/package/mcp-searxng)'s `searxng_web_search` and
`web_url_read`, and to `web_search` and `read_page` for a server exposing those instead; any other
tool of the server (mcp-searxng's suggestions and instance info) is never offered and refused if
called. The setup is in the README (*Web search*):

```toml
[tools.servers.web]
topic = "other"
command = "/full/path/to/npx"
args = ["-y", "mcp-searxng@2.5.1"]
env = { SEARXNG_URL = "http://127.0.0.1:8888", NODE_OPTIONS = "--dns-result-order=ipv4first", PATH = "/dir/of/node:/usr/bin:/bin" }
```

What it adds:

- **Two tools, short.** mcp-searxng's own schemas for the two are ~6000 characters; these, with
  `query` (and `max_results`) and `url` (and a length), ~1000. A listing is compacted to numbered
  results (title and site, snippet, URL), so ~8 fit the 2000 characters a result is cut to
  instead of ~2 with the server's scores, engine names and thumbnail URLs.
- **When to search** is its `guide` (asked to; current or specific facts: weather, news,
  results, prices, opening hours, the newest or latest of anything; never small talk, questions
  about her, or recommending music) plus a `nudge` under the sentence for an explicit request
  ("look up…", "search the web for…", "google…", "hae netistä…") and for a question, as the gate
  reads it, about something that changes ("now", "today", "latest", "weather", "price"…). She
  answers in two or three spoken sentences, may name the site and never reads out an address;
  the thinker also turns any address in her line into its site.
- **Failures in short.** "fetch failed" is "web search is not reachable right now"; "No results
  for <the query>" is "the search found nothing", without the query; a timeout, a refused site
  and a blocked address each get one line.
- **The journal** gets the result count and size of a search (`3 results, 1834 chars (not
  logged)`) or the kind of failure, never a result.
- **At most three calls a sentence**, and once a result is in, the thinker's untrusted rules.
  The `guard`: a query is one plain line of at most 200 characters; a page is read only by a URL
  that, in one strict canonical form, equals a URL field of this question's own results (never
  one a snippet mentions), and `forward` sends that result's canonical URL, not the model's
  string; anything two URL parsers could read differently is refused (a login part,
  backslashes, whitespace, an encoded host, a trailing dot, any IP notation, internal or
  single-label hosts, other schemes); one page a question and no search after it. Then `screen`
  looks the host up (1.5 s, the event loop's resolver) and refuses the read when the name does not
  resolve or any address it gives is not public: a public-looking name can point at 127.0.0.1.
  A rebinding answer that changes before the fetch is left to the server, which filters its own
  connections.

Tests: `tests/test_adapter_web.py`, against a fake mcp-searxng, including one whose results and
page carry injection text and a scripted model that obeys it.
