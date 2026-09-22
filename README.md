# Strawberry

A local desktop AI mascot: a cel-shaded crab that lives on the desktop and reacts to what happens on the machine.

- **`WIRING.md`** — the harness spec: message contract, `strawberryd`, doorways, build order, widget shell. Start here.
- **`PACKAGING.md`** — the plan for shipping her as `uv tool install strawberry` plus a standalone widget binary.
- **`ADAPTERS.md`** — adding an MCP server, and writing an adapter for one (Spotify as the worked example).
- **`strawberryd/`** — Python daemon (HTTP intake + websocket to the widget). `uv sync && uv run pytest`.
- **`widget/`** — Godot 4.7 desktop widget: transparent, always-on-top, click-through. Run with `bin/strawberry`.
- **Voice** — Piper TTS, on when `[speech] enabled = true` in `~/.config/strawberry/config.toml`. `bin/strawberry voices` lists or downloads voices, `bin/strawberry audition` compares them, `bin/strawberry say "…"` makes her talk.
- **The gate** — every sentence you say is sorted locally by `embeddinggemma` (kind, topic, which tool, is there an argument) before anyone answers; `bin/strawberry route "skip this song"` shows the reading, `scripts/gate_check.py` runs the phrase set. A plain music command ("skip this song", "pause", "louder", "what song is this") is a **reflex**: she does it herself in well under a second, with no model in the loop, then tells you the fact plus a quip.
- **Music control works out of the box** — skip, previous, pause, resume, volume and "what song is this" go over **MPRIS**, which every desktop player speaks (Spotify, VLC, Rhythmbox, mpv, a browser tab). Nothing to install, nothing to configure, no account.
- **Servers are optional, and so are adapters** — `[tools.servers.*]` in the config adds MCP servers and their tools; none ships configured. A server the shipped **adapters** know (Spotify is the first) also brings its own reflexes, the line that tells the brain what is playing, names for the recogniser and its own error wording. Spotify's server is a separate wrapper you install and authorise ([panuhen/spotify-mcp](https://github.com/panuhen/spotify-mcp)); without it the music reflexes still work over MPRIS. `bin/strawberry tools` lists what she can reach; **`ADAPTERS.md`** explains adding a server and writing an adapter.
- **One brain for everything else** — anything that is not a reflex ("play some Nina Simone", "who was the president in 1960", "how are you today") is one call to Qwen with the tools, the situation and her persona, while she says "On it." and thinks; the reply is hers, mood and all. No internet: a question about today's news gets told so. She remembers the last few minutes of conversation and nothing more (WIRING §8b). Without Qwen (`[thinker] enabled = false`) the small model answers instead, as before.
- **Names she can hear** — the recogniser is told any names you list under `[voice] vocabulary`, plus whatever a configured server's adapter can offer (with Spotify: your artists and playlists, refreshed every ten minutes).
- **Type to her** — right-click → *Chat with Strawberry…* (or press T) opens a text field under her; Enter sends the line through the daemon exactly like a spoken sentence (gate, reflexes, Qwen) and she answers on the desktop. `bin/strawberry talk` does the same from a terminal, with the routing shown underneath.
- **Talk to her** — `bin/strawberry hotkey` binds Super+Shift+Space: press, speak, she listens (faster-whisper on the CPU) and answers out loud.
- **Daily use** — `bin/strawberry install` makes her start on login (systemd user units + autostart). Right-click her for mute, quiet hour, volume, skin, always-on-top, the settings file, and quit. Her pupils follow the mouse, and she dances to the actual beat of whatever is playing (techno gets a rave, metal a headbang, hip hop a groove).
- **`model/`** — the 3D asset: the GLB the widget loads, its editable Blender scene and the procedural build script. See `model/README.md`.

End-to-end check: `scripts/check_phase1.sh` (unit tests, then a headless widget against a real daemon).
