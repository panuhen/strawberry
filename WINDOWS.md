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
| Microphone (`voice.py`) | `pw-record` from the default source | WASAPI capture (e.g. `sounddevice`), 16 kHz mono. |
| Tray (`tray.py`, `bus.py`, `icons.py`) | StatusNotifierItem + dbusmenu over jeepney | A notification-area icon (`pystray`, or Win32 `Shell_NotifyIcon`) with the same menu. The supervisor part of `tray.py` (children, backoff, `tray.json`) is OS-neutral and should be split out and shared. |
| Start on login (`cli.py install`) | systemd user unit + XDG autostart fallback | A shortcut in the Startup folder, or a per-user scheduled task at logon. |
| Wake from sleep (`wake.py`) | logind `PrepareForSleep` on the system bus | `WM_POWERBROADCAST` / `PBT_APMRESUMEAUTOMATIC`, or `PowerRegisterSuspendResumeNotification`. |
| Paths (`paths.py`, `widget/paths.gd`) | XDG dirs | `%APPDATA%\strawberry` (config), `%LOCALAPPDATA%\strawberry` (data, state, widget binary). |
| Hotkey (`cli.py hotkey`) | a GNOME custom shortcut via `gsettings` | `RegisterHotKey` in the tray process. |
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
- `strawberry stop` ends the daemon with TerminateProcess; a clean stop from another process
  (Ctrl+Break to its process group, or an HTTP call) belongs with the tray in step 4.
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

## Before starting on the Windows machine

Install Git, uv, Ollama (then `ollama pull embeddinggemma gemma3:1b` and the brain model),
Godot 4.7.2 with its export templates, and the NVIDIA driver with CUDA for whisper on the GPU.
