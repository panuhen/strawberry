# Changelog

All notable changes to Strawberry are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). One version covers the Python
package and the widget binary.

## [Unreleased]

Strawberry runs on Windows 10 (2004 or later) and 11, with the same package and the same
commands as on Linux: `uv tool install strawberry-crab`, `strawberry setup`, `strawberry`,
`strawberry install`. README.md has the Windows install and its known limits; WINDOWS.md says how
each part works there.

### Added

- Windows is a supported system. The config lives in `%APPDATA%\strawberry`, the widget, voices,
  cache, logs and state in `%LOCALAPPDATA%\strawberry`. `STRAWBERRY_ALLOW_UNSUPPORTED=1` remains
  for trying her on a system that is not supported (macOS).
- The widget for Windows: `strawberry-widget-<version>-windows-x86_64.exe` on each GitHub release,
  with the berry as its icon. `strawberry widget --fetch` and `strawberry setup` download it and
  check its SHA-256, as on Linux. She is transparent, always on top and clicks go through her
  empty space, as on Linux.
- Media on Windows: play, pause, skip, previous and "what's playing" work with any player that
  shows in Windows' media controls (the System Media Transport Controls), and the `smtc_watch`
  doorway makes her dance while music plays and names each new track. There is no volume
  control there.
- Notifications on Windows: the `toast_watch` doorway reads other apps' toasts from the
  notification centre (the app's name and logo, the title and the body) with the same
  `[notifications]` settings, body modes and privacy checks as on Linux. It needs "Let apps access
  your notifications" on in Windows' privacy settings, and it checks for new toasts once a second.
- The beat on Windows: `beat_watch` listens to the playing player's own sound only (WASAPI process
  loopback), found through its media session, and she dances on the beat as on Linux. `[beat]
  target` takes the player's short name there (`spotify`, `chrome`).
- The tray and start on login on Windows: the berry in the notification area with the same menu
  as on Linux (a left click shows or hides her); it runs the daemon, the doorways and the widget
  and restarts one that stops. `strawberry install` puts a shortcut to it in the Startup folder
  and starts it, `strawberry uninstall` removes it. It runs without a console window and logs to
  `%LOCALAPPDATA%\strawberry\state`. A new command, `strawberry-tray`, is `strawberry tray`
  without a console window.
- The microphone and the listen hotkey on Windows: she records from Windows' default recording
  device (or the one `[voice] source` names) at the same 16 kHz mono. The tray holds the hotkey,
  Ctrl+Alt+Space by default (Windows keeps Win-key combinations for itself); `strawberry hotkey
  COMBO` changes it and `--remove` turns it off. It is kept as `[voice] hotkey`. Whisper on CUDA
  works with the `gpu` wheels alone, without a CUDA toolkit.
- Warm on wake on Windows: after a resume the daemon reloads the gate's model and the reaction
  model, as it does after logind's signal on Linux.
- `strawberry doctor` on Windows checks, in place of PipeWire, D-Bus, the tray host and systemd:
  whether Windows lets her read notifications, the media sessions, the microphone and its privacy
  switches, whether the build can capture a player's sound, the Startup shortcut and what it
  runs, and the tray. `strawberry setup` there names Ollama's Windows installer (or winget) and
  offers the Startup shortcut.
- `strawberry setup --no-download` writes the config but pulls no model and fetches no voice or
  widget; it names each one it would have fetched.
- On Windows the widget falls asleep after inactivity as on Linux: its idle helper reads
  `GetLastInputInfo` on the interpreter `strawberry widget` and the tray pass it.
- The tests run on Windows in CI, and the release builds and checks the Windows widget beside
  the Linux one.

### Changed

- `jeepney` is installed on Linux only; the `winrt-*` packages and `sounddevice` on Windows only.
- The config file and the widget's preferences are read and written as UTF-8 whatever the
  system's locale.
- `scripts/build_widget.sh` and `scripts/check_phase1.sh` also run under Git Bash on Windows.

### Fixed

- The gate gave up at start when Ollama answered its first request with an error, and routed
  everything to chat (with every notification body private) until a restart or a resume. Seen
  with Ollama on Windows, where the first embed of a start sometimes went to a model runner that
  had just gone; an HTTP error at start is now tried once more.
- The widget's *Restart widget* passed `--display-driver X11` (or `Windows`) to the new process,
  which accepts only the lower-case names.
- On Windows her claws were cut off while she danced, and the tops of her eyes during a peek, a
  hop or a wave: the window's region (what Windows draws as well as what takes clicks) was her
  shape at rest. It now follows her pose and holds each pose for a moment, so the dance styles,
  the reactions and the top hat are drawn whole, and a raised claw takes clicks. Linux is
  unchanged.
- `scripts/gate_check.py` and `scripts/sensitive_check.py` read their phrase files in the
  locale's encoding; they are UTF-8.

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
