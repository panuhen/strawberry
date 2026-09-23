# Handoff (temporary)

The state of the work for the next session. Delete this file when the user says the Windows port
is done.

## Where things stand

- Released: 0.1.1 on PyPI (`strawberry-crab`) and GitHub (`v0.1.1`, wheel, sdist and the Linux
  widget binary). The release workflow has run twice, green both times.
- Linux is complete for 0.1.1: tray, daemon, doorways, widget binary, `setup`, `doctor`,
  privacy modes, the gate retry and warm-on-wake. The user's own Linux machine runs the tray from
  a checkout (developer mode) as a systemd user service.
- The Windows port (`WINDOWS.md`) has all eight steps done, unreleased and untagged: `win32` is in `osguard.SUPPORTED`; the widget ran on Windows 11 in developer mode
  and as the exported `strawberry-widget-<ver>-windows-x86_64.exe`; `ci.yml` tests on
  `windows-latest` and `release.yml` builds and attaches the `.exe`. Neither workflow has run on
  GitHub with the Windows jobs yet. `CHANGELOG.md` `[Unreleased]` describes the whole port.
- `WINDOWS.md` is now the "how it works on Windows / what is left" document.

## Open on Linux

- Warm-on-wake (`wake.py`) is covered by tests but not yet seen after a real suspend. After the
  next one, the journal should show `loaded on resume`, and a notification right after wake
  should be read normally rather than dropped as private.
- Privacy decisions still to be made by the user:
  - when the gate is down entirely (not just slow): keep "every body is private", or fall back
    to off-mode behaviour;
  - whether notification titles are shown in off mode, and whether titles appear in logs;
  - vacuuming the user's old journal, which holds notification bodies from before the privacy
    fix (only if the user asks).
- Known beat-tracker limits (WIRING §4c): half-time styles come out at the other octave, and
  two syncopated patterns land on the off-beat.

- After step 8, trying it on the user's Windows machine found and fixed three more things: the
  widget's claws were clipped mid-dance (the window region now follows her pose), she refused to
  recommend music without a music server, and the first start did not answer for minutes while
  whisper downloaded (it now loads in the background). All three are in `[Unreleased]`.
- `main` was pushed from Windows; nothing is tagged or released.

## Next

Development moves back to Linux: `LINUX.md` has what changed in code Linux runs, the checks to run
on the Linux desktop in order, and the release steps. In short, before a release with Windows in
it (the user decides when):

1. On the Linux machine, from a checkout of the merged `main`: `uv sync --inexact --group gpu`,
   `uv run pytest -q`, `scripts/check_phase1.sh`, `scripts/check_tray.sh`, and the widget by hand
   (the widget's passthrough and menu code changed; the Linux paths should be unchanged).
2. Bump the version (`pyproject.toml`, `src/strawberry_crab/__init__.py`), turn `[Unreleased]`
   into the version's section, update the compare links, `uv lock`, commit, push `main`.
3. Push `main` first and let `ci.yml` run: the `windows-latest` test job has never run.
4. Tag `vX.Y.Z`; `release.yml` builds the Linux and Windows widgets; approve the `pypi`
   environment.
5. Afterwards, on both systems, `uv tool install strawberry-crab==X.Y.Z` in a throwaway place,
   `strawberry widget --fetch`, `strawberry doctor`.

The open items on Windows are listed at the end of `WINDOWS.md` ("What is left").

## Working agreements (also in CLAUDE.md)

- The main session orchestrates and verifies; Opus subagents in worktrees do the tasks.
- Ask before pushing, tagging or releasing, and before anything that touches the user's running
  setup; the user restarts services themselves.
- Keep the repo free of personal references.
- Replies: lead with what was done, plain sentences, no filler.
