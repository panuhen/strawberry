"""The router's learning loop, second half (WIRING.md §8d): labels from synthetic outcome records, the
example store, the candidate, the held-out gate, the switch, the idle trainer and the CLI. No real
user data: every record here is written by the test."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import sys
import time
from dataclasses import replace
from datetime import time as dtime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from strawberry_crab import cli, gatecmd, gatehead, labels, learnfit, learning, paths
from strawberry_crab.config import Config, ConfigError, GateConfig, LearningConfig, _validate, default_toml, load
from strawberry_crab.gatehead import Head, QuestionHead
from strawberry_crab.labels import ExampleStore, extract, resolve
from strawberry_crab.learning import IdleTrainer, Learning, LearningBusy, LearningError
from strawberry_crab.systemone import Gate
from tests.test_gatehead import QUESTIONS, fake_samples, small_sets
from tests.test_systemone import FakeEmbedder

CANARY = "wombat-canary-91c2"


# ----------------------------------------------------------------------------- synthetic records


def rec(text: str, id: str, outcome: str, *, path: str = "reflex", tool: str = "skip", reflex: str | None = None,
        ok: bool = True, decision: str = "act", topic: str = "music", kind: str = "request", conf: float = 0.8,
        tool_conf: float = 0.8, teacher: str | None = None, by: str | None = None, follows: dict | None = None,
        ts: float | None = None, catalogue: float = 0.1) -> dict:
    """One outcome record as outcomes.py writes it (the fields the extractor reads)."""
    if reflex is None and path == "reflex":
        reflex = f"mpris.{tool}"
    return {"v": 1, "id": id, "ts": time.time() if ts is None else ts, "source": "voice", "text": text,
            "route": {"kind": kind, "topic": topic, "confidence": conf, "decision": decision, "tool": tool,
                      "tool_confidence": tool_conf, "has_argument": 0.1, "library_change": 0.1, "catalogue": catalogue,
                      "is_urgent": 0.1, "is_about_her": 0.1, "answers": {}},
            "path": path, "reflex": reflex, "ok": ok, "calls": [], "teacher": teacher, "follows": follows,
            "outcome": outcome, "after_s": 1.0, "by": by}


def write_outcomes(records: list[dict]) -> None:
    path = paths.outcomes_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def signals(evidence, record: str | None = "a") -> list[tuple]:
    """What the evidence says about one record (the first, "a", by default)."""
    return [(e.signal, e.labels, e.avoid, e.weight) for e in evidence if record is None or e.record == record]


PAUSE = labels.reflex_labels("pause")


def make_head(version: str, dims: int = 64, embedder: str = "embeddinggemma") -> Head:
    """A head that fits the fake gate (all-zero weights: every option equally likely)."""
    questions = {name: QuestionHead(tuple(options), np.zeros((len(options), dims)), np.zeros(len(options)))
                 for name, options in QUESTIONS.items()}
    return Head(questions, embedder, "", dims, 0.65, 0.05, "test", version, "2026-10-01T00:00:00+0000")


# ----------------------------------------------------------------------------- each signal's labels


def test_an_undone_reflex_is_a_strong_avoid_for_that_tool():
    records = [rec("skip this one", "a", "undo", by="b"), rec("go back", "b", "moved_on", tool="previous",
                                                              follows={"id": "a", "as": "undo"})]
    assert signals(extract(records)) == [("undo", {}, {"music_tool": "skip"}, 1.0)]


@pytest.mark.parametrize("change", [dict(ok=False), dict(reflex="spotify.like"), dict(tool="other"),
                                    dict(decision="offer"), dict(topic="calendar"), dict(path="thinker", reflex="")])
def test_only_the_gates_own_reflex_that_worked_is_learned_from(change):
    assert extract([rec("skip this one", "a", "undo", **change)]) == []


def test_a_correction_avoids_the_reflex_and_the_follow_up_hints_what_was_meant():
    records = [rec("next one please", "a", "correction", by="b"),
               rec("no, I meant pause", "b", "silence", tool="pause", follows={"id": "a", "as": "correction"})]
    assert signals(extract(records)) == [("correction", {}, {"music_tool": "skip"}, 1.0),
                                         ("hint", PAUSE, {}, 0.3)]


def test_no_hint_from_a_follow_up_that_was_objected_to_or_is_not_the_follow_up():
    undone = [rec("next one please", "a", "correction", by="b"),
              rec("no, I meant pause", "b", "undo", tool="pause", follows={"id": "a", "as": "correction"})]
    assert signals(extract(undone)) == [("correction", {}, {"music_tool": "skip"}, 1.0)]
    unrelated = [rec("next one please", "a", "correction", by="b"), rec("pause", "b", "silence", tool="pause")]
    assert signals(extract(unrelated)) == [("correction", {}, {"music_tool": "skip"}, 1.0)]


def test_a_rephrase_is_a_medium_avoid_and_a_thinker_follow_up_can_teach_the_hint():
    records = [rec("hold it", "a", "rephrase", path="thinker", by="b"),
               rec("hold the music", "b", "moved_on", path="thinker", teacher="pause", follows={"id": "a", "as": "rephrase"})]
    got = signals(extract(records), None)
    assert ("hint", PAUSE, {}, 0.3) in got and ("teacher", PAUSE, {}, 1.0) in got
    reflexed = [rec("skip", "a", "rephrase", by="b"), rec("skip it", "b", "silence", follows={"id": "a", "as": "rephrase"})]
    assert signals(extract(reflexed))[0] == ("rephrase", {}, {"music_tool": "skip"}, 0.6)
    assert all(s[0] != "hint" for s in signals(extract(reflexed)))      # the same reflex again is no hint


def test_the_teacher_is_a_strong_positive_unless_the_user_objected():
    assert signals(extract([rec("hold on", "a", "moved_on", path="thinker", teacher="pause")])) == [
        ("teacher", PAUSE, {}, 1.0)]
    playing = signals(extract([rec("what's on", "a", "silence", path="thinker", teacher="get_current_track")]))
    assert playing == [("teacher", labels.reflex_labels("now_playing"), {}, 1.0)]
    assert playing[0][1]["kind"] == "question"
    assert extract([rec("hold on", "a", "correction", path="thinker", teacher="pause")]) == []
    assert extract([rec("hold on", "a", "rephrase", path="thinker", teacher="pause")]) == []


def test_silence_is_a_weak_positive_but_not_where_the_gate_was_already_sure():
    assert signals(extract([rec("skip it", "a", "silence")])) == [("silence", labels.reflex_labels("skip"), {}, 0.25)]
    assert extract([rec("skip it", "a", "silence", conf=0.99, tool_conf=0.97)]) == []
    assert extract([rec("skip it", "a", "silence", ok=False)]) == []
    catalogue = rec("play some acid techno", "a", "silence", path="no_catalogue", tool="other", catalogue=0.6)
    assert signals(extract([catalogue])) == [("silence", {"needs_catalogue": "yes"}, {}, 0.25)]


@pytest.mark.parametrize("outcome", ["repeat", "moved_on", "none"])
def test_repeat_moved_on_and_none_teach_nothing(outcome):
    assert extract([rec("skip", "a", outcome)]) == []


def test_privacy_other_voices_old_records_and_odd_lines_are_never_examples():
    assert extract([rec("my code is 552-019, skip", "a", "undo")]) == []                 # privacy.pattern
    assert extract([rec("breaking news", "a", "undo", kind="other")]) == []
    assert extract([rec("skip", "a", "undo", ts=100.0)], ignore_before=200.0) == []
    assert extract([{"v": 2, "id": "x", "text": "skip", "route": {}, "outcome": "undo"}, {"junk": True}]) == []


# ----------------------------------------------------------------------------- resolution and the store


def entry(*evidence) -> dict:
    return {"text": "t", "first": 1.0, "last": 2.0, "evidence": [
        {"id": str(i), "signal": s, "ts": float(i), "labels": l, "avoid": a, "w": w} for i, (s, l, a, w) in enumerate(evidence)]}


def test_conflicts_resolve_the_same_way_whatever_the_order():
    pause_teacher = ("teacher", PAUSE, {}, 1.0)
    skip_silence = ("silence", labels.reflex_labels("skip"), {}, 0.25)
    skip_teacher = ("teacher", labels.reflex_labels("skip"), {}, 1.0)
    undo_skip = ("undo", {}, {"music_tool": "skip"}, 1.0)
    one = resolve("k", entry(pause_teacher, skip_silence), QUESTIONS)
    assert one.labels["music_tool"] == "pause" and one.weights["music_tool"] == 1.0 and not one.conflicts
    two = resolve("k", entry(pause_teacher, skip_teacher), QUESTIONS)
    assert "music_tool" in two.conflicts and "music_tool" not in two.labels
    assert two.labels["kind"] == "request" and two.weights["kind"] == 1.0      # they agree on the rest
    three = resolve("k", entry(undo_skip, skip_silence), QUESTIONS)
    assert three.avoid == {"music_tool": "skip"} and three.weights["music_tool"] == pytest.approx(0.75)
    assert three.labels["kind"] == "request"
    pieces = [pause_teacher, skip_silence, undo_skip, ("hint", labels.reflex_labels("previous"), {}, 0.3)]
    seen = set()
    for seed in range(6):
        random.Random(seed).shuffle(pieces)
        e = resolve("k", entry(*pieces), QUESTIONS)
        seen.add((json.dumps(e.labels, sort_keys=True), json.dumps(e.avoid, sort_keys=True), e.digest))
    assert len(seen) == 1
    capped = resolve("k", entry(pause_teacher, pause_teacher, pause_teacher), QUESTIONS)
    assert capped.weights["music_tool"] == 1.0


def test_the_store_dedupes_counts_a_record_once_and_is_the_users_alone():
    store = ExampleStore()
    first = extract([rec("Skip this.", "a", "undo"), rec("skip   this", "b", "undo"), rec("hold on", "c", "moved_on",
                                                                                       path="thinker", teacher="pause")])
    assert store.merge(first) == {"examples": 2, "evidence": 3, "gone": 0}
    assert store.merge(first) == {"examples": 0, "evidence": 0, "gone": 0}        # the same records again
    rows = {e.text.casefold().strip("."): e for e in store.examples(QUESTIONS)}
    assert len(rows["skip this"].signals) == 1 and rows["skip this"].signals["undo"] == 2
    if os.name == "posix":
        assert store.path.stat().st_mode & 0o777 == 0o600
    key = labels.key_of("hold on")
    assert store.drop([key], "private") == 1
    assert "hold on" not in store.path.read_text() and store.gone()[key]["why"] == "private"
    assert store.merge(first)["gone"] == 1                                       # never taken in again
    assert store.stats()["gone"] == {"private": 1}


def test_review_approves_or_deletes_an_example():
    store = ExampleStore()
    store.merge(extract([rec("skip it", "a", "undo"), rec("hold on", "b", "moved_on", path="thinker", teacher="pause")]))
    loop = Learning()
    assert loop.review(labels.key_of("skip it"), "approve") is True
    assert next(e for e in loop.examples() if e.text == "skip it").review == "approved"
    assert loop.review(labels.key_of("hold on"), "reject") is True
    assert all(e.text != "hold on" for e in loop.examples()) and "hold on" not in store.path.read_text()
    assert loop.review("nope", "approve") is False


def test_balance_caps_the_users_weight_per_option_and_per_question():
    base = [gatehead.Sample(f"b{i}", {"music_tool": "skip" if i < 4 else "pause"}, f"g{i}") for i in range(20)]
    learned = [gatehead.Sample(f"l{i}", {"music_tool": "skip"}, f"l{i}", weights={"music_tool": 1.0}) for i in range(5)]
    learned.append(gatehead.Sample("n", {}, "n", avoid={"music_tool": "pause"}, weights={"music_tool": 1.0}))
    scaled = learnfit.balance(learned, base, 0.25)
    skip = sum(s.weight_for("music_tool") for s in learned if s.labels)
    assert skip == pytest.approx(1.0)                          # 0.25 of the 4 base skip rows
    assert learned[-1].weight_for("music_tool") == pytest.approx(1.0)    # 0.25 of 16 pause rows is 4: no cap
    assert scaled == 5
    assert sum(s.weight_for("music_tool") for s in learned) <= 0.25 * 20 + 1e-9


def test_the_held_out_gate_needs_every_count_at_least_as_good():
    current = {"fields": [90, 100], "strict": [40, 50], "sensitive": [9, 10], "reflexes_wrong": 1}
    assert learnfit.better_or_equal(current, dict(current)) == (True, "")
    for change, words in ((dict(fields=[89, 100]), "fields"), (dict(strict=[39, 50]), "strict"),
                          (dict(sensitive=[8, 10]), "privacy"), (dict(reflexes_wrong=2), "wrong reflexes")):
        passed, why = learnfit.better_or_equal(current, current | change)
        assert not passed and words in why


# ----------------------------------------------------------------------------- the candidate


@pytest.fixture
def world(tmp_path, monkeypatch):
    """The shipped data set, held-out set and head as small fakes the fake embedder fits."""
    data, heldout = small_sets(tmp_path)
    monkeypatch.setattr(gatehead, "TRAIN_SET", data)
    monkeypatch.setattr(gatehead, "HELDOUT_SET", heldout)
    monkeypatch.setattr(learning, "phrases_file", lambda: None)
    samples, vectors = fake_samples()
    shipped = gatehead.train(samples, vectors, QUESTIONS, embedder="embeddinggemma", query_prefix="", dataset="t",
                             l2_grid=(0.01,))
    monkeypatch.setattr(gatehead, "SHIPPED", shipped.save(tmp_path / "shipped.npz"))
    monkeypatch.setattr(gatehead, "L2_GRID", (0.01,))
    original = gatehead.train
    monkeypatch.setattr(gatehead, "train", lambda *a, **kw: original(*a, **({"l2_grid": (0.01,)} | kw)))
    return SimpleNamespace(data=data, heldout=heldout, shipped=shipped)


async def fake_gate(**overrides) -> Gate:
    gate = Gate(replace(GateConfig(query_prefix="", document_prefix=""), **overrides), embedder=FakeEmbedder())
    await gate.start()
    return gate


def config(**learning_kw) -> Config:
    c = Config()
    c.gate = GateConfig(query_prefix="", document_prefix="")
    c.learning = LearningConfig(log_outcomes=True, **learning_kw)
    return c


def teach(texts: list[str], tool: str = "pause", start: int = 0) -> list[dict]:
    return [rec(t, f"r{start + i}", "moved_on", path="thinker", teacher=tool) for i, t in enumerate(texts)]


def worse(monkeypatch) -> None:
    """The candidate gets one field fewer than it really did: the held-out gate must say no."""
    real = learnfit.summary

    def summary(result):
        out = real(result)
        if result.name == "candidate":
            out["fields"] = [out["fields"][0] - 1, out["fields"][1]]
        return out

    monkeypatch.setattr(learnfit, "summary", summary)


async def train(loop: Learning, **kw) -> dict:
    gate = await fake_gate()
    try:
        return await loop.train(gate, **kw)
    finally:
        await gate.close()


async def test_nothing_to_train_without_labels(world):
    loop = Learning(config())
    result = await train(loop)
    assert result == {"trained": False, "why": "no labelled examples yet"}
    assert loop.state()["last_train"]["result"] == "nothing" and loop.heads.versions() == []


async def test_a_candidate_waits_for_accept_by_default_and_accept_puts_it_in_use(world):
    write_outcomes(teach(["hold everything", "freeze the music"]))
    loop = Learning(config())
    held = hashlib.sha256(world.heldout.read_bytes()).hexdigest()
    result = await train(loop)
    assert result["trained"] and result["passed"] and result["status"] == "candidate" and not result["switched"]
    version = result["version"]
    assert loop.current_version() == "shipped" and loop.state()["candidate"] == version
    m = loop.manifest(version)
    assert m["parent"] == "shipped" and m["dataset"] and m["status"] == "candidate"
    assert set(m["learned"]) == {labels.key_of("hold everything"), labels.key_of("freeze the music")}
    assert m["heldout"]["candidate"]["fields"][1] > 0 and m["created_at"] > 0
    assert loop.status()["candidate"]["version"] == version
    assert loop.accept() == {"from": "shipped", "to": version}
    assert loop.current_version() == version and loop.state()["candidate"] is None
    assert loop.manifest(version)["status"] == "accepted" and loop.manifest(version)["decided_by"] == "user"
    assert hashlib.sha256(world.heldout.read_bytes()).hexdigest() == held          # read, never written
    with pytest.raises(LearningError, match="no candidate"):
        loop.accept()


async def test_auto_switch_puts_a_passing_candidate_in_use_at_once(world):
    write_outcomes(teach(["hold everything"]))
    loop = Learning(config(auto_switch=True))
    result = await train(loop, trigger="idle")
    assert result["switched"] == {"from": "shipped", "to": result["version"]}
    assert loop.current_version() == result["version"] and loop.manifest(result["version"])["decided_by"] == "auto"


@pytest.mark.parametrize("auto", [False, True])
async def test_a_worse_candidate_is_rejected_in_either_mode(world, monkeypatch, auto):
    worse(monkeypatch)
    write_outcomes(teach(["hold everything"]))
    loop = Learning(config(auto_switch=auto))
    result = await train(loop)
    assert result["trained"] and not result["passed"] and result["status"] == "rejected" and "fields" in result["why"]
    assert loop.current_version() == "shipped" and loop.state()["candidate"] is None
    m = loop.manifest(result["version"])
    assert m["status"] == "rejected" and m["decided_by"] == "held-out gate"
    assert len(loop.heads.versions()) == 1                    # kept all the same
    with pytest.raises(LearningError):
        loop.accept(result["version"])


async def test_rollback_walks_back_one_switch_at_a_time(world):
    loop = Learning(config())
    write_outcomes(teach(["hold everything"]))
    first = (await train(loop))["version"]
    loop.accept()
    write_outcomes(teach(["hold everything", "stop the tunes", "silence the music please"]))
    second = (await train(loop))["version"]
    assert loop.manifest(second)["parent"] == first
    loop.accept()
    assert loop.status()["rollback_to"] == first
    assert loop.rollback() == {"from": second, "to": first}
    assert "rolled_back_at" in loop.manifest(second) and loop.manifest(second)["status"] == "accepted"
    assert loop.rollback() == {"from": first, "to": "shipped"}
    assert loop.heads.current() is None
    with pytest.raises(LearningError, match="nothing to roll back"):
        loop.rollback()
    assert {first, second} <= {r["version"] for r in loop.versions()}              # every head is kept
    assert [e["what"] for e in loop.state()["events"] if e["what"] in ("accept", "rollback")] == [
        "accept", "accept", "rollback", "rollback"]


async def test_accept_refuses_a_candidate_compared_with_another_head(world):
    loop = Learning(config())
    write_outcomes(teach(["hold everything"]))
    version = (await train(loop))["version"]
    other = loop.heads.add(make_head("other-1"))
    loop.use(gatehead.version_of_path(other))
    with pytest.raises(LearningError, match="train again"):
        loop.accept()
    assert loop.reject() == version and loop.manifest(version)["status"] == "rejected"


async def test_a_learned_sentence_close_to_the_held_out_set_is_left_out(world):
    write_outcomes(teach(["skip this song", "hold everything"]))       # the first is a held-out sentence
    loop = Learning(config())
    result = await train(loop)
    assert labels.key_of("skip this song") not in loop.manifest(result["version"])["learned"]
    assert loop.manifest(result["version"])["left_out"] == {"heldout": 1}
    assert result["counts"]["left_out"] == {"heldout": 1}


async def test_a_sentence_the_head_reads_as_private_is_deleted_from_the_store(world, monkeypatch):
    from strawberry_crab import privacy

    write_outcomes(teach(["your verification code is ready", "hold everything"]))
    monkeypatch.setattr(privacy, "SENSITIVE_P", 0.0)            # every sentence reads as private now
    loop = Learning(config())
    result = await train(loop)
    assert result == {"trained": False, "why": "nothing left to learn after the filters", "compared_with": "shipped",
                      "counts": result["counts"], "heldout": result["heldout"], "dataset": result["dataset"]}
    assert loop.examples() == [] and "verification" not in loop.store.path.read_text()
    assert loop.store.stats()["gone"] == {"private": 2}


async def test_only_new_labels_count_and_a_second_run_waits_for_them(world):
    write_outcomes(teach(["hold everything"]))
    loop = Learning(config())
    loop.extract()
    assert len(loop.new_examples()) == 1
    await train(loop)
    assert loop.new_examples() == []
    write_outcomes(teach(["hold everything", "freeze it"]))
    loop.extract()
    assert [e.text for e in loop.new_examples()] == ["freeze it"]


async def test_one_training_run_at_a_time(world):
    loop = Learning(config())
    with loop._training():
        with pytest.raises(LearningBusy):
            await train(loop)


@pytest.mark.linux_only
def test_a_lock_left_by_a_process_that_is_gone_is_taken_over(tmp_path):
    import subprocess

    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    lock = tmp_path / "x.lock"
    lock.write_text(str(gone.pid))
    with labels.file_lock(lock, timeout_s=0.0):
        assert lock.read_text().startswith(f"{os.getpid()}:")
    assert not lock.exists()
    lock.write_text(f"{os.getpid()}:other")                    # a live holder: no, however old its lock
    os.utime(lock, (time.time() - 10_000, time.time() - 10_000))
    with pytest.raises(TimeoutError):
        with labels.file_lock(lock, timeout_s=0.0, stale_s=1.0):
            pass
    assert lock.read_text() == f"{os.getpid()}:other"


def test_a_lock_is_removed_on_the_way_out_only_while_it_is_still_ours(tmp_path):
    lock = tmp_path / "x.lock"
    with labels.file_lock(lock, timeout_s=0.0):
        lock.write_text("4242:someone-else")                   # taken over meanwhile
    assert lock.read_text() == "4242:someone-else"


async def test_forget_deletes_the_examples_and_ignores_the_old_records(world):
    write_outcomes(teach(["hold everything"]))
    loop = Learning(config(auto_switch=True))
    version = (await train(loop))["version"]
    cache = gatecmd.cache_file("embeddinggemma-injected")
    assert cache.exists()
    assert loop.forget() == {"examples": 1, "heads": 0, "caches": 1}
    assert loop.examples() == [] and not cache.exists() and loop.manifest(version)["learned"] == {}
    loop.extract()
    assert loop.examples() == []                              # the outcome file is still there; it is ignored
    assert loop.forget(everything=True)["heads"] == 1
    assert loop.current_version() == "shipped" and loop.heads.versions() == []


async def test_the_report_counts_the_week(world):
    write_outcomes(teach(["hold everything", "freeze the music"]) + [rec("skip it", "u", "undo")])
    loop = Learning(config())
    await train(loop)
    loop.accept()
    report = loop.report()
    assert report["learned"] == 3 and report["accepted"] == 1 and report["candidates"] == 1
    assert report["tools"] == {"pause": 2, "skip": 1} and report["head_before"] == "shipped"
    line = Learning.report_line(report)
    assert line.startswith("learned 3 new examples, 0 rejected, held-out ") and "% → " in line
    assert loop.weekly_line() == "Learned three new things this week. I've got better at pausing the music."
    assert loop.report(now=time.time() + 30 * 86400)["learned"] == 0


def test_gate_use_goes_through_the_switch_and_rollback_comes_back():
    loop = Learning()
    a = gatehead.version_of_path(loop.heads.add(make_head("a")))
    assert gatecmd.use(a, out=lambda _: None) == 0 and loop.current_version() == a
    assert loop.rollback() == {"from": a, "to": "shipped"}


# ----------------------------------------------------------------------------- privacy: the canary


async def test_no_sentence_reaches_a_log_record_the_state_or_the_manifests(world, caplog, monkeypatch):
    write_outcomes(teach([f"hold everything {CANARY}", f"freeze {CANARY} music"])
                   + [rec(f"skip {CANARY}", "u", "undo"), rec(f"next {CANARY}", "c", "correction", by="d"),
                      rec(f"pause {CANARY}", "d", "silence", tool="pause", follows={"id": "c", "as": "correction"})])
    loop = Learning(config())
    with caplog.at_level(logging.DEBUG):
        result = await train(loop)
        loop.accept()
        outputs = [loop.status(), loop.versions(), loop.report(), loop.health(), loop.weekly_line(),
                   [e.summary() for e in loop.examples()], result]
        loop.rollback()
        trainer = IdleTrainer(SimpleNamespace(config=loop.config, gate=None, listener=None, voice_handling=0, voice_at=0.0))
        outputs.append(trainer.health())
    for record in caplog.records:
        assert CANARY not in record.getMessage(), record.getMessage()
    assert CANARY not in json.dumps(outputs)
    assert CANARY not in loop.state_path.read_text()
    for path in loop.heads.directory.iterdir():
        if path.suffix == ".json":
            assert CANARY not in path.read_text()
    assert CANARY in loop.store.path.read_text()               # the store is where sentences live (0600)
    assert any(CANARY in e.summary(text=True)["text"] for e in loop.examples())


async def test_the_child_process_runs_the_same_job_and_keeps_sentences_off_stderr(world, monkeypatch):
    write_outcomes(teach([f"hold everything {CANARY}"]))
    loop = Learning(config())
    seen = {}

    async def child(job):
        raw = job.to_bytes()
        again = learnfit.Job.from_bytes(raw)
        assert again.learned == job.learned and set(again.vectors) == set(job.vectors)
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "strawberry_crab.learnfit", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=dict(os.environ))
        out, err = await process.communicate(raw)
        seen["err"] = err.decode()
        seen["code"] = process.returncode
        return json.loads(out)

    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(sys.path))
    # The child reads the shipped files by the paths in the job; the head it compares with is a file too.
    result = await train(loop, runner=child)
    assert seen["code"] == 0 and CANARY not in seen["err"] and result["trained"]
    assert CANARY not in json.dumps(result)


# ----------------------------------------------------------------------------- the daemon's side


def daemon_like(cfg: Config, gate=None, **kw) -> SimpleNamespace:
    said: list = []

    async def perform(performance):
        said.append(performance)
        return 1

    base = dict(config=cfg, gate=gate, listener=SimpleNamespace(busy=False), voice_handling=0,
                voice_at=time.monotonic() - 3600, hub=SimpleNamespace(count=1), perform=perform, said=said,
                speaker=SimpleNamespace(quiet=None, clock=lambda: dtime(12, 0)))
    return SimpleNamespace(**(base | kw))


async def test_the_idle_trainer_waits_for_quiet_and_enough_new_labels(world):
    write_outcomes(teach([f"hold it {i}" for i in range(3)]))
    gate = await fake_gate()
    d = daemon_like(config(min_new_labels=3, idle_minutes=10), gate)
    trainer = IdleTrainer(d, runner=learnfit.run)
    try:
        await trainer.tick()
        await trainer.run_task
        assert trainer.runs == 1 and Learning(d.config).state()["candidate"]
        assert trainer.due() == (False, "0 new label(s), 3 needed")
    finally:
        await trainer.close()
        await gate.close()


@pytest.mark.parametrize("busy", [dict(listener=SimpleNamespace(busy=True)), dict(voice_handling=1),
                                  dict(voice_at=time.monotonic())])
async def test_the_idle_trainer_never_starts_while_voice_is_active(world, busy):
    write_outcomes(teach([f"hold it {i}" for i in range(3)]))
    gate = await fake_gate()
    d = daemon_like(config(min_new_labels=1, idle_minutes=10), gate, **busy)
    trainer = IdleTrainer(d, runner=learnfit.run)
    trainer.new_labels = 3
    try:
        ok, why = trainer.due()
        assert not ok and why in ("voice is active", "not idle long enough")
        await trainer.tick()
        assert trainer.run_task is None and trainer.runs == 0
    finally:
        await gate.close()


async def test_a_run_stops_between_two_chunks_when_the_user_speaks(world):
    write_outcomes(teach(["hold it"]))
    gate = await fake_gate()
    d = daemon_like(config(min_new_labels=1, idle_minutes=10), gate)
    inner = gate.embedder
    calls = []

    async def embedder(texts):
        calls.append(len(texts))
        d.voice_at = time.monotonic()                          # the user speaks during the first chunk
        return await inner(texts)

    gate.embedder = embedder
    trainer = IdleTrainer(d, runner=learnfit.run)
    trainer.new_labels = 1
    try:
        await trainer.run()
        assert calls == [learning.CHUNK] and trainer.interrupted == 1 and trainer.errors == 0
        assert Learning(d.config).heads.versions() == [] and trainer.phase == "idle"
        assert gatecmd.cache_file("embeddinggemma-injected").exists()     # what was embedded is kept
    finally:
        await gate.close()


async def test_the_gate_follows_current_without_a_restart(world, tmp_path):
    gate = await fake_gate()
    d = daemon_like(config(), gate)
    trainer = IdleTrainer(d)
    trainer.start()
    try:
        assert gate.head.version == world.shipped.version
        loop = Learning(d.config)
        retrained = replace(world.shipped, version="v2", path=None)
        loop.use(gatehead.version_of_path(loop.heads.add(retrained)))
        assert await trainer.follow_pointer() is True and gate.head.version == "v2"
        assert await trainer.follow_pointer() is False                         # nothing changed since
        loop.rollback()
        assert await trainer.follow_pointer() is True and gate.head.version == world.shipped.version
        loop.use(gatehead.version_of_path(loop.heads.add(make_head("narrow", dims=32))))
        assert await trainer.follow_pointer() is False and gate.head.version == world.shipped.version
    finally:
        await trainer.close()
        await gate.close()


async def test_the_weekly_line_is_off_by_default_waits_a_week_and_keeps_quiet_hours(world):
    write_outcomes(teach(["hold everything", "freeze the music"]))
    loop = Learning(config(auto_switch=True))
    await train(loop)
    off = daemon_like(config(), None)
    assert await IdleTrainer(off).maybe_weekly_line() is False and loop.state()["weekly_line_at"] is None
    d = daemon_like(config(weekly_line=True), None)
    trainer = IdleTrainer(d)
    assert await trainer.maybe_weekly_line() is False                    # the first week starts now
    assert loop.state()["weekly_line_at"] is not None
    with loop._state() as state:
        state["weekly_line_at"] = time.time() - 8 * 86400
    d.speaker = SimpleNamespace(quiet=(dtime(22, 0), dtime(8, 0)), clock=lambda: dtime(23, 30))
    assert await trainer.maybe_weekly_line() is False and d.said == []    # quiet hours
    d.speaker = SimpleNamespace(quiet=(dtime(22, 0), dtime(8, 0)), clock=lambda: dtime(12, 0))
    assert await trainer.maybe_weekly_line() is True
    assert [p.text for p in d.said] == ["Learned two new things this week. I've got better at pausing the music."]
    assert await trainer.maybe_weekly_line() is False                    # once a week


async def test_health_learning_through_the_daemon_has_no_sentences(aiohttp_client):
    from strawberry_crab.server import create_app
    from tests.test_outcomes import route
    from tests.test_thinker import ScriptedGate, bare, plain_config, voice_daemon

    cfg = plain_config()
    cfg.learning = LearningConfig(log_outcomes=True)
    toolbox, qwen, thinker = bare([f"[neutral] Fine, {CANARY}."] * 2)
    text = f"how are you {CANARY}"
    daemon, _ = voice_daemon(cfg, toolbox, thinker, gate=ScriptedGate({text: route(text, kind="chat", topic="other")}))
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await client.post("/event", json={"source": "voice", "title": text})
    health = await (await client.get("/health")).json()
    await daemon.close()
    got = health["learning"]
    assert got["enabled"] is True and got["head"] in ("shipped", "none") and got["trainer"]["phase"] == "idle"
    assert got["trainer"]["waiting_for"] == "the gate is not ready" or got["trainer"]["waiting_for"]
    assert CANARY not in json.dumps(got)
    assert daemon.voice_handling == 0 and daemon.voice_at > 0


# ----------------------------------------------------------------------------- config and the CLI


def test_settings_have_defaults_a_template_and_validation(tmp_path):
    c = LearningConfig()
    assert (c.idle_train, c.idle_minutes, c.min_new_labels, c.auto_switch, c.weekly_line, c.max_share) == (
        True, 20.0, 10, False, False, 0.25)
    template = default_toml()
    for line in ("idle_train = true", "idle_minutes = 20.0", "min_new_labels = 10", "auto_switch = false",
                 "weekly_line = false", "max_share = 0.25"):
        assert line in template
    path = tmp_path / "config.toml"
    path.write_text(template, encoding="utf-8")
    assert load(path).learning == LearningConfig()
    for bad in (dict(idle_minutes=0), dict(min_new_labels=0), dict(max_share=0.0), dict(max_share=1.5)):
        cfg = Config()
        cfg.learning = LearningConfig(**bad)
        with pytest.raises(ConfigError):
            _validate(cfg)


async def test_the_cli_trains_shows_accepts_rolls_back_and_forgets(world, monkeypatch, capsys):
    async def gate(cfg, **overrides):
        return await fake_gate(**overrides)

    monkeypatch.setattr(gatecmd, "_gate", gate)
    path = paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[gate]\nquery_prefix = ""\ndocument_prefix = ""\n[learning]\nlog_outcomes = true\n')
    write_outcomes(teach(["hold everything"]))
    from strawberry_crab import learncmd

    args = cli.parser().parse_args(["learning", "train"])
    assert await learncmd.train(Learning(load(path)), load(path)) == 0
    out = capsys.readouterr().out
    assert "candidate " in out and "held-out fields" in out and "strawberry learning accept" in out
    assert "hold everything" not in out
    assert args.learning_command == "train"
    assert cli.main(["learning", "status"]) == 0
    out = capsys.readouterr().out
    assert "in use     shipped" in out and "candidate  " in out and "1 labelled" in out
    assert cli.main(["learning", "accept"]) == 0 and "is in use (was shipped)" in capsys.readouterr().out
    assert cli.main(["learning", "versions"]) == 0
    out = capsys.readouterr().out
    assert "* " in out and "accepted" in out and "shipped" in out
    assert cli.main(["learning", "report"]) == 0 and "learned 1 new example, 0 rejected" in capsys.readouterr().out
    assert cli.main(["learning", "examples"]) == 0 and "hold everything" not in capsys.readouterr().out
    assert cli.main(["learning", "examples", "--text"]) == 0 and "hold everything" in capsys.readouterr().out
    assert cli.main(["learning", "rollback"]) == 0 and "rolled back: shipped is in use" in capsys.readouterr().out
    assert cli.main(["learning", "rollback"]) == 1 and "nothing to roll back" in capsys.readouterr().err
    assert cli.main(["learning", "reject"]) == 1 and "no candidate" in capsys.readouterr().err
    assert cli.main(["learning", "forget"]) == 0 and "forgot 1 example" in capsys.readouterr().out


async def test_with_outcome_logging_off_the_idle_trainer_learns_nothing_new(world):
    write_outcomes(teach([f"hold it {i}" for i in range(3)]))
    gate = await fake_gate()
    cfg = config(min_new_labels=1, idle_minutes=10)
    cfg.learning.log_outcomes = False
    trainer = IdleTrainer(daemon_like(cfg, gate), runner=learnfit.run)
    try:
        await trainer.tick()
        assert trainer.run_task is None and Learning(cfg).examples() == []
        assert trainer.due() == (False, "outcome logging is off")
    finally:
        await gate.close()


# ----------------------------------------------------------------------------- the review's fixes


@pytest.mark.linux_only
async def test_vectors_heads_and_the_pointer_are_the_users_alone(world):
    write_outcomes(teach(["hold everything"]))
    loop = Learning(config(auto_switch=True))
    result = await train(loop)
    cache = gatecmd.cache_file("embeddinggemma-injected")
    mode = lambda p: p.stat().st_mode & 0o777  # noqa: E731
    assert mode(cache) == 0o600 and mode(cache.parent) == 0o700
    assert mode(loop.heads.directory) == 0o700 and mode(loop.root) == 0o700
    assert mode(Path(result["path"])) == 0o600 and mode(loop.heads.pointer) == 0o600
    assert mode(loop.manifest_path(result["version"])) == 0o600 and mode(loop.state_path) == 0o600


@pytest.mark.parametrize("content", [b"", b"PK\x03\x04 not a zip at all", "meta"])
async def test_a_corrupt_head_falls_back_to_the_shipped_one_and_the_gate_still_starts(world, content):
    heads = gatehead.Heads()
    heads.directory.mkdir(parents=True, exist_ok=True)
    path = heads.directory / "head-broken.npz"
    if content == "meta":                                      # a zip whose meta has another shape
        with path.open("wb") as f:
            np.savez(f, meta=np.array(json.dumps(["not", "a", "dict"])))
    else:
        path.write_bytes(content)
    with pytest.raises(gatehead.HeadError):
        gatehead.load(path)
    heads.use(path)
    head, source, tried = gatehead.resolve()
    assert source == "shipped" and head.version == world.shipped.version and tried
    gate = await fake_gate()
    try:
        assert gate.ready and gate.scorer == "head" and gate.head.version == world.shipped.version
    finally:
        await gate.close()


@pytest.mark.parametrize("name", ["/etc/passwd", "../head-x.npz", "sub/head-x.npz", "learned.json", "head-..npz"])
def test_a_current_pointer_that_names_anything_but_a_head_here_is_ignored(name):
    heads = gatehead.Heads()
    heads.directory.mkdir(parents=True, exist_ok=True)
    heads.pointer.write_text(name + "\n")
    assert heads.current() is None
    assert gatehead.resolve()[1] in ("shipped", "")


async def test_a_corrupt_vector_cache_is_made_again(world):
    cache = gatecmd.cache_file("embeddinggemma-injected")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"")
    gate = await fake_gate()
    try:
        got = await gatecmd.embed(gate, ["hold everything"], "", cache)
    finally:
        await gate.close()
    assert got.shape == (1, 64)
    with np.load(cache) as data:
        assert len(data["keys"]) == 1


async def test_forget_while_a_run_is_going_discards_its_head_even_with_auto_switch(world):
    write_outcomes(teach(["hold everything", "freeze the music"]))
    loop = Learning(config(auto_switch=True))

    async def runner(job):
        result = await learnfit.run(job)
        await asyncio.to_thread(loop.forget, False, 0.0)       # the run holds the lock: forget goes on without it
        return result

    result = await train(loop, runner=runner)
    assert result == {"trained": False, "why": "what it learned from was forgotten while it ran"}
    assert loop.current_version() == "shipped" and loop.heads.versions() == [] and loop.state()["candidate"] is None
    assert loop.state()["last_train"]["result"] == "forgotten" and loop.examples() == []


async def test_forget_drops_the_candidate_waiting(world):
    write_outcomes(teach(["hold everything"]))
    loop = Learning(config())
    version = (await train(loop))["version"]
    loop.forget()
    assert loop.state()["candidate"] is None and loop.manifest(version)["status"] == "superseded"
    with pytest.raises(LearningError):
        loop.accept(version)


def test_malformed_records_and_store_entries_are_skipped_not_fatal():
    good = rec("skip this one", "a", "undo")
    bad = [
        "not a record", ["a", "list"], None,
        rec("x", "b1", "undo") | {"by": ["unhashable"]},
        rec("x2", "b2", "correction") | {"by": "a"},
        rec("x3", "b3", "silence") | {"route": dict(good["route"], confidence="0.4", tool_confidence=[1])},
        rec("x4", "b4", "undo") | {"ts": "yesterday"},
        rec("x5", "b5", "undo") | {"route": "a string"},
        rec("x" * 600, "b6", "undo"),
        rec("x7", "b7", "moved_on", path="thinker") | {"teacher": {"a": 1}},
        rec("x8", "b8", "correction", by="b9"), rec("x9", "b9", "silence", tool="pause") | {"follows": "a"},
    ]
    got = extract(bad + [good])
    assert ("undo", {}, {"music_tool": "skip"}, 1.0) in signals(got)
    assert all(len(e.text) <= labels.MAX_TEXT for e in got)
    store = ExampleStore()
    store.merge(extract([good]))
    data = json.loads(store.path.read_text())
    data["examples"]["junk"] = "a string"
    data["examples"]["junk2"] = {"text": "t", "evidence": [1, {"w": "heavy", "labels": ["x"], "avoid": {"q": 3}},
                                                           {"w": 1.0, "signal": 5, "labels": {"kind": "chat"}}],
                                 "first": "x", "review": 7}
    data["gone"]["g"] = "nope"
    store.path.write_text(json.dumps(data))
    rows = {e.key: e for e in store.examples(QUESTIONS)}
    assert "junk" not in rows and rows["junk2"].labels == {"kind": "chat"} and rows["junk2"].review == "auto"
    assert store.stats()["examples"] == 2 and store.gone() == {}
    assert store.merge(extract([rec("skip that", "c", "undo")]))["examples"] == 1


async def test_a_failing_tick_logs_the_type_only(world, monkeypatch, caplog):
    monkeypatch.setattr(learning, "TICK_S", 0.01)
    trainer = IdleTrainer(daemon_like(config(), None))

    async def tick():
        raise ValueError(f"the sentence {CANARY}")

    trainer.tick = tick
    with caplog.at_level(logging.DEBUG):
        trainer.start()
        await asyncio.sleep(0.1)
        await trainer.close()
    messages = [r.getMessage() for r in caplog.records if r.name == "strawberryd.learning"]
    assert any("ValueError" in m for m in messages) and not any(CANARY in m for m in messages)


async def test_a_timeout_inside_the_run_is_not_busy():
    loop = Learning()
    with pytest.raises(TimeoutError):
        with loop._training():
            raise TimeoutError("the run's own")
    with loop._training():                                     # and the lock was given back
        pass


async def test_the_child_is_killed_after_its_timeout_and_its_stderr_never_comes_back(monkeypatch):
    class Garbage:
        def to_bytes(self):
            return f"not a job {CANARY}".encode()

    with pytest.raises(LearningError) as failed:
        await learning.run_child(Garbage())                    # the child fails on it: type only
    assert CANARY not in str(failed.value) and "the trainer failed" in str(failed.value)
    monkeypatch.setattr(learning, "CHILD_TIMEOUT_S", 0.01)
    with pytest.raises(LearningError, match="took over"):
        await learning.run_child(Garbage())


@pytest.mark.parametrize("version", ["../x", "/etc/passwd", "a/b", "..", "x y", ""])
def test_version_names_are_checked_before_any_path_is_built(version):
    loop = Learning()
    for call in (loop.accept, loop.reject, loop.use):
        with pytest.raises(LearningError):
            call(version) if version else call("bad name")
    assert loop.manifest(version) is None


async def test_last_error_is_the_type_only(world):
    write_outcomes(teach(["hold it"]))
    gate = await fake_gate()

    async def runner(job):
        raise RuntimeError(f"a library printed {CANARY}")

    trainer = IdleTrainer(daemon_like(config(min_new_labels=1, idle_minutes=10), gate), runner=runner)
    try:
        await trainer.run()
    finally:
        await gate.close()
    assert trainer.last_error == "RuntimeError" and CANARY not in json.dumps(trainer.health())
