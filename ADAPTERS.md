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
# confirm = ["delete_note"]  # tools she asks about out loud first and runs only after a spoken yes
# adapter = "spotify"        # only when the server's own name does not name its adapter
# flags = ["private"]        # what the server is (below); without an adapter and without this, all three
# offer = "always"           # when the brain gets its tools: always, topic or asked (below)
```

`topic` matters twice: the gate reads every spoken sentence into one topic, and that topic's servers
go first when the tool list has to be cut (`[thinker] max_tools`). `careful` lists the tools that
change something the user would miss; they reach the brain only when the gate's
`wants_library_change` is ≥ 0.5 ("save this song", not "play this song").

`confirm` lists the tools she asks about before calling them, because the gate or the brain can
misread a sentence and these cannot be taken back. When the brain calls one, the call is not made:
she says what she is about to do in one line ("Shall I go ahead with delete note? Say yes.", or the
adapter's own wording) and keeps the call exactly as it was. The user's next sentence decides: a
yes (yes, sure, do it, go ahead…) makes that one call with those arguments and no model is asked
again; a no, any other sentence, or no answer within `[approvals] change_s` (10 s, not counted while
she is listening) leaves it undone and she says so, and any other sentence is then handled as
usual. Only the user's own sentence (spoken or typed) can answer, or the user's own click: her card
on a body that declared it can show approvals, and the Brain UI (WIRING §19); a notification, a
media change, a tool result or a web page never can. Without the key a server gets its adapter's
list (`confirm` on the adapter; the Spotify one asks about its two removals) or none; `confirm = []`
asks about nothing. A tool can be on both lists, on one, or on neither. `/health.confirm` shows what
she is waiting for.

Every tool also has an approval tier: `read`, `playback` (what plays and how, undone in a second: play,
pause, skip, volume, the queue, a like), `change`, `sends` (something reaches other people, or
leaves the machine for someone: a message, an email) or `destructive` (deletes, or cannot be
undone). A `sends` or `destructive` call is always asked about, confirm list or not, and waits 30 s
(`sends_s`, `destructive_s`); on her card its yes is a press-and-hold. The tier comes from
`[approvals] risk` for the tool (`"notes.send" = "sends"`), taken as written; else from the adapter
(`risks = {"send": "sends"}`; its `reads`, and every tool of a look-up-only server, are `read`); else
it is `change`. A server that marks a tool `destructiveHint` raises it to `destructive`; an
annotation never lowers a tier, so `readOnlyHint` does not make a tool `read` here. A whole-server
entry (`"notes" = "read"`) then sets the server's ordinary tools, but never lowers a tool below its
adapter's tier or its `destructiveHint`: only the tool's own entry can. Once text from strangers is in
a conversation, every call above `playback` is asked about too (WIRING §20). The card shows one line, the adapter's `describe` (by default her question without "Say
yes."), cut to 160 characters, and goes on the bus to every body that shows approvals. **Rule for
`sends` and `destructive` tools:** neither `describe` nor the question `ask` writes may carry free
text from the arguments: no message body, no note text, nothing being sent. Naming the target is
fine (the song, the playlist, the recipient's display name): "Send your message to Sam?", never
"Send 'running late, sorry' to Sam?".

**What a server is: its flags.** The trust model (WIRING §20) needs to know three things of each
server:

| flag | means | what follows |
|---|---|---|
| `private` | its results are the user's own (their library, what they play, their messages or notes) | once text from strangers is in a conversation, its tools are refused there |
| `foreign` | its results carry text written by others (a web page, a snippet, a message) | once one of its results is in a conversation, the user's private context leaves it, private and egress tools are refused, and every call above `playback` waits for a yes with a question the core writes |
| `egress` | a call sends what it carries off the machine to someone else (a query, an address, a name others may see) | once text from strangers is in, a call that carries a phrase of the user's private context is refused |

An adapter declares its server's flags (`private`, `foreign`, `egress` on the class). A server
without one is all three until its table says otherwise: `flags = []` for a desk lamp that holds
nothing of the user's and sends nothing anywhere, `flags = ["private"]` for a diary. On a server with
an adapter `flags` can only add (`["foreign"]` on a Spotify server whose catalogue you distrust); what
the adapter says holds.

| server | flags |
|---|---|
| web | foreign, egress |
| Spotify | private, egress, and every result with a name in it foreign (`view`): names are written by others, and trust is not decided by what they say. What they can steer is bounded by the tiers: its play, pause, skip, volume, queue, like and save tools are `playback` and go ahead; a playlist change asks. Its situation line names the playing track, so while one plays the sentence starts foreign |
| no adapter, no `flags` | private, foreign, egress |

The flags also decide the journal: a private, foreign or unknown server's calls are logged as the
arguments' names, a count and a size, never a result or an argument, whatever `log_sentences` says.

**When the brain is offered its tools: `offer`.** `always` (the default) offers them with every
sentence, which keeps the prompt the same from sentence to sentence (Ollama's cache, below);
`topic` only with a sentence the gate reads as the server's topic (or one its adapter's `wanted`
says asks for it); `asked` only with a sentence that asks for it (its adapter's `wanted`, or the
server's name or the adapter's `title` as a word: "ask my notes"). Use `topic` or `asked` for a large
server you use now and then: the prompt then changes between sentences, which costs seconds on the
27B model. Whatever is offered goes out in one order (topic, server, the server's own order), and
past `[thinker] max_tools` or `tool_tokens` the least likely tools are left out and logged.

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
| `shape_result` | a result as the brain reads it, one line per hit, before it is cut to `result_chars` (the brain's calls only: reflexes, `ask`, `done` and the vocabulary get the server's own text) |
| `log_result` | what the journal says of a call: a count and a size, never a result |
| `log_detail` | `True` to have the journal carry the arguments and a result's first 160 characters; ignored for a private or foreign server |
| `private`, `foreign`, `egress` | the trust flags (above, WIRING §20) |
| `view`, `reads_as_foreign` | for the brain's calls: the text it reads and whether that counts as strangers' text, decided in one pass, and the final text read once more. Decide by where text comes from, never by what it says (Spotify: every result with a name is foreign; listed fields only, quoted, cut). An exception in either counts as foreign. `view` is `shape_result` and "not foreign" by default |
| `asks_after_foreign` | `True` when `ask` and `describe` never put an argument as it came into her question or card: they are then still used after strangers' text |
| `situation_is_foreign` | whether its `situation` line carries text others wrote (a track's name). The situation line is part of the trust boundary: such a line starts the sentence foreign, so every call above `playback` asks. `True` by default |
| `offer` | when the brain is offered this server's tools: `always`, `topic`, `asked` (above); the config's `offer` decides over it |
| `reflex_tools` | which of the server's tools each reflex calls: a reflex whose tools would wait for a yes is not run and the brain asks instead |
| `guide`, `guide_for`, `unavailable` | a paragraph for the brain's rules when its tools are offered (`guide_for`: only for a server that lists the tools it names), or when the server is down |
| `wanted`, `nudge` | whether a sentence asks for this server outright, and the line added under it |
| `max_calls` | how many calls to its tools one sentence may make |
| `guard`, `forward`, `screen`, `observe` | what may follow a result (for a `foreign` server: its results are strangers' text), in what form a call is sent, and a last check that may wait on the network |
| `claims_tools` | tool names that give a server this adapter whatever it is called |
| `confirm`, `ask`, `describe`, `done` | the tools asked about before they run (when the config has no `confirm`), the question with the call pinned ("current" as the playing track), the one line her approval card shows (by default the question without "Say yes."), and the sentence after the yes |
| `risks`, `risk` | each tool's approval tier where it is not the default: `sends` or `destructive` always wait for a yes (WIRING §19); `reads` and a `looks_up_only` adapter's tools are `read`, the rest `change`. `[approvals] risk` in the config decides over it |
| `title`, `labels` | how the widget's step chip names a call while it runs: the adapter's words for a tool ("searching the web…"), else `<title>: <tool>` ("Spotify: play"); written by you, never from an argument (WIRING §18) |
| `reads` | tools that only look things up: a stop drops one at once, where any other call is let finish first. A tool the server marks `readOnlyHint` counts too, and so does every tool of a `looks_up_only` adapter |

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
- **A `foreign` server's results are never trusted.** Once one is in a conversation, the
  thinker takes the ledger, the situation (but the date) and the other servers' results out of
  it, refuses every private or egress server's tool, asks before any call above `playback` (in its
  own words: an adapter's `ask` and `describe` are not used then, unless it sets `asks_after_foreign`), and leaves three rounds; the
  adapter's `guard` decides each further call, `forward` sends it in the form that was checked, and
  `screen` may still refuse it on a check that waits on the network (a DNS lookup). Say which flags
  your server has: an adapter that sets none says its server is none of them.
- **A reflex never asks.** List the tools each reflex calls in `reflex_tools`; when one of them would
  wait for a yes (the user raised its tier, or put it on the confirm list) the reflex is skipped and
  the brain asks instead. A reflex that calls a tool it did not list is refused that call.

Tests: `tests/test_adapters.py` covers matching and the routing above it, and
`tests/test_adapter_spotify.py` covers one adapter's own behaviour against a fake server. A new
adapter wants the same pair.

## The Spotify adapter

The worked example, `strawberry/adapters/spotify.py` (private and egress: why, in the table above).
Its results reach the brain one line per hit, names quoted and cut to 80 characters, descriptions
dropped (`tracks: 5`, then `1. "Blue Monday" – "New Order" ("Substance") · uri=spotify:track:…`), every
result with a name counts as strangers' text, and its playback tools are `playback` so they still go
ahead after one (WIRING §20), its schemas keep `device_id` only where playback starts
(`play`, `play_liked`), and the journal gets counts ("5 tracks, 812 chars"), never a name. Its server is a separate MCP wrapper around
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

The two removals, `remove_from_playlist` and `remove_saved_tracks`, are also asked about first (the
adapter's `confirm`, used when the table has no `confirm` key of its own): "Remove 'Teardrop' from
Gym? Say yes." Before asking, the adapter pins the call: `track = "current"` becomes the playing
track's URI, so a yes after the song has changed removes the one she named, and a playlist name that
matches exactly one playlist becomes that playlist's URI, said by its own name. After the yes she
says "Removed Teardrop by Massive Attack from Gym." or that it was not on the playlist. A Liked
Songs removal whose ID the server could not read (the model made one up from the track's name) is
sent back to the model to look the track up first, so the question names a track a yes can remove.
`confirm = []` in the table turns the question off; `confirm = ["remove_saved_tracks"]` keeps it
for one of them. Both are of the `change` tier (a track can be added back): a 10 s wait and a plain
tap on her card. `[approvals] risk = { "spotify.remove_saved_tracks" = "destructive" }` makes one
wait 30 s for a held yes instead, and asks about it even with `confirm = []`.

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

`strawberry/adapters/web.py` (foreign and egress), for a SearXNG instance behind an MCP server: `[tools.servers.web]`,
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
- **Which pages can be read.** Only a URL that one of this question's results names as its own.
  The adapter reads those URLs from the server's whole answer, before it is compacted and cut,
  and only from a layout a page cannot write a line of: web-mcp's numbered listing (numbered in
  order, as many results as its header says) or a JSON list's `url` fields. mcp-searxng's
  `Title:/Description:/URL:` blocks keep a page's line breaks, so a snippet could write a block
  of its own: they pin nothing. With mcp-searxng she answers from the results and reads no page.
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
