"""The desktop's own controls: open and close the apps on the list, the computer's volume, the screen lock, and
Spotify opened when a play finds no device (desktop.py, WIRING.md §27). Every program is a fake: nothing is
launched, closed or locked, and the real volume is never touched (tests/conftest.py refuses run_command)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from strawberry_crab import apps, desktop, gestures
from strawberry_crab.actions import Actor
from strawberry_crab.adapters.spotify import _failed
from strawberry_crab.apps import AppStore
from strawberry_crab.config import ConfigError, ToolsConfig, load
from strawberry_crab.daemon import Daemon
from strawberry_crab.desktop import Desktop, DesktopSession, SpotifyWake
from strawberry_crab.events import Event
from strawberry_crab.tools import Toolbox
from tests.test_apps import install, standard, user_apps_dir
from tests.test_gestures import gesture_daemon
from tests.test_runs import until
from tests.test_thinker import ScriptedGate, bare, plain_config, voice_daemon
from tests.test_tools import FakeContent, FakeResult, FakeSession, FakeTool, make_connect

LIST = """\
[apps."spotify_spotify.desktop"]
name = "Spotify"
open = true
close = true
close_asks = false

[apps."firefox_firefox.desktop"]
name = "Firefox"
aliases = ["browser"]
open = true
close = true

[apps."steam.desktop"]
name = "Steam"
open = false
close = false
"""
TOOLS_HERE = ("gtk-launch", "systemd-run", "wpctl", "pactl", "wmctrl", "xdotool", "loginctl", "gdbus")
WINDOWS = ("0x0400000a  0 spotify.Spotify   host Spotify Premium\n"
           "0x0520001f  0 Navigator.firefox host A page - Mozilla Firefox\n"
           "0x0520002a  0 Navigator.firefox host Another page - Mozilla Firefox\n")
NEVER = ("kill", "windowkill", "windowclose", "pkill", "killall", "-9", "-KILL", "-TERM")


class FakeRunner:
    """Records each argv; answers by the longest matching prefix of "program arg arg…" (the program's name only)."""

    def __init__(self, replies: dict[str, tuple[int, str]] | None = None) -> None:
        self.replies = replies or {}
        self.calls: list[list[str]] = []
        self.envs: list[dict | None] = []

    async def __call__(self, argv: list[str], **kwargs: Any) -> tuple[int, str]:
        self.calls.append(list(argv))
        self.envs.append(kwargs.get("env"))
        said = " ".join([argv[0].rpartition("/")[2], *argv[1:]])
        for prefix in sorted(self.replies, key=len, reverse=True):
            if said.startswith(prefix):
                return self.replies[prefix]
        return 0, ""

    def ran(self, program: str) -> list[list[str]]:
        return [c for c in self.calls if any(w.rpartition("/")[2] == program for w in c)]

    def words(self) -> set[str]:
        return {w for call in self.calls for w in call[1:]}


def which(available=TOOLS_HERE):
    return lambda name: f"/usr/bin/{name}" if name in available else None


def write_list(text: str = LIST) -> None:
    path = apps.apps_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


async def desk_daemon(script: list | None = None, text: str = LIST, replies=None, available=TOOLS_HERE):
    standard()
    write_list(text)
    config = plain_config()
    config.actions.mpris = False
    toolbox, qwen, thinker = bare(script or [])
    daemon, sink = voice_daemon(config, toolbox, thinker, gate=ScriptedGate({}))
    runner = FakeRunner(replies)
    daemon.desktop._run = runner
    daemon.desktop.which = which(available)
    daemon.desktop.env = {"XDG_SESSION_ID": "3", "XDG_DATA_DIRS": "/usr/share"}
    await daemon.start()
    return daemon, sink, runner, qwen


async def say(daemon: Daemon, text: str) -> str:
    performance, _ = await daemon.handle_voice(Event(source="voice", title=text))
    return performance.text or ""


def no_force(runner: FakeRunner) -> None:
    for call in runner.calls:
        assert call[0].startswith("/usr/bin/"), call               # a program by its path, never a shell line
        assert not set(call[1:]) & set(NEVER), call


# ----------------------------------------------------------------------------- open


async def test_open_spotify_is_a_reflex_that_launches_its_desktop_id_and_nothing_else():
    daemon, sink, runner, qwen = await desk_daemon()
    try:
        said = await say(daemon, "Could you open Spotify, please?")
        assert said.startswith("Opened Spotify.") and qwen.payloads == []           # no model in the loop
        assert runner.calls == [["/usr/bin/systemd-run", "--user", "--scope", "--collect", "--quiet", "--slice=app.slice",
                                 "--", "/usr/bin/gtk-launch", "spotify_spotify.desktop"]]
        assert str(user_apps_dir().parent) in runner.envs[0]["XDG_DATA_DIRS"]    # where gtk-launch finds the entry
        assert (await say(daemon, "launch the browser")).startswith("Opened Firefox.")
        no_force(runner)
    finally:
        await daemon.close()


async def test_an_app_that_may_not_be_opened_or_is_not_listed_is_refused():
    daemon, sink, runner, qwen = await desk_daemon(script=[
                                                           [("open_app", {"app": "gimp"})], "[neutral] Not on it."])
    try:
        said = await say(daemon, "open steam")
        assert said.startswith("I'm not allowed to open Steam") and runner.calls == [] and qwen.payloads == []
        # Not a name on the list at all: the thinker's, whose call the guard refuses.
        install(user_apps_dir(), **{"gimp.desktop": "[Desktop Entry]\nType=Application\nName=GIMP\nExec=gimp\n"})
        await say(daemon, "open gimp")
        assert runner.calls == []
        told = [m["content"] for m in qwen.payloads[-1]["messages"] if m["role"] == "tool"]
        assert told and "not on the user's list" in told[-1]
        offered = [t["function"]["name"] for t in qwen.payloads[-1]["tools"]]
        assert "open_app" in offered and "lock_screen" not in offered
        # And by hand, around every check but the session's own: the same refusal.
        session = DesktopSession(daemon.apps, daemon.desktop)
        result = await session.call_tool("open_app", {"app": "steam.desktop"})
        assert result["isError"] and "not allowed" in result["content"][0]["text"] and runner.calls == []
    finally:
        await daemon.close()


async def test_open_asks_once_strangers_text_is_in_the_run_and_not_before():
    script = [[("open_app", {"app": "Spotify"})], "[happy] Opened.", [("open_app", {"app": "Spotify"})]]
    daemon, sink, runner, qwen = await desk_daemon(script=script)
    try:
        # The user's own sentence, through the thinker (no reflex for this one): made at once.
        await say(daemon, "open spotify and then tell me a joke")
        assert len(runner.ran("gtk-launch")) == 1 and daemon.approvals.open is None
        # A notice with others' words in the timeline: the same call waits for a yes, in the list's words.
        daemon.ledger.notice("notification", "Signal from a stranger", "A message came.", foreign=True)
        said = await say(daemon, "open spotify and then tell me a joke")
        assert said == "Open Spotify? Say yes." and daemon.approvals.open is not None
        assert daemon.approvals.open.risk == "change" and len(runner.ran("gtk-launch")) == 1
        await say(daemon, "yes")
        await until(lambda: len(runner.ran("gtk-launch")) == 2)
    finally:
        await daemon.close()


# ----------------------------------------------------------------------------- close


async def test_close_asks_on_her_card_first_unless_close_asks_is_false():
    replies = {"wmctrl -l -x": (0, WINDOWS)}
    daemon, sink, runner, qwen = await desk_daemon(script=[[("close_app", {"app": "Firefox"})]], replies=replies)
    try:
        # Firefox: close_asks left out, so true. The reflex leaves it to the thinker, which holds the call.
        said = await say(daemon, "close firefox")
        assert said == "Close Firefox? Say yes." and daemon.approvals.open.prompt == "Close Firefox?"
        assert runner.ran("wmctrl") == []
        await say(daemon, "yes")
        await until(lambda: len([c for c in runner.calls if "-c" in c]) == 2)
        assert ["/usr/bin/wmctrl", "-i", "-c", "0x0520001f"] in runner.calls
        # Spotify: close_asks = false, a reflex with no question and no model.
        asked = len(qwen.payloads)
        said = await say(daemon, "quit spotify")
        assert said.startswith("Asked Spotify to close.") and len(qwen.payloads) == asked
        assert ["/usr/bin/wmctrl", "-i", "-c", "0x0400000a"] in runner.calls
        no_force(runner)
    finally:
        await daemon.close()


async def test_a_close_that_would_ask_is_refused_to_a_reflex_that_tries_it_anyway():
    """Security review: the per-call confirm holds on every path, not only the thinker's."""
    daemon, sink, runner, qwen = await desk_daemon()
    try:
        assert daemon.toolbox.needs_approval("desktop", "close_app", arguments={"app": "Firefox"})
        assert not daemon.toolbox.needs_approval("desktop", "close_app", arguments={"app": "spotify"})
        assert daemon.toolbox.needs_approval("desktop", "close_app", arguments={"app": "nothing listed"})
        from strawberry_crab.actions import Guarded

        guarded = Guarded(daemon.toolbox)
        result = await guarded.call("desktop", "close_app", {"app": "Firefox"})
        assert not result.ok and guarded.refused == "desktop.close_app" and runner.calls == []
        # After strangers' text even close_asks = false asks (close is `change`).
        assert daemon.toolbox.needs_approval("desktop", "close_app", foreign=True, arguments={"app": "spotify"})
    finally:
        await daemon.close()


async def test_closing_is_polite_or_not_at_all():
    entry = apps.scan([standard()])["firefox_firefox.desktop"]
    # No wmctrl, an old xdotool without windowquit: she says so and touches nothing.
    runner = FakeRunner({"xdotool help": (0, "Available commands:\n  windowkill\n  windowclose\n")})
    done, why = await Desktop(run=runner, which=which(("xdotool",))).close(entry, "Firefox")
    assert not done and "wmctrl" in why and runner.calls == [["/usr/bin/xdotool", "help"]]
    # A newer xdotool: windowquit, the graceful request, for each window of its class.
    runner = FakeRunner({"xdotool help": (0, "windowquit\nwindowclose\n"), "xdotool search --class ^firefox$": (0, "123\n456\n")})
    done, _ = await Desktop(run=runner, which=which(("xdotool",))).close(entry, "Firefox")
    assert done and ["/usr/bin/xdotool", "windowquit", "123"] in runner.calls and "windowclose" not in runner.words()
    # Nothing of it open.
    runner = FakeRunner({"wmctrl -l -x": (0, "0x01  0 gnome-terminal.Gnome-terminal host Terminal\n")})
    done, why = await Desktop(run=runner, which=which(("wmctrl",))).close(entry, "Firefox")
    assert not done and why == "Firefox doesn't seem to be open" and len(runner.calls) == 1


class FakeBus:
    def __init__(self, players: dict[str, dict]) -> None:
        self.players = players
        self.called: list[tuple[str, str]] = []

    async def list_names(self):
        return ["org.freedesktop.DBus", *self.players]

    async def get_all(self, name, interface):
        return self.players[name]

    async def call(self, name, interface, member):
        self.called.append((name, member))


async def test_a_media_player_is_asked_to_quit_over_mpris():
    entry = apps.scan([standard()])["spotify_spotify.desktop"]
    bus = FakeBus({"org.mpris.MediaPlayer2.firefox.instance_1": {"DesktopEntry": "firefox", "CanQuit": True},
                   "org.mpris.MediaPlayer2.spotify": {"DesktopEntry": "spotify", "CanQuit": True}})
    runner = FakeRunner()
    done, _ = await Desktop(run=runner, which=which(), bus=bus).close(entry, "Spotify")
    assert done and bus.called == [("org.mpris.MediaPlayer2.spotify", "Quit")] and runner.calls == []
    # A player that cannot quit: the windows are asked instead.
    bus = FakeBus({"org.mpris.MediaPlayer2.spotify": {"DesktopEntry": "spotify", "CanQuit": False}})
    runner = FakeRunner({"wmctrl -l -x": (0, WINDOWS)})
    done, _ = await Desktop(run=runner, which=which(), bus=bus).close(entry, "Spotify")
    assert done and bus.called == [] and ["/usr/bin/wmctrl", "-i", "-c", "0x0400000a"] in runner.calls


# ----------------------------------------------------------------------------- the computer's volume


@pytest.mark.parametrize("sentence, asked", [
    ("set the computer volume to 30", {"action": "set", "level": 30}),
    ("set the system volume to 150 percent", {"action": "set", "level": 100}),
    ("could you put the computer's volume at 5% please", {"action": "set", "level": 5}),
    ("system volume down", {"action": "down"}),
    ("turn the computer volume up", {"action": "up"}),
    ("turn up the system volume", {"action": "up"}),
    ("lower the computer volume", {"action": "down"}),
    ("mute the computer", {"action": "mute"}),
    ("unmute the computer please", {"action": "unmute"}),
    ("turn it down", None),                       # the music's, as before (Spotify or MPRIS)
    ("volume down", None),
    ("set the volume to 30", None),
    ("mute the computer and play some jazz", None),
])
def test_the_computer_volume_by_whole_sentences(sentence, asked):
    assert desktop.read_volume(sentence) == asked


async def test_the_volume_is_the_default_sinks_and_never_above_100():
    runner = FakeRunner({"wpctl get-volume": (0, "Volume: 0.95\n")})
    session = DesktopSession(AppStore(apps.apps_file(), scanner=dict), Desktop(run=runner, which=which()))
    assert (await session.call_tool("system_volume", {"action": "up"}))["content"][0]["text"] == "Computer volume up to 100."
    assert runner.calls[-1] == ["/usr/bin/wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "1.00"]
    runner.replies["wpctl get-volume"] = (0, "Volume: 1.30 [MUTED]\n")      # boosted past 100 elsewhere
    text = (await session.call_tool("system_volume", {"action": "down"}))["content"][0]["text"]
    assert text == "Computer volume down to 90 (still muted)." and runner.calls[-1][-1] == "0.90"
    for level, sent in ((150, "1.00"), (-5, "0.00"), (30.4, "0.30")):
        await session.call_tool("system_volume", {"action": "set", "level": level})
        assert runner.calls[-1] == ["/usr/bin/wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", sent]
    assert (await session.call_tool("system_volume", {"action": "set", "level": "loud"}))["isError"]
    assert (await session.call_tool("system_volume", {"action": "boost"}))["isError"]
    await session.call_tool("system_volume", {"action": "mute"})
    assert runner.calls[-1] == ["/usr/bin/wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"]
    # Only volume and mute, only the default sink: nothing else in the audio setup is ever asked for.
    assert {c[1] for c in runner.calls} <= {"get-volume", "set-volume", "set-mute"}
    assert {c[2] for c in runner.calls} == {"@DEFAULT_AUDIO_SINK@"}
    # No wpctl: pactl, the same way.
    runner = FakeRunner({"pactl get-sink-volume": (0, "Volume: front-left: 26214 /  40% / -23.88 dB\n"),
                         "pactl get-sink-mute": (0, "Mute: no\n")})
    session = DesktopSession(AppStore(apps.apps_file(), scanner=dict), Desktop(run=runner, which=which(("pactl",))))
    assert (await session.call_tool("system_volume", {"action": "up", "level": 80}))["content"][0]["text"] == \
        "Computer volume up to 100."
    assert runner.calls[-1] == ["/usr/bin/pactl", "set-sink-volume", "@DEFAULT_SINK@", "100%"]


async def test_the_volume_sentence_is_a_reflex_of_the_playback_tier():
    daemon, sink, runner, qwen = await desk_daemon(replies={"wpctl get-volume": (0, "Volume: 0.40\n")})
    try:
        said = await say(daemon, "set the computer volume to 30")
        assert said.startswith("Computer volume 30.") and qwen.payloads == []
        assert daemon.toolbox.risk("desktop", "system_volume") == "playback"
        assert not daemon.toolbox.needs_approval("desktop", "system_volume", foreign=True, arguments={"action": "up"})
    finally:
        await daemon.close()


# ----------------------------------------------------------------------------- the lock


@pytest.mark.parametrize("sentence, locks", [
    ("lock the screen", True),
    ("Lock my computer.", True),
    ("could you lock the screen please", True),
    ("hey strawberry, lock my screen", True),
    ("lock the screen and play some jazz", False),
    ("don't lock the screen", False),
    ("I locked the screen", False),
    ("the lock screen is ugly", False),
    ("how do I lock the screen", False),
    ("lock", False),
])
def test_the_lock_is_a_whole_sentence_only(sentence, locks):
    assert desktop.says_lock(sentence) is locks


async def test_lock_the_screen_runs_without_a_question_and_is_never_the_thinkers():
    daemon, sink, runner, qwen = await desk_daemon(script=[[("lock_screen", {})], "[neutral] Hm."])
    try:
        said = await say(daemon, "lock the screen")
        assert said.startswith("Locked.") and runner.calls == [["/usr/bin/loginctl", "lock-session", "3"]]
        assert daemon.approvals.open is None and qwen.payloads == []
        # A model cannot call it: never offered, so a call to it is not made.
        await say(daemon, "open sesame and lock everything")
        assert len(runner.ran("loginctl")) == 1
        assert all(t["function"]["name"] != "lock_screen" for p in qwen.payloads for t in p.get("tools", []))
    finally:
        await daemon.close()


async def test_the_lock_falls_back_to_the_screensaver_and_finds_the_session():
    runner = FakeRunner({"loginctl show-user": (0, "c2\n"), "loginctl lock-session": (1, "")})
    assert await Desktop(run=runner, which=which(), env={}).lock()
    assert runner.calls[1] == ["/usr/bin/loginctl", "lock-session", "c2"]
    assert runner.calls[2][:3] == ["/usr/bin/gdbus", "call", "--session"] and "org.gnome.ScreenSaver.Lock" in runner.calls[2]
    runner = FakeRunner({"loginctl show-user": (0, "$(evil)\n")})
    assert await Desktop(run=runner, which=which(("loginctl",)), env={}).lock()
    assert runner.calls[1] == ["/usr/bin/loginctl", "lock-session"]       # an odd session id is never passed on


def test_a_gesture_may_be_mapped_to_lock_and_to_nothing_else_above_playback():
    assert gestures.check_map({"fist_hold": "lock"}) == {"fist_hold": "lock"}
    for action in ("open", "close", "system_volume", "open_app", "lock_screen"):
        with pytest.raises(gestures.GestureError):
            gestures.check_map({"fist_hold": action})


def test_lock_maps_from_the_config_but_is_not_mapped_by_default(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[gestures.map]\nfist_hold = "lock"\n', encoding="utf-8")
    assert load(path, env={}).gestures.map == {"fist_hold": "lock"}
    path.write_text('[gestures.map]\nfist_hold = "open"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load(path, env={})


async def test_a_mapped_gesture_locks_the_screen_and_reaches_nothing_else_of_the_desktop():
    daemon, _mpris = gesture_daemon()
    daemon.config.gestures.map = {"fist_hold": "lock", "victory_hold": "pause"}
    daemon.actor = Actor(daemon.config.actions, daemon.toolbox, mpris=_mpris)
    runner = FakeRunner()
    daemon.desktop._run = runner
    daemon.desktop.which = which()
    daemon.desktop.env = {"XDG_SESSION_ID": "7"}
    out = await daemon.gestures.done("fist_hold")
    assert out["action"] == "lock" and out["run_id"]
    await until(lambda: runner.calls == [["/usr/bin/loginctl", "lock-session", "7"]])
    # The desktop's other reflexes are never a gesture's, whatever the map says.
    assert await daemon.actor.named("open") is None and await daemon.actor.named("system_volume") is None
    # Raised by [approvals] risk above its tier, even the lock does nothing from a hand.
    daemon.toolbox.risks = {"desktop.lock_screen": "sends"}
    runner.calls.clear()
    found = await daemon.actor.named("lock")
    assert await daemon.actor.act_named("lock", found, tiers=gestures.TIERS + ("change",)) is None
    assert runner.calls == []


# ----------------------------------------------------------------------------- Spotify, opened for a play


def spotify_toolbox(appear_after: int | None = 2, here: str = "deskhost"):
    """A Spotify server with no device until `appear_after` device listings after a launch (None: never)."""
    state = {"launched": False, "listings": 0, "played": []}

    async def handle(name: str, arguments: dict) -> FakeResult:
        if name == "get_devices":
            devices = [{"id": "phone1", "name": "Phone", "type": "Smartphone", "is_active": False}]
            if state["launched"]:
                state["listings"] += 1
                if appear_after is not None and state["listings"] >= appear_after:
                    devices.append({"id": "desk01", "name": here, "type": "Computer", "is_active": False})
            return FakeResult([FakeContent(json.dumps({"devices": devices}))])
        if name in ("play", "play_liked"):
            if "device_id" not in arguments:
                return FakeResult([FakeContent(json.dumps({"error": "No active device", "code": "no_active_device",
                                                           "status": 404}))], is_error=True)
            state["played"].append(arguments)
            return FakeResult([FakeContent(json.dumps({"success": True}))])
        return FakeResult([FakeContent("{}")])

    session = FakeSession([FakeTool("play"), FakeTool("play_liked"), FakeTool("get_devices")], handle)
    toolbox = Toolbox(ToolsConfig(servers={"spotify": {"topic": "music", "command": "spotify"}}, preconnect=False),
                      connect=make_connect({"spotify": session}))
    return toolbox, state


class FakeLaunch(Desktop):
    def __init__(self, state: dict, why: str = "") -> None:
        super().__init__(run=FakeRunner(), which=which())
        self.state, self.why, self.launched = state, why, []

    async def launch(self, entry) -> str:
        self.launched.append(entry.id)
        self.state["launched"] = not self.why
        return self.why


def wake_for(text: str, toolbox, state, why: str = "", host: str = "otherhost"):
    standard()
    write_list(text)
    lines: list[str] = []

    async def told(line: str) -> None:
        lines.append(line)

    async def no_wait(_s: float) -> None:
        return None

    clock = iter(range(0, 1000))
    wake = SpotifyWake(AppStore(apps.apps_file()), FakeLaunch(state, why), say=told, sleep=no_wait,
                       clock=lambda: float(next(clock)), host=host)
    toolbox.recover = wake
    return wake, lines


async def test_a_play_with_no_device_opens_spotify_waits_for_it_and_plays_there():
    toolbox, state = spotify_toolbox(appear_after=3)
    wake, lines = wake_for(LIST, toolbox, state)
    result = await toolbox.call("spotify", "play", {"uri": "spotify:track:abc"})
    assert result.ok and state["played"] == [{"uri": "spotify:track:abc", "device_id": "desk01"}]
    assert wake.desktop.launched == ["spotify_spotify.desktop"] and lines == ["Opening Spotify…"]


async def test_a_device_that_never_shows_is_a_short_honest_failure():
    toolbox, state = spotify_toolbox(appear_after=None)
    wake, lines = wake_for(LIST, toolbox, state)
    result = await toolbox.call("spotify", "play", {})
    assert not result.ok and json.loads(result.text)["code"] == "device_timeout" and state["played"] == []
    assert _failed("resume", result).fact == ("I tried to resume, but Spotify did not show up as a device within 15 "
                                              "seconds of opening it.")


async def test_spotify_is_not_opened_when_the_list_does_not_allow_it():
    """Security review: open = false, no entry, or an app the user merely calls "Spotify" never auto-opens."""
    for text in (LIST.replace('name = "Spotify"\nopen = true', 'name = "Spotify"\nopen = false'),
                 '[apps."firefox_firefox.desktop"]\nname = "Spotify"\nopen = true\n',
                 "# nothing listed\n"):
        toolbox, state = spotify_toolbox()
        wake, lines = wake_for(text, toolbox, state)
        result = await toolbox.call("spotify", "play", {})
        assert not result.ok and "no active device" in result.text.lower()
        assert wake.desktop.launched == [] and lines == []


async def test_an_idle_spotify_on_this_computer_is_played_on_and_not_opened_again():
    toolbox, state = spotify_toolbox()
    state["launched"], state["listings"] = True, 5        # already running here, only idle
    wake, lines = wake_for(LIST, toolbox, state, host="deskhost")
    result = await toolbox.call("spotify", "play_liked", {"shuffle": True})
    assert result.ok and state["played"] == [{"shuffle": True, "device_id": "desk01"}]
    assert wake.desktop.launched == [] and lines == []


async def test_other_failures_and_other_servers_are_left_as_they_were():
    toolbox, state = spotify_toolbox()
    wake, lines = wake_for(LIST, toolbox, state)
    result = await toolbox.call("spotify", "play", {"device_id": "phone1"})   # a device named: no recovery
    assert result.ok
    result = await toolbox.call("spotify", "get_devices", {})
    assert result.ok and wake.desktop.launched == []
