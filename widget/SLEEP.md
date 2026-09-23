# Sleeping

Right-click Strawberry → **Sleep after inactivity**: 1, 5 (default), 10, or 30 minutes, or Never. **Sleep now** previews it immediately when she is idle. Leave the mouse still after selecting it; keyboard or mouse activity wakes her.

The timer measures desktop-wide input inactivity and time since the last widget activity. Music, listening, thinking, talking, visible speech, reactions, dragging, and an open menu prevent sleep. After music stops or a conversation finishes, a fresh delay starts. The preference is saved in `user://widget.cfg` as `[sleep] after_minutes`; fractional values are accepted there, and zero disables automatic sleep.

She takes 2.5 seconds to settle onto her belly, folds all six legs, rests her claws beside her, and shuts both lids. `sleep_loop` has an eight-second breath with a longer exhale. `wake_up` stands her up in 1.5 seconds. Input during descent reverses it from the current pose. Music or a notification wakes her before its performance starts; newer incoming performances replace older pending ones during that short transition. The hat follows the shell and rests during sleep.

Sleep is a local widget state, so the daemon's existing idle/dancing contract is unchanged. The updated Blender source and both GLB copies include `sleep_enter`, `sleep_loop`, `wake_up`, and a `sleep_fold` morph on each leg. Only the three sleep clips animate eyelids; awake clips still use the existing blink controller. Audio retains ownership of claw-opening morphs.

Desktop inactivity is queried once per second on a worker thread through `desktop_idle.py`. X11 uses XScreenSaver; GNOME Wayland uses Mutter's idle monitor; Windows uses `GetLastInputInfo`. Only elapsed idle time is read, never key contents. If the API is unavailable, automatic sleep is disabled rather than mistaking activity in another app for inactivity. Other Wayland desktops need their own idle adapter. On Linux the helper runs on the system Python 3 (`/usr/bin/python3`) with the desktop's existing X11 libraries; Windows has no system Python, so `strawberry widget` and the tray pass their own interpreter in `STRAWBERRY_PYTHON`, and without it automatic sleep stays off. The exported binary carries the helper and copies it out to the cache dir to run it.

After updating: right-click → **Restart widget**. The new menu appears after restart.

Validation: `godot --headless --audio-driver Dummy --path widget --script res://validate_sleep.gd` uses isolated settings and no daemon connection. It checks timing, state blockers, closed lids, breathing, hat rest, mid-descent reversals, music/notification wake-up, and persistence. Add `-- --capture-dir=/absolute/path` and run with a graphical display to capture awake, sleeping, and waking frames. Blender export and clean re-import reports are in `v2/export_checks.json` and `v2/reimport_checks.json`.
