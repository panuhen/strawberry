# Working on Strawberry

Read `HANDOFF.md` first while it exists (temporary: the state of the work and what is next),
then `WIRING.md` (how everything fits and why) and, for the Windows port, `WINDOWS.md`.

## How the work is done

- The main session orchestrates and verifies; Opus subagents do the tasks, each in its own git
  worktree (`isolation: "worktree"`, launched with the repo as the working directory, or the
  launch fails with "not in a git repository"). The main session reviews each branch, merges it
  into `main` with `--no-ff`, resolves conflicts and runs the checks below on the merged code.
- Agents never push, tag, publish or merge. Pushing `main`, tagging and releasing happen only
  after the user says go.
- Give every agent the hard constraints below in its prompt; they do not inherit this file's
  intent reliably.

## Hard constraints

- Never stop, restart or reinstall the user's running service (Linux: `strawberry-tray.service`).
  The user restarts it. Test against a separate instance on another port (not 8770) with
  throwaway config/data/state/cache dirs.
- Never use the OS notification sender (`notify-send` on Linux) in tests: it reaches the live
  daemon. Drive a throwaway daemon over its HTTP/ws API instead.
- Never run a bare `uv sync`: it removes the CUDA wheels. Always `uv sync --inexact --group gpu`.
- No personal references anywhere in the repo (names, employers, other projects, preferences).
  Prompts and docs say "the user".
- Never log notification body text. `tests/` has a canary test for it.
- Ollama is shared and read-only for agents: never unload models (no `keep_alive=0`).
- Commits end with the attribution lines the session gives.

## Checks

```bash
uv sync --inexact --group gpu
uv run pytest -q                      # all must pass
uv run python scripts/gate_check.py   # the gate's phrase set
uv run python scripts/sensitive_check.py
scripts/check_phase1.sh               # tests + a headless widget against a real daemon
scripts/check_tray.sh                 # Linux: registers the tray and reads it back
```

## Releasing

Bump `version` in `pyproject.toml` and `__version__` in `src/strawberry_crab/__init__.py`, turn
`## [Unreleased]` in `CHANGELOG.md` into the version's section (the release notes are that
section), update the compare links at the bottom, `uv lock`, commit, push `main`, then
`git tag -a vX.Y.Z` and push the tag. `.github/workflows/release.yml` builds the wheel and the
widget, creates the GitHub release and waits for the user to approve the `pypi` environment.
Afterwards install the version from PyPI in a throwaway dir and run `strawberry widget --fetch`
and `strawberry doctor`.

## Style

Match the surrounding code: its comment density, naming and idioms. Docs are plain and direct.
