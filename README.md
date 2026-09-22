# Strawberry

A local desktop AI mascot: a cel-shaded crab that lives on the desktop and reacts to what happens on the machine.

- **`WIRING.md`** — the harness spec: message contract, `strawberryd`, doorways, build order, widget shell. Start here.
- **`PACKAGING.md`** — the plan for shipping her as `uv tool install strawberry-crab` plus a standalone widget binary.
- **`ADAPTERS.md`** — adding an MCP server, and writing an adapter for one (Spotify as the worked example).
- **`src/strawberry_crab/`** — the one Python package: the daemon (`strawberryd`: HTTP intake + websocket to the widget), the doorways (`strawberry-doorway <name>`), the tray and the `strawberry` CLI. Tests in `tests/`.
- **`widget/`** — Godot 4.7 desktop widget: transparent, always-on-top, click-through. Installed, she is one executable, `~/.local/share/strawberry/widget/strawberry-widget` (`strawberry widget --fetch` downloads it from the release and checks its SHA-256); in a checkout without that binary, `bin/strawberry` runs the Godot project with `godot` from PATH (developer mode). `scripts/build_widget.sh` exports the binary.
- **Voice** — Piper TTS, on when `[speech] enabled = true` in `~/.config/strawberry/config.toml`. `strawberry voices` lists or downloads voices, `strawberry audition` compares them, `strawberry say "…"` makes her talk.
- **The gate** — every sentence you say is sorted locally by `embeddinggemma` (kind, topic, which tool, is there an argument) before anyone answers; `strawberry route "skip this song"` shows the reading, `scripts/gate_check.py` runs the phrase set. A plain music command ("skip this song", "pause", "louder", "what song is this") is a **reflex**: she does it herself in well under a second, with no model in the loop, then tells you the fact plus a quip.
- **Notifications, privately** — she sees every desktop notification (a D-Bus monitor, nothing leaves the machine) and by default reads only the app and the sender, never the message: `[notifications] body = "off"`. `"react"` lets the small model read the body and react in its own words; `"glance"` has it say a plain one-line gist first ("Alex asks about lunch at noon."), then a quip; `body_apps` sets it per app. In every mode codes, sign-ins, password resets and bank or card alerts are dropped before any model reads them (patterns plus an `embeddinggemma` check; if that check is down the body counts as private), and she says only "Slack sent something private." No message text is written to any log. `scripts/sensitive_check.py` runs the filter's test set (WIRING §4).
- **Music control works out of the box** — skip, previous, pause, resume, volume and "what song is this" go over **MPRIS**, which every desktop player speaks (Spotify, VLC, Rhythmbox, mpv, a browser tab). Nothing to install, nothing to configure, no account.
- **Servers are optional, and so are adapters** — `[tools.servers.*]` in the config adds MCP servers and their tools; none ships configured. A server the shipped **adapters** know (Spotify is the first) also brings its own reflexes, the line that tells the brain what is playing, names for the recogniser and its own error wording. Spotify's server is a separate wrapper you install and authorise ([panuhen/spotify-mcp](https://github.com/panuhen/spotify-mcp)); without it the music reflexes still work over MPRIS. `strawberry tools` lists what she can reach; **`ADAPTERS.md`** explains adding a server and writing an adapter.
- **One brain for everything else** — anything that is not a reflex ("play some Nina Simone", "who was the president in 1960", "how are you today") is one call to Qwen with the tools, the situation and her persona, while she says "On it." and thinks; the reply is hers, mood and all. No internet: a question about today's news gets told so. She remembers the last few minutes of conversation and nothing more (WIRING §8b). Without Qwen (`[thinker] enabled = false`) the small model answers instead, as before.
- **Names she can hear** — the recogniser is told any names you list under `[voice] vocabulary`, plus whatever a configured server's adapter can offer (with Spotify: your artists and playlists, refreshed every ten minutes).
- **Type to her** — right-click → *Chat with Strawberry…* (or press T) opens a text field under her; Enter sends the line through the daemon exactly like a spoken sentence (gate, reflexes, Qwen) and she answers on the desktop. `strawberry talk` does the same from a terminal, with the routing shown underneath.
- **Talk to her** — `strawberry hotkey` binds Super+Shift+Space: press, speak, she listens (faster-whisper on the CPU) and answers out loud.
- **Daily use** — `strawberry install` makes her start on login: one systemd user unit for the **tray**, which starts the daemon, the doorways and the widget and restarts whatever dies. The 🍓 in the top bar carries the same menu as right-clicking her — show/hide, chat, mute, quiet hour, volume, skin, sleep, top hat, always-on-top, the settings file, voices folder, restart, quit — plus a line saying whether she is idle, listening, thinking or talking. Her pupils follow the mouse, and she dances to the actual beat of whatever is playing (techno gets a rave, metal a headbang, hip hop a groove). On GNOME the icon needs the AppIndicator extension (Ubuntu ships it on); without it nothing breaks and her right-click menu covers everything.
- **`model/`** — the 3D asset: the GLB the widget loads, its editable Blender scene and the procedural build script. See `model/README.md`.

## Install and develop

From a checkout, `bin/strawberry` stands in for `strawberry` everywhere above: it runs the CLI from the checkout's `.venv`, creating it on first use.

```bash
uv sync --inexact --group gpu        # the venv, plus the CUDA wheels whisper uses; never a bare `uv sync`, which prunes them
bin/strawberry                       # daemon + doorways, then the widget
bin/strawberry install               # start on login: one systemd user unit for the tray
bin/strawberry git-hooks install     # she reacts to commits and pushes on this machine
.venv/bin/python -m pytest -q        # tests
```

As a tool, without a checkout:

```bash
uv tool install /path/to/strawberry  # or 'strawberry[gpu] @ /path/to/strawberry' for whisper on CUDA
strawberry setup                     # models that fit, a voice, the widget binary; --yes for the defaults
strawberry doctor                    # what is missing and how to fix it; --talk times each model slot
strawberry                           # or strawberry tray / strawberry install
```

**Setup and doctor.** `strawberry setup` reads the GPU's VRAM (nvidia-smi) and proposes the models that fit: the tested setup on a 24 GB card (Qwen 27B brain, `gemma3:1b`, `embeddinggemma`, whisper `medium` on CUDA, Piper `en_GB-alba-medium`), a smaller Qwen3 for 16, 10 and 6 GB, and no brain on the CPU. Any slot can be typed over. It writes the choices into `config.toml` (backing it up first and keeping the values already there), prints each model's licence, runs `ollama pull` for what is missing, downloads the voice, fetches the widget if the installed one is another version, offers to append the settings the file does not have, and offers `strawberry install`. The models are not part of this package: you download each from its publisher, under its own terms. `--yes` takes the defaults without asking (`--install` also installs; `--tier 10gb` picks a tier). `strawberry doctor` checks Ollama and each model, the GPU, whisper and CUDA, the voice, the widget version, PipeWire, git, the tray host, the config and the daemon, prints ✓ / ! / ✗ with a fix per problem and exits 1 if anything is ✗; `doctor --talk` also times a short scripted conversation through the running daemon, per slot.

**Which widget runs.** `strawberry widget` and the tray run the installed binary when it is there, else the checkout's Godot project with `godot` on PATH, else they say to run `strawberry widget --fetch`. A binary built from a checkout (`scripts/build_widget.sh`, needs Godot 4.7.2 and its export templates) can be tried by hand: `dist/strawberry-widget-<version>-linux-x86_64 --display-driver x11 -- --ws=ws://127.0.0.1:8770/ws`. The widget tells the daemon its version; a source run says `dev` and is always accepted, a release on another major version is refused (WIRING §1).

Entry points: `strawberry` (the CLI; `strawberry --help`), `strawberryd` (the daemon alone), `strawberry-doorway mpris_watch|notify_watch|beat_watch`. Files: config and the widget's preferences `~/.config/strawberry/` (`config.toml`, `widget.cfg`), voices and the widget binary `~/.local/share/strawberry/` (`voices/`, `widget/`), state and logs `~/.local/state/strawberry/` (XDG variables respected). `uv build` makes the wheel.

End-to-end check: `scripts/check_phase1.sh` (unit tests, then a headless widget against a real daemon; `WIDGET=dist/strawberry-widget-… scripts/check_phase1.sh` runs the same checks in the exported binary); `scripts/check_tray.sh` registers the tray and reads it back.
