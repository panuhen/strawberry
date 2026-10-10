# Strawberry

A desktop mascot for Linux and Windows: a small cel-shaded crab that sits on top of your windows
and reacts to what happens on the machine. She comments on notifications, dances to the beat of
whatever is playing, cheers your git commits, and answers when you talk or type to her. Every
model runs on your own computer through [Ollama](https://ollama.com); nothing is sent anywhere.

## What she does

- **Notifications.** She sees each desktop notification (on Windows, each toast in the
  notification centre) and reacts to it in a line of her own. By default she knows only the
  app and the sender, never the message (see [Privacy](#privacy)). Ask her "any new messages?"
  ("Three: two in Signal from Alex, one in Slack from the build bot.") or "what did Alex say?":
  she answers from the notifications of the last day, with the message itself only as far as your
  message-body setting lets her (otherwise she says she only sees who wrote and where). She can
  only look: she never sends, replies or marks anything as read.
- **Music.** Skip, previous, pause, resume, volume and "what song is this" work for any desktop
  player (Spotify, VLC, Rhythmbox, mpv, a browser tab) over MPRIS, with no account and no setup.
  On Windows the same works for any player in Windows' media controls, except volume. She dances
  to the actual beat of the playing track.
- **Git.** With `strawberry git-hooks install` she reacts to your commits and pushes.
- **Talking.** `strawberry hotkey` binds Super+Shift+Space on GNOME (on Windows the tray holds
  Ctrl+Alt+Space): press it, speak, and she answers. Or right-click her and choose *Chat with
  Strawberry…* (or press T) to type. A plain music
  command is done in well under a second; anything else goes to the larger local model.
  While she works on something slower, a small chip under her bubble says what she is doing
  ("thinking…", "searching the web…", "Spotify: play") and its ✕ stops it. Saying "stop" or
  "never mind" does the same, and a new sentence takes over from the one she was on. Something
  already under way that changes things (a track starting) is let finish, and she says so.
- **Touch.** Drag her to move her. A quick tap pokes her: pat her shell, tickle her belly, poke an
  eye or a claw, or hold a finger on her, and she reacts to each; poke her a few times in a row
  and she gets a little annoyed. It works with a mouse or a touch screen. She turns a little
  toward the middle of the screen when she sits near an edge. *Touch reactions*, *Talk when
  poked* (a short line now and then, off by default) and *Turn toward the screen* are in her
  right-click menu.
- **Tools.** You can give her MCP servers (a calendar, notes, Spotify) in the config. None is
  configured out of the box. See [ADAPTERS.md](ADAPTERS.md).
- **Web search (optional).** With a local SearXNG and its MCP server configured, she searches
  when you ask her to ("look up…", "search the web for…", "google…") and when a question
  depends on something current (the weather, news, results, prices, opening hours, the latest
  version of something), and answers in a sentence or two, naming the site, never reading a
  link out. See [Web search](#web-search-optional).
- **Your notes (optional).** With a [recall](#your-notes-optional) notes server configured and
  logged in, "what did we decide about the orbs?" makes her search your notes, read the one or
  two that fit and answer in a sentence or two. She only reads, and only the workspaces you list.
- **A tray icon.** The 🍓 in the top bar (on Windows, the notification area) shows her state
  (idle, listening, thinking, talking) and has the same menu as right-clicking her: show/hide, chat, mute, quiet hour, volume, skin,
  top hat, settings file, restart, quit.

## Requirements

- Linux, or Windows 10 (version 2004 or later) or 11, on x86-64. macOS is not supported.
- Linux: X11 or XWayland (GNOME and KDE on Ubuntu, Fedora, Arch and similar, 2022 or later).
- Linux: PipeWire (`pw-record`, `pw-dump`) and `systemd --user`.
- Linux: a tray that speaks StatusNotifierItem. On GNOME that is the AppIndicator extension, which
  Ubuntu ships enabled. Without it she still works; her right-click menu has everything.
- Python 3.12 or later, and [uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io).
- [Ollama](https://ollama.com). `strawberry setup` offers to install it.
- A GPU. The default models are sized for a 24 GB NVIDIA card. Smaller cards work with a smaller
  brain model, and with no usable GPU she runs on the small model only (setup explains the
  choice for your card). AMD cards work for the models through Ollama's ROCm support; speech
  recognition then runs on the CPU, because its GPU engine is CUDA only. Intel graphics count
  as no usable GPU.

## Install

```bash
uv tool install strawberry-crab        # or: pipx install strawberry-crab
strawberry setup                       # the widget, Ollama and the models, a voice
strawberry                             # she appears
strawberry install                     # optional: start her on login, with the 🍓 tray icon
```

For speech recognition on an NVIDIA GPU install the CUDA extra instead:
`uv tool install 'strawberry-crab[gpu]'`. On AMD it does not apply.

### On Windows

1. Install [Ollama for Windows](https://ollama.com/download) (or `winget install Ollama.Ollama`)
   and open it once from the Start menu. `strawberry setup` pulls the models into it.
2. Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then in PowerShell or
   Windows Terminal:

   ```powershell
   uv tool install strawberry-crab        # or 'strawberry-crab[gpu]' for whisper on an NVIDIA card
   strawberry setup                       # models, a voice and the widget .exe
   strawberry                             # she appears
   strawberry install                     # optional: start her at login, with the tray icon
   ```

   The `[gpu]` extra is enough for whisper on CUDA; no CUDA toolkit is needed.
3. In Settings > Privacy & security, these must be on (they usually are):
   *Notifications* > "Let apps access your notifications" (she cannot read toasts without it),
   and, for speaking to her, *Microphone* > "Microphone access", "Let apps access your
   microphone" and "Let desktop apps access your microphone". `strawberry doctor` reads all of
   them and says which one is off.
4. The listen hotkey is Ctrl+Alt+Space while the tray runs (`strawberry hotkey COMBO` changes
   it; Windows keeps Win-key combinations for itself). The tray icon starts in the notification
   area's overflow (the ^ by the clock); drag it out to keep it in view.

`strawberry install` writes `Strawberry.lnk` into your Startup folder; `strawberry uninstall`
deletes it. There is no service, scheduled task or registry entry.

Known limits on Windows:

- Notifications are read once a second from the notification centre, so a toast shown and
  dismissed within that second, or one from an app that keeps its toasts out of the notification
  centre, is missed. Toasts carry no urgency, so `min_urgency = "critical"` drops all of them.
  Desktop apps usually give no logo, so her bubble has no app badge for them.
- Media: no volume control (Windows' media controls have none); two windows of one app are told
  apart by their order.
- The beat: only the player's own process tree is heard; a player muted in the volume mixer is
  silent to her; Firefox installed outside its default folder needs `[beat] target = "firefox"`.
- The hotkey works only while the tray runs.
- AMD cards get the CPU tier from `setup` (no rocm-smi on Windows).
- She has been run on Windows 11; Windows 10 from 2004 on is expected to work and is untried.

What each command does:

- **`strawberry setup`** can be run again at any time; it keeps the values already in your
  config. It reads your GPU memory and proposes models that fit, prints each model's licence
  before pulling it, offers Ollama's installer if Ollama is missing, downloads a Piper voice and
  the whisper model (~1.5 GB for `medium`; if you skip it, her first start fetches it in the
  background and she listens once it is done), downloads the widget binary for your version,
  backs up and updates `config.toml`, offers to add the settings your file does not have yet,
  and offers `strawberry install`. `--yes` takes the defaults without asking.
- **`strawberry install`** writes one systemd user unit, `strawberry-tray.service`, plus an
  autostart entry as a fallback (on Windows, a shortcut in the Startup folder). The tray starts
  the daemon, the watchers and the widget, and restarts any of them that dies.
  `strawberry uninstall` removes both.
- **`strawberry doctor`** checks what she depends on and says what is missing and how to fix
  it: the config, Ollama and each model, GPU memory, whisper and CUDA, the voice, the widget and
  its version, PipeWire, git, the tray host, whether the notification monitor is allowed, the
  media players on the bus, the systemd unit (and whether it still points at this install), the
  git hooks, the daemon, and whether the beat watcher is posting and listening to the player.
  On Windows it checks notification access, the media sessions, the microphone and its privacy
  switches, the Windows build, the Startup shortcut and the tray instead of the Linux parts.
  It only looks: it never starts, stops or restarts anything. It exits non-zero when something
  is broken. `strawberry doctor --talk` also runs a short scripted conversation through the running
  daemon and prints the time each model took. Please include its output in bug reports.

Other commands: `strawberry status`, `stop`, `restart`, `say "…"`, `talk`, `voices`,
`audition`, `tools`, `hotkey`, `git-hooks install|remove`. `strawberry --help` lists them all.

## Configuration

`strawberry config` opens `~/.config/strawberry/config.toml` (on Windows
`%APPDATA%\strawberry\config.toml`) in your editor, creating it with
every setting commented if it does not exist. Her right-click menu has the same thing as
*Settings file…*. Run `strawberry restart` after editing it.

The settings you are most likely to change:

| Section | Setting | What it does |
|---|---|---|
| `[notifications]` | `body`, `body_apps` | whether she reads message text; see [Privacy](#privacy) |
| `[notifications]` | `ignore_apps`, `only_apps`, `min_urgency` | which notifications she reacts to |
| `[media]` | `only`, `ignore` | which media players she follows |
| `[beat]` | `enabled` | `false` stops her listening to the player's audio for the beat |
| `[voice]` | `enabled`, `model`, `device` | speech recognition: whisper size, `cpu` or `cuda` |
| `[speech]` | `enabled`, `voice`, `quiet_hours`, `noise_scale`, `noise_w` | her voice (off by default; the bubble always shows); the two noise settings make it livelier |
| `[brain]`, `[thinker]` | `reaction_model`, `action_model` | which Ollama models she uses |
| `[tools.servers.*]` | | MCP servers; see [ADAPTERS.md](ADAPTERS.md), [Web search](#web-search-optional) and [Your notes](#your-notes-optional) |
| `[tools.servers.*]` | `url`, `workspaces` | a server reached over HTTP instead of a `command` (`strawberry tools login <name>` signs in); recall's readable workspaces |
| `[tools.servers.*]` | `flags`, `offer` | what a server without an add-on is (`private`, `foreign`, `egress`; unset: all three, the safe default) and when she gets its tools (`always`, `topic`, `asked`); see [ADAPTERS.md](ADAPTERS.md) |
| `[thinker]` | `max_tools`, `tool_tokens` | at most this many tool descriptions (30), and this many tokens of them (4000), in the big model's prompt; past either the least likely are left out |
| `[daemon]` | `log_sentences` | write what you say or type to the log (off: only its length); see [Privacy](#privacy) |
| `[learning]` | `log_outcomes` | keep what you say and how it was routed, locally, so she can learn from it (off); see [Learning](#learning) |
| `[learning]` | `auto_switch`, `weekly_line` | put a better router in use without asking (off); say once a week what she learned (off) |
| `[runs]` | `events`, `supersede`, `keep` | the chip under her bubble and the Brain UI's Runs (`events`); whether a new sentence stops the one she is on (`supersede`, on) or waits; how many finished runs the Brain UI keeps |
| `[approvals]` | `change_s`, `sends_s`, `destructive_s`, `grace_s`, `hold`, `risk` | the calls that wait for your yes: how long she waits per tier (10, 30, 30 s) and at most how much longer while you are still answering (10 s), which tiers need a press-and-hold on her card, and a tool's (or a server's) tier: `read`, `playback`, `change`, `sends`, `destructive` |
| `[thinker]` | `stream` | read the big model's reply as it is written, for the tokens-per-second gauge (on) |
| `[ledger]` | `turns`, `notices`, `window_minutes`, `foreign_minutes` | her short memory: your last exchanges (8) and what she reacted to on her own (8), none older than 60 minutes; for 10 minutes after a sender's or a track's name, a change asks first |
| `[messages]` | `enabled`, `keep`, `max_age_hours` | her inbox for "any new messages?": on, at most 100 notifications, none older than 24 hours, in memory only; message text only as `[notifications] body` allows |
| `[gate]` | `scorer` | how she sorts what you say: `head` (a trained head, the default) or `nearest` (each option's nearest examples); `strawberry gate eval` compares them |

Her persona is a file of its own: copy the shipped `persona.md` (in the package's `data/` folder,
or the Brain UI's *Her* tab) to `~/.config/strawberry/persona.md` and edit who she is, how she
talks, the example reactions she copies and her fixed lines. No restart: she reads it again when
it changes, and keeps the shipped persona while yours has a problem (the log and the Brain UI say
which).

Files she keeps: settings in `~/.config/strawberry/`, the widget binary and voices in
`~/.local/share/strawberry/`, logs and state in `~/.local/state/strawberry/`. The XDG variables
are respected. Her key for talking to the daemon is `bus-secret` in the state folder (see
[Privacy](#privacy)); delete it and restart her for a new one. On Windows: settings in `%APPDATA%\strawberry\`, the widget, voices and cache in
`%LOCALAPPDATA%\strawberry\`, logs and state in `%LOCALAPPDATA%\strawberry\state\`.

## Privacy

Everything runs on your machine. The models run in your local Ollama, speech recognition and
her voice run locally, and the daemon listens only on `127.0.0.1`. On its first start it makes a
random key, `~/.local/state/strawberry/bus-secret` (on Windows
`%LOCALAPPDATA%\strawberry\state\bus-secret`), readable only by you; her widget, her tray, the
`strawberry` command and the watchers present it, and the daemon takes nothing that would make her
act or speak without it, and gives nothing she hears or says to a program without it. Another
user on the computer, or a sandboxed app, can see that she moves (her state, her dance, that a
run is going on and how long its steps take) but not what she says, what you said or which tools
she used, and cannot type to her, answer her questions or open the Brain UI. Programs you run
yourself can read the key, as her widget does. If the key file was ever readable by others, she
makes a new one at her next start. Strawberry makes no network
requests of its own except to download what you ask for: the widget binary from this project's
GitHub releases, and models and voices from Ollama and Hugging Face during setup. An MCP server
you add is its own program and may use the network (the Spotify one talks to Spotify).

**Web search is where your words leave the machine.** With a web server configured (below), a
search query she writes from what you said goes to your SearXNG, which passes it on to the
search engines it is set up to ask, and a page she reads is fetched from its site. It is on
whenever the server is configured; remove the `[tools.servers.web]` table to turn it off. Only
your own spoken or typed sentences can lead to a search: notifications, media, git events and
her reactions never reach the search tools. The log says that a search ran, how many results came
back and how long it took, never the query and never a result; her answer from web results
is logged as its length, and her short-term memory keeps a placeholder instead of it. Text
from the web is treated as untrusted: while it is in her conversation, your recent exchanges
and what is playing are taken out of it, your other tools (Spotify) are refused, anything else
that would change something waits for your yes, a page can only be read from the results of that
same search, and nothing a page says can send her elsewhere.

**What the log keeps of a tool.** For Spotify, the web, and any server you have not described (see
[ADAPTERS.md](ADAPTERS.md)), only the tool's name, the names of its arguments, how many results and
how long they were: never what you asked for or what came back, even with `log_sentences` on.

What she reads:

- **Notifications.** A D-Bus monitor sees every desktop notification (on Windows, a reader of
  the notification centre, allowed by "Let apps access your notifications"). By default she uses only
  the app name and the sender; the message text never leaves the watcher process
  (`[notifications] body = "off"`). You can change that, for all apps or per app with `body_apps`:
  - `"off"`: app and sender only (the default).
  - `"react"`: the small local model reads the message and reacts in its own words, without
    quoting it.
  - `"glance"`: she first says a plain one-line gist ("Alex asks about lunch at noon."), then
    her reaction.

  To switch without editing the file, use the tray menu: **Message bodies ▸ Off / React /
  Glance**. It writes `body` into your config (backing it up first) and applies it at once;
  `body_apps` entries still win for their apps.

  In every mode a sensitive filter drops one-time codes, sign-in and password-reset messages and
  bank or card alerts before any model sees them; she says only "Slack sent something private."
  The filter fails closed: if its model check is unavailable, every body counts as private.

  So you can ask about them later, she keeps the notifications of the last 24 hours (at most 100)
  **in memory only**: lost when she restarts, never written to disk or logged. Each keeps what the
  setting above let her have: with `"off"` the app, the sender and the time; with `"react"` or
  `"glance"` the message too (its first 300 characters); a private one only the app. Asked what a
  message said, she answers as that setting allows: with `"react"` in her own words, with `"glance"`
  the gist she gave when it came, and never a link, an address, a number from it or a run of its
  words. While a message is part of what she is answering, your other tools that reach the network
  (web search, Spotify) are refused and anything that would change something waits for your yes.
  `[messages] enabled = false` turns the inbox off; `keep` and `max_age_hours` bound it.
- **Media.** The title, artist and album the player publishes over MPRIS (on Windows, to the
  media controls).
- **Audio.** Only the playing media player's own output stream (on Windows, the player's own
  process through WASAPI process loopback, after its volume in the mixer), and only to measure
  the tempo, where the bar starts and whether the track is in a build, a drop or a break.
  Nothing is recorded or stored. `[beat] enabled = false` turns it off.
- **Microphone.** Only after you press the hotkey (or run `strawberry listen`), until you stop
  speaking. The audio is transcribed locally in memory and never written to disk.
- **Git.** Only if you run `strawberry git-hooks install`: the repository and branch name, the
  commit subject, and for a push the remote's name and the number of commits.

No message text is written to any log, and neither is what you say or type to her: the log
says only how long a sentence was (`<sentence, 23 chars>`). `[daemon] log_sentences = true` writes
your sentences into the log as they are, which helps when tuning how she routes them. The first time she starts she says in her bubble that
notification bodies are off and where to change it, and logs the same note.

She remembers the last few exchanges of conversation in memory, for context, with what she reacted
to on her own (a notification's app and sender, a commit, a track; never a message's text), and what you ask her
to remember about you ("remember I prefer 24-hour time", "call me Sam") in `profile.md` beside
your settings: readable only by you, edited by hand or in the Brain UI, with every earlier version
kept in the state folder so a change can be undone ("forget that") or reverted. She writes to it
only from your own sentence, never while a notification, a song or a web page is part of what
she is answering, and says back what she saved. Nothing else is kept,
unless you turn on the learning log (`[learning] log_outcomes = true`, off by default). Then each
sentence you say or type is kept in `outcomes.jsonl` in the state directory, with how she routed
it and what came of it (an undo, a correction, a rephrase, or silence), so she can learn from
it (below). The file is readable only by you and pruned to 30 days
and 5000 records; sentences that read as private (codes, sign-ins, bank matters) and other
voices in the room are never kept, and no sentence goes into a log. `strawberry outcomes` shows
what is there, `strawberry outcomes --clear` deletes it. What she learned from it is kept in
`~/.local/share/strawberry/gate/learned.json`, also readable only by you;
`strawberry learning forget` deletes it.

## Learning

With the learning log on (`[learning] log_outcomes = true`), she learns from how you react to
what she does. When she skips a song and you say "go back" straight away, she learns that the
sentence did not mean skip. When she sends something to the big model and it just pauses the
music, she learns that the sentence was a plain "pause" she could have handled herself, faster.
A correction ("no, I meant…"), a rephrase and silence after a reflex count too. She never
learns from her own guesses, from other voices in the room or from anything private.

What she learns from is the small classifier that sorts what you say (the gate's head), not the
big model. When nobody has spoken to her for 20 minutes and there are at least 10 new labelled
sentences, she trains a new head in the background at low priority, and stops if you start
talking. The new head must do at least as well as the one in use on a fixed test set that ships
with her and that nothing you say ever enters. If it does not, it is rejected. If it does, it
waits for you:

```bash
strawberry learning status      # what is in use, what is waiting, what she has learned
strawberry learning accept      # put the waiting head in use (or: reject)
strawberry learning rollback    # back to the one before; every head is kept
strawberry learning versions    # every head, its test score and what became of it
strawberry learning report      # the week: "learned 14 new examples, 2 rejected, held-out 95.0% → 95.6%"
strawberry learning train       # build and test a new head now instead of waiting
strawberry learning examples --text   # the sentences she learned from, and what from
strawberry learning forget      # delete them (--all: and every head she made)
```

A switch takes effect within a few seconds; no restart is needed. The tray menu has
**Router: roll back to previous** while there is something to roll back to. With
`auto_switch = true` a head that passes the test is put in use without asking, and with
`weekly_line = true` she says once a week what she has got better at (never in quiet hours).
`idle_train = false` stops the background training; `idle_minutes` and `min_new_labels` set
when it runs. She learns only to answer her existing questions better; new kinds of request
still need a new version of Strawberry.

## Brain UI

```bash
strawberry ui
```

opens a page in your browser, served by her own daemon, that shows what she does with what you
say and what she has learned from it:

The page has five groups: **Her**, **Activity**, **Learning**, **Settings** and **System**, and a
spot in the header for whatever needs you now (her question waiting for a yes, a persona.md with a
problem, a new router head waiting).

- **Persona**: her persona.md in an editor, checked as you type (each section's size against its
  limit, what is wrong, the changes against the file), **Save** (the old file kept as
  `persona.md.bak`) and **Try it**, which shows what the small model says with your draft on a few
  made-up events before you save.
- **Profile**: what she knows about you, with every change she or you made and a way back to any of
  them.
- **Learning**: the head in use and the one waiting, side by side on the test set (fields right,
  wrong reflexes, each question), with Accept, Reject, Train now and Roll back; the sentences she
  learned from, each with Approve and Reject; the learning log; every head, with Use; the week.
- **Router live**: each sentence as she routes it: what she read it as, how sure she was, what
  handled it and how long it took.
- **Runs**: the timeline (what the big model is told happened lately, and which of it was written
  by others), and each thing she did, step by step (how she read it, thinking, each tool and how long it
  took, what she said, how it ended), with Cancel on the one going on. Tool names and timings
  only: never what you said, what a tool was given or what it found.
- **Data and scores**: what a head is trained and tested on, and each head's test score over time.
- **Settings and privacy**: what is kept and logged, and the line in `config.toml` that changes
  it, what applies at once and what needs a restart, **Apply** for the notification settings, the
  whole effective `config.toml` (read-only: the page does not edit it), and Forget.
- **System**: the models, where the gate runs, the head in use.

The link works once, within a minute; the page then runs on a cookie that ends after 12 hours
without use or when she restarts. Everything stays on this machine and the page loads nothing
from the internet. Sentences show only while the learning log is on. No other web page can
reach her: every other address refuses browsers, and this one takes only its own page, signed
in. The sign-in link needs her key (see [Privacy](#privacy)), so other users on the computer cannot
get one; programs you run yourself can. No browser opening (a remote shell, say)?
`strawberry ui --no-browser` prints the link.

## Spotify (optional)

Music control works for every player without Spotify's API. If you want more (play an artist,
a playlist, save a track), install the separate Spotify MCP server
[panuhen/spotify-mcp](https://github.com/panuhen/spotify-mcp), authorise it with your Spotify
account, and add it to the config as shown in [ADAPTERS.md](ADAPTERS.md). Strawberry's Spotify
adapter then recognises it and adds its reflexes and your artist and playlist names. "I like this"
saves the playing track to your Liked Songs, and "add this to my gym playlist" or "play my
running playlist" find the playlist by name.

Removals are asked about first. "Take this off my gym playlist" gets "Remove 'Teardrop' from Gym?
Say yes." and nothing happens until you answer: yes (sure, do it, go ahead) removes that track,
even if the song has changed meanwhile; no, anything else, or 10 seconds without an answer leaves
it, and she says so; a late yes gets "ask me again". Anything else you said is still done. Only your own spoken or typed sentence
can answer, never a notification. The list is the server's `confirm` key, next to `careful`:

```toml
[tools.servers.spotify]
confirm = ["remove_from_playlist", "remove_saved_tracks"]   # the default; [] turns the question off
```

`[approvals] change_s` sets how long she waits (the old `[actions] confirm_s` still works). Saves
and adds are never asked about. Any call a server's tools make that sends something to someone
(`sends`) or deletes something for good (`destructive`) is asked about the same way, with 30
seconds to answer; `[approvals] risk` sets a tool's tier. Besides saying or typing yes or no, you
can answer in the Brain UI's Runs section, and the widget will show the question as a card.

## Web search (optional)

She searches through a [SearXNG](https://docs.searxng.org/) instance you run yourself, with its
JSON output on (`search.formats` includes `json`), and the
[mcp-searxng](https://www.npmjs.com/package/mcp-searxng) MCP server in front of it (Node.js 20 or
newer). Add to the config:

```toml
[tools.servers.web]
topic = "other"
command = "/full/path/to/npx"          # `command -v npx`; the tray's service may not have it on PATH
args = ["-y", "mcp-searxng@2.5.1"]
env = { SEARXNG_URL = "http://127.0.0.1:8888", NODE_OPTIONS = "--dns-result-order=ipv4first", PATH = "/dir/of/node:/usr/bin:/bin" }
```

With mcp-searxng she answers from the search results but reads no page of them: its listing
keeps a page's line breaks, so a result's own URL cannot be told from one a page wrote
(ADAPTERS.md, *Which pages can be read*). A server that lists results as JSON, or as numbered
results with one line each, lets her read one page a question.

`PATH` is needed when Node.js is not on the service's own PATH (a Node from nvm is not): `npx`
starts `node` by name. `NODE_OPTIONS` makes the page reader use IPv4 first; without it, on a
machine with no IPv6 route, every page read timed out after 10 s. The name `web` picks her web
adapter (ADAPTERS.md); a server listing the same tools gets it under any name. Then
`strawberry restart`, and `strawberry tools` should list `searxng_web_search` and
`web_url_read`. If `npx` is missing or SearXNG is down, the daemon logs a warning and carries
on, and she says search isn't available.

## Your notes (optional)

She can read your notes in [recall](ADAPTERS.md#the-recall-adapter), a markdown knowledge base served
as a remote MCP server with an OAuth login. She only searches and reads: every tool of the server
that writes is never offered to her and is refused if anything asks for it. Add to the config:

```toml
[tools.servers.recall]
topic = "notes"
url = "https://recall.example.com/mcp"   # your recall server's MCP address
workspaces = ["My project"]              # the only workspaces she may read, by name or id
```

Then sign in once:

```bash
strawberry tools login recall        # opens the sign-in page; --no-browser only prints its address
```

The sign-in page is your recall server's own. After it, the page sends your browser back to a
listener on 127.0.0.1 that `login` started on a random port, and the tokens are saved in
`~/.local/state/strawberry/tokens/recall.json` (on Windows under `%LOCALAPPDATA%`), readable by you
alone. They are never logged or printed, and never shown in `/health`, `/config` or the Brain UI.
She refreshes them herself; when the server no longer takes them she says she can't reach your
notes, and `strawberry tools` shows "recall needs a login: run `strawberry tools login recall`".
`strawberry tools logout recall` deletes them. No restart is needed either way.

The server must be on a public address (or on this machine, for one you run locally): every
connection goes to an address checked to be public, so a server on your LAN is not reachable.

`workspaces` is an allowlist: a note in any other workspace is dropped from her search results
before the model sees it, and a note whose workspace she cannot tell is not read. With an empty
list she reads nothing, and the log says so. She is offered the notes tools only for a sentence
about notes, decisions or plans ("what did we decide…", "check my notes…") or one the router reads
as being about notes, so the rest of the time they cost nothing. A note's text counts as
strangers' text (notes quote web pages and other people): once she has read one, your private
context leaves that conversation and anything that would change something waits for your yes,
as after a web search. Your query goes to your recall server; the log says how many notes came
back and how long it took, never the query or a note.

## Developers

Start with [WIRING.md](WIRING.md): the message contract, the daemon, the watchers, the gate, the
widget and the tray. [PACKAGING.md](PACKAGING.md) is the plan for the package and the release
process; [PROTOCOL.md](PROTOCOL.md) is the wire protocol for writing another body;
[ADAPTERS.md](ADAPTERS.md) covers MCP servers and adapters; [CHANGELOG.md](CHANGELOG.md)
lists what changed in each version.

From a checkout, `bin/strawberry` stands in for `strawberry` and runs from the checkout's `.venv`.
Without an exported widget binary it runs the Godot project in `widget/` with `godot` 4.7 from
PATH.

```bash
uv sync --inexact --group gpu        # never a bare `uv sync`: it prunes the CUDA wheels
bin/strawberry                       # daemon, watchers, widget
.venv/bin/python -m pytest -q        # tests
scripts/build_widget.sh              # export dist/strawberry-widget-<version>-linux-x86_64
scripts/check_phase1.sh              # tests, then a headless widget against a real daemon
```

On Windows the same scripts run under Git Bash: `scripts/build_widget.sh` exports
`dist/strawberry-widget-<version>-windows-x86_64.exe` with the templates in
`%APPDATA%\Godot\export_templates\4.7.2.stable\`, and `scripts/check_phase1.sh` uses
`.venv\Scripts`. `GODOT=` names another Godot binary (the `_console.exe` one shows its output).

**Setup and doctor.** `strawberry setup` reads the GPU's VRAM (nvidia-smi; for AMD rocm-smi, else the amdgpu driver's `mem_info_vram_total` in sysfs) and proposes the models that fit: the tested setup on a 24 GB card (Qwen 27B brain, `gemma3:1b`, `embeddinggemma`, whisper `medium` on CUDA, Piper `en_GB-alba-medium`), a smaller Qwen3 for 16, 10 and 6 GB, and no brain on the CPU. On an AMD card the brain rows are the same (Ollama runs them on ROCm) and whisper is `small` on the CPU in every tier, since faster-whisper's GPU engine is CUDA only; Intel and other cards get the CPU tier. Any slot can be typed over. It writes the choices into `config.toml` (backing it up first and keeping the values already there), prints each model's licence, runs `ollama pull` for what is missing, downloads the gate's embeddinggemma for ONNX Runtime (~1.2 GB, from Hugging Face into the data dir; the gate runs it in the daemon's process on the CPU, ~20 ms a sentence, and uses Ollama's `embeddinggemma` until it is there), the voice and the whisper model (into the Hugging Face cache; left out, the daemon downloads it on its first start, in the background, and `/health` says so), fetches the widget if the installed one is another version, offers to append the settings the file does not have, and offers `strawberry install`. The models are not part of this package: you download each from its publisher, under its own terms. `--yes` takes the defaults without asking (`--install` also installs; `--tier 10gb` picks a tier); `--no-download` writes the config but downloads nothing, naming each model, voice, whisper model and widget it would fetch. `strawberry doctor` checks Ollama and each model, where the gate's model runs, the GPU, whisper and CUDA, the voice, the widget version, PipeWire, git, the tray host, the config, whether the bus allows the notification monitor (a `BecomeMonitor` with a rule that matches nothing, closed at once), the MPRIS players on the bus, `strawberry-tray.service` (installed, enabled, active, and whether its ExecStart still exists and is this install), the git hooks (core.hooksPath and both hook files, and the `strawberry` they call), the daemon, and the beat watcher (posting while a player plays, from `/health`'s `tempo_age_s`, and linked to the player's stream rather than a sink, from `pw-dump`), prints ✓ / ! / ✗ with a fix per problem and exits 1 if anything is ✗; `doctor --talk` also times a short scripted conversation through the running daemon, per slot.

**Which widget runs.** `strawberry widget` and the tray run the installed binary when it is there, else the checkout's Godot project with `godot` on PATH, else they say to run `strawberry widget --fetch`. A binary built from a checkout (`scripts/build_widget.sh`, needs Godot 4.7.2 and its export templates) can be tried by hand: `dist/strawberry-widget-<version>-linux-x86_64 --display-driver x11 -- --ws=ws://127.0.0.1:8770/ws` (on Windows `dist\strawberry-widget-<version>-windows-x86_64.exe -- --ws=ws://127.0.0.1:8770/ws`). The widget tells the daemon its version; a source run says `dev` and is always accepted, a release on another major version is refused (WIRING §1).

Entry points: `strawberry` (the CLI; `strawberry --help`), `strawberryd` (the daemon alone), `strawberry-doorway mpris_watch|notify_watch|beat_watch` (Windows: `smtc_watch|toast_watch|beat_watch`), `strawberry-tray` (the tray without a console window, for the Windows Startup shortcut). Files: config and the widget's preferences `~/.config/strawberry/` (`config.toml`, `widget.cfg`), voices and the widget binary `~/.local/share/strawberry/` (`voices/`, `widget/`), state and logs `~/.local/state/strawberry/` (XDG variables respected). `uv build` makes the wheel.

End-to-end check: `scripts/check_phase1.sh` (unit tests, then a headless widget against a real daemon; `WIDGET=dist/strawberry-widget-… scripts/check_phase1.sh` runs the same checks in the exported binary); `scripts/check_tray.sh` registers the tray and reads it back.

## Licence

MIT, see [LICENSE](LICENSE). Models and voices are downloaded by you under their own licences;
[THIRD_PARTY.md](THIRD_PARTY.md) lists them and the libraries she uses.
