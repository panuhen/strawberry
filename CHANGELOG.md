# Changelog

All notable changes to Strawberry are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). One version covers the Python
package and the widget binary.

## [Unreleased]

### Added

- The first step of the Windows port (`WINDOWS.md`): the daemon and the CLI run on Windows by
  hand with `STRAWBERRY_ALLOW_UNSUPPORTED=1`, keeping the config in `%APPDATA%\strawberry` and
  data and state in `%LOCALAPPDATA%\strawberry`. There are no doorways, tray or start-on-login
  there yet, and Windows is not a supported system until the port is done.

### Changed

- The config file and the widget's preferences are read and written as UTF-8 whatever the
  system's locale.

## [0.1.1] - 2026-09-23

### Fixed

- A notification that arrived just after a resume from suspend was dropped as "something
  private": Ollama had unloaded `embeddinggemma` over the suspend, and the privacy check's 2 s
  timeout treated a model that was loading as a gate that was down. A timed-out check now waits
  for one shared reload of the model, up to `[gate] retry_timeout_s` (15 s), and asks once more;
  it still fails closed if that retry fails, and a hard error (Ollama not running, an HTTP error)
  fails closed at once. Several notifications at once share the one wait. Spoken sentences never
  wait: a timeout there still reads the sentence as chat. The notification watcher now waits up to
  30 s for the daemon's reply instead of logging a slow one as "strawberryd unreachable".
- The daemon now reloads the gate's model and the reaction model (`gemma3:1b`) in the background
  when the machine wakes, from logind's `PrepareForSleep` signal on the system bus. Without a
  system bus or logind it logs one line and carries on. `[daemon] warm_on_wake = false` turns it
  off; `/health` shows it under `wake`, and the gate's `retries` and `warmups`.
- A gate whose examples could not be embedded at start (Ollama was down) now comes up at the next
  resume from suspend instead of staying off until a restart.
- The widget comes back on the display it was left on. Its saved position was kept inside the
  primary screen, so a widget on a second display reappeared at the bottom of the main one.
  *Reset position* now uses the corner of the screen she is on.
- The app switcher showed a generic gear and, in developer mode, "Strawberry (DEBUG)". The window
  now carries the berry icon and a plain title, and `strawberry install` adds a hidden
  `strawberry.desktop` entry and icon that GNOME matches her window to (run `install` again to
  get it).

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
- `strawberry doctor` checks whether the notification monitor is allowed, the MPRIS players on
  the bus, `strawberry-tray.service` (installed, enabled, active, and a stale ExecStart after a
  move or reinstall), the git hooks, and whether the beat watcher is posting and linked to the
  player's stream. `/health` has `tempo_age_s` for that last one.
- `strawberry setup` and `doctor` detect AMD cards (rocm-smi, or sysfs without ROCm). The models
  run on them through Ollama's ROCm support; whisper runs on the CPU. Intel graphics count as no
  usable GPU.
- A tray submenu, *Message bodies ▸ Off / React / Glance*: it writes `[notifications] body`
  into config.toml (comments kept, a backup first) and applies it without a restart.
- A steadier beat tracker: onsets in three bands with the kick leading the phase, octave
  checks, hysteresis and a median over the last estimates. On a synthetic set of 35 clips the
  right tempo went from 56 % to 84 % of estimates at the same CPU cost. Tempo events carry a
  `steady` flag, and the widget sways instead of stepping while it is false.
  `scripts/beat_eval.py` generates the set and scores a tracker.
- With no music server configured, asking her to play, queue or save particular music gets a
  plain answer that this needs a music add-on, instead of a wrong claim or a vague refusal.
  Skip, pause, resume and "what song is this" keep working over MPRIS.
- Supported platforms are checked at start: on anything but Linux the commands say so and exit.

### Fixed

- "Unclosed client session" errors when the tray service stopped: the daemon gets SIGTERM
  twice (systemd and the tray), and the second one cancelled aiohttp's cleanup. The daemon now
  runs its own shutdown and ignores repeat signals.
- The journal no longer gets a line every 2 s for the tray's `/health` poll and the beat
  watcher's tempo posts.

[Unreleased]: https://github.com/panuhen/strawberry/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/panuhen/strawberry/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/panuhen/strawberry/releases/tag/v0.1.0
