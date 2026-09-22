# Strawberry

A desktop mascot for Linux: a small cel-shaded crab that sits on top of your windows and reacts
to what happens on the machine. She comments on notifications, dances to the beat of whatever is
playing, cheers your git commits, and answers when you talk or type to her. Every model runs on
your own computer through [Ollama](https://ollama.com); nothing is sent anywhere.

## What she does

- **Notifications.** She sees each desktop notification and reacts to it in a line of her own.
  By default she knows only the app and the sender, never the message (see [Privacy](#privacy)).
- **Music.** Skip, previous, pause, resume, volume and "what song is this" work for any desktop
  player (Spotify, VLC, Rhythmbox, mpv, a browser tab) over MPRIS, with no account and no setup.
  She dances to the actual beat of the playing track.
- **Git.** With `strawberry git-hooks install` she reacts to your commits and pushes.
- **Talking.** `strawberry hotkey` binds Super+Shift+Space on GNOME: press it, speak, and she answers.
  Or right-click her and choose *Chat with Strawberry…* (or press T) to type. A plain music
  command is done in well under a second; anything else goes to the larger local model.
- **Tools.** You can give her MCP servers (a calendar, notes, Spotify) in the config. None is
  configured out of the box. See [ADAPTERS.md](ADAPTERS.md).
- **A tray icon.** The 🍓 in the top bar shows her state (idle, listening, thinking, talking)
  and has the same menu as right-clicking her: show/hide, chat, mute, quiet hour, volume, skin,
  top hat, settings file, restart, quit.

## Requirements

- Linux with X11 or XWayland (GNOME and KDE on Ubuntu, Fedora, Arch and similar, 2022 or later).
- PipeWire (`pw-record`, `pw-dump`) and `systemd --user`.
- A tray that speaks StatusNotifierItem. On GNOME that is the AppIndicator extension, which
  Ubuntu ships enabled. Without it she still works; her right-click menu has everything.
- Python 3.12 or later, and [uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io).
- [Ollama](https://ollama.com). `strawberry setup` offers to install it.
- A GPU. The default models are sized for a 24 GB NVIDIA card. Smaller cards work with a smaller
  brain model, and with no usable GPU she runs on the small model only (setup explains the
  choice for your card).

## Install

```bash
uv tool install strawberry-crab        # or: pipx install strawberry-crab
strawberry setup                       # the widget, Ollama and the models, a voice
strawberry                             # she appears
strawberry install                     # optional: start her on login, with the 🍓 tray icon
```

For speech recognition on the GPU install the CUDA extra instead:
`uv tool install 'strawberry-crab[gpu]'`.

What each command does:

- **`strawberry setup`** can be run again at any time and picks up where it stopped. It
  downloads the widget binary for your version and checks its SHA-256, finds or installs Ollama,
  reads your GPU memory and proposes models that fit, prints each model's licence before pulling
  it, downloads a Piper voice, checks for the PipeWire tools, git and the GNOME AppIndicator
  extension (and prints the install line for your distro when one is missing), writes
  `config.toml` if you have none, and offers `strawberry install` and `strawberry git-hooks install`.
- **`strawberry install`** writes one systemd user unit, `strawberry-tray.service`, plus an
  autostart entry as a fallback. The tray starts the daemon, the watchers and the widget, and
  restarts any of them that dies. `strawberry uninstall` removes both.
- **`strawberry doctor`** checks everything she depends on and says what is missing and how to
  fix it: the daemon, the widget and its version, the tray, Ollama and each model, GPU memory,
  the voice, the microphone device, PipeWire, the D-Bus session, the notification monitor, media
  players, the beat watcher, the systemd unit and the git hooks. It ends with a short scripted
  conversation and the time each model took. It exits non-zero when something you can fix is
  missing. Please include its output in bug reports.

Other commands: `strawberry status`, `stop`, `restart`, `say "…"`, `talk`, `voices`,
`audition`, `tools`, `hotkey`, `git-hooks install|remove`. `strawberry --help` lists them all.

## Configuration

`strawberry config` opens `~/.config/strawberry/config.toml` in your editor, creating it with
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
| `[speech]` | `enabled`, `voice`, `quiet_hours` | her voice (off by default; the bubble always shows) |
| `[brain]`, `[thinker]` | `reaction_model`, `action_model` | which Ollama models she uses |
| `[tools.servers.*]` | | MCP servers; see [ADAPTERS.md](ADAPTERS.md) |

Files she keeps: settings in `~/.config/strawberry/`, the widget binary and voices in
`~/.local/share/strawberry/`, logs and state in `~/.local/state/strawberry/`. The XDG variables
are respected.

## Privacy

Everything runs on your machine. The models run in your local Ollama, speech recognition and
her voice run locally, and the daemon listens only on `127.0.0.1`. Strawberry makes no network
requests of its own except to download what you ask for: the widget binary from this project's
GitHub releases, and models and voices from Ollama and Hugging Face during setup. An MCP server
you add is its own program and may use the network (the Spotify one talks to Spotify).

What she reads:

- **Notifications.** A D-Bus monitor sees every desktop notification. By default she uses only
  the app name and the sender; the message text never leaves the watcher process
  (`[notifications] body = "off"`). You can change that, for all apps or per app with `body_apps`:
  - `"off"`: app and sender only (the default).
  - `"react"`: the small local model reads the message and reacts in its own words, without
    quoting it.
  - `"glance"`: she first says a plain one-line gist ("Alex asks about lunch at noon."), then
    her reaction.

  In every mode a sensitive filter drops one-time codes, sign-in and password-reset messages and
  bank or card alerts before any model sees them; she says only "Slack sent something private."
  The filter fails closed: if its model check is unavailable, every body counts as private.
- **Media.** The title, artist and album the player publishes over MPRIS.
- **Audio.** Only the playing media player's own output stream, and only to measure the tempo.
  Nothing is recorded or stored. `[beat] enabled = false` turns it off.
- **Microphone.** Only after you press the hotkey (or run `strawberry listen`), until you stop
  speaking. The audio is transcribed locally in memory and never written to disk.
- **Git.** Only if you run `strawberry git-hooks install`: the repository and branch name, the
  commit subject, and for a push the remote's name and the number of commits.

No message text is written to any log. The first time she starts she says in her bubble that
notification bodies are off and where to change it, and logs the same note.

She remembers the last few exchanges of conversation in memory, for context, and nothing more.

## Spotify (optional)

Music control works for every player without Spotify's API. If you want more (play an artist,
a playlist, save a track), install the separate Spotify MCP server
[panuhen/spotify-mcp](https://github.com/panuhen/spotify-mcp), authorise it with your Spotify
account, and add it to the config as shown in [ADAPTERS.md](ADAPTERS.md). Strawberry's Spotify
adapter then recognises it and adds its reflexes and your artist and playlist names.

## Developers

Start with [WIRING.md](WIRING.md): the message contract, the daemon, the watchers, the gate, the
widget and the tray. [PACKAGING.md](PACKAGING.md) is the plan for the package and the release
process; [ADAPTERS.md](ADAPTERS.md) covers MCP servers and adapters; [CHANGELOG.md](CHANGELOG.md)
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

## Licence

MIT, see [LICENSE](LICENSE). Models and voices are downloaded by you under their own licences;
[THIRD_PARTY.md](THIRD_PARTY.md) lists them and the libraries she uses.
