# Back on Linux after the Windows port

What the next session on the Linux machine needs to do. The Windows port (`WINDOWS.md`) was
built and tested on Windows 11. The Linux suite ran in WSL (Ubuntu 24.04), but nothing ran on a
real Linux desktop: no GNOME, no session bus, no PipeWire, no systemd. This file lists what changed
in code that Linux runs too, and the checks that were not possible from Windows. It is the
counterpart of the handoff in `HANDOFF.md` that took the work to Windows. Delete it once the
checks below have passed and the release is out.

## Before anything else

The user's machine runs the tray from a checkout as the systemd user service
`strawberry-tray.service`. Never stop, restart or reinstall it; the user does that. Test
against a separate instance on another port (not 8770), with throwaway XDG dirs.

1. Pull `main` into the checkout.
2. `uv sync --inexact --group gpu`. Never a bare `uv sync`: it removes the CUDA wheels. Use a
   recent uv (`uv self update`). uv 0.6 rewrites `uv.lock` into an older format.
3. `uv run pytest -q`. In WSL: 668 passed, 38 skipped, and every skip is a Windows-only test
   (WASAPI, winproc, wintray, startup, winwake, the Windows doctor probes, the DLL search
   path). Any other skip or failure is new.

## What changed in code Linux runs

The Linux paths were kept identical in behaviour, but they were checked by reasoning and by the
tests, not on a desktop.

| Part | Change | Where |
|---|---|---|
| Tray | `tray.py` was split. The supervisor (children, backoff, restarts, `tray.json`) is `supervisor.py`, the menu as data and its actions is `traymenu.py`, and `tray.py` is just the StatusNotifierItem front end, re-exporting the moved names. | `supervisor.py`, `traymenu.py`, `tray.py` |
| Media | `Mpris` sends its buttons and volume through two hooks, `_command` and `_set_volume`; the daemon gets it through `media.controls()`. | `mpris.py`, `media.py`, `daemon.py` |
| Notifications | The OS-neutral half of the doorway (dedupe, filters, the body rule, batching, the POST) moved to `doorways/notifications.py`; `notify_watch.Watcher` subclasses its `Forwarder`. One DEBUG line changed: "relay copy of" is now "repeat of". | `doorways/notify_watch.py`, `doorways/notifications.py` |
| Beat | `beat_watch.py` is the shared watcher; the `pw-record` capture moved unchanged to `beat_pipewire.py`, picked at runtime. Doctor imports its names from there. | `doorways/beat_watch.py`, `doorways/beat_pipewire.py` |
| Voice | The recording loop is a shared `capture()`; `record()` still runs `pw-record`. Whisper now loads in the background: the daemon opens its port at once, `/health.voice` says `phase: "loading"` and why, and the hotkey gets "I'm still getting my ears on." until it is ready. A stop during a download exits at once. | `voice.py`, `server.py`, `daemon.py` |
| Startup log | "listening on" is logged once the port is really open, with the seconds since start. | `server.py` |
| Gate | Its start tries once more after an HTTP error from Ollama. Timeouts and "Ollama not there" are as before. | `systemone.py` |
| Gate and thinker | Recommending music and talking about artists, albums and genres are answered from her own knowledge; only playing, queueing, opening or saving particular music gets the "needs a music add-on" line. The gate has eight new examples, and `gate_phrases.json` has 13 new cases: `gate_check.py` is 85/87 (the same two documented misses). | `systemone.py`, `thinker.py`, `scripts/gate_phrases.json` |
| Wake | `wake.watcher()` picks logind on Linux; `wake.py` imports jeepney behind a guard. | `wake.py` |
| Doctor | `Probes.system` chooses the Linux or Windows checks. The Linux output should be exactly as before. | `doctor.py` |
| Setup | A new step, "Her ears (whisper)", fetches the whisper model up front, so the first start is not a 1.5 GB download. `--no-download` writes the config and fetches nothing. The widget, settings and install steps are now 5–7. | `setupcmd.py`, `cli.py` |
| Files | The config, `widget.cfg` and the phrase files are read and written as UTF-8. With a UTF-8 locale this changes nothing. | several |
| Dependencies | `jeepney` is `sys_platform == 'linux'` (and in the dev group everywhere); the `winrt-*` packages and `sounddevice` are Windows-only. `pyproject.toml` has a `strawberry-tray` gui-script; on Linux it is `strawberry tray`. | `pyproject.toml`, `uv.lock` |
| osguard | `SUPPORTED = ("linux", "win32")`. | `osguard.py` |
| Widget | `update_passthrough()` returns early while the right-click menu is open (`popup_hide` brings the polygon back). Restart widget passes `--display-driver` in lower case: `DisplayServer.get_name()` is "X11", and Godot accepts only "x11", so this was probably broken on Linux too. Everything else new in `widget.gd` (the pose-following window region, the bubble and badge in it) runs on Windows only. | `widget/widget.gd`, `widget/bubble.gd` |
| Idle helper | `desktop_idle.py` has a Windows branch; the widget takes its Python from `STRAWBERRY_PYTHON` when set. The Linux path is unchanged. | `widget/desktop_idle.py`, `widget/paths.gd` |
| Scripts | `check_phase1.sh` also runs under Git Bash (it picks `.venv/bin` on Linux). `build_widget.sh` picks the preset by host; `TARGET=windows` builds the other one. | `scripts/` |
| CI and release | `ci.yml` has a `test-windows` job. `release.yml` has a `build-widget-windows` job, and the release waits for it and attaches the `.exe`. The Linux jobs and the `pypi` environment are unchanged. Neither Windows job has run on GitHub yet. | `.github/workflows/` |

## Checks to run on the Linux desktop

In this order, each against a throwaway instance, never the service:

1. `uv run pytest -q`, as above.
2. `scripts/check_phase1.sh`: the tests plus a headless widget against a real daemon.
3. `scripts/check_tray.sh`: registers the tray and reads it back. This is the check that matters
   most, because the tray code was split and the SNI front end now sits on `TrayCore`.
4. The tray by hand on a throwaway port: the menu's rows and check marks, Message bodies,
   Restart, Quit, the per-app note. Then `strawberry status` and `strawberry stop`.
5. The widget by hand: click-through around her, the right-click menu (open and close, and
   click-through after it closes), *Restart widget* (the lower-case display-driver fix), the
   speech bubble, the type box, falling asleep after five idle minutes.
6. `strawberry doctor` and `strawberry setup --no-download` on throwaway dirs: the Linux output
   as before, plus the new whisper step in setup.
7. The whisper background load: with a model that is not cached (point `HF_HUB_CACHE` at a
   throwaway dir, `[voice] model = "tiny"`), `/health` answers during the download and the
   hotkey says she is still getting her ears on.
8. `uv run python scripts/gate_check.py` (85/87) and `scripts/sensitive_check.py` (38/38).
9. `scripts/check_reconnect.sh`, which is Linux-only.

When they pass, tell the user; the user restarts `strawberry-tray.service` to run the new code.

## Then the release

1. Push `main` (done from Windows) and let `ci.yml` run. The `windows-latest` job runs there for
   the first time.
2. Bump the version (`pyproject.toml` and `src/strawberry_crab/__init__.py`), turn
   `## [Unreleased]` in `CHANGELOG.md` into the version's section, update the compare links,
   `uv lock`, commit, push. 0.2.0 fits a new platform.
3. `git tag -a vX.Y.Z` and push the tag. `release.yml` builds the Linux and Windows widgets; the
   user approves the `pypi` environment.
4. Afterwards, on both systems: `uv tool install strawberry-crab==X.Y.Z` in a throwaway place,
   `strawberry widget --fetch`, `strawberry doctor`.

## Still open

- On Linux, from before the port: warm-on-wake not yet seen after a real suspend; the privacy
  decisions in `HANDOFF.md`; the beat tracker's octave and off-beat limits (WIRING §4c).
- On Windows: the list at the end of `WINDOWS.md` ("What is left").
- Found while trying it on Windows: after she had refused one question, she once repeated the
  same refusal for every question that followed. A restart cleared it, and it did not come back
  in later runs. Watch for it; if it comes back, look at what the thinker is given of her earlier
  lines.
- `Daemon.start` still awaits Piper, the brain warm-up and the gate before the port opens (5–9 s
  on the Windows machine; up to their timeouts when Ollama is slow). Opening the port first is a
  larger change to startup and shutdown.
