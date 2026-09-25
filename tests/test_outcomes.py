"""The learning loop's outcome logger (WIRING.md §8c): records, signals, guards, the file, the CLI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time

import pytest

from strawberry_crab import cli, firstrun, paths
from strawberry_crab.actions import Actor, Outcome
from strawberry_crab.config import Config, ConfigError, LearningConfig, _validate, default_toml, load
from strawberry_crab.events import Event
from strawberry_crab.outcomes import OutcomeLog, cosine, is_correction, read
from strawberry_crab.server import create_app
from strawberry_crab.systemone import Answer, Gate, Route
from strawberry_crab.tools import ToolResult
from tests.test_systemone import FakeEmbedder
from tests.test_thinker import ScriptedGate, bare, make, plain_config, voice_daemon

CANARY = "quokka-canary-7f3a"


def route(text: str, kind: str = "request", topic: str = "music", tool: str = "", tool_confidence: float = 0.9,
          vector: tuple[float, ...] = (1.0, 0.0, 0.0), sensitive: float | None = 0.02, decision: str = "act",
          has_argument: float = 0.05) -> Route:
    """A gate reading with answers, as the real gate gives them (is_sensitive included)."""
    answers = {"kind": Answer("kind", "choice", {kind: 0.9, "chat": 0.1}, 0.8, choice=kind)}
    if sensitive is not None:
        answers["is_sensitive"] = Answer("is_sensitive", "noul", {"yes": sensitive, "no": 1 - sensitive}, 0.9,
                                         score=sensitive)
    norm = sum(x * x for x in vector) ** 0.5
    return Route(text=text, kind=kind, topic=topic, confidence=0.8, is_urgent=0.1, is_about_her=0.1,
                 decision=decision, tool=tool, tool_confidence=tool_confidence, has_argument=has_argument,
                 answers=answers, vector=tuple(x / norm for x in vector))


def on(**kw) -> LearningConfig:
    return LearningConfig(log_outcomes=True, **kw)


def records() -> list[dict]:
    path = paths.outcomes_file()
    return read(path) if path.exists() else []


def reflex(log: OutcomeLog, text: str, tool: str, **kw) -> None:
    record = log.heard(text, route(text, tool=tool, **kw), "voice")
    log.acted(record, "reflex", True, reflex=f"mpris.{tool}")


# ----------------------------------------------------------------------------- config


def test_off_by_default_and_in_the_template_and_config(tmp_path):
    config = Config()
    assert config.learning.log_outcomes is False
    assert config.to_dict()["learning"]["log_outcomes"] is False          # GET /config
    assert "[learning]" in default_toml() and "log_outcomes = false" in default_toml()
    path = tmp_path / "config.toml"
    path.write_text(default_toml() + "", encoding="utf-8")
    assert load(path).learning == LearningConfig()
    path.write_text("[learning]\nlog_outcomes = true\nundo_s = 5\n", encoding="utf-8")
    loaded = load(path).learning
    assert loaded.log_outcomes is True and loaded.undo_s == 5.0


@pytest.mark.parametrize("bad", [dict(max_days=0), dict(max_records=0), dict(undo_s=0), dict(rephrase_s=-1),
                                 dict(silence_s=5.0), dict(rephrase_similarity=0.0), dict(rephrase_similarity=1.5)])
def test_learning_settings_are_validated(bad):
    config = Config()
    config.learning = LearningConfig(**bad)
    with pytest.raises(ConfigError):
        _validate(config)


# ----------------------------------------------------------------------------- the signals


def test_correction_phrases_are_conservative():
    for text in ("no, I meant the next song", "No. The other one", "nope, not that", "not that one please",
                 "I meant the previous track", "that's not what I asked", "wrong one", "ei kun seuraava",
                 "tarkoitin edellistä", "no I said pause"):
        assert is_correction(text), text
    for text in ("no more music", "no worries", "play nothing else matters", "not now", "know what, skip it",
                 "turn it up", "what is this"):
        assert not is_correction(text), text


def test_cosine():
    assert cosine((1.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)
    assert cosine((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)
    assert cosine((), (1.0,)) is None


async def test_off_keeps_nothing():
    log = OutcomeLog(LearningConfig())
    log.start()
    assert log.heard("skip this", route("skip this", tool="skip"), "voice") is None
    log.close()
    assert not paths.outcomes_file().exists()
    assert log.stats() == {"enabled": False, "records": 0}


async def test_the_opposite_reflex_soon_after_is_an_undo():
    log = OutcomeLog(on())
    reflex(log, "skip this song", "skip")
    reflex(log, "go back", "previous", vector=(0.0, 1.0, 0.0))
    log.close()
    first, second = records()
    assert first["text"] == "skip this song" and first["outcome"] == "undo" and first["by"] == second["id"]
    assert first["reflex"] == "mpris.skip" and first["path"] == "reflex" and first["ok"] is True
    assert first["after_s"] is not None and first["after_s"] < 5
    assert second["follows"] == {"id": first["id"], "as": "undo"} and second["outcome"] == "none"
    for a, b in (("pause", "resume"), ("volume_up", "volume_down")):
        reflex(log, a, a)
        reflex(log, b, b, vector=(0.0, 1.0, 0.0))
    log.close()
    assert [r["outcome"] for r in records()[2:]] == ["undo", "moved_on", "undo", "none"]


async def test_the_same_reflex_again_is_a_repeat_not_a_rephrase():
    log = OutcomeLog(on())
    reflex(log, "skip", "skip")
    reflex(log, "skip", "skip")
    log.close()
    assert [r["outcome"] for r in records()] == ["repeat", "none"]


async def test_a_correction_and_a_rephrase():
    log = OutcomeLog(on())
    record = log.heard("play the next one", route("play the next one", tool="other", vector=(1.0, 0.0, 0.0)), "voice")
    log.acted(record, "thinker", True)
    log.heard("no, I meant the next song", route("no, I meant the next song", vector=(0.0, 1.0, 0.0)), "voice")
    log.heard("what's the weather", route("what's the weather", kind="question", topic="other",
                                           vector=(0.0, 0.0, 1.0)), "typed")
    log.heard("is it going to rain", route("is it going to rain", kind="question", topic="other",
                                            vector=(0.1, 0.0, 1.0)), "typed")
    log.close()
    got = records()
    assert [r["outcome"] for r in got] == ["correction", "moved_on", "rephrase", "none"]
    assert got[1]["follows"]["as"] == "correction" and got[3]["follows"]["as"] == "rephrase"
    assert got[2]["source"] == "typed"


async def test_outside_the_windows_nothing_is_related(monkeypatch):
    log = OutcomeLog(on(undo_s=1.0, rephrase_s=1.0, silence_s=100.0))
    reflex(log, "skip this song", "skip")
    later = time.monotonic() + 5.0
    monkeypatch.setattr(time, "monotonic", lambda: later)
    reflex(log, "go back", "previous")
    log.close()
    assert [r["outcome"] for r in records()] == ["moved_on", "none"]


async def test_silence_after_the_window():
    log = OutcomeLog(on(undo_s=0.01, rephrase_s=0.01, silence_s=0.05))
    reflex(log, "pause", "pause")
    await asyncio.sleep(0.15)
    (only,) = records()
    assert only["outcome"] == "silence" and only["by"] is None and only["after_s"] >= 0.05
    assert log.pending is None and log.stats()["signals"] == {"silence": 1}


async def test_another_voice_is_ignored_and_does_not_break_the_silence():
    log = OutcomeLog(on())
    reflex(log, "skip this", "skip")
    assert log.heard(f"breaking news {CANARY}", route("tv", kind="other", topic="other"), "voice") is None
    assert log.pending is not None and log.pending.text == "skip this"
    assert log.stats()["skipped"]["other"] == 1
    log.close()
    assert all(CANARY not in json.dumps(r) for r in records())


@pytest.mark.parametrize("text,sensitive", [
    ("my code is 482913", 0.01),        # the patterns
    ("read me my bank balance", 0.93),  # IS_SENSITIVE on the gate's vector
    ("anything at all", None),          # no answer: fail closed
])
async def test_private_sentences_are_never_kept(text, sensitive):
    log = OutcomeLog(on())
    reflex(log, "skip this", "skip")
    assert log.heard(text, route(text, sensitive=sensitive, vector=(0.0, 1.0, 0.0)), "voice") is None
    log.close()
    got = records()
    assert [r["text"] for r in got] == ["skip this"]        # the one before is settled, the private one is not kept
    assert got[0]["outcome"] == "moved_on" and log.stats()["skipped"]["private"] == 1


async def test_an_unrouted_sentence_is_not_kept():
    log = OutcomeLog(on())
    assert log.heard("anything", None, "voice") is None
    assert log.stats()["skipped"]["unrouted"] == 1


def call(name: str, arguments: dict | None = None, ok: bool = True) -> ToolResult:
    return ToolResult("spotify", name, ok, f"result text {CANARY}", 12.0, arguments=arguments or {})


async def test_system_two_as_teacher_and_calls_without_results():
    log = OutcomeLog(on())
    cases = [
        ([call("pause")], "pause"),
        ([call("next")], "skip"),
        ([call("set_volume", {"volume": 30})], None),          # an argument: not a reflex
        ([call("search", {"q": CANARY}), call("play", {"uri": "x"})], None),   # two calls
        ([call("pause", ok=False)], None),                     # it failed
        ([], None),
    ]
    for i, (calls, _) in enumerate(cases):
        record = log.heard(f"sentence {i}", route(f"sentence {i}", tool="other", vector=(1.0, float(i), 0.0)), "voice")
        log.acted(record, "thinker", True, calls=calls)
        log.close()
    got = records()
    assert [r["teacher"] for r in got] == [want for _, want in cases]
    assert got[0]["calls"] == [{"server": "spotify", "name": "pause", "arguments": False, "ok": True}]
    assert got[3]["calls"][0] == {"server": "spotify", "name": "search", "arguments": True, "ok": True}
    assert CANARY not in paths.outcomes_file().read_text()     # neither the results nor the arguments


async def test_the_record_carries_the_whole_route():
    log = OutcomeLog(on())
    reflex(log, "skip this", "skip")
    log.close()
    (only,) = records()
    assert only["v"] == 1 and only["source"] == "voice" and isinstance(only["ts"], float) and only["at"]
    assert only["route"]["kind"] == "request" and only["route"]["tool"] == "skip"
    assert only["route"]["answers"]["kind"]["choice"] == "request"
    assert "is_sensitive" not in only["route"]["answers"] and "vector" not in only["route"]


# ----------------------------------------------------------------------------- the file


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
async def test_the_file_is_the_users_alone():
    log = OutcomeLog(on())
    reflex(log, "skip", "skip")
    log.close()
    assert (paths.outcomes_file().stat().st_mode & 0o777) == 0o600
    os.chmod(paths.outcomes_file(), 0o644)
    OutcomeLog(on()).start()                                    # the prune at start puts it right
    assert (paths.outcomes_file().stat().st_mode & 0o777) == 0o600


async def test_retention_by_count_and_by_age():
    path = paths.outcomes_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    old = time.time() - 40 * 86400
    lines = [{"v": 1, "id": f"old{i}", "ts": old, "text": "x"} for i in range(3)]
    lines += [{"v": 1, "id": f"new{i}", "ts": time.time(), "text": "y"} for i in range(6)]
    path.write_text("".join(json.dumps(r) + "\n" for r in lines) + "not json\n")
    log = OutcomeLog(on(max_days=30, max_records=4))
    log.start()
    assert [r["id"] for r in records()] == ["new2", "new3", "new4", "new5"]
    assert log.count() == 4
    log.PRUNE_EVERY = 2
    reflex(log, "a", "skip")
    reflex(log, "b", "skip", vector=(0.0, 1.0, 0.0))
    log.close()
    assert len(records()) <= 4 and records()[-1]["text"] == "b"


# ----------------------------------------------------------------------------- through the daemon


class FakeMpris:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    def reflexes(self):
        def press(tool: str):
            async def reflex(_toolbox, _server):
                self.pressed.append(tool)
                return Outcome(f"pressed {tool}", f"Done: {tool}.", True)
            return reflex

        return {tool: press(tool) for tool in ("skip", "previous", "pause", "resume")}

    async def situation(self) -> str:
        return ""


async def test_the_daemon_records_reflexes_and_the_thinker(aiohttp_client):
    config = plain_config()
    config.learning = on()
    spotify, toolbox, qwen, thinker = make([[("pause", {})], "[neutral] Paused."])
    gate = ScriptedGate({
        "skip this song": route("skip this song", tool="skip"),
        "go back": route("go back", tool="previous", vector=(0.0, 1.0, 0.0)),
        "hold everything": route("hold everything", tool="other", has_argument=0.8, vector=(0.0, 0.0, 1.0)),
    })
    actor = Actor(config.actions, toolbox, reflexes={}, mpris=FakeMpris())
    daemon, sink = voice_daemon(config, toolbox, thinker, gate=gate, actor=actor)  # type: ignore[arg-type]
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await client.post("/event", json={"source": "voice", "title": "skip this song"})
    await daemon.handle_event(Event(source="voice", title="go back", spoken=True))
    await client.post("/event", json={"source": "voice", "title": "hold everything"})
    health = await (await client.get("/health")).json()
    assert health["learning"]["enabled"] is True and health["learning"]["records"] == 2
    assert health["learning"]["waiting"] is True
    await daemon.close()
    got = records()
    assert [(r["source"], r["path"], r["reflex"], r["outcome"]) for r in got] == [
        ("typed", "reflex", "mpris.skip", "undo"),
        ("voice", "reflex", "mpris.previous", "moved_on"),
        ("typed", "thinker", None, "none"),
    ]
    assert got[2]["teacher"] == "pause" and got[2]["calls"][0]["name"] == "pause"
    await toolbox.close()


async def test_health_says_off_with_no_file(aiohttp_client):
    config = plain_config()
    toolbox, qwen, thinker = bare(["[neutral] Hello."])
    daemon, _ = voice_daemon(config, toolbox, thinker, gate=ScriptedGate({"hi": route("hi", kind="chat")}))
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await client.post("/event", json={"source": "voice", "title": "hi"})
    health = await (await client.get("/health")).json()
    assert health["learning"] == {"enabled": False, "records": 0}
    await daemon.close()
    assert not paths.outcomes_file().exists()


async def test_no_sentence_text_in_the_outcome_logs(aiohttp_client, caplog):
    """The canary, like the notification body's in test_privacy.py: a sentence goes through the real
    gate (fake embedder), the reflex tier and the thinker with every logger at DEBUG; it is in the
    outcome file and in no record the outcome logger wrote, whatever the signal."""
    config = plain_config()
    config.learning = on(undo_s=0.25, rephrase_s=0.25, silence_s=0.3)
    config.gate.query_prefix = config.gate.document_prefix = ""
    config.actions.mpris = False
    toolbox, qwen, thinker = bare([f"[neutral] Fine, {CANARY}."] * 4)
    gate = Gate(config.gate, embedder=FakeEmbedder())
    daemon, _ = voice_daemon(config, toolbox, thinker, gate=gate)
    client = await aiohttp_client(create_app(daemon))
    with caplog.at_level(logging.DEBUG):
        await daemon.start()
        for text in (f"play some {CANARY} music", f"no, I meant the next song {CANARY}"):
            await client.post("/event", json={"source": "voice", "title": text})
        await asyncio.sleep(0.5)                # the second one settles on silence
        await client.post("/event", json={"source": "voice", "title": f"how are you today {CANARY}"})
        await daemon.close()
    kept = records()
    assert len(kept) == 3 and all(CANARY in r["text"] for r in kept)
    assert [r["outcome"] for r in kept] == ["correction", "silence", "none"]
    mine = [r for r in caplog.records if r.name.startswith("strawberryd.outcomes")]
    assert mine
    for record in mine:
        assert CANARY not in record.getMessage(), record.getMessage()
    assert CANARY not in json.dumps(daemon.outcomes.stats())


# ----------------------------------------------------------------------------- the CLI and the notes


def test_cli_counts_shows_and_clears(capsys):
    log = OutcomeLog(on())
    reflex(log, "skip this song", "skip")
    reflex(log, "go back", "previous", vector=(0.0, 1.0, 0.0))
    log.close()
    assert cli.main(["outcomes", "--last", "1"]) == 0
    out = capsys.readouterr().out
    assert "2 record(s); logging is off" in out
    assert "signals  none 1, undo 1" in out and "paths    reflex 2" in out
    assert "last 1:" in out and '"go back"' in out and '"skip this song"' not in out
    assert cli.main(["outcomes", "--clear"]) == 0
    assert "deleted" in capsys.readouterr().out and not paths.outcomes_file().exists()
    assert cli.main(["outcomes"]) == 0
    assert "0 record(s)" in capsys.readouterr().out


def test_the_privacy_note_says_what_is_kept():
    config = Config()
    assert "not stored" in firstrun.log_text(config)
    config.learning = on()
    line = firstrun.log_text(config)
    assert str(paths.outcomes_file()) in line and "strawberry outcomes --clear" in line
