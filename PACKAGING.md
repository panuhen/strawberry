# Packaging plan: from developer checkout to `uv tool install strawberry`

Status: plan, not started (2026-09-20). Do after Phase 5 (voice in, WIRING.md §10).

## Goal

A Linux user runs one command and gets the daemon, the doorways, the launcher and the widget, with
the model and a voice fetched on first run. No Godot editor, no cloning, no hunting for apt packages.

```bash
uv tool install strawberry        # or: pipx install strawberry
strawberry setup                  # widget binary, Ollama + gemma3:1b, a voice; explains each step
strawberry                        # she appears
strawberry install                # optional: start on login
```

Scope: Linux desktops with X11 or XWayland, PipeWire and `systemd --user`. That is GNOME and KDE on
Ubuntu, Fedora, Arch and friends since ~2022. Windows/macOS are separate projects (the widget would
run; the notification, media and audio-capture doorways would each need a native replacement).

## Why not the sandboxed formats

Flatpak and Snap fight what she does: monitoring the notification bus, capturing another app's
PipeWire stream, writing systemd user units and global git hooks, opening the config in the user's
editor, spawning the widget. Each needs a permission hole and some cannot be granted. A `.deb`
would work but is Ubuntu/Debian only; Nix only for Nix users. A Python package plus a standalone
widget binary reaches every distro with the same artefact.

## What blocks a single-command install today

| Assumption now | Fix |
|---|---|
| `godot` on PATH; the widget runs from `widget/` source | Export a standalone Linux binary per release; the CLI downloads the matching one |
| Doorways run on `/usr/bin/python3` for PyGObject (`gi`) | Rewrite the two D-Bus watchers on a pure-Python D-Bus library (jeepney) so they live in the package |
| `numpy` from the system for the beat watcher | Package dependency (wheels exist) |
| `bin/strawberry` bash launcher finds files via the repo | Python CLI entry point; assets in XDG data dir |
| Widget finds the repo via `res://..` (menu: settings file, restart) | Use XDG paths and talk to the daemon (`/config` already exists) |
| Ollama present with `gemma3:1b` pulled | `strawberry setup` installs Ollama (their script) and pulls the model |
| Piper voice already downloaded | `strawberry setup` downloads the default voice; `strawberry voices` for more |
| `jq`, `curl` in the git hooks and `say` | Hooks call `strawberry git-event` (Python); `say` is Python |
| `pw-record`, `pw-dump` | Runtime check with the apt/dnf line in the message (pipewire tools ship with every PipeWire desktop) |
| No licence | Add one (MIT unless Panu prefers otherwise) |

## Steps

### 1. One Python package (`strawberry`)

- Rename the project `strawberryd` → `strawberry`; keep `strawberryd` as a console script alias.
- Move `doorways/*.py` into `strawberry/doorways/`. Entry points: `strawberry` (CLI), `strawberryd`
  (daemon), `strawberry-doorway <name>` (for systemd units).
- Rewrite `mpris_watch` and `notify_watch` on **jeepney** (pure Python, asyncio-friendly,
  supports `BecomeMonitor` via raw messages and `PropertiesChanged` signals). Keep the current
  behaviour and the gotchas in WIRING.md §4 (monitor filter consumes `Notify`; every Notify crosses
  the bus twice → content dedupe). Run all doorways inside the daemon process as asyncio tasks,
  or keep them as separate processes; separate is more robust (a D-Bus hiccup cannot take the
  websocket down) and matches the systemd units. Decide when rewriting.
- Dependencies: aiohttp, piper-tts, onnxruntime, numpy, jeepney. Optional extra `[dev]`: pytest.
- Port `bin/strawberry` to `strawberry/cli.py` (click or argparse): `widget`, `daemon`, `status`,
  `stop`, `restart`, `config`, `say`, `voices`, `audition`, `install`, `uninstall`, `setup`,
  `doctor`, `git-event`, `git-hooks install|remove`.
- Tests move with the code; `check_phase1.sh` becomes `strawberry doctor --acceptance` or stays a
  dev script.

### 2. Widget as a standalone binary

- Godot export preset `Linux/X11` (x86_64, embedded PCK), built with the 4.7.2 export templates.
  Output `strawberry-widget-<version>-linux-x86_64`. Test: transparent window, always-on-top,
  passthrough, audio bus, all with the exported build (some project settings behave differently
  once exported; verify `per_pixel_transparency` and `--display-driver x11` still apply).
- Remove the repo assumptions in `menu.gd`: settings file path from XDG config, "Apply settings"
  via `POST /restart`-style call or by running the `strawberry` CLI found on PATH, voices dir
  from XDG data. Captures/acceptance flags stay.
- The widget reports its version in the websocket `hello`; the daemon logs a warning on mismatch
  and refuses a major mismatch.
- Where it lives: `~/.local/share/strawberry/widget/strawberry-widget`; the CLI downloads it from
  the GitHub release matching the installed package version and checks a SHA-256 from the release.

### 3. `strawberry setup` (idempotent, verbose, resumable)

1. Widget binary (download + checksum) unless present and matching.
2. Ollama: detect `ollama` on PATH or the API on :11434; otherwise offer to run the official
   installer; then `ollama pull gemma3:1b` with progress and the TLS-retry loop we already needed.
3. Voice: download `en_GB-alba-medium` (or a chosen one) into the voices dir; offer `audition`.
4. PipeWire tools, `git`: check, print the distro's install line if missing.
5. Config: write `config.toml` from the template if absent; ask two questions (voice, persona
   register: Scottish crab / plain English) and set them.
6. Offer `strawberry install` (login start) and `strawberry git-hooks install`.

### 4. `strawberry doctor`

Checks and explains: daemon reachable, widgets connected, Ollama and model, voice file, PipeWire
tools, D-Bus session reachable, notification monitor allowed, MPRIS players seen, beat watcher
capturing, systemd units, git hooks path, versions of package and widget. Exit code non-zero
when something a user could fix is missing. Every bug report starts with its output.

### 5. Releases

- GitHub Actions on tag `v*`: build the wheel (uv build), export the widget (Godot headless with
  the export templates in the runner), compute checksums, attach both to the release, publish
  the wheel to PyPI.
- Versioning: one version for package and widget; the widget binary name carries it.
- CHANGELOG.md kept by hand.

### 6. Before shipping

- LICENSE file. Attribution for the Piper voices (Alba and friends have their own licences;
  Piper voices are under various open licences, check each) and for Gemma (Gemma terms of use).
- README rewritten for users: install, setup, what she reacts to, privacy (what is read: the
  notification bus, media metadata, the player's audio for beat only, nothing leaves the
  machine), how to turn each doorway off.
- First-run privacy note in the widget bubble, once.
- Remove `v1/`, `v2/` build assets from the shipped package (they stay in the repo).

## Order and size

1. jeepney rewrite of the two watchers (half a day; unblocks everything, testable without audio).
2. Package restructure + CLI port (half a day).
3. Widget export + XDG paths + version handshake (half a day; needs export templates downloaded).
4. `setup` + `doctor` (half a day).
5. Release workflow, licence, README (a few hours).

Total: about three working days end to end, in five independently useful pieces.
