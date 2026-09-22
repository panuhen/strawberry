# Changelog

All notable changes to Strawberry are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). One version covers the Python
package and the widget binary.

## [Unreleased]

## [0.1.0] - 2026-09-22

The first release on PyPI (`strawberry-crab`; 0.0.1 was a name placeholder) with the widget
binary on GitHub.

### Added

- The desktop widget: a transparent, always-on-top, click-through Godot 4.7 window with the
  cel-shaded crab. Her pupils follow the cursor; reactions (wave, peek, shiver, hop, nod), an app
  icon badge beside the speech bubble, a sleep mode, a top hat, skins, and a right-click menu.
  Shipped as one standalone Linux binary; `strawberry widget --fetch` downloads it from the
  GitHub release and checks its SHA-256.
- `strawberryd`, the daemon: HTTP intake and one websocket to the widget, the message contract,
  and a version handshake that refuses a widget on another major version.
- Doorways: desktop notifications (a D-Bus monitor with filters and coalescing), media players
  over MPRIS, git commits and pushes (global hooks), and the beat of the playing track from the
  player's own PipeWire stream, with five dance styles.
- Her voice: Piper TTS (default voice `en_GB-alba-medium`) with claws that move to the audio,
  quiet hours, and `strawberry voices` / `audition` / `say`.
- Listening: a GNOME hotkey (Super+Shift+Space) and faster-whisper, on the CPU or CUDA, with a
  Bluetooth headset microphone when there is one and a vocabulary of names to recognise.
- Typing to her: the *Chat with Strawberry…* box in the widget and `strawberry talk`.
- The gate: every sentence is sorted locally by `embeddinggemma` (kind, topic, tool, argument).
  Plain music commands (skip, previous, pause, resume, volume, what's playing) are reflexes over
  MPRIS for any player, answered in well under a second with no model in the loop.
- One brain for everything else: Qwen with the configured MCP servers' tools, the situation and
  her persona, a thinking pose, and a short memory of recent exchanges. `gemma3:1b` writes her
  one-line reactions.
- MCP servers in the config and adapters for them; Spotify is the first adapter (reflexes,
  situation text, recogniser names, error wording). No server ships configured.
- The 🍓 tray icon (StatusNotifierItem): the login process that starts and restarts the daemon,
  the watchers and the widget, with her whole menu and a status line.
- The `strawberry` CLI: `setup`, `doctor`, `install` / `uninstall` (one systemd user unit, with
  an autostart fallback), `status`, `stop`, `restart`, `config`, `hotkey`, `route`, `tools`,
  `tool`, `think`, `git-hooks`. Files follow the XDG base directories.
- Notification privacy: message bodies are off by default; `react` and `glance` modes per app;
  a sensitive filter that drops codes, sign-ins and bank alerts and fails closed; no message
  text in any log. A one-time first-run note in her bubble says so and where to change it.
- MIT licence, THIRD_PARTY.md, and release automation: on a `v*` tag GitHub Actions tests,
  builds the wheel and the widget, runs the widget's headless acceptance, creates the release and
  publishes to PyPI by trusted publishing.

[Unreleased]: https://github.com/panuhen/strawberry/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/panuhen/strawberry/releases/tag/v0.1.0
