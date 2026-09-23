# Handoff (temporary)

The state of the work when development moved to Windows, for the next session. Delete this file
when the port is done.

## Where things stand

- Released: 0.1.1 on PyPI (`strawberry-crab`) and GitHub (`v0.1.1`, wheel, sdist and the Linux
  widget binary). The release workflow has run twice, green both times.
- Linux is complete for this version: tray, daemon, doorways, widget binary, `setup`, `doctor`,
  privacy modes, the gate retry and warm-on-wake. 487 tests.
- The user's own Linux machine runs the tray from a checkout (developer mode) as a systemd user
  service.

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

## Next

The Windows port: `WINDOWS.md` has the plan and the order of work. Start with step 1 (daemon
and widget by hand, paths, the test suite on Windows).

## Working agreements (also in CLAUDE.md)

- The main session orchestrates and verifies; Opus subagents in worktrees do the tasks.
- Ask before pushing, tagging or releasing, and before anything that touches the user's running
  setup; the user restarts services themselves.
- Keep the repo free of personal references.
- Replies: lead with what was done, plain sentences, no filler.
