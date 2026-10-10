"""Gestures and touch as the user tunes them: input.toml, the Brain UI's Input tab, the recent list, the live view
and practice mode (inputs.py, WIRING.md §17, §25, §26)."""

from __future__ import annotations

import asyncio
import logging
import os

import pytest

from strawberry_crab import brainui, inputs, paths
from strawberry_crab.config import Config, ConfigError, load
from strawberry_crab.daemon import Daemon
from strawberry_crab.doorways import gesture_watch
from strawberry_crab.events import CannedReactor
from strawberry_crab.server import create_app
from tests.fake_body import FakeBody
from tests.test_bodylink import FakePlayer, plain
from tests.test_brainui import read_event, sign_in
from tests.test_gestures import FakeDaemon, gesture_daemon, hand, spin, watcher_for
from tests.test_runs import until

CANARY = "kestrelq"     # an entity id: it may be in input.toml and the page, never in a log line

INPUT = """\
[gestures]
hold_ms = 600.0
zone = 0.7

[gestures.map]
thumb_up = "now_playing"
"music:thumb_up" = "skip"

[touch]
target_s = 5.0
cooldown_s = 0.0
music.flick = "next"
"""


def config_at(tmp_path, text: str = "[gestures]\nenabled = true\nhold_ms = 300.0\n"):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def bump(path) -> None:
    """A new mtime even within the file system's timestamp granularity."""
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


# ----------------------------------------------------------------------------- the file and the loader


def test_input_toml_replaces_the_sections_but_enabled_stays_in_config_toml(tmp_path):
    path = config_at(tmp_path)
    assert load(path).input_path is None and load(path).gestures.hold_ms == 300.0
    (tmp_path / "input.toml").write_text(INPUT, encoding="utf-8")
    config = load(path)
    assert config.input_path == tmp_path / "input.toml" and config.input_error == ""
    g = config.gestures
    assert g.enabled is True                                    # config.toml's switch
    assert g.hold_ms == 600.0 and g.zone == 0.7 and g.min_size == 0.12   # the section whole: defaults for the rest
    assert g.map == {"thumb_up": "now_playing", "music:thumb_up": "skip"}
    assert config.touch.target_s == 5.0 and config.touch.actions == {"music": {"flick": "skip"}}   # "next" is skip
    assert load(path, inputs=False).gestures.hold_ms == 300.0
    # A section input.toml leaves out stays config.toml's.
    (tmp_path / "input.toml").write_text("[touch]\ncooldown_s = 2.0\n", encoding="utf-8")
    config = load(path)
    assert config.gestures.hold_ms == 300.0 and config.touch.cooldown_s == 2.0 and config.touch.actions == {}


@pytest.mark.parametrize("text, words", [
    ("[gestures]\nenabled = true\n", "stays in config.toml"),
    ("[gestures]\nholdms = 3.0\n", "unknown keys"),
    ("[daemon]\nport = 1\n", "[gestures] and [touch] only"),
    ('[gestures.map]\nthumb_up = "save_tracks"\n', "read and playback"),
    ('[touch]\nmusic.flick = "send_message"\n', "not a reflex a touch can do"),
    ("[gestures]\nhold_ms = 50.0\n", "hold_ms must be between"),
    ("[gestures\n", "does not parse"),
])
def test_an_input_toml_that_does_not_check_out_is_left_out_with_the_reason(tmp_path, caplog, text, words):
    path = config_at(tmp_path)
    (tmp_path / "input.toml").write_text(text, encoding="utf-8")
    config = load(path)                                         # she still starts
    assert words in config.input_error and config.input_path is None
    assert config.gestures.hold_ms == 300.0 and config.gestures.enabled


def test_a_touch_action_the_config_puts_above_playback_is_refused_in_input_toml_too(tmp_path):
    path = config_at(tmp_path, '[tools.servers.spotify]\ncommand = "x"\nconfirm = ["next"]\n')
    (tmp_path / "input.toml").write_text('[touch]\nmusic.flick = "skip"\n', encoding="utf-8")
    assert "would be a change call" in load(path).input_error


def test_render_and_parse_agree_and_the_page_settings_go_through_the_loaders_checks(tmp_path):
    base = Config()
    gestures, touch, present = inputs.parse(INPUT, base)
    text = inputs.render(gestures, touch)
    again, touch2, _ = inputs.parse(text, base)
    assert again == gestures and touch2 == touch and present == {"gestures", "touch"}
    assert "\nenabled" not in text and '"music:thumb_up" = "skip"' in text and 'music.flick = "skip"' in text
    view = {"gestures": {"hold_ms": 500, "map": {"swipe_left": "previous"}},
            "touch": {"target_s": 3, "actions": {"music": {"grab": "pause"}}}}
    text, g, t = inputs.from_view(view, base)
    assert g.hold_ms == 500.0 and g.map == {"swipe_left": "previous"} and t.actions == {"music": {"grab": "pause"}}
    for bad, words in (({"gestures": {"enabled": True}}, "unknown settings"),
                       ({"gestures": {"map": {"thumb_up": "delete"}}}, "read and playback"),
                       ({"touch": {"actions": {"Music!": {"flick": "skip"}}}}, "entity id"),
                       ({"gestures": {"zone": "high"}}, "must be float"),
                       ([], "settings must be")):
        with pytest.raises(ConfigError, match=words.replace("(", r"\(")):
            inputs.from_view(bad, base)


# ----------------------------------------------------------------------------- the daemon reads it live


def daemon_at(path) -> Daemon:
    daemon = Daemon(reactor=CannedReactor(), config=load(path))
    daemon.actor.mpris = FakePlayer()
    return daemon


def test_the_daemon_takes_a_changed_input_toml_keeps_the_last_good_one_and_falls_back_when_it_goes(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    path = config_at(tmp_path)
    daemon = daemon_at(path)
    store = daemon.input.store
    assert not store.check() and daemon.config.gestures.hold_ms == 300.0
    file = tmp_path / "input.toml"
    file.write_text(INPUT, encoding="utf-8")
    assert store.check()
    assert daemon.config.gestures.hold_ms == 600.0 and daemon.config.gestures.enabled
    assert daemon.targets.ttl_s == 5.0 and daemon.touch_action("music", "flick") == "skip"
    assert store.status()["in_use"] and store.status()["error"] is None
    # A bad edit: she keeps what she had; the reason is for the tab, the log says only that it is not used.
    file.write_text(INPUT.replace('"skip"\n\n[touch]', f'"skip"\n"{CANARY}:fist_hold" = "delete"\n\n[touch]'),
                    encoding="utf-8")
    bump(file)
    assert not store.check()
    assert daemon.config.gestures.hold_ms == 600.0 and "read and playback" in store.status()["error"]
    assert CANARY in store.status()["error"]
    # Gone: config.toml's sections again.
    file.unlink()
    assert store.check() and daemon.config.gestures.hold_ms == 300.0 and daemon.config.touch.actions == {}
    assert not store.status()["in_use"] and store.status()["source"] == "config.toml"
    assert CANARY not in caplog.text


def test_the_trays_switch_still_turns_gestures_on_and_off_over_input_toml(tmp_path):
    path = config_at(tmp_path)
    (tmp_path / "input.toml").write_text(INPUT, encoding="utf-8")
    daemon = daemon_at(path)
    assert daemon.config.gestures.hold_ms == 600.0
    path.write_text("[gestures]\nenabled = false\nhold_ms = 300.0\n", encoding="utf-8")
    assert daemon.gestures.reload() is False and daemon.config.gestures.hold_ms == 600.0
    (tmp_path / "input.toml").write_text("[gestures]\nenabled = true\n", encoding="utf-8")   # not its business
    path.write_text("[gestures]\nenabled = true\n", encoding="utf-8")
    assert daemon.gestures.reload() is True and daemon.config.gestures.hold_ms == 600.0  # the rest kept


def test_the_watcher_follows_input_toml_and_keeps_its_settings_when_it_is_bad(tmp_path, monkeypatch):
    watcher, cameras, fake, clock, path = watcher_for(tmp_path, monkeypatch, lambda t: None)
    spin(watcher, clock, 1.5)
    assert watcher.settings.hold_ms == 400.0 and watcher.tracker.settings.hold_s == 0.4
    file = path.with_name("input.toml")
    file.write_text("[gestures]\nhold_ms = 800.0\nfps = 10.0\n", encoding="utf-8")
    spin(watcher, clock, 1.5)
    assert watcher.settings.hold_ms == 800.0 and watcher.settings.enabled and watcher.tracker.settings.hold_s == 0.8
    file.write_text("[gestures]\nhold_ms = 1.0\n", encoding="utf-8")
    bump(file)
    spin(watcher, clock, 1.5)
    assert watcher.settings.hold_ms == 800.0                     # kept
    path.write_text("[gestures]\nenabled = false\n", encoding="utf-8")
    bump(path)
    spin(watcher, clock, 1.5)
    assert watcher.settings.enabled is False and watcher.settings.hold_ms == 800.0 and watcher.camera is None


def test_the_watcher_says_its_camera_frame_rate_and_cpu_in_a_header(tmp_path, monkeypatch):
    watcher, cameras, fake, clock, _ = watcher_for(tmp_path, monkeypatch, lambda t: hand())
    spin(watcher, clock, 3.0)
    assert fake.report.startswith("camera=open; fps=")
    report = inputs.parse_watcher(fake.report)
    assert report["camera"] is True and 3.0 <= report["fps"] <= 16.0 and report["cpu"] >= 0.0
    for bad in ("camera=open", "camera=open; fps=1; cpu=1; x=2", "camera=maybe; fps=1; cpu=1", "x" * 100, None):
        assert inputs.parse_watcher(bad) is None
    poster = gesture_watch.Poster("http://127.0.0.1:1")
    poster.report = "camera=closed; fps=0.0; cpu=0.4"
    assert poster.report                                        # sent as X-Strawberry-Watcher on each request


# ----------------------------------------------------------------------------- recent, practice


def test_the_recent_list_keeps_twenty_and_folds_repeats():
    clock = [100.0]
    recent = inputs.Recent(clock=lambda: clock[0])
    for _ in range(3):
        recent.add("touch", "music drag", "ignored", why="not_mapped")
    assert len(recent.view()) == 1 and recent.view()[0]["count"] == 3
    for i in range(30):
        clock[0] += 5
        recent.add("gesture", f"g{i}", "did", "skip")
    view = recent.view()
    assert len(view) == 20 and view[0]["name"] == "g29"
    assert set(view[0]) == {"id", "at", "source", "name", "outcome", "action", "why", "count"}


async def test_gestures_say_what_they_did_or_why_not_and_practice_mode_does_nothing():
    daemon, mpris = gesture_daemon()
    await daemon.start()
    try:
        result = await daemon.gestures.gesture({"name": "swipe_right", "phase": "done"})
        await asyncio.wait_for(daemon.runs.get(result["run_id"]).finished.wait(), 5)
        daemon.gestures.last_done = -1e9
        await daemon.gestures.gesture({"name": "victory_hold", "phase": "done"})
        rows = daemon.input.recent.view()
        assert [(r["name"], r["outcome"], r["action"], r["why"]) for r in rows] == [
            ("victory_hold", "ignored", "", "not_mapped"), ("swipe_right", "did", "skip", "")]
        daemon.input.practice = True
        daemon.gestures.last_done = -1e9
        result = await daemon.gestures.gesture({"name": "swipe_right", "phase": "done"})
        assert result["action"] == "" and result["refused"] == "practice" and result["practice"] == "skip"
        await asyncio.sleep(0.1)
        assert mpris.pressed == ["skip"]                          # only the one before practice
        assert daemon.input.recent.view()[0]["outcome"] == "practice"
        daemon.gestures.last_done = -1e9
        assert (await daemon.gestures.gesture({"name": "arm", "phase": "done"}))["refused"] == "practice"
        assert daemon.gestures.state()["armed_s"] == 0.0
    finally:
        await daemon.close()


async def test_practice_mode_shows_a_mapped_touch_and_does_not_run_it(aiohttp_client):
    config = plain()
    config.touch.actions = {"music": {"flick": "skip"}}
    config.touch.cooldown_s = 0.0
    daemon = Daemon(reactor=CannedReactor(), config=config)
    daemon.actor.mpris = FakePlayer()
    client = await aiohttp_client(create_app(daemon))
    crab = await FakeBody.join(client, body_id="crab", entities=[], sends={}, phases=["touch"])
    orbs = await FakeBody.join(client)
    daemon.input.practice = True
    await orbs.touch("music", "flick")
    await orbs.settle()
    got = await crab.drain()
    assert [(m["entity"], m["kind"], m.get("action")) for m in got] == [("music", "flick", None)]
    assert daemon.actor.mpris.done == [] and daemon.input.recent.view()[0]["outcome"] == "practice"
    daemon.input.practice = False
    await orbs.touch("music", "flick")
    await until(lambda: daemon.actor.mpris.done == ["skip"])
    await until(lambda: daemon.input.recent.view()[0]["outcome"] == "did")
    assert daemon.input.recent.view()[0]["action"] == "skip"
    for body in (crab, orbs):
        await body.close()


# ----------------------------------------------------------------------------- the Input tab


@pytest.fixture
async def client(aiohttp_client):
    daemon, _ = gesture_daemon()
    return await aiohttp_client(create_app(daemon))


def daemon_of(client) -> Daemon:
    return client.server.app[brainui.UI].daemon


POSTS = [("/ui/api/input/check", {"settings": {}}), ("/ui/api/input/save", {"settings": {}}),
         ("/ui/api/input/practice", {"on": True})]


@pytest.mark.parametrize("path, body", POSTS)
async def test_every_input_write_needs_the_session_the_csrf_header_and_the_origin(client, path, body):
    signed = await sign_in(client)
    origin = f"http://127.0.0.1:{client.port}"
    assert (await client.post(path, json=body, headers={"Origin": origin})).status == 401
    assert (await signed.post(path, body, csrf=False)).status == 403
    assert (await signed.post(path, body, origin="http://127.0.0.1:1")).status == 403
    assert (await signed.post(path, body, origin=None)).status == 403
    assert (await signed.get("/ui/api/input", origin="https://evil.example")).status == 403
    assert (await signed.get("/ui/api/input/live", csrf=False)).status == 403
    assert not inputs.input_file().exists() and daemon_of(client).input.practice is False


async def test_the_input_tab_reads_checks_and_saves_input_toml_never_config_toml(client, caplog):
    caplog.set_level(logging.DEBUG)
    signed = await sign_in(client)
    view = await (await signed.get("/ui/api/input")).json()
    assert view["settings"]["gestures"]["map"]["thumb_up"] == "like" and "enabled" not in view["settings"]["gestures"]
    assert view["status"]["enabled"] is True and view["status"]["watcher"] == {"heard": False, "age_s": None}
    assert "like" in view["options"]["gesture_actions"] and "like" not in view["options"]["touch_actions"]
    assert view["file"]["exists"] is False and view["file"]["source"] == "config.toml"
    settings = view["settings"]
    settings["gestures"]["map"][f"{CANARY}:thumb_up"] = "now_playing"
    settings["touch"]["actions"] = {CANARY: {"flick": "skip"}}
    settings["gestures"]["hold_ms"] = 700
    checked = await (await signed.post("/ui/api/input/check", {"settings": settings})).json()
    assert checked["ok"] and f'+"{CANARY}:thumb_up" = "now_playing"' in checked["diff"]
    bad = {**settings, "gestures": {**settings["gestures"], "map": {"thumb_up": "save_tracks"}}}
    checked = await (await signed.post("/ui/api/input/check", {"settings": bad})).json()
    assert not checked["ok"] and "read and playback" in checked["problems"][0]
    assert (await signed.post("/ui/api/input/save", {"settings": bad})).status == 400
    assert not inputs.input_file().exists()
    saved = await (await signed.post("/ui/api/input/save", {"settings": settings})).json()
    assert saved["backup"] is None and saved["file"]["in_use"] and saved["file"]["source"] == "input.toml"
    daemon = daemon_of(client)
    assert daemon.config.gestures.hold_ms == 700.0 and daemon.config.gestures.enabled      # used at once
    assert daemon.touch_action(CANARY, "flick") == "skip"
    assert not paths.config_file().exists()                                                 # never config.toml
    settings["gestures"]["hold_ms"] = 800
    again = await (await signed.post("/ui/api/input/save", {"settings": settings})).json()
    assert again["backup"] and "hold_ms = 700.0" in inputs.input_file().with_name("input.toml.bak").read_text()
    assert load(paths.config_file()).gestures.hold_ms == 800.0                              # the loader agrees
    assert CANARY not in caplog.text


async def test_a_bad_input_toml_shows_in_the_tab_and_she_keeps_her_settings(client):
    signed = await sign_in(client)
    file = inputs.input_file()
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("[gestures]\nhold_ms = 1.0\n", encoding="utf-8")
    daemon_of(client).input.store.check()
    view = await (await signed.get("/ui/api/input")).json()
    assert "hold_ms must be between" in view["file"]["error"] and view["settings"]["gestures"]["hold_ms"] == 400.0
    assert view["file"]["text"] == "[gestures]\nhold_ms = 1.0\n"


async def test_practice_mode_is_a_runtime_switch(client):
    signed = await sign_in(client)
    assert (await signed.post("/ui/api/input/practice", {"on": "yes"})).status == 400
    assert (await (await signed.post("/ui/api/input/practice", {"on": True})).json()) == {"practice": True}
    assert daemon_of(client).input.practice is True and not inputs.input_file().exists()
    view = await (await signed.get("/ui/api/input")).json()
    assert view["status"]["practice"] is True


async def test_the_live_view_asks_the_watcher_for_the_hand_only_while_it_is_open(client):
    from tests.bus import BUS_SECRET

    signed = await sign_in(client)
    daemon = daemon_of(client)
    assert daemon.gestures.state()["wanted"] == {"gesture": False, "hand": False}
    stream = await signed.get("/ui/api/input/live")
    assert stream.status == 200 and stream.headers["Content-Type"].startswith("text/event-stream")
    status = await read_event(stream, "status")
    assert status["enabled"] is True and "watcher" in status
    await until(lambda: daemon.gestures.state()["wanted"]["hand"])
    headers = {"X-Strawberry-Secret": BUS_SECRET, inputs.WATCHER_HEADER: "camera=open; fps=14.9; cpu=12.5"}
    message = {"present": True, "x": 0.5, "y": 0.4, "size": 0.25, "pinch": 0.1, "open": 1.0, "engaged": True,
               "shape": "palm_hold", "points": {"wrist": [0.5, 0.5], "index": [0.45, 0.3]}}
    reply = await client.post("/hand", json=message, headers=headers)
    assert (await reply.json())["state"]["wanted"]["hand"] is True
    got = await read_event(stream, "hand")
    assert got == message                                                # numbers and names; no frame exists
    await client.post("/gesture", json={"name": "thumb_up", "phase": "progress", "progress": 0.5}, headers=headers)
    assert (await read_event(stream, "gesture")) == {"name": "thumb_up", "phase": "progress", "progress": 0.5}
    watcher = (await read_event(stream, "status"))["watcher"]
    assert watcher["heard"] and watcher["camera"] is True and watcher["fps"] == 14.9 and watcher["cpu"] == 12.5
    stream.close()
    await until(lambda: not daemon.input.live.watching)
    assert daemon.gestures.state()["wanted"] == {"gesture": False, "hand": False}


def test_the_page_has_the_input_tab_and_puts_text_in_as_text():
    page = (brainui.UI_DIR / "index.html").read_text(encoding="utf-8")
    assert 'data-section="input"' in page and '<section id="input"' in page
    script = (brainui.UI_DIR / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script and "eval(" not in script
    assert "/ui/api/input/live" in script and "getImageData" not in script and "drawImage" not in script
