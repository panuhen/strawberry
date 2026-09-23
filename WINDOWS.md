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
| Notifications (`doorways/notify_watch.py`) | D-Bus `BecomeMonitor` on the session bus | `UserNotificationListener` (WinRT, via the `winrt-*` packages). The user grants access once in Settings. Reads other apps' toasts: app name, title, body. |
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

## Before starting on the Windows machine

Install Git, uv, Ollama (then `ollama pull embeddinggemma gemma3:1b` and the brain model),
Godot 4.7.2 with its export templates, and the NVIDIA driver with CUDA for whisper on the GPU.
