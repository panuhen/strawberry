"""The tray's children: start them, restart the ones that exit, stop them all (WIRING.md §14).

The same on every system; only the icon in front of it differs (tray.py on Linux, wintray.py on
Windows). The children are the daemon, this system's doorways and the widget. A child that exits
comes back after a backoff; one asked to restart (a setting changed) comes back at once; Quit
stops them all. `tray.json` in the state dir says what runs, for `strawberry status`.

Windows differs in how a child is started and stopped, not in what is done:

- Each child gets no console window (CREATE_NO_WINDOW) and its own process group, and writes to
  `<state>\\<name>.log` (`strawberryd.log` for the daemon), because a tray started at login has
  no console or journal for it to inherit.
- Stopping a child sets its stop event (winproc.py): the daemon and the doorways shut down as on
  SIGTERM. What has no such event (the widget) is ended with TerminateProcess. The event's name
  goes to the child in STRAWBERRY_STOP_EVENT.
- Every child is put in a job object that ends it when the tray's last handle to the job closes,
  so a tray that is killed does not leave a daemon holding the port (systemd's cgroup does that
  on Linux).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Callable

from . import doorways as doorway_modules
from . import paths, widgetbin

log = logging.getLogger("strawberryd.tray")

NOTIFY_CHILD = "notify_watch"     # the doorway that reads [notifications] body at its start
NOTIFY_CHILDREN = (NOTIFY_CHILD, "toast_watch")   # ... on Linux, and on Windows
LOG_ROTATE_BYTES = 5 * 1024 * 1024                # Windows: a child's log is moved to .1 past this


@dataclass
class Child:
    name: str
    argv: list[str]
    env: dict[str, str] = dataclass_field(default_factory=dict)   # on top of the tray's own environment
    process: asyncio.subprocess.Process | None = None
    restarts: int = 0
    task: asyncio.Task | None = None
    asked_to_restart: bool = False     # restart_child: the next exit is ours, not a crash
    stop_key: str = ""                 # Windows: this run's stop event (winproc.event_name)

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process and self.process.returncode is None else None


def child_specs(port: int, config: Path | None, widget: bool = True,
                resolve_widget: Callable[[], widgetbin.Widget] | None = None,
                doorways: tuple[str, ...] | None = None) -> list[Child]:
    """The daemon, this system's doorways (`doorways.for_system()`: the three on Linux) and the
    widget, in the order they should come up.

    Everything Python runs on this same interpreter as a module of the package (`python -m
    strawberry_crab.strawberryd`, `python -m strawberry_crab.doorways.<name>`), so an installed tray never
    reaches back into a checkout. The widget is the exported binary when it is installed, run
    directly on the X11 backend; else developer mode, `python -m strawberry_crab widget` with the
    checkout's Godot project (it imports the project first when needed); else no widget child,
    and the log says how to get one (widgetbin.resolve). STRAWBERRY_CLI tells the widget's menu
    which `strawberry` to run for "Settings file…" and "Apply settings".
    """
    python = child_python()
    url = f"http://127.0.0.1:{port}"
    daemon = [python, "-m", "strawberry_crab.strawberryd", "--port", str(port)]
    if config:
        daemon += ["--config", str(config)]
    children = [Child("daemon", daemon)]
    for module in (doorway_modules.for_system() if doorways is None else doorways):
        argv = [python, "-m", f"strawberry_crab.doorways.{module}", "--daemon", url]
        if config and module in NOTIFY_CHILDREN:
            argv += ["--config", str(config)]      # the file "Message bodies" writes, not the XDG one
        children.append(Child(module, argv))
    if not widget:
        return children
    try:
        found = (resolve_widget or widgetbin.resolve)()
    except widgetbin.WidgetMissing as exc:
        log.warning("no widget: %s", exc)
        return children
    env = {"STRAWBERRYD_PORT": str(port)}
    strawberry = widgetbin.strawberry_cli()
    if strawberry:
        env["STRAWBERRY_CLI"] = strawberry
    if found.kind == "binary":
        children.append(Child("widget", found.argv(port), env))
    else:
        children.append(Child("widget", [python, "-m", "strawberry_crab", "widget"], env))
    return children


def child_python() -> str:
    """This interpreter. On Windows the console one (python.exe) when the tray runs windowless
    (pythonw.exe, the Startup shortcut's): a child started with CREATE_NO_WINDOW shows no console
    either way, and a console interpreter has its stdout and stderr where they are expected."""
    executable = Path(sys.executable)
    if paths.windows() and executable.name.lower() == "pythonw.exe":
        console = executable.with_name("python.exe")
        if console.is_file():
            return str(console)
    return sys.executable


def log_file(directory: Path, name: str) -> Path:
    """Where a child writes on Windows: the same file a by-hand run of it uses (cli.py)."""
    return directory / f"{'strawberryd' if name == 'daemon' else name}.log"


def open_log(path: Path):
    """The log opened for appending, the old one moved to `.1` once it is past LOG_ROTATE_BYTES."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > LOG_ROTATE_BYTES:
            path.replace(path.with_name(path.name + ".1"))
    except OSError:
        pass
    return path.open("ab")


class Children:
    """Start each child, restart the ones that exit, stop them all on the way out.

    Backoff doubles from 1 s to 30 s and resets once a child has stayed up for a while, so a
    doorway that cannot reach its bus does not spin, and a crash after an hour restarts at once.
    """

    FIRST_BACKOFF_S = 1.0
    MAX_BACKOFF_S = 30.0
    SETTLED_S = 30.0
    TERM_GRACE_S = 5.0

    def __init__(self, children: list[Child], state_path: Path | None = None,
                 log_dir: Path | None = None) -> None:
        self.children = children
        self.state_path = state_path
        self.log_dir = log_dir          # Windows: each child writes <log_dir>/<name>.log; None: inherited
        self.stopping = False
        self.job = None                 # Windows: winproc.KillOnCloseJob, made at the first start
        self.starts = 0

    def start(self) -> None:
        for child in self.children:
            child.task = asyncio.ensure_future(self._supervise(child))

    async def _supervise(self, child: Child) -> None:
        backoff = self.FIRST_BACKOFF_S
        while not self.stopping:
            started = time.monotonic()
            try:
                child.process = await self._spawn(child)
            except OSError as exc:
                log.error("%s will not start (%s)", child.name, exc)
                return
            log.info("%s started (pid %d)", child.name, child.process.pid)
            self.write_state()
            code = await child.process.wait()
            if self.stopping:
                return
            if child.asked_to_restart:
                child.asked_to_restart = False
                log.info("%s stopped to pick up a setting; starting it again", child.name)
                continue
            lived = time.monotonic() - started
            child.restarts += 1
            log.warning("%s exited with %s after %.0f s; restarting in %.0f s", child.name, code, lived, backoff)
            self.write_state()
            await asyncio.sleep(backoff)
            backoff = self.FIRST_BACKOFF_S if lived > self.SETTLED_S else min(backoff * 2, self.MAX_BACKOFF_S)

    async def _spawn(self, child: Child) -> asyncio.subprocess.Process:
        if not paths.windows():
            return await asyncio.create_subprocess_exec(*child.argv, env=self._env(child), start_new_session=False)
        from . import winproc

        self.starts += 1
        child.stop_key = f"{os.getpid()}-{child.name}-{self.starts}"
        options: dict[str, Any] = {
            "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            "stdin": subprocess.DEVNULL,
        }
        output = open_log(log_file(self.log_dir, child.name)) if self.log_dir is not None else None
        if output is not None:
            options |= {"stdout": output, "stderr": subprocess.STDOUT}
        try:
            process = await asyncio.create_subprocess_exec(*child.argv, env=self._env(child), **options)
        finally:
            if output is not None:
                output.close()          # the child has its own handle now
        try:
            if self.job is None:
                self.job = winproc.KillOnCloseJob()
            self.job.add(process.pid)
        except OSError as exc:
            log.debug("%s is not tied to the tray (%s)", child.name, exc)
        return process

    def _env(self, child: Child | None = None) -> dict[str, str]:
        # The widget launcher must not start a daemon or doorways of its own: we own those.
        env = {**os.environ, "STRAWBERRY_TRAY": "1", **(child.env if child else {})}
        if child is not None and child.stop_key:
            from .winproc import STOP_ENV

            env[STOP_ENV] = child.stop_key
        return env

    def _ask_to_stop(self, child: Child) -> None:
        """SIGTERM on Linux. On Windows the child's stop event, and TerminateProcess for a child
        that has none (the widget)."""
        if paths.windows() and child.stop_key:
            from . import winproc

            if winproc.request_stop(child.stop_key):
                return
        child.process.terminate()

    async def restart(self) -> None:
        """Ask every child to go; the supervisors bring them back."""
        for child in self.children:
            if child.process and child.process.returncode is None:
                self._ask_to_stop(child)

    def restart_child(self, name: str) -> bool:
        """Stop one child so its supervisor starts it again at once (no backoff, not counted as a
        restart). False when it is not running: it reads its settings when it next starts anyway."""
        for child in self.children:
            if child.name == name and child.process and child.process.returncode is None:
                child.asked_to_restart = True
                self._ask_to_stop(child)
                return True
        return False

    async def stop(self) -> None:
        self.stopping = True
        for child in self.children:
            if child.process and child.process.returncode is None:
                self._ask_to_stop(child)
        for child in self.children:
            if child.process is None:
                continue
            try:
                await asyncio.wait_for(child.process.wait(), self.TERM_GRACE_S)
            except asyncio.TimeoutError:
                log.warning("%s did not stop; killing it", child.name)
                child.process.kill()
        for child in self.children:
            if child.task and not child.task.done():
                child.task.cancel()
        if self.job is not None:
            self.job.close()
            self.job = None
        self.clear_state()

    def report(self) -> list[dict[str, Any]]:
        return [{"name": c.name, "pid": c.pid, "restarts": c.restarts,
                 "running": c.pid is not None, "command": c.argv} for c in self.children]

    def write_state(self) -> None:
        """`bin/strawberry status` reads this; nothing else depends on it."""
        if self.state_path is None:
            return
        payload = {"pid": os.getpid(), "updated": time.time(), "children": self.report()}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, indent=2))
            temporary.replace(self.state_path)
        except OSError as exc:
            log.debug("could not write %s (%s)", self.state_path, exc)

    def clear_state(self) -> None:
        if self.state_path is not None:
            self.state_path.unlink(missing_ok=True)
