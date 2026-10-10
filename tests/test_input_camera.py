"""The Input tab's camera switch (brainui.api_input_camera, inputs.InputDesk.camera_view, WIRING.md §26): it writes
`[gestures] enabled` in config.toml as the tray's row does, a settings save never touches it, and the tab shows
what the watcher says of the camera apart from the switch. Fake cameras only."""

from __future__ import annotations

import logging
import time

import pytest

from strawberry_crab import brainui, inputs, paths
from strawberry_crab.config import load
from strawberry_crab.daemon import Daemon
from strawberry_crab.server import create_app
from tests.test_brainui import sign_in
from tests.test_gestures import gesture_daemon, spin, watcher_for

CONFIG = "# the user's own comment\n[gestures]\nenabled = false   # off for now\nhold_ms = 300.0\n"
OPEN = "camera=open; fps=4.0; cpu=3.0"
CLOSED = "camera=closed; fps=0.0; cpu=0.2"


@pytest.fixture
async def client(aiohttp_client):
    daemon, _ = gesture_daemon(enabled=False)
    path = paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG, encoding="utf-8")
    return await aiohttp_client(create_app(daemon))


def daemon_of(client) -> Daemon:
    return client.server.app[brainui.UI].daemon


async def test_the_camera_switch_needs_the_session_the_csrf_header_and_the_origin(client):
    signed = await sign_in(client)
    origin = f"http://127.0.0.1:{client.port}"
    path = "/ui/api/input/camera"
    assert (await client.post(path, json={"on": True}, headers={"Origin": origin})).status == 401
    assert (await signed.post(path, {"on": True}, csrf=False)).status == 403
    assert (await signed.post(path, {"on": True}, origin="http://127.0.0.1:1")).status == 403
    assert (await signed.post(path, {"on": True}, origin=None)).status == 403
    assert (await signed.post(path, {"on": "yes"})).status == 400
    assert paths.config_file().read_text(encoding="utf-8") == CONFIG
    assert daemon_of(client).config.gestures.enabled is False


async def test_the_switch_writes_enabled_in_config_toml_as_the_tray_does_and_she_follows(client, caplog):
    caplog.set_level(logging.INFO)
    signed = await sign_in(client)
    daemon = daemon_of(client)
    reply = await (await signed.post("/ui/api/input/camera", {"on": True})).json()
    assert reply["enabled"] is True and daemon.config.gestures.enabled is True
    assert reply["status"]["camera"] == {"enabled": True, "state": "unknown"}     # the watcher not heard yet
    text = paths.config_file().read_text(encoding="utf-8")
    assert "# the user's own comment" in text and "enabled = true" in text and "hold_ms = 300.0" in text
    assert load(paths.config_file()).gestures.enabled is True
    assert not inputs.input_file().exists()                                      # never input.toml
    assert "the Input tab's camera switch" in caplog.text
    reply = await (await signed.post("/ui/api/input/camera", {"on": False})).json()
    assert reply["enabled"] is False and daemon.config.gestures.enabled is False
    assert load(paths.config_file()).gestures.enabled is False
    backups = sorted(paths.config_file().parent.glob("config.toml.bak-*"))       # configedit's, one a write
    assert len(backups) == 2 and backups[0].read_text(encoding="utf-8") == CONFIG


async def test_a_settings_save_never_turns_the_camera_on(client):
    signed = await sign_in(client)
    daemon = daemon_of(client)
    view = await (await signed.get("/ui/api/input")).json()
    assert "enabled" not in view["settings"]["gestures"] and view["status"]["camera"]["enabled"] is False
    settings = view["settings"]
    settings["gestures"]["hold_ms"] = 700
    saved = await signed.post("/ui/api/input/save", {"settings": settings})
    assert saved.status == 200 and daemon.config.gestures.hold_ms == 700.0
    assert daemon.config.gestures.enabled is False and load(paths.config_file()).gestures.enabled is False
    assert paths.config_file().read_text(encoding="utf-8") == CONFIG
    # `enabled` in the settings is refused outright, and in input.toml by hand it makes the file not used.
    sneaky = {**settings, "gestures": {**settings["gestures"], "enabled": True}}
    assert not (await (await signed.post("/ui/api/input/check", {"settings": sneaky})).json())["ok"]
    assert (await signed.post("/ui/api/input/save", {"settings": sneaky})).status == 400
    inputs.input_file().write_text("[gestures]\nenabled = true\nhold_ms = 800.0\n", encoding="utf-8")
    daemon.input.store.check(force=True)
    assert daemon.config.gestures.enabled is False and daemon.config.gestures.hold_ms == 700.0
    assert load(paths.config_file()).gestures.enabled is False
    assert daemon.gestures.reload() is False                                     # the tray's reload agrees


def test_the_camera_state_is_the_watchers_word_not_the_switch():
    daemon, _ = gesture_daemon(enabled=True)
    desk = daemon.input
    assert desk.camera_view(True) == {"enabled": True, "state": "unknown"}
    assert desk.camera_view(False) == {"enabled": False, "state": "closed"}
    desk.heard(OPEN)
    assert desk.camera_view(True)["state"] == "open"
    assert desk.camera_view(False)["state"] == "closing"      # switched off, its last word was open
    desk.heard(CLOSED)
    assert desk.camera_view(True)["state"] == "closed"        # on, and closed (watch = "armed", a busy camera)
    desk.heard(OPEN)
    desk.watcher_at = time.monotonic() - 6.0                  # it stopped posting: off, it lets go and goes quiet
    assert desk.camera_view(False)["state"] == "closed" and desk.camera_view(True)["state"] == "unknown"


async def test_the_switch_opens_and_closes_the_fake_camera_through_the_watcher(aiohttp_client, tmp_path, monkeypatch):
    watcher, cameras, fake, clock, path = watcher_for(tmp_path, monkeypatch, lambda t: None, "enabled = false")
    daemon, _ = gesture_daemon(enabled=False)
    daemon.config.path = path
    client = await aiohttp_client(create_app(daemon))
    signed = await sign_in(client)
    spin(watcher, clock, 2.0)
    assert cameras == [] and watcher.camera is None
    assert (await signed.post("/ui/api/input/camera", {"on": True})).status == 200
    spin(watcher, clock, 2.0)
    assert len(cameras) == 1 and watcher.camera is cameras[0]
    daemon.input.heard(watcher.report)
    assert daemon.input.camera_view(daemon.config.gestures.enabled)["state"] == "open"
    assert (await signed.post("/ui/api/input/camera", {"on": False})).status == 200
    spin(watcher, clock, 2.0)
    assert watcher.camera is None and cameras[0].closed
