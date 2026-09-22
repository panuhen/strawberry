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

- Linux only for now; Windows and macOS are not supported yet.
- X11 or XWayland (GNOME and KDE on Ubuntu, Fedora, Arch and similar, 2022 or later).
- PipeWire (`pw-record`, `pw-dump`) and `systemd --user`.
- A tray that speaks StatusNotifierItem. On GNOME that is the AppIndicator extension, which
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

What each command does:

- **`strawberry setup`** can be run again at any time; it keeps the values already in your
  config. It reads your GPU memory and proposes models that fit, prints each model's licence
  before pulling it, offers Ollama's installer if Ollama is missing, downloads a Piper voice,
  downloads the widget binary for your version, backs up and updates `config.toml`, offers to
  add the settings your file does not have yet, and offers `strawberry install`. `--yes` takes
  the defaults without asking.
- **`strawberry install`** writes one systemd user unit, `strawberry-tray.service`, plus an
  autostart entry as a fallback. The tray starts the daemon, the watchers and the widget, and
  restarts any of them that dies. `strawberry uninstall` removes both.
- **`strawberry doctor`** checks what she depends on and says what is missing and how to fix
  it: the config, Ollama and each model, GPU memory, whisper and CUDA, the voice, the widget and
  its version, PipeWire, git, the tray host, whether the notification monitor is allowed, the
  media players on the bus, the systemd unit (and whether it still points at this install), the
  git hooks, the daemon, and whether the beat watcher is posting and listening to the player.
  It only looks: it never starts, stops or restarts anything. It exits non-zero when something
  is broken. `strawberry doctor --talk` also runs a short scripted conversation through the running
  daemon and prints the time each model took. Please include its output in bug reports.

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

**Setup and doctor.** `strawberry setup` reads the GPU's VRAM (nvidia-smi; for AMD rocm-smi, else the amdgpu driver's `mem_info_vram_total` in sysfs) and proposes the models that fit: the tested setup on a 24 GB card (Qwen 27B brain, `gemma3:1b`, `embeddinggemma`, whisper `medium` on CUDA, Piper `en_GB-alba-medium`), a smaller Qwen3 for 16, 10 and 6 GB, and no brain on the CPU. On an AMD card the brain rows are the same (Ollama runs them on ROCm) and whisper is `small` on the CPU in every tier, since faster-whisper's GPU engine is CUDA only; Intel and other cards get the CPU tier. Any slot can be typed over. It writes the choices into `config.toml` (backing it up first and keeping the values already there), prints each model's licence, runs `ollama pull` for what is missing, downloads the voice, fetches the widget if the installed one is another version, offers to append the settings the file does not have, and offers `strawberry install`. The models are not part of this package: you download each from its publisher, under its own terms. `--yes` takes the defaults without asking (`--install` also installs; `--tier 10gb` picks a tier). `strawberry doctor` checks Ollama and each model, the GPU, whisper and CUDA, the voice, the widget version, PipeWire, git, the tray host, the config, whether the bus allows the notification monitor (a `BecomeMonitor` with a rule that matches nothing, closed at once), the MPRIS players on the bus, `strawberry-tray.service` (installed, enabled, active, and whether its ExecStart still exists and is this install), the git hooks (core.hooksPath and both hook files, and the `strawberry` they call), the daemon, and the beat watcher (posting while a player plays, from `/health`'s `tempo_age_s`, and linked to the player's stream rather than a sink, from `pw-dump`), prints ✓ / ! / ✗ with a fix per problem and exits 1 if anything is ✗; `doctor --talk` also times a short scripted conversation through the running daemon, per slot.

**Which widget runs.** `strawberry widget` and the tray run the installed binary when it is there, else the checkout's Godot project with `godot` on PATH, else they say to run `strawberry widget --fetch`. A binary built from a checkout (`scripts/build_widget.sh`, needs Godot 4.7.2 and its export templates) can be tried by hand: `dist/strawberry-widget-<version>-linux-x86_64 --display-driver x11 -- --ws=ws://127.0.0.1:8770/ws`. The widget tells the daemon its version; a source run says `dev` and is always accepted, a release on another major version is refused (WIRING §1).

Entry points: `strawberry` (the CLI; `strawberry --help`), `strawberryd` (the daemon alone), `strawberry-doorway mpris_watch|notify_watch|beat_watch`. Files: config and the widget's preferences `~/.config/strawberry/` (`config.toml`, `widget.cfg`), voices and the widget binary `~/.local/share/strawberry/` (`voices/`, `widget/`), state and logs `~/.local/state/strawberry/` (XDG variables respected). `uv build` makes the wheel.

End-to-end check: `scripts/check_phase1.sh` (unit tests, then a headless widget against a real daemon; `WIDGET=dist/strawberry-widget-… scripts/check_phase1.sh` runs the same checks in the exported binary); `scripts/check_tray.sh` registers the tray and reads it back.

## Licence

MIT, see [LICENSE](LICENSE). Models and voices are downloaded by you under their own licences;
[THIRD_PARTY.md](THIRD_PARTY.md) lists them and the libraries she uses.
