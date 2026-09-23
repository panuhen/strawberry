# Strawberry on Windows

Strawberry runs on Windows 10 (2004 or later) and 11 with the same package, the same commands and
the same config as on Linux. This is how each part works there, what was measured, and what is
left. `WIRING.md` has the details of every part on both systems; `README.md` has the install.

## What is the same

Most of the system has nothing Linux-specific in it and is shared as it is:

- the daemon (`strawberryd`): HTTP and websocket on 127.0.0.1, the event contract, the reactor;
- the models through Ollama: the gate (`systemone.py`, embeddinggemma), the reaction voice
  (gemma3:1b), the thinker and its MCP tools;
- the privacy rules (`privacy.py`), the ledger, persona, actions and adapters;
- Piper for speech and faster-whisper for recognition (both have Windows wheels);
- the widget's Godot project, and its protocol with the daemon;
- what the notification and beat doorways do once they have read something
  (`doorways/notifications.py`, `doorways/beat_watch.py`, `beat_track.py`), and the tray's menu
  and children (`traymenu.py`, `supervisor.py`).

## Each part on Linux and on Windows

| Part | Linux | Windows |
|---|---|---|
| Notifications | D-Bus `BecomeMonitor` on the session bus (`doorways/notify_watch.py`) | `UserNotificationListener` (WinRT), polled once a second: `doorways/toast_watch.py`. Access is the Settings switch "Let apps access your notifications". |
| Media | MPRIS over D-Bus (`mpris.py`, `doorways/mpris_watch.py`) | System Media Transport Controls (WinRT): `smtc.py`, `doorways/smtc_watch.py`. No volume. |
| Beat capture | `pw-record` of the player's own stream (`doorways/beat_pipewire.py`) | WASAPI process loopback of the player's process tree, found through its SMTC session: `doorways/beat_loopback.py`, `wasapi.py`. |
| Microphone | `pw-record` from the default source | WASAPI through `sounddevice`, 16 kHz mono: `winmic.py`. |
| Tray | StatusNotifierItem + dbusmenu over jeepney (`tray.py`) | A notification-area icon, `Shell_NotifyIconW` through ctypes, with the same menu: `wintray.py`. |
| Start on login | systemd user unit + XDG autostart fallback | `Strawberry.lnk` in the user's Startup folder (`startup.py`); no service, task or registry. |
| Stopping a process | SIGTERM | A named stop event per process (`winproc.py`), then TerminateProcess after a timeout. |
| Wake from sleep | logind `PrepareForSleep` (`wake.py`) | `PowerRegisterSuspendResumeNotification`, `PBT_APMRESUMEAUTOMATIC` (`winwake.py`). |
| Paths | XDG dirs (`paths.py`, `widget/paths.gd`) | `%APPDATA%\strawberry` (config), `%LOCALAPPDATA%\strawberry` (data, cache; state in `state\`). |
| Hotkey | a GNOME custom shortcut via `gsettings` | `RegisterHotKey` in the tray (`wintray.py`), `[voice] hotkey` (`hotkey.py`), Ctrl+Alt+Space. |
| App switcher entry | `strawberry.desktop` and a hicolor icon | Not needed: the window's own icon and title. |
| Widget window | `--display-driver x11`; click-through by an input shape | Godot's Windows driver; `mouse_passthrough_polygon` is the window's region, which clips drawing too. |
| Widget binary | `strawberry-widget-<ver>-linux-x86_64` | `strawberry-widget-<ver>-windows-x86_64.exe` |
| Idle time (sleep) | `desktop_idle.py` on `/usr/bin/python3`: XScreenSaver or Mutter | `desktop_idle.py` on `STRAWBERRY_PYTHON`: `GetLastInputInfo` |
| Git hooks | POSIX sh hooks calling `strawberry git-event` | The same hooks, with LF and forward slashes; a repository's own hook runs through Git's `sh`. |
| Doctor/setup | PipeWire, D-Bus, systemd, AppIndicator | notification access, SMTC sessions, the microphone and its privacy switches, the build for process loopback, the Startup shortcut, the tray. |

## How the code is shaped

- `osguard.py` is the one place that says which systems are supported: `linux` and `win32`.
  `STRAWBERRY_ALLOW_UNSUPPORTED=1` lets the entry points run anywhere else (macOS), for working
  on a port.
- One backend per system behind the same small interface, picked at runtime: `media.controls()`,
  `doorways.for_system()`, `beat_watch.backend()`, `voice.default_backend`, `wake.watcher()`,
  and `strawberryd --tray` picks `wintray` or `tray`. Shared code never imports a Linux or
  Windows module at import time; `tests/test_imports.py` imports each side with the other's
  packages blocked.
- OS-specific dependencies use environment markers in `pyproject.toml` (`jeepney` on Linux; the
  `winrt-*` packages and `sounddevice` on Windows), so `uv tool install strawberry-crab` works on
  both. Everything else Windows needs (the tray, process loopback, the hotkey, the wake watcher,
  the stop events, the idle helper) is the Win32 API through ctypes.
- The doorways talk to the daemon over HTTP exactly as on Linux; the daemon does not know which
  system produced an event.
- Redirected output (a child's log file, `strawberry doctor > doctor.txt`) is UTF-8
  (`winproc.utf8_streams`): Windows gives a redirected stream the ANSI code page.

## Processes

- A by-hand `strawberry daemon | status | say | talk | stop | git-event | git-hooks` works as on
  Linux. Ctrl+C and Ctrl+Break stop the daemon cleanly (a plain signal handler hands the signal
  to the loop, which has no `add_signal_handler` on Windows).
- Clean stop: each long-running process (the daemon, the doorways, the tray) waits on
  `Local\strawberry-stop-<key>` and shuts down as on SIGTERM when it is set. `<key>` is its own
  pid and the key its parent passes in `STRAWBERRY_STOP_EVENT`, because a venv's `python.exe` and
  uv's launchers start the real interpreter as a child of their own. `Local\` is the logon
  session's namespace, and the event has the creating token's default DACL. `strawberry stop` sets
  it, waits, and uses TerminateProcess after 10 s (20 s for the tray). Ctrl+Break was not used: it
  reaches only a process that shares the sender's console.
- The tray's children run without a console window, in their own process group, with output to
  `<state>\<name>.log`, in a kill-on-close job, so a killed tray takes them along. The widget
  binary has no stop event and gets TerminateProcess. `strawberry widget` in developer mode runs
  Godot tied to itself by the same kind of job (`winproc.call_tied`).
- `strawberry restart` sets the tray's `Local\strawberry-restart-<pid>`; the tray restarts its
  children. Her menu's "Apply settings" runs it from inside the tray's job.

## Media (SMTC)

- `smtc.Smtc` subclasses `mpris.Mpris` and replaces only what talks to the bus, so the choice of
  player and every sentence are shared. `doorways/smtc_watch.py` posts what `mpris_watch` posts:
  dancing/idle on `/perform`, one `{"source": "media", ...}` event per track. WinRT events drive
  it, with a 5 s poll behind them.
- App identity: `SourceAppUserModelId` -> `smtc.app_key` (`spotify`, `chrome`, `msedge`,
  `firefox`, and `chromium` for every Chromium-based browser) for `[media] only`/`ignore`, and
  `smtc.app_name` for what she says. Firefox's id is a hash of its install folder; only the
  default folder's is known.
- Seen live with a silent session of our own: the watcher followed it from events alone, and the
  reflexes read, paused and resumed it.

## Notifications

- `toast_watch` reads `UserNotificationListener.Current.GetNotificationsAsync(Toast)` every
  second and diffs by `UserNotification.Id`; a read takes 150-170 ms and about 4 ms of CPU.
  Per toast: `DisplayInfo.DisplayName` is the app, the `AppUserModelId` gives the short key (in
  the `desktop_entry` slot, so `ignore_apps`/`only_apps`/`body_apps` match by name or key), and
  the `ToastGeneric` binding's text elements are the title (the first) and the body (the rest).
  Toasts carry no urgency, category or replaces id. The app's logo (packaged apps only, in
  practice) goes once per app to `%LOCALAPPDATA%\strawberry\cache\app-icons\<key>.png`.
- What is already in the notification centre at start is not announced.
- The no-body-in-logs canary (`tests/test_privacy.py`) runs through this doorway too.

### Access: what was found on this machine

Windows 11, build 26200, from an unpackaged Python 3.12 and 3.13 process (no package identity,
no manifest, no sparse package):

| Call | Result |
|---|---|
| `UserNotificationListener.Current` | a listener, no exception |
| `GetAccessStatus()` | `1`, `Allowed` |
| `RequestAccessAsync()` | `1`, `Allowed`, at once; no prompt was shown |
| `GetNotificationsAsync(Toast)` | the notification centre's toasts, with app names and text elements |
| `add_NotificationChanged(handler)` | `OSError: [WinError -2147023728]` (`0x80070490`, Element not found) |

The status follows Settings > Privacy & security > Notifications > "Let apps access your
notifications" (`HKCU\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\userNotificationListener`,
`Value = Allow` here; HKLM has the same key for the device-wide policy). There was no
`NonPackaged` subkey, so desktop apps are not listed there separately on this build.

So the listener works without identity tricks, and only the change event needs identity; hence
the poll. Where the switch is off, the watcher logs `Windows says denied to reading
notifications; turn it on in Settings > Privacy & security > Notifications, 'Let apps access your
notifications'` and exits 3. Not done, and not needed: a sparse package or MSIX (identity, hence
`NotificationChanged`, at the price of a signed package), and reading `wpndatabase.db`
(undocumented, and it holds every app's payload).

## Tray and start on login

- `wintray.py`: a hidden top-level window on its own thread owns a `Shell_NotifyIconW` icon
  (NOTIFYICON_VERSION_4); asyncio stays on the main thread with `TrayCore` and the supervisor.
  Right click builds the popup menu from `menu_items()` (check marks, radio rows, the four
  submenus, the greyed status row) and shows it with `TrackPopupMenuEx`; "Hide her"/"Show her"
  is the default row and a left click runs it. The icon is a 32-bit HICON with alpha made from
  the packaged PNGs at the notification area's size. `TaskbarCreated` adds it again after
  Explorer restarts, a failed add at login is retried every 2 s (logged once), and
  `WM_ENDSESSION` stops the children before logoff.
- Why no tray library: pystray would add pystray (LGPL-3.0) and Pillow for a wrapper around the
  same calls, with its own loop and menu model; written directly, every flag is ours and the tests
  read the real menu back. pywin32 is installed (mcp needs it) but not used.
- `strawberry install` writes `Strawberry.lnk` in the Startup folder with the berry as its icon
  (`%LOCALAPPDATA%\strawberry\strawberry.ico`), stops what ran before and starts the tray the way
  the shortcut will. The shortcut runs `strawberry-tray.exe --port <port>`, a
  `[project.gui-scripts]` entry point (a windowless launcher that runs `pythonw.exe`). The .lnk is
  written by `WScript.Shell` through Windows PowerShell 5.1. `strawberry uninstall` deletes the
  shortcut and the .ico and stops the tray.
- Seen live against a throwaway daemon: install, the icon found in the notification area, the
  20-row menu read back through `GetMenuItemInfoW`, status, restart, stop in 0.3 s with a clean
  daemon shutdown, uninstall; nothing left running. Nothing was clicked.

## Beat

- `beat_watch` is one doorway on both systems; `beat_watch.backend()` picks the capture. The
  capture (`wasapi.py`) activates `VAD\Process_Loopback` with the target pid and
  `PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE` through a completion handler of our own,
  and asks for mono float32 at 22050 Hz with `AUTOCONVERTPCM`, so the audio engine resamples and
  mixes down. Event driven, 200 ms buffer, reads of half a chunk. About 19 ms of CPU per second.
- The player: the SMTC session that plays (or `[beat] target`), its process from the output
  devices' audio sessions (a packaged app by `GetApplicationUserModelId`, a desktop app by image
  name), up to the topmost ancestor with the same image name. Never our own processes or the
  widget. It hears the player after its session volume and mute.
- `scripts/beat_eval.py` runs on Windows unchanged; `run --synthetic` gave the scores in WIRING
  §4c. Seen live with a test player of our own:

  | the loop | known BPM | beat_watch | the same audio offline |
  |---|---|---|---|
  | four on the floor, off-beat bass | 124 | 123.1–123.6 | 123.7–123.8 |
  | rock | 100 | 99.6–100.0 | 99.9 |
  | four on the floor, off-beat bass | 140 | 139.6–139.8 | — |

## Microphone and hotkey

- `voice.capture()` is the loop both systems share; on Windows `winmic.py` feeds it from a
  shared-mode WASAPI stream through `sounddevice` at 16 kHz mono s16 with `auto_convert`. The
  input is `[voice] source` as a name fragment, else the default recording device.
- Whisper on CUDA with only the gpu wheels: `preload_cuda_libraries` loads `cublasLt64_12.dll`
  and `cublas64_12.dll` from `nvidia\cublas\bin` by path and adds the wheels' `bin` directories to
  PATH and the DLL search path. On an RTX 3090, `small`, `int8_float16`: 1.2 s to load, 0.22-0.30 s
  per transcription of a 4.5 s sentence after a first one of 0.4-0.5 s; the CPU took 1.4 s.
- The hotkey: the tray's hidden window registers `[voice] hotkey` with `RegisterHotKey` and turns
  WM_HOTKEY into `POST /listen`. The default is `<Control><Alt>space`: Windows refused
  Win+Shift+Space, Win+Space, Win+Shift+S, Win+L and Win+E with `ERROR_HOTKEY_ALREADY_REGISTERED`.

## Wake, doctor and setup

- `winwake.PowerWatcher` registers a callback with `PowerRegisterSuspendResumeNotification`; the
  resume (`PBT_APMRESUMEAUTOMATIC`) calls `Daemon.warm_models("on resume")`, and the rest is
  shared.
- `strawberry doctor` on Windows runs the shared checks, then notification access
  (`GetAccessStatus`, never `RequestAccessAsync`), the count of media sessions, the microphone
  and its privacy switches (read with `KEY_READ`), the build (19041 or later), the Startup
  shortcut and what it runs, the tray and its stop event, the git hooks, the daemon and the beat
  watcher. It only reads. `strawberry setup` names Ollama's Windows installer (winget offered
  interactively) and offers the Startup shortcut.

## The widget

Run on Windows 11 on 2026-09-23 in developer mode (`strawberry widget` with Godot 4.7.2 on PATH)
and as the exported `.exe` installed in the data dir, each against a throwaway daemon; the
headless validators ran in both. What was checked, through Win32 and screen captures of her
window only:

| Property | Found |
|---|---|
| Transparency | the desktop showed through everywhere but her; `--capture` PNGs have a transparent background |
| Borderless | `WS_POPUP`, no caption |
| Always on top | `WS_EX_TOPMOST`; the `on_top` command cleared and set it |
| Click-through | `WindowFromPoint` found her window on her body and the window below in the empty corners; the window region is the padded hull |
| Title and icon | "Strawberry" (no " (DEBUG)" in developer mode); `WM_GETICON` big and small are the berry; the .exe's own icon is the berry |
| Taskbar | an unowned visible window with `WS_EX_APPWINDOW`, so it has a taskbar button |
| Paths | preferences `%APPDATA%\strawberry\widget.cfg`, config `%APPDATA%\strawberry\config.toml`, voices `%LOCALAPPDATA%\strawberry\voices`, the export's copied-out files in `%LOCALAPPDATA%\strawberry\cache\widget`; a relative or empty variable falls back under `%USERPROFILE%` |
| Protocol | hello with the version (`dev`, or `0.1.1` from the export), performances, commands, typed lines; `check_phase1.sh` passes on the source project and on the `.exe` |
| Idle helper | `{"source": "windows"}` with the seconds since the last input, from both |

Fixed on the way: Godot makes `mouse_passthrough_polygon` the window's region (`SetWindowRgn`),
which clips what is drawn as well as what is clicked, so her speech bubble was not shown at all;
on Windows the polygon now takes in the bubble's whole line and the badge while they show. The
idle helper ran on `/usr/bin/python3`, which Windows does not have; the widget now takes
`STRAWBERRY_PYTHON`. *Restart widget* passed `--display-driver Windows`, which Godot refuses (it
takes `windows`); the restart was then seen to work with a display. "Settings file…" and "Voices
folder…" hand ShellExecute a plain path, with Notepad for a `.toml` nothing opens. Killing a
developer-mode `strawberry widget` left Godot running; it is now tied to it.

The export: the `Windows` preset in `widget/export_presets.cfg`; `scripts/build_widget.sh` under
Git Bash writes `dist/strawberry-widget-<ver>-windows-x86_64.exe` (110.4 MB; the template is
109.3 MB) and its `.sha256`, file version `0.1.1.0`. `strawberry widget --fetch` from a local
release directory (`STRAWBERRY_RELEASE_URL`) installed it, refused a wrong checksum and a missing
release, and `strawberry widget` then ran it before any checkout.

## Release and CI

- `ci.yml` runs the tests on `windows-latest` too (uv, Python 3.12, no gpu group).
- `release.yml` has `build-widget-windows` on `windows-latest`: Godot for Windows and the one
  template the export needs, checked against the release's SHA-512s and cached on them,
  `scripts/build_widget.sh`, the tests, and `check_phase1.sh` on the `.exe`. The release job
  checks both widgets' `.sha256` and attaches the `.exe` beside the Linux binary. Both files pass
  actionlint and the GitHub workflow schema; the Windows job was rehearsed here with an APPDATA
  holding only that template. Neither workflow has run on GitHub with these jobs yet.

## Throwaway runs

Every live check used a daemon on a port other than 8770 (8782-8788), throwaway APPDATA and
LOCALAPPDATA, `[actions] mpris = false`, and `[notifications] only_apps` and `[media] only`
naming no real app. No toast was ever sent; notifications were driven through
`toast_watch.Watcher` on a fake listener.

The end-to-end run for this step (port 8788, the gate on embeddinggemma, gemma3:1b, the thinker
on mistral-small:24b in a throwaway config, the exported widget connected): typed "hi
strawberry, how is your day going?" -> "I'm just a happy crab, always ready for a chat." (chat,
0.5 s); "what is seventeen times twenty-three?" -> "391." (question, 0.2 s); a commit in a
throwaway repository -> "A readme, that's it. A quick check."; two fake toasts from "Chat
Step8" with bodies off (only the app and the sender reach her) -> "Chat Step8. Right, that's a
bit of a glitch, isn't it?" and "Bank, always a mess. Seriously."; a toast from an app outside
`only_apps` was not forwarded. Each reaction took 0.35-0.5 s. (In a first run, right after the
thinker's model had loaded, gemma3:1b missed its 1.5 s three times and she said the canned
line; afterwards it answered in 0.15-0.57 s every time.) `scripts/gate_check.py` 72/74 (the two misses are the library sentences that need a
music server, as documented in `gate_phrases.json`), `scripts/sensitive_check.py` 38/38.

## What is left

- Windows 10 is untried (2004 and later should work; an older build refuses the listener or the
  loopback activation, and the log says so).
- Notifications: a toast shown and dismissed within one poll is missed; toast scenarios (urgent,
  alarm, call) are not visible, so every toast is `normal`; desktop apps give no logo.
- Media: the first track of a player that appears already playing is not announced; two sessions
  of one app are told apart by their order; `pause` and `skip` on a real player are untried.
- Beat: Spotify, the browsers and packaged players are untried live; a player muted in the mixer
  is reconnected every 12 s; of two processes of one app with sound, the first wins.
- Microphone: a real recording is untried (no input here), and so is the privacy switch turned
  off. The hotkey is the tray's only.
- Tray: the menu's rows were clicked only in the fake-icon tests; the icon starts in the
  overflow; the Startup folder is taken from `%APPDATA%`, so a redirected one is not followed;
  the Startup shortcut check takes about half a second (PowerShell).
- The widget binary is ended with TerminateProcess on stop and restart; the preferences are
  written when they change, so nothing was seen lost, but it was not looked for.
- The widget on a second display, next to a full-screen app, and falling asleep after five idle
  minutes were not watched; the right-click menu was not clicked (no input was sent to the
  desktop).
- A real suspend and resume on Windows is untried.
- AMD cards get the CPU tier (no rocm-smi or sysfs on Windows).
- Ollama 0.34.3 on Windows answered about every other first `/api/embed` of a process with HTTP
  400 (a model runner that had gone). The gate's start now tries once more; a later call that
  meets it reads as chat or, for a body, fails closed, as any gate error does.
- `scripts/check_tray.sh` and `scripts/check_reconnect.sh` are Linux-only as written.
