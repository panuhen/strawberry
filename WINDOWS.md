# Porting Strawberry to Windows

The plan for making Strawberry run on Windows 10 (2004 or later) and 11, with one package and
one install command on both systems. Read `WIRING.md` for how each part works on Linux.

## What stays the same

Most of the system has nothing Linux-specific in it and carries over unchanged:

- the daemon (`strawberryd`): HTTP and websocket on 127.0.0.1, the event contract, the reactor;
- the models through Ollama: the gate (`systemone.py`, embeddinggemma), the reaction voice
  (gemma3:1b), the thinker (Qwen) and its MCP tools;
- the privacy rules (`privacy.py`), the ledger, persona, actions and adapters;
- Piper for speech and faster-whisper for recognition (both have Windows wheels);
- the widget's Godot project, and its protocol with the daemon.

## What is Linux-only today, and its Windows counterpart

| Part | Linux (now) | Windows |
|---|---|---|
| Notifications (`doorways/notify_watch.py`) | D-Bus `BecomeMonitor` on the session bus | `UserNotificationListener` (WinRT, via the `winrt-*` packages), polled: `doorways/toast_watch.py` (step 3). Access is the Settings switch "Let apps access your notifications". Reads other apps' toasts: app name and logo, title, body. |
| Media (`mpris.py`, `doorways/mpris_watch.py`) | MPRIS over D-Bus | System Media Transport Controls: `GlobalSystemMediaTransportControlsSessionManager` (WinRT). Spotify, browsers and most players register with it. Play/pause/next/previous, now playing, change events. |
| Beat capture (`doorways/beat_watch.py`) | `pw-record` of the player's own stream | WASAPI process loopback (`AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK`, Windows 10 2004+): capture one process's output, found through the SMTC session's app. `beat_track.py` is pure numpy and stays. |
| Microphone (`voice.py`) | `pw-record` from the default source | WASAPI capture through `sounddevice`, 16 kHz mono: `winmic.py` (step 6). |
| Tray (`tray.py`, `bus.py`, `icons.py`) | StatusNotifierItem + dbusmenu over jeepney | A notification-area icon, Win32 `Shell_NotifyIconW` through ctypes, with the same menu: `wintray.py` (step 4). The menu and the supervisor are shared: `traymenu.py`, `supervisor.py`. |
| Start on login (`cli.py install`) | systemd user unit + XDG autostart fallback | A shortcut in the Startup folder (`startup.py`, step 4); no scheduled task. |
| Wake from sleep (`wake.py`) | logind `PrepareForSleep` on the system bus | `WM_POWERBROADCAST` / `PBT_APMRESUMEAUTOMATIC`, or `PowerRegisterSuspendResumeNotification`. |
| Paths (`paths.py`, `widget/paths.gd`) | XDG dirs | `%APPDATA%\strawberry` (config), `%LOCALAPPDATA%\strawberry` (data, state, widget binary). |
| Hotkey (`cli.py hotkey`) | a GNOME custom shortcut via `gsettings` | `RegisterHotKey` in the tray process (`wintray.py`), the combination in `[voice] hotkey` (`hotkey.py`, step 6). |
| App switcher entry (`cli.py install`) | `~/.local/share/applications/strawberry.desktop` | Not needed: the window icon (`config/icon`) and title are used directly. |
| Widget window | `--display-driver x11`, transparent, always on top, click-through by polygon | Godot's Windows driver supports the same flags and `mouse_passthrough_polygon`; drop the `x11` argument. Needs testing on a second display. |
| Widget binary (`widgetbin.py`, `scripts/build_widget.sh`) | `strawberry-widget-<ver>-linux-x86_64` | `strawberry-widget-<ver>-windows-x86_64.exe`; `PLATFORM` chosen by OS. |
| Git hooks | POSIX sh hooks calling `strawberry git-event` | Git for Windows runs sh hooks too; check the path quoting. |
| Doctor/setup checks (`doctor.py`, `setupcmd.py`) | PipeWire, D-Bus, systemd, AppIndicator | the notification-listener permission, SMTC sessions, the Startup entry. `nvidia-smi` works on Windows as is. |

## How the code should be shaped

- `osguard.py` is the one place that says which systems are supported. Add `win32` there when
  the port runs end to end, not before.
- One backend per OS behind the same small interface: `doorways/linux/…` and
  `doorways/windows/…` (or a `platform` package), chosen at runtime. Shared code never imports
  a Linux or Windows module directly.
- OS-specific dependencies use environment markers in `pyproject.toml`, e.g.
  `jeepney; sys_platform == "linux"` and `winrt-Windows.Media.Control; sys_platform == "win32"`,
  so `uv tool install strawberry-crab` works on both.
- The doorways keep talking to the daemon over HTTP exactly as now; the daemon does not care
  which OS produced an event.

## Order of work

1. **Run what already works.** On Windows, start the daemon and the widget by hand (Godot
   project, no tray), type to her. Fix paths (`paths.py`, `paths.gd`) and anything that assumes
   POSIX. Tests: make the suite pass on Windows, skipping Linux-only tests by marker.
2. **Media**: SMTC backend for the reflexes and the now-playing watcher.
3. **Notifications**: UserNotificationListener backend, with the same privacy modes.
4. **Tray + supervisor + Startup entry**, then `install`/`uninstall`.
5. **Beat**: WASAPI process loopback feeding `beat_track.py`; `scripts/beat_eval.py` scores it.
6. **Microphone + hotkey**.
7. **Wake from sleep**, doctor and setup checks.
8. **Release**: a `windows-latest` job in `release.yml` builds the `.exe` widget; CI runs the
   tests on Windows too. Then add `win32` to `osguard.py` and update README and THIRD_PARTY.

Each step ends with the checks passing on both systems; the Linux tests must not regress.

## Step 1: where it stands

Done:

- `STRAWBERRY_ALLOW_UNSUPPORTED=1` lets the entry points past `osguard` on a system it does not
  list. It is for working on the port; `tests/conftest.py` sets it for the suite.
- Paths: config in `%APPDATA%\strawberry`, data (voices, `widget\strawberry-widget.exe`) in
  `%LOCALAPPDATA%\strawberry`, state (pidfiles, logs, `tray.json`) in
  `%LOCALAPPDATA%\strawberry\state`, the widget's copied-out files in
  `%LOCALAPPDATA%\strawberry\cache`. Git hooks go to `%APPDATA%\strawberry\git-hooks`.
- `strawberry daemon | status | say | talk | stop | git-event | git-hooks` work by hand. The
  daemon starts without the Ollama models and says so; `strawberry daemon` started no doorways
  off Linux (step 2 adds the media one on Windows). Ctrl+C and Ctrl+Break stop the daemon
  cleanly (Windows loops have no `add_signal_handler`, so a plain handler hands the signal to
  the loop).
- `strawberry widget`: no `--display-driver`, the `.exe` name, run as a child (no exec on
  Windows). The release asset name is `strawberry-widget-<ver>-windows-x86_64.exe`.
- Hooks are written with LF and forward-slash paths; a repository's own hook runs through Git's
  `sh`. Without `fork`, the git event is posted from a detached Python.
- The config and the widget's preferences are UTF-8 on every system.
- Tests: `@pytest.mark.linux_only` skips a test off Linux. Skipped on Windows: the systemd unit
  tests (`test_the_unit_starts_the_installed_tray_not_the_repo`,
  `test_install_rewrites_an_old_unit_and_restarts_it`) and the XDG icon-theme lookup of the
  notification watcher (`test_resolve_icon_prefers_a_path_then_walks_the_theme`). The old-symlink
  hook test skips itself where symlinks cannot be made (Windows without developer mode).

Left:

- Run the widget by hand once Godot is installed (`paths.gd` has the Windows paths, untested).
- `strawberry stop` ended the daemon with TerminateProcess; step 4 gave it a clean stop (a named
  stop event, below).
- The wake watcher finds no system bus and logs one line (step 7).
- `scripts/check_phase1.sh` and the other shell checks are Linux-only as written.

## Step 2: where it stands

Done:

- Layout: one module per system's media API, next to each other, and one shared place that
  picks. `mpris.py` (Linux) and `smtc.py` (Windows) are the reflexes; `media.controls()` picks
  one for the daemon. `doorways/mpris_watch.py` and `doorways/smtc_watch.py` are the watchers;
  `doorways.for_system()` says which doorways a system runs (Linux: the three as before;
  Windows: `smtc_watch` only; step 3 adds `toast_watch`) and `cli.doorways()`, `strawberry daemon` and the tray's
  `child_specs` use it. Nothing was moved, so every Linux import and test stays as it was.
- The interface is the one `actions.Actor` already used: `reflexes()`, `situation()`,
  `close()`. `smtc.Smtc` subclasses `mpris.Mpris` and replaces only what talks to the bus
  (`players`, `name_of`, `reread`, and two new hooks `_command` and `_set_volume` that `Mpris`
  now routes its button presses through), so the choice of player and every sentence are
  shared. SMTC has no volume; "turn it up" says the player won't say where the volume is.
- The watcher posts exactly what `mpris_watch` posts (WIRING §4b): dancing/idle on
  `/perform`, `{"source": "media", "app": ..., "title": "Artist — Title"}` on `/event`, once
  per track. WinRT events drive it, with a 5 s poll behind them.
- App identity: `SourceAppUserModelId` -> `smtc.app_key` (the short name for `[media] only`
  and `ignore`: `spotify`, `chrome`, `msedge`, `firefox`, and `chromium` for every
  Chromium-based browser) and `smtc.app_name` (what she says: "Spotify", "Google Chrome").
  Store ids (`<family>!<app>`), `.exe` names and full paths all reduce to the app's own name;
  Firefox's id is a hash of its install folder, and only the default folder's is known.
- Dependencies: `jeepney; sys_platform == 'linux'`, and on `win32` `winrt-runtime`,
  `winrt-Windows.Foundation`, `winrt-Windows.Foundation.Collections` and
  `winrt-Windows.Media.Control` (3.2.1, MIT). Nothing else is needed for sessions, media
  properties and controls. The dev group keeps jeepney everywhere for the D-Bus tests.
- Nothing that runs on Windows imports jeepney at import time: `wake.py` finds no system bus
  without it, and doctor's tray-host check says "not checked". `tests/test_imports.py` imports
  each side in a fresh interpreter with the other's package blocked.
- The Spotify adapter does not use MPRIS (it is an MCP server over Spotify's Web API); nothing
  there changed. Doctor and setup have no SMTC checks yet (step 7); the MPRIS check says "not
  checked" on Windows and nothing crashes.
- Tests: `tests/test_smtc.py` and `tests/test_smtc_watch.py` run a fake session manager
  shaped like the WinRT one, on any system; `tests/conftest.py` makes the real manager
  unreachable in every test, so no test can press the user's players' buttons.
- Seen live on a throwaway daemon (port 8782) with a silent session of its own: the watcher
  followed it from events alone, and the reflexes read it and paused and resumed it.

Left:

- The first track of a player that appears already playing is not announced, as on MPRIS; a
  browser tab often registers its session with the track already set.
- Two sessions of one app are told apart by their order (`Chrome`, `Chrome#2`), which can
  change when one closes.
- A `pause` or `skip` from the user has not yet been tried on a real player; the fake covers
  the calls, and the calls on a test session of our own worked.

## Step 3: where it stands

Done:

- Layout: `doorways/notifications.py` is what both notification doorways do once they have
  read a notification: `clean`, the content deduper, `allowed` (own notifications, `only_apps`,
  `ignore_apps`, `min_urgency`, replacements), the body decision (`body` / `body_apps`), the
  event, the burst summary, and `Forwarder` (the log line with `body_len` and never the body,
  the coalescing window, the 30 s POST). `notify_watch.py` keeps the D-Bus reading,
  `parse_notify` and the XDG icon lookup, and re-exports the shared names, so Linux behaves as
  before and its tests did not change. `doorways/toast_watch.py` is the Windows reader. The
  privacy checks (`privacy.py`, the gate, "private") are the daemon's and were already shared.
- Reading: `UserNotificationListener.Current`, `GetNotificationsAsync(NotificationKinds.Toast)`.
  Per toast: `AppInfo.DisplayInfo.DisplayName` is the app, `AppInfo.AppUserModelId` gives the
  short key (`smtc.app_key`, in the `desktop_entry` slot, so `ignore_apps`/`only_apps`/
  `body_apps` match by name or key), and the `ToastGeneric` binding's text elements are the
  title (the first) and the body (the rest, joined). Toasts carry no urgency, category or
  replaces id: `normal`, empty, 0. The app's logo (`DisplayInfo.GetLogo`, packaged apps only in
  practice) is written once per app to `%LOCALAPPDATA%\strawberry\cache\app-icons\<key>.png` and
  is the event's `icon`.
- Change events: none. `add_NotificationChanged` from an unpackaged process fails with
  `OSError [WinError -2147023728]`, `0x80070490`, Element not found. The watcher polls every
  second and diffs by `UserNotification.Id`; a read took 150-170 ms (one 500 ms) and about 4 ms of
  CPU. What is already in the notification centre at start is not announced.
- `doorways.for_system()` on Windows is `smtc_watch` and `toast_watch`, for `strawberry daemon`
  and the tray's children. The tray passes its `--config` to either notification doorway and
  restarts whichever runs when Message bodies changes.
- Dependencies (`win32` only, 3.2.1, MIT): `winrt-Windows.UI.Notifications` (the toast's visual
  and text elements, `NotificationKinds`), `winrt-Windows.UI.Notifications.Management` (the
  listener), `winrt-Windows.ApplicationModel` (without it `UserNotification.AppInfo` raises
  `ModuleNotFoundError`), `winrt-Windows.Storage.Streams` (the logo's bytes).
- Tests: `tests/test_toast_watch.py` runs a fake listener shaped like the WinRT one on any
  system, including one notification giving the same `allowed` and event on both systems;
  `tests/conftest.py` makes the real listener unreachable in every test; the no-body-in-logs
  canary in `tests/test_privacy.py` runs through both doorways; `tests/test_imports.py` imports
  `toast_watch` without jeepney and without winrt.
- Seen live 2026-09-23 on a throwaway daemon (port 8783), the gate down (no embeddinggemma): a
  fake toast driven into the watcher and over HTTP came out as "Chat sent something private."
  with bodies on (`private, gate unavailable`) and "Chat: Sam" with bodies off, and no body in
  any log. The watcher with the real listener started, counted the toasts already there and
  polled; its `only_apps` named no real app, so no real toast was forwarded or logged.

### Access: what was found on this machine

Windows 11, build 26200, from an unpackaged Python 3.12 and 3.13 process (no package
identity, no manifest, no sparse package):

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

So the listener works without identity tricks, and only the change event needs identity. What
the user does: nothing, where that switch is on. Where it is off, the watcher logs
`Windows says denied to reading notifications; turn it on in Settings > Privacy & security >
Notifications, 'Let apps access your notifications'` and exits 3; turning the switch on and
starting the doorway again is all it takes.

Not needed, and not done: a sparse package or MSIX with an external location (it would give
identity, hence `NotificationChanged` and a named entry in the Settings list, at the price of a
signed package), the `userNotificationListener` capability (it belongs to a package manifest),
and reading `%LOCALAPPDATA%\Microsoft\Windows\Notifications\wpndatabase.db` (undocumented, and
it holds every app's notification payload).

Left:

- Windows 10 (2004 and later) is untried: older builds may refuse the listener to an unpackaged
  process. `GetAccessStatus` answers first there; a refusal is the same "denied" line and exit 3.
- A toast that appears and is dismissed within one poll (1 s) is missed; a toast from an app
  whose toasts do not go to the notification centre may be seen only while it is on screen, or not at all (not tried).
- Toast scenarios (`urgent`, `alarm`, `incomingCall`) are not visible through the listener's
  API, so every toast is `normal` urgency and `min_urgency = "critical"` drops all of them.
- Desktop (unpackaged) apps usually have no logo through `GetLogo`, so no badge.
- Doctor and setup have no notification-access check yet (step 7); doctor's D-Bus monitor check
  says "not checked" on Windows.

## Step 4: where it stands

Done:

- The split. `supervisor.py` is the tray's children on every system: `child_specs`, the backoff,
  `restart_child` (Message bodies restarts `notify_watch` or `toast_watch`), the widget child,
  `tray.json`. `traymenu.py` is the menu as data and what its rows do: `menu_items`, the
  preferences read back from `widget.cfg`, the body setting from `config.toml`, and `TrayCore`
  (clicks, Message bodies, the health poll, `announce` for the front end). `tray.py` keeps the
  StatusNotifierItem on top of `TrayCore` and re-exports the moved names, so Linux logs and
  behaves as before and `tests/test_tray.py` did not change. `strawberryd --tray` picks
  `wintray.py` on Windows and `tray.py` elsewhere.
- The icon: `wintray.py`, the Win32 API through ctypes, no new dependency. A hidden top-level
  window on its own thread owns a `Shell_NotifyIconW` icon (NOTIFYICON_VERSION_4); asyncio stays
  on the main thread with `TrayCore` and the supervisor. Right click builds a popup menu from
  `menu_items()` with `InsertMenuItemW` and shows it with `TrackPopupMenuEx`: check marks
  (`MFS_CHECKED`), the radio lists (`MFT_RADIOCHECK`), the four submenus, the disabled status
  row, separators; "Per-app overrides in config" is left out while it is hidden on Linux. The
  menu refreshes from `/health` first (up to 0.5 s), as AboutToShow does. "Hide her"/"Show her"
  is the default row (bold) and a left click runs it, as Activate does on Linux. The tooltip is
  "Strawberry: <status>". The icon is a 32-bit HICON with alpha made from the packaged PNGs at
  the notification area's size (`SM_CXSMICON`, per-monitor DPI aware), so no .ico is needed for
  it. `TaskbarCreated` (Explorer restarted) adds it again, a failed add at login is retried every
  2 s, and `WM_ENDSESSION` stops the children before logoff.
- Why not pystray: it would add pystray (LGPL-3.0) and Pillow (its icons are PIL images) to
  every Windows install, for a wrapper around the same Win32 calls used here (about 300 lines),
  with its own message loop and menu model between us and them. Written directly, every menu
  flag is ours to set and the tests read the real menu back. The Linux tray is likewise written
  on jeepney rather than a tray library. pywin32 is installed (mcp depends on it on Windows) but
  is not a dependency of ours, and nothing here needed it.
- Clean stop: a named event per process, `Local\strawberry-stop-<key>` (`winproc.py`). The daemon
  (`server.serve`) and everything that uses `client.stop_on_signals` (the doorways, the tray)
  create it and shut down as on SIGTERM when it is set. `<key>` is the process's own pid and the
  key its parent passes in `STRAWBERRY_STOP_EVENT`, because a venv's `python.exe` and uv's
  launchers start the real interpreter as a child of their own, so the pid a parent holds is
  not the interpreter's. The supervisor keys each run `<tray pid>-<name>-<n>`; `strawberry daemon`
  keys a by-hand process by its pidfile's name and path (`cli.pidfile_stop_key`), so a
  throwaway state dir never answers for the user's own; the tray answers to the pid in
  `tray.json`. Who may set it: `Local\` is the logon session's namespace and the event has the
  creating token's default DACL (the user, SYSTEM, the logon session). No port is opened, and
  the HTTP API is unchanged. `strawberry stop` sets it, waits, and falls back to TerminateProcess
  after 10 s (20 s for the tray, which stops its children first); the supervisor does the same
  with its 5 s grace. What has no event (the widget binary) gets TerminateProcess. Ctrl+Break
  was not used: it only reaches a process that shares the sender's console.
- The tray's children on Windows: no console window (`CREATE_NO_WINDOW`), their own process
  group, output to `<state>\<name>.log` (`strawberryd.log` for the daemon, the same files a
  by-hand run uses; moved to `.1` past 5 MB), and a kill-on-close job object, so a tray that is
  killed takes its children with it, as systemd's cgroup does. The tray itself logs to
  `<state>\tray.log` with dates. When the tray runs windowless (pythonw), its children run on
  `python.exe` beside it.
- Restart: the tray also answers `Local\strawberry-restart-<pid>`. `strawberry restart` sets it
  when a tray runs, and the tray restarts its children as its Restart row does. Her menu's
  "Apply settings" runs `strawberry restart` from inside the tray's job, where stopping the tray
  would end the caller too.
- Start on login: `strawberry install` writes `Strawberry.lnk` in the user's Startup folder
  (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`, `paths.startup_shortcut()`), with
  the berry as its icon (`%LOCALAPPDATA%\strawberry\strawberry.ico`, PNG entries), stops what ran
  before (by-hand daemon and doorways, an older tray) and starts the tray the way the shortcut
  will. The shortcut runs `strawberry-tray.exe --port <port>`, a new `[project.gui-scripts]`
  entry point: uv and pip make it a GUI-subsystem launcher that runs `pythonw.exe`, so no console
  window opens (checked: subsystem 2, and the launcher names `Scripts\pythonw.exe`). Without it,
  `pythonw.exe -m strawberry_crab tray`; without pythonw, `python.exe`, and install says a console
  stays open. The .lnk is written by `WScript.Shell` through Windows PowerShell 5.1, with the
  values in environment variables so nothing is quoted. No scheduled task and no registry: a
  Startup shortcut runs at login as the user with no extra rights, shows in Task Manager's
  Startup apps, and is undone by deleting a file. `strawberry uninstall` deletes the shortcut and
  the .ico and stops the tray. Once installed, `strawberry daemon` starts the tray rather than a
  daemon of its own, `status` prints `starts at login: <path>` and the tray's log, and `tray`
  refuses to start a second tray.
- Tests: `tests/test_wintray.py` builds real Win32 menus and icons and reads them back (nothing is
  shown: `tests/conftest.py` blocks `Shell_NotifyIconW`), and runs the tray's loop with a fake
  icon; `tests/test_winproc.py` stops its own processes through their events, through a venv
  launcher, and the real daemon child through the supervisor (`stop requested: shutting down`,
  `shut down; sessions closed`, exit 0); `tests/test_startup.py` writes a real .lnk under the
  throwaway APPDATA and covers install, uninstall, status, restart and the second-tray refusal,
  with the tray's start recorded (conftest refuses a real one). The systemd uninstall test and
  the app-switcher test are `linux_only` now. `tests/test_imports.py` imports the new modules
  without jeepney and without winrt.
- Seen live 2026-09-23 against a throwaway daemon (port 8784, throwaway APPDATA and
  LOCALAPPDATA, brain, gate, thinker, voice and speech off, `[actions] mpris = false`, `[media]
  only` and `[notifications] only_apps` naming no real app): `strawberry install` wrote the
  shortcut under the throwaway APPDATA and started `strawberry-tray.exe`; the tray's window was
  found and `Shell_NotifyIconGetRect` found its icon in the notification area; the menu built
  from the live daemon's state and read back through `GetMenuItemInfoW` had all 20 rows, the
  submenus, the checked radio rows, the greyed status row and the default row; `status` listed
  the shortcut, the tray and its three children; `restart` brought the children back with new
  pids; `stop` took 0.3 s, the daemon logged `stop requested: shutting down` and `shut down;
  sessions closed`, and afterwards no tray, child, window, icon or listener was left; install
  again and `uninstall` removed the shortcut and the .ico and stopped the tray. The widget child
  was reported missing (no Godot, no binary) and the rest ran. Nothing was clicked.

Left:

- At login the tray runs without `STRAWBERRY_ALLOW_UNSUPPORTED`, so until `win32` is in
  `osguard.SUPPORTED` (step 8) it refuses to start unless the user sets that variable in their
  user environment. `install` says so.
- The widget binary is ended with TerminateProcess on stop and restart (Godot has no stop event);
  whether it loses anything that way is untried until Godot is installed. In developer mode the
  widget child is `python -m strawberry_crab widget`, whose Godot is a grandchild; the tray's job
  takes it along on stop, but not on a per-child restart.
- The menu was read back from a menu built in the probe from the same state, not from the
  tray's own popup: opening that would have meant clicking on the user's desktop. Clicking the
  rows, the left click and Quit from the menu are covered by the fake-icon tests only.
- New icons land in the notification area's overflow on Windows 11 until the user drags them
  out; no GUID is registered for the icon (it would tie the icon to one executable path).
- Restart counts the children's stops as restarts in `tray.json` and logs them as exits, as the
  Linux Restart row does.
- The Startup folder is taken from `%APPDATA%`, not the `FOLDERID_Startup` known folder, so a
  redirected Startup folder is not followed.

## Step 6: where it stands

Done:

- Recording: `voice.capture()` is now the loop both systems share (0.1 s chunks, the noise
  floor + 12 dB, `silence_s`, the poke, `max_seconds`, giving up after 3 s without data), fed by a
  `read(timeout)` callable. Linux feeds it from pw-record exactly as before. Windows feeds it from
  `winmic.py`: a shared-mode WASAPI stream through `sounddevice` at 16 kHz, mono, s16, with
  `WasapiSettings(auto_convert=True)`, so Windows converts from the device's own format and whisper
  gets the same samples as on Linux. PortAudio's callback hands the blocks over a queue. The
  `Listener` picks the pair (recorder, microphone) by system (`voice.default_backend`).
- The input: `[voice] source` as a case-insensitive fragment of the device's name, else the WASAPI
  default recording device, else the first input. The default is used as it is: on Windows it is
  a real input, not the speaker monitor it is on a PipeWire desktop. `bluetooth` does nothing:
  Windows switches a headset to its hands-free profile itself when its microphone is opened. A
  device that is gone between the pick and the recording falls back to the default; none at all
  is "I can't find a microphone.".
- Dependency: `sounddevice>=0.5; sys_platform == 'win32'` (0.5.6, MIT; its Windows wheels carry
  PortAudio, MIT; it pulls in cffi, MIT-0). Linux stays on pw-record: its `--target` source names,
  the monitor-source rule and the Bluetooth profile switch are PipeWire's, and PortAudio on Linux
  would reach PipeWire through its ALSA plugin without them. sounddevice is imported only when a
  microphone is picked or opened; `tests/test_imports.py` imports everything without it and checks
  that building a `Listener` does not load it.
- Whisper on CUDA: with the toolkit on PATH (this machine has CUDA 12.8 there) it worked with no
  preload at all, from the toolkit's cuBLAS. With only the gpu wheels it failed at the first
  transcription with `RuntimeError: Library cublas64_12.dll is not found or cannot be loaded`:
  the wheels keep their DLLs in `nvidia\<name>\bin`, where the loader does not look.
  `preload_cuda_libraries` now loads `cublasLt64_12.dll` then `cublas64_12.dll` from there by path
  (a DLL already loaded is what a later load by name gets), and adds the `bin` directories to
  PATH and to the DLL search path (`os.add_dll_directory`). cuDNN is not preloaded: ctranslate2
  4.8.2's Windows wheel carries its own `cudnn64_9.dll` (9.10), and transcription loaded no other
  cuDNN DLL. A missing `nvidia` package is now "none found" rather than a `ModuleNotFoundError`.
- Measured 2026-09-23, RTX 3090, driver 610.88, the toolkit taken off PATH, `small` from the
  Hugging Face cache, `int8_float16`, `language = "en"`, the VAD on, a 4.5 s sentence spoken by
  Windows' own speech synthesiser to a 16 kHz WAV ("Play something by Daft Punk, and turn the
  volume down a little."): loaded in 1.2 s, the first transcription 0.4-0.5 s, then 0.22-0.30 s,
  word for word each time. The CPU (`int8`) took 1.4 s.
- The hotkey: the tray's hidden window registers `[voice] hotkey` with `RegisterHotKey`
  (`MOD_NOREPEAT`) and turns WM_HOTKEY into a bare `POST /listen` (`cli.listen_fast`, what
  `strawberry listen` sends). The syntax is GNOME's (`hotkey.py`: `<Control><Alt>space`;
  `Ctrl+Alt+Space` is read too; letters, digits, F1-F24 and named keys, at least one modifier).
  "" is the default and "off" none; a value that does not parse is a config error. The tray reads
  it again when config.toml changes and re-registers. A combination someone holds is one warning
  in `tray.log` and the tray runs on.
- The default is `<Control><Alt>space`. Linux's `<Super><Shift>space` cannot be had: RegisterHotKey
  on this machine refused Win+Shift+Space, Win+Space, Win+Shift+S, Win+L and Win+E with 1409
  (`ERROR_HOTKEY_ALREADY_REGISTERED`); Windows keeps Win-key combinations for itself, and
  Win+Shift+Space switches back through the input languages. Ctrl+Alt+Space, Ctrl+Shift+Space and
  Win+Alt+Space registered. Ctrl+Shift+Space is taken inside many apps, and PowerToys' Command
  Palette uses Win+Alt+Space by default. On layouts with AltGr, AltGr+Space is Ctrl+Alt+Space too.
- `strawberry hotkey [COMBO] [--remove]` on Windows checks the combination and writes
  `[voice] hotkey` through configedit (comments kept, validated, backed up); it cannot register
  from its own process, since a hotkey belongs to the window that registers it. With no COMBO it
  sets the default, as on Linux.
- Tests: `tests/test_winmic.py` (a fake sounddevice: the pick, the stream asked for, a recording
  through the callback, a poke, a silent device, a device that will not open);
  `tests/test_hotkey.py` (the syntax, the setting, the CLI); `tests/test_wintray.py` (the tray
  following config changes with a fake icon, and on Windows a real hidden window registering
  Ctrl+Alt+Shift+F24, hearing a posted WM_HOTKEY, and logging a combination already held);
  `tests/test_voice.py` (`capture()` alone, the Windows preload). `tests/conftest.py` makes the
  real microphone unreachable and refuses any real hotkey but F24, which no keyboard has.
- Seen live 2026-09-23: a throwaway daemon (port 8786, throwaway APPDATA and LOCALAPPDATA, voice
  off, `[actions] mpris = false`, `[notifications] only_apps` and `[media] only` naming nothing
  real) and a throwaway `--tray --no-children` with `hotkey = "<Control><Alt><Shift>F24"`: the tray
  logged `hotkey <Control><Alt><Shift>F24 registered: it runs listen`; a WM_HOTKEY posted to its
  window (no keys pressed or sent) reached the daemon as `POST /listen` (503, voice off) within
  0.11 s; writing `hotkey = "off"` unregistered it within the poll; both stopped through their stop
  events, and no window or process was left.
- The microphone on this machine: PortAudio's WASAPI host lists no input and no default input,
  and Windows has no active recording endpoint, so the live check could open no microphone;
  `winmic` picks none and she would say she cannot find one. A 16 kHz mono stream with
  `auto_convert` did open on the 48 kHz output device (a plain one is refused, `Invalid sample
  rate`), which is the same conversion in the other direction.

Left:

- A real recording on Windows is untried (no microphone here): the level thresholds and the
  WASAPI conversion of a real input, a USB or Bluetooth headset, a device taken in exclusive mode
  by another app (it should give up after 3 s without data).
- The microphone privacy switch (Settings > Privacy & security > Microphone, "Let desktop apps
  access your microphone"): untried with it off, where opening the stream fails or Windows
  delivers silence (then she says she did not catch that). Doctor should check it (step 7).
- The hotkey is registered by the tray only; `strawberry daemon` by hand has none (`strawberry
  listen` still works from anything that can run a command).
- `[voice] hotkey` is ignored on Linux, where the GNOME shortcut holds the binding.

## Before starting on the Windows machine

Install Git, uv, Ollama (then `ollama pull embeddinggemma gemma3:1b` and the brain model),
Godot 4.7.2 with its export templates, and the NVIDIA driver with CUDA for whisper on the GPU.
