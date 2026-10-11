"""Her living mood (mood.py, WIRING.md §29): the dials, what moves them and by how much, what they move, and above
all what they never move: the daemon's decisions are the same at any mood."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import wave
from pathlib import Path

import pytest

from strawberry_crab import mood as moods
from strawberry_crab import paths, persona
from strawberry_crab.brain import OllamaReactor
from strawberry_crab.config import BrainConfig, Config, MoodConfig, SpeechConfig, ThinkerConfig, default_toml, load
from strawberry_crab.daemon import Daemon
from strawberry_crab.events import CannedReactor, Event
from strawberry_crab.hub import body_from_hello
from strawberry_crab.mood import NUDGES, TOTAL_CAP, Mood, is_thanks, is_wrong, words
from strawberry_crab.persona import Temperament
from strawberry_crab.server import create_app
from strawberry_crab.speech import Speaker
from tests.bus import BUS_SECRET
from tests.fake_spotify import fake_gate
from tests.test_brainui import origin, quiet_config, sign_in
from tests.test_runs import settle, steps
from tests.test_thinker import FakeQwen, make, plain_config
from tests.test_voice import Sink

GOLDEN = json.loads((Path(__file__).parent / "golden" / "persona_before.json").read_text(encoding="utf-8"))
ON = MoodConfig(enabled=True)
CALM = Temperament(energy=0.0, feeling=0.0, recovery=10.0, sulk=30.0)
HELLO_MOOD = {"type": "hello", "client": "test-body", "version": "dev", "protocol": 2, "body": {"id": "orbs"},
              "capabilities": {"phases": [], "mood": True}, "secret": BUS_SECRET}


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def a_mood(temperament: Temperament = CALM, hour: float = 14.0, path: Path | None = None) -> tuple[Mood, Clock]:
    clock = Clock()
    mood = Mood(ON, temperament=lambda: temperament, path=path, clock=clock, wall=lambda: 1.8e9 + clock.t,
                hour=lambda: hour)
    return mood, clock


# ----------------------------------------------------------------------------- the dials


def test_a_nudge_moves_the_dials_and_they_ease_back_exponentially():
    mood, clock = a_mood()
    assert mood.current() == (0.0, 0.0)
    energy, feeling = mood.nudge("voice.thanks")
    assert (energy, feeling) == (NUDGES["voice.thanks"].energy, NUDGES["voice.thanks"].feeling)
    clock.t += CALM.recovery * 60.0     # one time constant: about a third is left
    e, f = mood.current()
    assert e == pytest.approx(0.03 / 2.71828, rel=1e-3) and f == pytest.approx(0.15 / 2.71828, rel=1e-3)
    clock.t += 3600.0 * 3
    energy, feeling = mood.current()
    assert abs(feeling) < 1e-6 and energy == pytest.approx(moods.IDLE_ENERGY, abs=1e-6)   # three hours of nothing


def test_a_sulk_lingers_longer_than_a_good_mood():
    up, up_clock = a_mood()
    down, down_clock = a_mood()
    up.nudge("touch.pat")
    down.nudge("voice.wrong")
    up_clock.t += 600.0
    down_clock.t += 600.0
    left_up = up.current()[1] / NUDGES["touch.pat"].feeling
    left_down = down.current()[1] / NUDGES["voice.wrong"].feeling
    assert left_down > left_up > 0.0     # 30 minutes' sulk against 10 minutes' recovery


def test_each_source_is_capped_per_minute_and_all_of_them_together():
    mood, clock = a_mood()
    for _ in range(20):
        mood.nudge("touch.pat")
    assert mood.current()[1] == pytest.approx(NUDGES["touch.pat"].cap, abs=1e-9)
    assert mood.counts["capped"] > 0
    for source in ("voice.thanks", "tool.ok", "music.drop"):
        for _ in range(20):
            mood.nudge(source)
    assert mood.current()[1] <= TOTAL_CAP + 1e-9
    clock.t += 61.0       # a new minute: room again
    before = mood.feeling
    mood.nudge("touch.pat")
    assert mood.feeling > before


def test_the_dials_clamp():
    mood, clock = a_mood()
    for minute in range(30):
        clock.t += 61.0
        for _ in range(5):
            mood.nudge("music.drop")
            mood.nudge("touch.pat")
    energy, feeling = mood.current()
    assert -1.0 <= energy <= 1.0 and -1.0 <= feeling <= 1.0
    assert energy == pytest.approx(1.0, abs=0.05)


def test_late_at_night_and_idle_she_rests_calmer():
    day, _ = a_mood(hour=14.0)
    night, _ = a_mood(hour=1.5)
    evening, _ = a_mood(hour=22.0)
    assert day.baseline()[0] == 0.0 and night.baseline()[0] == -0.35 and evening.baseline()[0] == -0.15
    idle, clock = a_mood()
    clock.t += moods.IDLE_AFTER_S - 1
    assert idle.baseline()[0] == 0.0
    clock.t += (moods.IDLE_FULL_S - moods.IDLE_AFTER_S) / 2 + 1
    assert idle.baseline()[0] == pytest.approx(moods.IDLE_ENERGY / 2, abs=0.01)
    idle.heard("hello")                  # the user: idle starts over
    assert idle.baseline()[0] == 0.0
    idle.tick()
    clock.t += moods.IDLE_FULL_S
    idle.tick()
    assert any(r["source"] == "baseline.idle" for r in idle.reasons)


def test_reset_puts_her_at_her_baseline():
    mood, _ = a_mood(Temperament(energy=0.3, feeling=0.4, recovery=8.0, sulk=20.0))
    mood.nudge("voice.wrong")
    assert mood.reset() == (0.3, 0.4) and mood.current() == (0.3, 0.4)
    assert mood.reasons[-1]["source"] == "reset"


# ----------------------------------------------------------------------------- the inputs


@pytest.mark.parametrize("text, thanks", [
    ("thanks", True), ("Thank you!", True), ("thanks, Strawberry", True), ("thank you so much", True),
    ("well done", True), ("oh, that's brilliant", True), ("cheers mate", True), ("no thanks", False),
    ("thanks, now play some jazz", False), ("thank you next by ariana grande", False), ("", False)])
def test_a_thanks_is_a_whole_sentence(text, thanks):
    assert is_thanks(text) is thanks


@pytest.mark.parametrize("text, wrong", [
    ("no, wrong", True), ("Wrong!", True), ("that's not what I asked for", True), ("nope, wrong song", True),
    ("not that one", True), ("no", False), ("what's wrong with my code", False), ("stop", False),
    ("play the wrong trousers soundtrack", False)])
def test_a_correction_is_a_whole_sentence(text, wrong):
    assert is_wrong(text) is wrong


def test_her_users_sentence_moves_her_and_is_not_kept(caplog):
    caplog.set_level(logging.DEBUG)
    mood, _ = a_mood()
    assert mood.heard("thank you so much") == ["voice.spoke", "voice.thanks"]
    assert mood.heard("nope, wrong song") == ["voice.spoke", "voice.wrong"]
    assert mood.heard("what's the weather like") == ["voice.spoke"]
    mood.tick()
    blob = json.dumps(mood.view()) + caplog.text
    assert "weather" not in blob and "so much" not in blob and "wrong song" not in blob
    assert not any(isinstance(v, str) and "weather" in v for v in vars(mood).values())


def test_a_pat_pleases_her_and_pokes_in_a_row_annoy_her():
    mood, clock = a_mood()
    assert mood.touched("crab", "pat") == "touch.pat" and mood.current()[1] > 0
    assert [mood.touched("crab", "poke") for _ in range(4)] == ["touch.poke", "touch.poke", "touch.pokes", "touch.pokes"]
    clock.t += moods.STREAK_S + 0.5
    assert mood.touched("crab", "poke") == "touch.poke"           # a new streak
    feeling = mood.current()[1]
    assert mood.touched("music", "flick") == "" and mood.touched("orb", "poke") == ""   # not her
    assert mood.current()[1] == pytest.approx(feeling)


def test_the_music_moves_her_energy_by_its_numbers_and_sections():
    mood, clock = a_mood()
    fast = {"bpm": 140.0, "period_s": 0.43, "confidence": 0.8, "next_beat": 1.0, "steady": True}
    assert mood.tempo(fast) == ["music.beat"] and mood.current()[0] > 0
    slow, _ = a_mood()
    slow.tempo({"bpm": 70.0, "period_s": 0.86, "confidence": 0.8, "next_beat": 1.0, "steady": True})
    assert slow.current()[0] < 0
    assert mood.tempo(fast | {"section": "drop"}) == ["music.drop", "music.beat"]
    assert mood.tempo(fast | {"section": "drop"}) == ["music.beat"]           # once per change
    assert mood.tempo(fast | {"section": "break"})[0] == "music.break"
    assert mood.tempo({"silent": True}) == ["music.stopped"] and mood.tempo({"silent": True}) == []
    unsure, _ = a_mood()
    assert unsure.tempo(fast | {"steady": False}) == [] and unsure.current() == (0.0, 0.0)


def test_tool_outcomes_and_a_stop_come_from_the_run_events():
    mood, _ = a_mood()
    assert mood.run_event({"type": "tool.completed", "ok": True}) == "tool.ok"
    assert mood.run_event({"type": "tool.completed", "ok": False, "error": "failed"}) == "tool.failed"
    assert mood.run_event({"type": "tool.completed", "ok": False, "error": "refused"}) == ""
    assert mood.run_event({"type": "run.cancelled", "reason": "stopped"}) == "voice.stop"
    assert mood.run_event({"type": "run.cancelled", "reason": "superseded"}) == ""


def test_with_the_mood_off_nothing_moves_and_nothing_shows():
    mood = Mood(MoodConfig(enabled=False), temperament=lambda: CALM)
    assert mood.heard("thanks") == [] and mood.touched("crab", "pat") == "" and mood.tempo({"silent": True}) == []
    assert mood.nudge("music.drop") == (0.0, 0.0) and mood.line() == "" and mood.pace() == 1.0
    assert mood.due() is None


# ----------------------------------------------------------------------------- the outputs


def test_her_line_is_words_never_numbers():
    for energy in (-1.0, -0.4, 0.0, 0.4, 1.0):
        for feeling in (-1.0, -0.4, 0.0, 0.4, 1.0):
            line = words(energy, feeling)
            assert line.startswith("You feel ") and line.endswith(" right now.") and not re.search(r"\d", line)
    assert words(0.4, 0.4) == "You feel a bit lively and pleased right now."
    assert words(-0.9, -0.9) == "You feel sleepy and grumpy right now."
    assert words(0.0, 0.0) == "You feel even and settled right now."


def test_the_line_follows_the_reaction_models_prompt_and_nothing_else_changes():
    reactor = OllamaReactor(BrainConfig(), fallback=CannedReactor())
    assert reactor.system() == GOLDEN["reaction_system"]          # no mood: byte for byte as before
    reactor.mood = lambda: "You feel a bit lively and pleased right now."
    assert reactor.system() == GOLDEN["reaction_system"] + " You feel a bit lively and pleased right now."
    messages = reactor._messages("source: git")
    assert messages[0]["content"].endswith("pleased right now.") and messages[-1] == {"role": "user",
                                                                                      "content": "source: git"}
    # A draft persona (the Brain UI's Try it) is tried without it.
    draft = reactor._messages("source: git", draft=persona.shipped())
    assert draft[0]["content"] == GOLDEN["reaction_system"]
    reactor.mood = lambda: ""
    assert reactor.system() == GOLDEN["reaction_system"]


def test_the_pace_is_bounded_and_reaches_piper():
    mood, _ = a_mood()
    mood.energy = 1.0
    assert mood.pace() == pytest.approx(1.0 - moods.PACE_MAX)
    mood.energy = -1.0
    assert mood.pace() == pytest.approx(1.0 + moods.PACE_MAX)
    paced: list[float] = []

    def synth(text, wav, pace=1.0):
        paced.append(pace)
        wav.setnchannels(1), wav.setsampwidth(2), wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 160)

    synth.paced = True
    plain: list[str] = []

    def unpaced(text, wav):
        plain.append(text)
        wav.setnchannels(1), wav.setsampwidth(2), wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 160)

    for fn, pace in ((synth, lambda: 0.5), (synth, lambda: 1.05), (unpaced, lambda: 0.95)):
        speaker = Speaker(SpeechConfig(enabled=True), synth_factory=lambda *a: fn)
        speaker.synth, speaker.pace = fn, pace
        path = Path(paths.state_dir()) / "line.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        speaker._write("hello", path)
    assert paced == [0.9, 1.05] and plain == ["hello"]       # clamped to speech.PACE_BOUNDS; a fake without it


def test_she_carries_her_mood_over_a_restart_eased_by_the_time_since(tmp_path):
    path = tmp_path / "mood.json"
    mood, clock = a_mood(path=path)
    mood.nudge("voice.wrong")
    mood.tick()
    mood.save()
    saved = json.loads(path.read_text())
    assert set(saved) == {"version", "energy", "feeling", "at", "history", "reasons"}
    assert all(set(r) == {"t", "source", "energy", "feeling"} for r in saved["reasons"])
    again, clock2 = a_mood(path=path)
    clock2.t = clock.t            # the same wall time: nothing eased away
    assert again.load() and again.feeling == pytest.approx(mood.feeling, abs=1e-3)
    later, clock3 = a_mood(path=path)
    clock3.t = clock.t + 3 * 3600.0   # three hours on: back near her temperament
    assert later.load() and abs(later.feeling) < 0.01
    path.write_text('{"energy": "lots", "feeling": 2}')
    broken, _ = a_mood(path=path)
    assert not broken.load() and broken.current() == (0.0, 0.0)


# ----------------------------------------------------------------------------- the bus


def test_only_a_trusted_v2_body_that_asks_gets_it():
    assert body_from_hello(HELLO_MOOD, BUS_SECRET).protocol == 2
    from strawberry_crab.hub import WidgetHub

    hub = WidgetHub()
    asked, untrusted, quiet, v1 = Sink(), Sink(), Sink(), Sink()
    for sink, hello in ((asked, HELLO_MOOD), (untrusted, {k: v for k, v in HELLO_MOOD.items() if k != "secret"}),
                        (quiet, HELLO_MOOD | {"capabilities": {"phases": []}}),
                        (v1, {"type": "hello", "client": "x", "version": "dev", "secret": BUS_SECRET,
                              "capabilities": {"mood": True}})):
        hub.add(sink)
        hub.hello(sink, hello, BUS_SECRET)
    sent = asyncio.run(hub.send_mood({"type": "mood", "energy": 0.4, "feeling": -0.2}))
    assert sent == 1 and asked.got == [{"type": "mood", "energy": 0.4, "feeling": -0.2}]
    assert untrusted.got == quiet.got == v1.got == []


async def test_a_body_gets_her_mood_on_hello_and_when_it_changes(aiohttp_client):
    daemon = Daemon(reactor=CannedReactor(), config=quiet_config())
    client = await aiohttp_client(create_app(daemon))
    ws = await client.ws_connect("/ws")
    await ws.send_json(HELLO_MOOD)
    welcome = await ws.receive_json(timeout=5)
    assert welcome["type"] == "welcome" and welcome["accepted"]["mood"] is True
    first = await ws.receive_json(timeout=5)
    assert first["type"] == "mood" and set(first) == {"type", "energy", "feeling"}
    for _ in range(3):
        daemon.mood.touched("crab", "pat")
    got = await ws.receive_json(timeout=5)
    assert got["type"] == "mood" and got["feeling"] > first["feeling"]
    health = await (await client.get("/health", headers={"X-Strawberry-Secret": BUS_SECRET})).json()
    assert health["mood"]["enabled"] and health["mood"]["nudges"] == 3
    assert any(b.get("mood") is True for b in health["bodies"])
    await ws.close()


async def test_with_the_mood_off_no_body_gets_it(aiohttp_client):
    config = quiet_config()
    config.mood.enabled = False
    daemon = Daemon(reactor=CannedReactor(), config=config)
    client = await aiohttp_client(create_app(daemon))
    ws = await client.ws_connect("/ws")
    await ws.send_json(HELLO_MOOD)
    welcome = await ws.receive_json(timeout=5)
    assert welcome["accepted"]["mood"] is False
    await ws.send_json({"type": "ping"})
    assert await ws.receive_json(timeout=5) == {"type": "pong"}
    await ws.close()


# ----------------------------------------------------------------------------- foreign text


async def test_a_notification_or_a_track_never_moves_her_by_its_words():
    daemon = Daemon(reactor=CannedReactor(), config=quiet_config())
    before = (daemon.mood.energy, daemon.mood.feeling)
    for event in (Event(source="notification", app="Chat", title="thanks, well done", body="thank you so much"),
                  Event(source="notification", app="Chat", title="no, wrong", body="wrong"),
                  Event(source="media", app="Spotify", title="Thank You", body="Well Done"),
                  Event(source="git", app="post-commit", title="repo", body="thanks")):
        await daemon.handle_event(event)
    assert not daemon.mood.reasons and daemon.mood.counts["nudges"] == 0
    assert (daemon.mood.energy, daemon.mood.feeling) == pytest.approx(before, abs=1e-3)
    await daemon.handle_event(Event(source="voice", title="thanks, well done", spoken=True))
    assert [r["source"] for r in daemon.mood.reasons] == ["voice.spoke", "voice.thanks"]
    await daemon.close()


# ----------------------------------------------------------------------------- the temperament


def test_the_temperament_is_parsed_checked_and_the_users_file_changes_it(tmp_path):
    shipped = persona.shipped()
    assert shipped.temperament == Temperament(energy=0.1, feeling=0.25, recovery=8.0, sulk=20.0)
    text = shipped.text
    without = text[:text.index("## Temperament")]
    assert persona.check(without)[0].temperament is None          # optional: the shipped values stand in
    for old, new, problem in (("- energy: 0.1", "- energy: 2", "energy must be from -0.8 to 0.8"),
                              ("- sulk: 20 minutes", "- sulk: 3", "sulk (3 minutes) must be at least recovery"),
                              ("- energy: 0.1", "- spirit: 1", "is not one of"),
                              ("- recovery: 8 minutes", "- recovery: soon", "recovery must be a number"),
                              ("- feeling: 0.25", "- feeling: nan", "feeling must be from")):
        problems = persona.check(text.replace(old, new))[1]
        assert any(problem in p for p in problems), problems
    mine = persona.check(text.replace("- feeling: 0.25", "- feeling: -0.5").replace("- recovery: 8 minutes",
                                                                                   "- recovery: 4"))[0]
    assert mine.temperament == Temperament(energy=0.1, feeling=-0.5, recovery=4.0, sulk=20.0)
    store = persona.PersonaStore(tmp_path / "persona.md")
    assert store.temperament() == shipped.temperament
    (tmp_path / "persona.md").write_text(text.replace("- energy: 0.1", "- energy: -0.6"), encoding="utf-8")
    assert store.temperament().energy == -0.6
    (tmp_path / "persona.md").write_text(text.replace("- energy: 0.1", "- energy: 9"), encoding="utf-8")
    assert store.temperament() == shipped.temperament           # a bad edit: the shipped persona, all of it


def test_the_temperament_changes_no_golden():
    p = persona.shipped()
    assert p.reaction_system() == GOLDEN["reaction_system"]
    assert set(p.sizes()) == set(persona.SECTIONS)


def test_the_config_switch_and_the_template(tmp_path):
    assert "[mood]" in default_toml() and "enabled = true" in default_toml().split("[mood]")[1]
    path = tmp_path / "config.toml"
    path.write_text("[mood]\nenabled = false\n", encoding="utf-8")
    assert load(path).mood.enabled is False and Config().mood.enabled is True


# ----------------------------------------------------------------------------- the Brain UI


async def test_the_mood_view_reads_and_resets_behind_the_session(aiohttp_client):
    daemon = Daemon(reactor=CannedReactor(), config=quiet_config())
    client = await aiohttp_client(create_app(daemon))
    signed = await sign_in(client)
    daemon.mood.heard("nope, not that one")
    view = await (await signed.get("/ui/api/mood")).json()
    assert view["enabled"] and [r["source"] for r in view["reasons"]][:2] == ["voice.wrong", "voice.spoke"]
    assert set(view["nudges"]) == set(NUDGES) and view["history"] and "wrong" not in json.dumps(view["history"])
    assert "nope" not in json.dumps(view)
    assert (await client.get("/ui/api/mood")).status == 401
    assert (await signed.get("/ui/api/mood", csrf=False)).status == 403
    assert (await signed.post("/ui/api/mood/reset", origin="http://evil.example")).status == 403
    assert (await signed.post("/ui/api/mood/reset", csrf=False)).status == 403
    reset = await (await signed.post("/ui/api/mood/reset")).json()
    assert reset["reasons"][0]["source"] == "reset"
    assert (reset["energy"], reset["feeling"]) == (reset["baseline"]["energy"], reset["baseline"]["feeling"])
    daemon.mood.enabled = False
    assert (await signed.post("/ui/api/mood/reset")).status == 409


# ----------------------------------------------------------------------------- the rule: expression only


class LineModel(CannedReactor):
    """The reaction model's stand-in: its line is canned, and it notes the mood sentence it was given."""

    def __init__(self) -> None:
        self.mood = None
        self.seen: list[str] = []

    async def react(self, event: Event, context: str = ""):
        self.seen.append(self.mood() if self.mood else "")
        return await super().react(event, context)


def pin(mood: Mood, energy: float | None, feeling: float | None) -> None:
    """The dials held at one extreme through everything: the real nudges still run, and are undone."""
    if energy is None:
        return
    nudge = mood.nudge

    def held(source: str, scale: float = 1.0):
        moved = nudge(source, scale)
        mood.energy, mood.feeling = energy, feeling
        return moved

    mood.energy, mood.feeling = energy, feeling
    mood.advance = lambda now=None: None
    mood.nudge = held


SENTENCES = ("skip this track", "play some jazz", "thanks, well done", "no, wrong", "what's the weather like",
             "never mind")
SCRIPT = [[("play", {"query": "jazz"})], "[happy] Jazz on.", "[happy] Any time.", "[neutral] Sorry about that.",
          "[neutral] I can't see outside.", "[neutral] Spare line."]


async def decisions(aiohttp_client, monkeypatch, energy, feeling) -> dict:
    monkeypatch.setattr(Daemon, "today", staticmethod(lambda: "Today is a fixed day."))
    config = plain_config()
    config.mood.enabled = energy is not None
    config.thinker = ThinkerConfig(ack_after_s=30.0, still_on_it_s=60.0)
    spotify, toolbox, qwen, thinker = make(list(SCRIPT), config=config.thinker)
    reactor = LineModel()
    daemon = Daemon(reactor=reactor, config=config, gate=fake_gate(toolbox), toolbox=toolbox, thinker=thinker)
    sink = Sink()
    daemon.hub.add(sink)
    daemon.hub.hello(sink, {"type": "hello", "client": "t", "version": "dev", "protocol": 2, "body": {"id": "t"},
                            "capabilities": {"phases": ["routing", "thinking", "tool", "speaking", "run"],
                                             "approvals": True, "sends": {"cancel": True}},
                            "secret": BUS_SECRET}, BUS_SECRET)
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    pin(daemon.mood, energy, feeling)
    for text in SENTENCES:
        await client.post("/event", json={"source": "voice", "title": text})
        await settle()
    await client.post("/event", json={"source": "notification", "app": "Chat", "title": "Alex", "body": "lunch?"})
    await client.post("/event", json={"source": "git", "app": "post-commit", "title": "repo", "body": "fix"})
    await settle()
    drop = {"t", "duration", "run_id", "ms", "expires_t", "seq"}
    out = {
        "events": [{k: v for k, v in m.items() if k not in drop} for m in steps(sink)],
        "performances": [{k: m.get(k) for k in ("state", "text", "emotion", "anim", "reaction", "hop", "source")}
                         for m in sink.got if "state" in m],
        "thinker": [re.sub(r"\d+", "#", json.dumps(p, sort_keys=True)) for p in qwen.payloads],
        "spotify": list(spotify.log),
        "ledger": [(t.said, t.reply, t.did, t.foreign) for t in daemon.ledger.turns],
        "approvals": daemon.approvals.stats(),
    }
    await daemon.close()
    return {"decisions": out, "seen": reactor.seen}


async def test_the_same_decisions_at_any_mood(aiohttp_client, monkeypatch):
    """The rule: the mood is expression only. The same sentences, a notification and a commit, through the gate,
    a reflex, the thinker (its prompts and tools), the approvals and the ledger, give the same decisions with the
    mood off and pinned at each corner of the dials; only the reaction model's mood sentence differs."""
    base = await decisions(aiohttp_client, monkeypatch, None, 0.0)     # the mood off
    assert base["decisions"]["spotify"] and base["decisions"]["thinker"] and set(base["seen"]) == {""}
    assert any(e.get("path") == "reflex" for e in base["decisions"]["events"])
    assert any(e.get("path") == "escalate" for e in base["decisions"]["events"])
    for energy, feeling in ((1.0, 1.0), (-1.0, -1.0), (1.0, -1.0), (-1.0, 1.0)):
        got = await decisions(aiohttp_client, monkeypatch, energy, feeling)
        assert got["decisions"] == base["decisions"], (energy, feeling)
        assert set(got["seen"]) == {words(energy, feeling)}
        assert all("right now." not in p for p in got["decisions"]["thinker"])   # never in the thinker's prompt


def test_the_mood_view_is_text_only():
    script = (Path(__file__).parents[1] / "src" / "strawberry_crab" / "ui" / "app.js").read_text(encoding="utf-8")
    block = script[script.index("her: mood (moodui.py"):script.index("// her: mood end")]
    assert "innerHTML" not in block and "insertAdjacentHTML" not in block and "/ui/api/mood" not in block
    assert 'api("mood")' in block and 'api("mood/reset", {})' in block


