# Packaging plan: from developer checkout to `uv tool install strawberry`

Status: agreed 2026-09-22, work starts the same day. Built by Opus agents step by step; the
orchestrator verifies each step (tests, acceptance, live run) before the next starts.

## Goal

A Linux user runs one command and gets the daemon, the doorways, the tray icon and the widget, with
the models and a voice fetched on first run from where they are published. No Godot editor, no
cloning, no hunting for apt packages.

```bash
uv tool install strawberry        # or: pipx install strawberry
strawberry setup                  # widget binary, Ollama + models, a voice; explains each step
strawberry                        # she appears, with a 🍓 in the top bar
strawberry install                # optional: start on login
```

Scope: Linux desktops with X11 or XWayland, PipeWire and `systemd --user`. That is GNOME and KDE on
Ubuntu, Fedora, Arch and friends since ~2022. Windows/macOS are separate projects (the widget would
run; the notification, media and audio-capture doorways would each need a native replacement).

## Decisions (2026-09-22)

- **We ship code, not models.** The package contains our code, her GLB, the widget binary and the
  icon. `strawberry setup` pulls the models from Ollama's library, the Piper voice from the Rhasspy
  release, and faster-whisper fetches its own weights. The user downloads each under its own terms;
  setup prints one line per model naming its licence (Gemma terms of use, Apache-2.0 for Qwen, ...).
  Our own licence is MIT; every runtime dependency is MIT or similar (Godot, Piper, faster-whisper,
  the MCP SDK, jeepney, aiohttp, numpy).
- **The default config is the tested setup**: `qwen3.8:27b` (brain), `gemma3:1b` (desktop voice),
  `embeddinggemma` (gate), whisper `medium` on CUDA, Piper `en_GB-alba-medium`. It needs a 24 GB
  card. Every slot is a config value already; setup detects VRAM and is honest about what fits
  (see step 5) and MODELS.md documents the constraint per slot.
- **Shell and adapters.** The core knows nothing about music: it connects whatever MCP servers the
  config lists and gives the brain their tools. Basic music control (skip, pause, resume, volume,
  what's playing) runs over MPRIS for any player with no server at all. Spotify becomes the first
  optional *adapter* (situation text, whisper vocabulary, error rewording, gate examples) that
  lights up when a matching server is configured; it leaves the shipped defaults and is documented
  as the example of how to add one.
- **A tray icon replaces the autostart.** `strawberry tray` is a D-Bus StatusNotifierItem (jeepney)
  with the 🍓 emoji rendered to PNGs at build time (Noto Color Emoji, Apache-2.0). It is the one
  process that starts on login; it launches the daemon, the doorways and the widget, and its menu
  is the crab's right-click menu minus the appearance items: show/hide her, chat box, mute, quiet
  hour, restart, quit, plus a status line (listening / thinking). GNOME needs the AppIndicator
  extension (Ubuntu ships it on); without it nothing breaks, the right-click menu covers everything,
  and `doctor` says which case you are in.

## Why not the sandboxed formats

Flatpak and Snap fight what she does: monitoring the notification bus, capturing another app's
PipeWire stream, writing systemd user units and global git hooks, opening the config in the user's
editor, spawning the widget. Each needs a permission hole and some cannot be granted. A `.deb`
would work but is Ubuntu/Debian only; Nix only for Nix users. A Python package plus a standalone
widget binary reaches every distro with the same artefact.

## What blocks a single-command install today

| Assumption now | Fix (step) |
|---|---|
| `godot` on PATH; the widget runs from `widget/` source | Export a standalone Linux binary per release; the CLI downloads the matching one (4) |
| Doorways run on `/usr/bin/python3` for PyGObject (`gi`) | Rewrite the two D-Bus watchers on jeepney so they live in the package (1) |
| `numpy` from the system for the beat watcher | Package dependency (2) |
| `bin/strawberry` bash launcher finds files via the repo | Python CLI entry point; assets in XDG data dir (2) |
| Widget finds the repo via `res://..` (menu: settings file, restart) | XDG paths and daemon commands (4) |
| ~~Music control needs the Spotify MCP server~~ | done (3): MPRIS reflexes for any player; Spotify is an adapter |
| Ollama present with the models pulled | `strawberry setup` installs Ollama (their script) and pulls the models (5) |
| Piper voice already downloaded | `strawberry setup` downloads the default voice; `strawberry voices` for more (5) |
| `jq`, `curl` in the git hooks and `say` | Hooks call `strawberry git-event` (Python); `say` is Python (2) |
| `pw-record`, `pw-dump` | Runtime check with the apt/dnf line in the message (5) |
| No licence | MIT (6) |

## Steps

### 1. jeepney: the D-Bus watchers and the tray — **done 2026-09-22**

Evidence: `strawberryd/.venv/bin/python -m pytest -q` → 230 passed (186 before); `scripts/check_tray.sh` registered `org.kde.StatusNotifierItem-<pid>-1`, `busctl --user` read back its properties and the whole menu, and the watcher listed it; the 🍓 showed in the GNOME top bar (screenshot), and `show` / `hide` / `chat` / `skin` / `hat` / `volume` / `mute` / `sleep_after` reached a live widget through `POST /command`. Two departures from the plan below, both deliberate: the tray menu carries **every** preference her right-click menu has, appearance included (asked for during the work), and `IconName` is left empty unless the icon is installed in a theme, because GNOME's AppIndicator extension draws a placeholder for a name it cannot resolve.


- Rewrite `doorways/mpris_watch.py` and `doorways/notify_watch.py` on **jeepney** (pure Python,
  asyncio-friendly, supports `BecomeMonitor` via raw messages and `PropertiesChanged` signals),
  as modules inside the daemon's package so they run on the venv's Python. Keep the behaviour and
  the gotchas in WIRING.md §4 (the monitor filter consumes `Notify`; every Notify crosses the bus
  twice → content dedupe; all MPRIS players, not just Spotify). Separate processes, as now: a D-Bus
  hiccup cannot take the websocket down.
- `strawberry tray`: a StatusNotifierItem on the session bus (`org.kde.StatusNotifierItem`,
  registered with `org.kde.StatusNotifierWatcher`, a `com.canonical.dbusmenu` menu). Icon pixmaps
  from `assets/icons/strawberry-{16,22,24,32,48}.png`, rendered once from Noto Color Emoji by a
  build script that is checked in with its output. Menu: Show her / Hide her, Chat with
  Strawberry…, Mute her voice, Quiet for an hour, Restart, Quit; a disabled status row. The tray
  talks to the daemon over HTTP and the widget over the daemon's command channel (`{"command":
  "show"|"hide"|"chat"}` on the websocket, added to the widget).
- The tray is the login process: it starts the daemon, the doorways and the widget as children,
  restarts a child that dies, and stops them on Quit. `strawberry install` writes one systemd user
  unit (`strawberry-tray.service`) and one autostart entry as a fallback; the current per-doorway
  units go away.
- Tests: jeepney message building/parsing on recorded bus traffic (no live bus in unit tests), the
  tray's menu model and command mapping, a live check script that registers the item and lists it.

### 2. One Python package (`strawberry`) and the CLI

- Rename the project `strawberryd` → `strawberry`; keep `strawberryd` as a console script alias.
  `strawberry/doorways/` holds the watchers and the beat tracker. Entry points: `strawberry` (CLI),
  `strawberryd` (daemon), `strawberry-doorway <name>` (for the tray to spawn).
- Dependencies: aiohttp, piper-tts, onnxruntime, numpy, jeepney, mcp, faster-whisper, sounddevice.
  Groups: `gpu` (cuBLAS/cuDNN wheels), `dev` (pytest). `uv sync --inexact` in every script.
- Port `bin/strawberry` to `strawberry/cli.py` (argparse): `widget`, `daemon`, `tray`, `status`,
  `stop`, `restart`, `config`, `say`, `voices`, `audition`, `talk`, `route`, `tools`, `tool`,
  `think`, `install`, `uninstall`, `setup`, `doctor`, `git-event`, `git-hooks install|remove`.
  `bin/strawberry` becomes a thin shim to the CLI for the developer checkout.
- Assets in XDG: config `~/.config/strawberry/`, data (voices, widget binary, icons)
  `~/.local/share/strawberry/`, state (token caches, logs) `~/.local/state/strawberry/`.
- Tests move with the code; `check_phase1.sh` stays a dev script.

### 3. Shell and adapters ✅ (2026-09-22)

Done: `strawberryd/mpris.py` (jeepney), `strawberryd/adapters/{base,spotify}.py` with the registry,
`[thinker] max_tools`, and an empty `[tools.servers]` default. Evidence: 215 tests pass (was 186),
`scripts/gate_check.py` 71/71 with a Spotify server and 69/71 without (only the two library
sentences that need one), and a daemon on port 8772 did "what song is this", "skip this", "pause"
and the volume pair over `mpris.*` with no server configured, then the same sentences over
`spotify.*` with the server back — MPRIS 9/403/7 ms against Spotify's 463/1201/425 ms. The 25
Spotify tool schemas measure 2382 prompt tokens, not the ~360 assumed below.

- **MPRIS reflexes**: skip, previous, pause, resume, volume up/down, now playing over
  `org.mpris.MediaPlayer2.Player` (jeepney, session bus) for the active player. The gate's bare
  reflexes route here when no music server is configured, and to the server's reflexes when one
  is. Same facts sentences, same Gemma quip.
- **Adapters** live in `strawberry/adapters/<name>.py` and declare: the server name they match,
  reflexes (optional), situation text, whisper vocabulary, error rewording, extra gate examples,
  and a short "common tools" list the brain gets first when the context is tight. The core loads
  an adapter only when a configured server matches it. Spotify is the first adapter; its server
  (a community MCP wrapper, not ours) is *not* in the default config. `default_toml()` shows a
  commented example of adding a server; ADAPTERS.md explains how to write one.
- The brain's context: `[thinker] num_ctx` 8192 and 25 Spotify tools ≈ 360 prompt tokens today;
  with several servers the gate's TOPIC picks whose tools go first and adapters' common lists
  keep the schema count down. Measure and document.
- Tests: MPRIS reflexes on a fake bus, adapter loading with and without a matching server, the
  existing Spotify tests moved under the adapter.

### 4. Widget as a standalone binary

- Godot export preset `Linux/X11` (x86_64, embedded PCK), built with the 4.7.2 export templates.
  Output `strawberry-widget-<version>-linux-x86_64`. Test: transparent window, always-on-top,
  passthrough, audio bus, all with the exported build (verify `per_pixel_transparency` and
  `--display-driver x11` still apply once exported).
- Remove the repo assumptions in `menu.gd`: settings file path from XDG config, "Apply settings"
  via the `strawberry` CLI on PATH or a daemon endpoint, voices dir from XDG data. Captures and
  acceptance flags stay.
- Widget commands `show`, `hide`, `chat` (step 1 needs them; land them here if step 1 is first).
- The widget reports its version in the websocket `hello`; the daemon logs a warning on mismatch
  and refuses a major mismatch.
- Where it lives: `~/.local/share/strawberry/widget/strawberry-widget`; the CLI downloads it from
  the GitHub release matching the installed package version and checks a SHA-256 from the release.

### 5. `strawberry setup` and `strawberry doctor`

`setup` is idempotent, verbose and resumable:

1. Widget binary (download + checksum) unless present and matching.
2. Ollama: detect `ollama` on PATH or the API on :11434; otherwise offer to run the official
   installer.
3. **Models, by what fits.** Read VRAM (nvidia-smi / rocm-smi / none). Above ~22 GB free: pull
   the defaults with one confirmation. Below: show the defaults, say the brain will not fit,
   propose the documented smaller brain for that card, and let the user type any Ollama model
   name per slot. No usable GPU: offer the Gemma-only mode (thinker disabled; reflexes, chat and
   the desktop voice still work). Print each model's licence line before pulling. Write the
   choices into `config.toml`. MODELS.md carries the constraint per slot: the brain must do
   Ollama tool calling well (Qwen3 family, Llama 3.x, Mistral variants; below ~8B tool use gets
   unreliable); a new embedder needs its prompt prefixes set and `scripts/gate_check.py` run;
   whisper any faster-whisper size; any Piper voice.
4. Voice: download `en_GB-alba-medium` (or a chosen one) into the voices dir; offer `audition`.
5. PipeWire tools, `git`, the GNOME AppIndicator extension: check, print the distro's install
   line if missing.
6. Config: write `config.toml` from the template if absent.
7. Offer `strawberry install` (login start via the tray) and `strawberry git-hooks install`.

`doctor` checks and explains: daemon reachable, widgets connected, tray registered (or why not),
Ollama and each model, VRAM headroom, voice file, whisper device, PipeWire tools, D-Bus session
reachable, notification monitor allowed, MPRIS players seen, beat watcher capturing and linked to
the player (not a sink), systemd unit, git hooks path, versions of package and widget. Then a
short scripted conversation through the daemon with latency per slot. Exit code non-zero when
something a user could fix is missing. Every bug report starts with its output.

### 6. Releases and before shipping

- GitHub Actions on tag `v*`: build the wheel (uv build), export the widget (Godot headless with
  the export templates in the runner), compute checksums, attach both to the release, publish
  the wheel to PyPI. One version for package and widget; the widget binary name carries it.
  CHANGELOG.md kept by hand.
- LICENSE (MIT). THIRD_PARTY.md: Godot, Piper, Noto Color Emoji, and a note that the models and
  voices are downloaded by the user under their own licences, each named.
- README rewritten for users: install, setup, what she reacts to, privacy (what is read: the
  notification bus, media metadata, the player's audio for beat only, nothing leaves the
  machine), how to turn each doorway off, how to add an MCP server.
- First-run privacy note in the widget bubble, once.
- `model/` (the Blender sources) stays in the repo, out of the shipped package; the widget binary carries the GLB.

## Order and size

| Step | Depends on | Size |
|---|---|---|
| 1. jeepney watchers + tray | – | a day — **done 2026-09-22** |
| 3. MPRIS reflexes + adapters | – (parallel with 1) | a day |
| 2. package + CLI | 1, 3 | half a day |
| 4. widget binary + XDG + handshake | 2 | half a day |
| 5. setup + doctor | 2, 4 | a day |
| 6. releases, licence, README | 5 | half a day |
