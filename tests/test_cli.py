"""The `strawberry` CLI: the commands exist, the unit and hooks it writes point at the installed
entry point, and nothing here touches the real systemd, git config or daemon."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from strawberry import cli, paths

COMMANDS = ("widget", "daemon", "tray", "tray-autostart", "status", "stop", "restart", "install", "uninstall",
            "config", "listen", "hotkey", "route", "tools", "tool", "think", "talk", "say", "voices", "audition",
            "git-event", "git-hooks", "setup", "doctor")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Throwaway XDG dirs and git config; systemctl is recorded, never run."""
    for name in ("CONFIG", "DATA", "STATE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("STRAWBERRYD_PORT", raising=False)
    monkeypatch.delenv("STRAWBERRY_TRAY", raising=False)
    calls: list[tuple[str, ...]] = []

    def fake_systemctl(*args, capture=True):
        calls.append(args)
        return subprocess.CompletedProcess(["systemctl", "--user", *args], 1, "", "")

    monkeypatch.setattr(cli, "systemctl", fake_systemctl)
    return calls


def test_every_command_of_the_old_launcher_and_the_new_ones_exist():
    choices = next(a for a in cli.parser()._actions if a.dest == "command").choices
    assert set(COMMANDS) <= set(choices)


def test_the_port_comes_from_the_env_then_the_config_then_the_default(monkeypatch):
    assert cli.config_port() == 8770
    paths.config_file().parent.mkdir(parents=True)
    paths.config_file().write_text("[brain]\nport = 1\n[daemon]\nport = 8799\n")
    assert cli.config_port() == 8799
    monkeypatch.setenv("STRAWBERRYD_PORT", "8812")
    assert cli.config_port() == 8812
    monkeypatch.setenv("STRAWBERRYD_PORT", "")
    paths.config_file().write_text("[daemon\nbroken")
    assert cli.config_port() == 8770


def test_cli_argv_is_the_running_executable_or_this_interpreter(monkeypatch, tmp_path):
    exe = tmp_path / "bin" / "strawberry"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(exe), "install"])
    assert cli.cli_argv() == [str(exe)]
    monkeypatch.setattr(sys, "argv", ["/somewhere/strawberry/__main__.py"])
    assert cli.cli_argv() == [sys.executable, "-m", "strawberry"]


def test_the_unit_starts_the_installed_tray_not_the_repo():
    text = cli.unit_text(8770, ["/home/u/.local/bin/strawberry"])
    assert "ExecStart=/home/u/.local/bin/strawberry tray --port 8770\n" in text
    assert "WantedBy=graphical-session.target" in text
    fallback = cli.unit_text(8771, [sys.executable, "-m", "strawberry"])
    assert f"ExecStart={sys.executable} -m strawberry tray --port 8771\n" in fallback
    assert "Exec=/x/strawberry tray-autostart\n" in cli.autostart_text(["/x/strawberry"])


def test_install_rewrites_an_old_unit_and_restarts_it(isolated, monkeypatch, capsys):
    monkeypatch.setattr(cli, "wait_daemon", lambda here: True)
    monkeypatch.setattr(sys, "argv", [sys.executable, "install"])   # not named strawberry: -m form
    here = cli.Here()
    here.unit_dir.mkdir(parents=True)
    old = here.unit_dir / cli.TRAY_UNIT
    old.write_text("[Service]\nExecStart=/old/checkout/strawberryd/.venv/bin/strawberryd --tray --port 8770\n")
    (here.unit_dir / "strawberryd.service").write_text("")
    assert cli.cmd_install(here) == 0
    assert f"ExecStart={sys.executable} -m strawberry tray --port 8770" in old.read_text()
    assert not (here.unit_dir / "strawberryd.service").exists()
    assert here.autostart.read_text().startswith("[Desktop Entry]")
    verbs = [call[0] for call in isolated]
    assert verbs.index("daemon-reload") < verbs.index("enable") < verbs.index("restart")
    assert ("restart", cli.TRAY_UNIT) in isolated
    assert "rewrote" in capsys.readouterr().out


def test_uninstall_removes_the_unit_and_the_autostart(isolated):
    here = cli.Here()
    here.unit_dir.mkdir(parents=True)
    (here.unit_dir / cli.TRAY_UNIT).write_text("")
    here.autostart.parent.mkdir(parents=True)
    here.autostart.write_text("")
    assert cli.cmd_uninstall(here) == 0
    assert not (here.unit_dir / cli.TRAY_UNIT).exists() and not here.autostart.exists()
    assert ("disable", "--now", cli.TRAY_UNIT) in isolated


def test_status_without_anything_running_says_so(monkeypatch, capsys):
    monkeypatch.setenv("STRAWBERRYD_PORT", "1")
    assert cli.main(["status"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[:2] == ["tray: down", "strawberryd: down"]
    assert out[2:] == ["mpris_watch: down", "notify_watch: down", "beat_watch: down"]


def test_status_reads_the_trays_children(monkeypatch, capsys):
    monkeypatch.setenv("STRAWBERRYD_PORT", "1")
    state = paths.tray_state_file()
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"pid": os.getpid(), "children": [
        {"name": "daemon", "pid": 42, "restarts": 0, "running": True},
        {"name": "beat_watch", "pid": None, "restarts": 3, "running": False}]}))
    cli.main(["status"])
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"tray: up (pid {os.getpid()}, started by hand)"
    assert out[-2:] == ["daemon: up pid 42", "beat_watch: down (3 restarts)"]


def test_listen_and_say_fail_cleanly_without_a_daemon(monkeypatch, capsys):
    monkeypatch.setenv("STRAWBERRYD_PORT", "1")
    assert cli.main(["listen"]) == 1
    assert cli.main(["say", "hello"]) == 1
    assert "strawberryd is down" in capsys.readouterr().err


def test_voices_lists_what_is_installed(capsys):
    paths.voices_dir().mkdir(parents=True)
    for name in ("en_GB-alba-medium", "en_US-amy-medium"):
        (paths.voices_dir() / f"{name}.onnx").write_bytes(b"")
        (paths.voices_dir() / f"{name}.onnx.json").write_text("{}")
    assert cli.main(["voices"]) == 0
    assert capsys.readouterr().out.splitlines() == ["en_GB-alba-medium", "en_US-amy-medium", cli.VOICES_HINT]


def test_the_widget_without_a_checkout_says_why(monkeypatch, capsys):
    monkeypatch.setattr(paths, "widget_project", lambda: None)
    assert cli.main(["widget"]) == 2
    assert "source checkout" in capsys.readouterr().err


def test_setup_and_doctor_are_placeholders_for_step_5(capsys):
    assert cli.main(["setup"]) == 1
    assert cli.main(["doctor"]) == 1
    assert capsys.readouterr().err.count("step 5") == 2


def test_config_writes_the_template_once(monkeypatch, capsys):
    monkeypatch.delenv("EDITOR", raising=False)
    assert cli.main(["config"]) == 0
    assert paths.config_file().read_text().startswith("#")
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    assert out.count(str(paths.config_file())) == 3     # --init-config's line, then one per run


# --- the git doorway ---------------------------------------------------------------

def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    path = tmp_path / "myrepo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.invalid")
    git(path, "config", "user.name", "T")
    (path / "a").write_text("a")
    git(path, "add", "a")
    git(path, "commit", "-qm", 'first "quoted" commit')
    monkeypatch.chdir(path)
    return path


def test_git_event_payloads(repo):
    assert cli.git_event_payload("post-commit", []) == {
        "source": "git", "app": "post-commit", "title": "myrepo", "body": 'first "quoted" commit'}
    head = git(repo, "rev-parse", "HEAD")
    new_branch = f"refs/heads/main {head} refs/heads/main {cli.ZERO_SHA}\n"
    assert cli.git_event_payload("pre-push", ["origin", "url"], new_branch)["body"] == \
        "pushing 1 commit on main to origin"
    assert cli.git_event_payload("pre-push", ["origin", "url"], f"(delete) {cli.ZERO_SHA} refs/heads/x {head}\n") is None
    assert cli.git_event_payload("pre-push", ["up", "3"])["body"] == "pushing 3 commits on main to up"


def test_git_event_posts_detached_and_hands_over_to_the_repos_hook(repo, monkeypatch):
    posted = []
    monkeypatch.setattr(cli, "post_detached", lambda url, payload: posted.append((url, payload)))
    monkeypatch.setenv("STRAWBERRYD_URL", "http://127.0.0.1:1")
    own = repo / ".git" / "hooks" / "pre-push"
    own.write_text("#!/bin/sh\ncat > \"$(dirname \"$0\")/seen\"\nexit 3\n")
    own.chmod(0o755)
    head = git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(sys, "stdin", type("S", (), {"isatty": lambda self: False,
                                                     "read": lambda self: f"a {head} b {cli.ZERO_SHA}\n"})())
    assert cli.cmd_git_event("pre-push", ["origin", "url"]) == 3            # the repo's hook still vetoes
    assert posted == [("http://127.0.0.1:1", {"source": "git", "app": "pre-push", "title": "myrepo",
                                              "body": "pushing 1 commit on main to origin"})]
    assert (repo / ".git" / "hooks" / "seen").read_text() == f"a {head} b {cli.ZERO_SHA}\n"


def test_git_hooks_install_replaces_the_old_symlinks_and_remove_undoes_it(tmp_path, capsys):
    hooks = paths.git_hooks_dir()
    hooks.mkdir(parents=True)
    old = tmp_path / "checkout" / "doorways" / "git"
    old.mkdir(parents=True)
    for name in ("post-commit", "pre-push", "strawberry-git-event"):
        (old / name).write_text("#!/bin/sh\n")
        (hooks / name).symlink_to(old / name)
    assert cli.main(["git-hooks", "install"]) == 0
    for name in cli.GIT_HOOKS:
        text = (hooks / name).read_text()
        assert not (hooks / name).is_symlink() and os.access(hooks / name, os.X_OK)
        assert cli.GIT_MARKER in text and f"git-event {name} \"$@\"" in text
    assert not (hooks / "strawberry-git-event").exists()
    assert subprocess.run(["git", "config", "--global", "core.hooksPath"], capture_output=True,
                          text=True).stdout.strip() == str(hooks)
    assert cli.main(["git-hooks", "install"]) == 0                  # idempotent
    assert cli.main(["git-hooks", "remove"]) == 0
    assert not any(hooks.iterdir())
    assert subprocess.run(["git", "config", "--global", "core.hooksPath"], capture_output=True).returncode == 1


def test_git_hooks_leave_someone_elses_alone(capsys):
    hooks = paths.git_hooks_dir()
    hooks.mkdir(parents=True)
    (hooks / "post-commit").write_text("#!/bin/sh\necho mine\n")
    assert cli.main(["git-hooks", "install"]) == 1
    assert "not a Strawberry hook" in capsys.readouterr().err
    subprocess.run(["git", "config", "--global", "core.hooksPath", "/elsewhere"], check=True)
    assert cli.main(["git-hooks", "install"]) == 1
    assert "already /elsewhere" in capsys.readouterr().err


def test_an_installed_hook_runs_git_event_and_never_fails_git(tmp_path, repo):
    hooks = paths.git_hooks_dir()
    hooks.mkdir(parents=True)
    fake = tmp_path / "strawberry"
    fake.write_text(f"#!/bin/sh\necho \"$@\" > {tmp_path / 'called'}\n")
    fake.chmod(0o755)
    (hooks / "post-commit").write_text(cli.hook_text("post-commit", [str(fake)]))
    (hooks / "post-commit").chmod(0o755)
    subprocess.run([str(hooks / "post-commit")], check=True, cwd=repo)
    assert (tmp_path / "called").read_text() == "git-event post-commit\n"
    fake.unlink()                                                     # uninstalled: the hook stays quiet
    assert subprocess.run([str(hooks / "post-commit")], cwd=repo).returncode == 0
