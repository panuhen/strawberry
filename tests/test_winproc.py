"""Stopping our own processes on Windows: the named stop event, the TerminateProcess fallback,
the job that takes the tray's children with it, and the daemon's clean shutdown through all of
it. Every process here is one the test started."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time

import pytest

from strawberry_crab import cli, winproc
from strawberry_crab.client import DaemonClient
from strawberry_crab.supervisor import Child, Children

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows stop events")

# A doorway in miniature: stop_on_signals, then wait; says how it ended.
LISTENER = """
import asyncio, sys
from strawberry_crab.client import stop_on_signals

async def main():
    stopping = asyncio.Event()
    stop_on_signals(stopping)
    print("listening", flush=True)
    await stopping.wait()
    print("stopped cleanly", flush=True)

asyncio.run(main())
"""
SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def start_listener(key: str | None = None) -> subprocess.Popen:
    env = None
    if key:
        import os

        env = {**os.environ, winproc.STOP_ENV: key}
    process = subprocess.Popen([sys.executable, "-c", LISTENER], stdout=subprocess.PIPE, text=True, env=env)
    assert process.stdout.readline().strip() == "listening"
    return process


def test_the_event_name_is_in_the_sessions_own_namespace():
    assert winproc.event_name("42") == "Local\\strawberry-stop-42"


def test_a_process_stops_itself_when_its_event_is_set_through_a_launcher():
    # sys.executable is a venv's python.exe, which starts the real interpreter as its own child:
    # the pid we hold is the launcher's, so the key has to be handed down.
    process = start_listener("test-launcher-key")
    assert winproc.stop(process.pid, "test-launcher-key", timeout_s=10) == "stopped"
    assert process.stdout.read().strip() == "stopped cleanly"
    assert process.wait(5) == 0
    assert not winproc.request_stop("test-launcher-key")          # gone with the process


def test_without_an_event_it_is_terminated_and_a_dead_pid_is_gone():
    process = subprocess.Popen(SLEEPER)
    assert winproc.stop(process.pid, "no-such-key", timeout_s=1) == "killed"
    assert process.wait(5) != 0
    assert winproc.stop(process.pid, "no-such-key", timeout_s=1) == "gone"


def test_a_process_that_ignores_its_event_is_terminated_after_the_timeout():
    stubborn = ("import os, time\nfrom strawberry_crab import winproc\n"
                "winproc.listen_for_stop(lambda: None)\nprint('listening', flush=True)\ntime.sleep(60)\n")
    process = subprocess.Popen([sys.executable, "-c", stubborn], stdout=subprocess.PIPE, text=True,
                               env={**__import__("os").environ, winproc.STOP_ENV: "test-stubborn"})
    assert process.stdout.readline().strip() == "listening"
    started = time.monotonic()
    assert winproc.stop(process.pid, "test-stubborn", timeout_s=1) == "killed"
    assert 0.9 < time.monotonic() - started < 10
    assert process.wait(5) != 0


def test_the_job_ends_its_processes_when_it_closes():
    job = winproc.KillOnCloseJob()
    process = subprocess.Popen(SLEEPER)
    job.add(process.pid)
    assert process.poll() is None
    job.close()
    process.wait(10)                  # a minute's sleep, over at once (its exit code is the job's: 0)


async def test_the_supervisor_stops_a_child_through_its_event(tmp_path):
    child = Child("listener", [sys.executable, "-c", LISTENER])
    children = Children([child], state_path=tmp_path / "tray.json", log_dir=tmp_path)
    children.start()
    for _ in range(100):
        await asyncio.sleep(0.05)
        if (tmp_path / "listener.log").is_file() and b"listening" in (tmp_path / "listener.log").read_bytes():
            break
    assert child.stop_key.endswith("-listener-1")
    process = child.process
    await children.stop()
    assert process.returncode == 0                                   # its own exit, not TerminateProcess
    assert (tmp_path / "listener.log").read_text().splitlines() == ["listening", "stopped cleanly"]


async def test_a_child_without_an_event_is_terminated_by_the_supervisor(tmp_path):
    child = Child("sleeper", SLEEPER)
    children = Children([child], state_path=tmp_path / "tray.json", log_dir=tmp_path)
    children.start()
    await asyncio.sleep(0.3)
    process = child.process
    await children.stop()
    assert process.returncode not in (None, 0)


def test_a_by_hand_process_is_stopped_through_its_pidfile(tmp_path, capsys):
    pidfile, logfile = tmp_path / "listener.pid", tmp_path / "listener.log"
    cli.spawn([sys.executable, "-c", LISTENER], logfile, pidfile)
    for _ in range(200):
        if logfile.is_file() and b"listening" in logfile.read_bytes():
            break
        time.sleep(0.05)
    cli.stop_pidfile(pidfile, "listener")
    assert capsys.readouterr().out == "listener stopped\n"
    assert logfile.read_text().splitlines() == ["listening", "stopped cleanly"]
    assert not pidfile.exists()


def test_the_pidfile_key_differs_between_state_dirs(tmp_path):
    assert cli.pidfile_stop_key(tmp_path / "a" / "strawberryd.pid") != cli.pidfile_stop_key(tmp_path / "b" / "strawberryd.pid")
    assert cli.pidfile_stop_key(tmp_path / "strawberryd.pid").startswith("strawberryd-")


async def test_the_daemon_child_shuts_down_cleanly_when_the_supervisor_stops_it(tmp_path):
    """The real daemon, as the tray starts it, with the brain, the gate and the thinker on and
    Ollama a closed port: the stop event runs the whole shutdown, as SIGTERM does on Linux."""
    import socket

    from strawberry_crab.supervisor import child_specs

    def free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    port, ollama = free_port(), free_port()
    config = tmp_path / "config.toml"
    config.write_text(f"""
[brain]
enabled = true
ollama_url = "http://127.0.0.1:{ollama}"
timeout_s = 0.2
[gate]
enabled = true
[thinker]
enabled = true
[speech]
enabled = false
[voice]
enabled = false
[tools]
enabled = false
[actions]
mpris = false
""")
    daemon = child_specs(port, config, widget=False, doorways=())
    children = Children(daemon, state_path=tmp_path / "tray.json", log_dir=tmp_path)
    children.start()
    log = tmp_path / "strawberryd.log"
    health = DaemonClient(f"http://127.0.0.1:{port}")
    for _ in range(300):                                             # the port opens after the model loads
        await asyncio.sleep(0.1)
        if await health.get("/health"):
            break
    process = daemon[0].process
    await children.stop()
    text = log.read_text(errors="replace")
    assert "stop requested: shutting down" in text, text
    assert "shut down; sessions closed" in text, text
    assert "Unclosed" not in text, text
    assert process.returncode == 0, text

