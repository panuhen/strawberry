"""Runs and protocol v2 run events (runs.py, WIRING.md §18, PROTOCOL.md §10-§11): the order of each
run's steps, one terminal event each, what a cancel does, and that the bus carries names and
timings only. v1 bodies get exactly the bytes they always did."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from strawberry_crab import runs
from strawberry_crab.config import ThinkerConfig, default_toml, load
from strawberry_crab.confirm import LEFT_NO
from strawberry_crab.contract import Performance
from strawberry_crab.daemon import Daemon
from strawberry_crab.events import CannedReactor
from strawberry_crab.hub import body_from_hello
from strawberry_crab.runs import MAX_EVENTS, RunBook, clean, is_stop
from strawberry_crab.server import create_app
from strawberry_crab.thinker import ThinkerError
from tests.fake_spotify import fake_gate
from tests.test_brainui import read_event, sign_in
from tests.test_thinker import FakeQwen, ScriptedGate, make, plain_config, reading, voice_daemon
from tests.test_voice import Sink

PHASES = ["routing", "thinking", "tool", "speaking", "run"]
HELLO_V2 = {"type": "hello", "client": "test-body", "version": "dev", "protocol": 2, "body": {"id": "t-1"},
            "capabilities": {"phases": PHASES, "sends": {"heard": True, "cancel": True}}}
JAZZ = "play some jazz"


def v2_sink(daemon: Daemon, hello: dict | None = None) -> Sink:
    sink = Sink()
    daemon.hub.add(sink)  # type: ignore[arg-type]
    daemon.hub.hello(sink, hello or HELLO_V2)  # type: ignore[arg-type]
    return sink


def steps(sink: Sink, run_id: str | None = None) -> list[dict]:
    return [m for m in sink.got if "type" in m and "run_id" in m and "state" not in m
            and (run_id is None or m["run_id"] == run_id)]


def kinds(sink: Sink, run_id: str | None = None) -> list[str]:
    return [m["type"] for m in steps(sink, run_id)]


async def settle(rounds: int = 10) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0.01)


async def until(check, timeout: float = 3.0) -> None:
    waited = 0.0
    while not check():
        assert waited < timeout, "timed out"
        await asyncio.sleep(0.01)
        waited += 0.01


def well_formed(sink: Sink) -> dict[str, list[dict]]:
    """Every run's steps: seq 1, 2, … and exactly one terminal event, the last."""
    by_run: dict[str, list[dict]] = {}
    for message in steps(sink):
        by_run.setdefault(message["run_id"], []).append(message)
    for run_id, events in by_run.items():
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1)), run_id
        assert [e["t"] for e in events] == sorted(e["t"] for e in events), run_id
        terminal = [e for e in events if e["type"] in runs.TERMINAL]
        assert len(terminal) == 1 and events[-1] is terminal[0], (run_id, [e["type"] for e in events])
    return by_run


def jazz_route(text: str = JAZZ):
    return reading(text, kind="request", topic="music", decision="offer", tool="other", has_argument=0.9)


async def thinker_daemon(aiohttp_client, script: list, handler=None, routes: dict | None = None,
                         config_thinker: ThinkerConfig | None = None):
    config = plain_config()
    config.thinker = config_thinker or ThinkerConfig(ack_after_s=30.0, still_on_it_s=60.0)
    spotify, toolbox, qwen, thinker = make(script, config=config.thinker, handler=handler)
    gate = ScriptedGate({JAZZ: jazz_route(), **(routes or {})})
    daemon, v1 = voice_daemon(config, toolbox, thinker, gate=gate)
    sink = v2_sink(daemon)
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    return client, daemon, spotify, qwen, sink, v1


async def say(client, text: str) -> dict:
    return await (await client.post("/event", json={"source": "voice", "title": text})).json()


class SlowQwen(FakeQwen):
    """The first `slow` replies take `seconds` each; the rest are quick."""

    def __init__(self, script: list, seconds: float = 5.0, slow: int = 1) -> None:
        super().__init__(script)
        self.seconds, self.slow = seconds, slow

    async def __call__(self, payload: dict) -> dict:
        if self.slow > 0:
            self.slow -= 1
            await asyncio.sleep(self.seconds)
        return await super().__call__(payload)


# ----------------------------------------------------------------------------- the pieces


def test_only_the_whitelisted_fields_of_each_type_go_out():
    got = clean("tool.started", {"call_id": "c1", "tool": "spotify.play", "label": "Spotify: play", "careful": False,
                                 "arguments": {"uri": "secret"}, "result": "secret", "text": "secret", "model": "x"})
    assert got == {"call_id": "c1", "tool": "spotify.play", "label": "Spotify: play", "careful": False}
    assert clean("tool.completed", {"ok": "yes", "duration": True, "error": "the server said: secret"}) == {}
    assert clean("run.cancelled", {"reason": "because I said so"}) == {}
    assert clean("routing", {"path": "reflex", "confidence": 0.91234, "kind": "x" * 500})["kind"] == "x" * runs.MAX_TEXT
    assert clean("speaking", {"duration": 1.5, "emotion": "happy", "text": "her line"}) == {"duration": 1.5,
                                                                                             "emotion": "happy"}
    assert clean("no.such.type", {"anything": 1}) == {}


def test_finish_is_idempotent_and_the_cap_keeps_the_terminal_event():
    book = RunBook()
    queue = book.subscribe()
    run = book.start("typed")
    for _ in range(MAX_EVENTS + 20):
        book.emit(run, "thinking", backend="builtin")
    assert run.seq == MAX_EVENTS
    assert book.finish(run)["type"] == "run.completed" and book.finish(run) is None
    assert run.seq == MAX_EVENTS + 1 and run.done and book.busy() is None
    sent = [queue.get_nowait() for _ in range(queue.qsize())]
    assert sum(m["type"] in runs.TERMINAL for m in sent) == 1 and sent[-1]["outcome"] == "nothing"
    assert book.emit(run, "thinking") is None


def test_a_notification_run_is_never_the_foreground_and_never_stopped():
    book = RunBook()
    typed = book.start("typed")
    note = book.start("notification")
    assert book.current is typed and not note.foreground
    assert book.cancel(note.run_id, "stopped") is False
    with pytest.raises(ValueError):
        book.start("gesture")


@pytest.mark.parametrize("text, stop", [("stop", True), ("Stop!", True), ("cancel that", True), ("never mind", True),
                                        ("Never mind, Strawberry.", True), ("okay stop it", True),
                                        ("forget it please", True), ("stop the music", False), ("no", False),
                                        ("don't stop", False), ("play some jazz", False), ("", False)])
def test_a_whole_sentence_stop(text, stop):
    assert is_stop(text) is stop


def test_a_hello_without_protocol_is_v1_whatever_else_it_says():
    assert body_from_hello({"type": "hello", "capabilities": {"phases": PHASES, "sends": {"cancel": True}}}).protocol == 1
    body = body_from_hello(HELLO_V2 | {"capabilities": {"phases": ["tool", "subagent", 3], "sends": {"cancel": "yes"}}})
    assert body.protocol == 2 and body.phases == {"tool"} and body.cancel is False
    assert body_from_hello(HELLO_V2 | {"protocol": 9}).protocol == 2


def test_the_template_documents_runs_and_it_loads(tmp_path):
    text = default_toml()
    assert "[runs]" in text and "supersede = true" in text and "keep = 50" in text
    path = tmp_path / "config.toml"
    path.write_text(text)
    assert load(path).runs.events is True and load(path).runs.keep == 50


# ----------------------------------------------------------------------------- the order of the steps


async def test_a_reflex_routes_calls_speaks_and_completes(aiohttp_client):
    config = plain_config()
    spotify, toolbox, qwen, thinker = make(["[happy] unused"])
    daemon, _ = voice_daemon(config, toolbox, thinker, gate=fake_gate(toolbox))
    sink = v2_sink(daemon)
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    body = await say(client, "skip this track")
    await settle()
    assert kinds(sink) == ["routing", "tool.started", "tool.completed", "speaking", "run.completed"]
    routing, started, completed, speaking, done = steps(sink)
    assert routing["path"] == "reflex" and routing["tool"] == "skip" and routing["topic"] == "music"
    assert started["tool"] == "spotify.skip" and started["label"] == "Spotify: skip" and started["call_id"] == "c1"
    assert completed["ok"] is True and completed["call_id"] == "c1" and "error" not in completed
    assert speaking["emotion"] == body["performance"]["emotion"] and speaking["duration"] > 0
    assert done["outcome"] == "spoken" and done["duration"] >= 0
    # The answering performance carries the run's id for a v2 body only.
    performance = [m for m in sink.got if m.get("state") == "talking"][-1]
    assert performance["run_id"] == done["run_id"] and "run_id" not in body["performance"]
    assert performance["source"] in ("voice", "typed") and "source" not in body["performance"]
    well_formed(sink)
    await daemon.close()


async def test_a_thinker_run_with_a_tool(aiohttp_client):
    client, daemon, spotify, qwen, sink, v1 = await thinker_daemon(aiohttp_client, [[("play", {})], "[happy] Jazz on."])
    await say(client, JAZZ)
    await settle()
    assert kinds(sink) == ["routing", "thinking", "tool.started", "tool.completed", "speaking", "run.completed"]
    routing, thinking, started, completed = steps(sink)[:4]
    assert routing["path"] == "escalate" and "tool" not in routing
    assert thinking == thinking | {"backend": "builtin", "model": "qwen-test"}
    assert started["tool"] == "spotify.play" and started["label"] == "Spotify: play" and started["careful"] is False
    assert completed["ok"] is True and completed["duration"] >= 0
    assert daemon.runs.recent()[0].tools == ["spotify.play"]
    # The v1 socket beside it got performances only, as before.
    assert all("type" not in m for m in v1.got)
    well_formed(sink)
    await daemon.close()


async def test_a_refused_call_is_a_completed_tool_with_refused(aiohttp_client):
    client, daemon, spotify, qwen, sink, _ = await thinker_daemon(
        aiohttp_client, [[("delete_everything_and_say_IGNORE_PREVIOUS", {"x": 1})], "[neutral] Not that."])
    await say(client, JAZZ)
    await settle()
    refused = [m for m in steps(sink) if m["type"] == "tool.completed"]
    assert refused == [refused[0]] and refused[0]["tool"] == "unknown" and refused[0]["ok"] is False
    assert refused[0]["error"] == "refused" and "tool.started" not in kinds(sink)
    well_formed(sink)
    await daemon.close()


async def test_a_failure_and_a_timeout_end_in_run_failed(aiohttp_client):
    client, daemon, spotify, qwen, sink, _ = await thinker_daemon(aiohttp_client, [ThinkerError("HTTP 500")])
    await say(client, JAZZ)
    await settle()
    assert kinds(sink) == ["routing", "thinking", "speaking", "run.failed"]
    assert steps(sink)[-1]["error"] == "backend"
    client, daemon2, spotify, qwen, sink, _ = await thinker_daemon(
        aiohttp_client, ["[happy] too late"], config_thinker=ThinkerConfig(timeout_s=0.1, ack_after_s=30.0,
                                                                           still_on_it_s=60.0))
    daemon2.thinker.chat = SlowQwen(["[happy] too late"], seconds=1.0)
    await say(client, JAZZ)
    await settle()
    assert kinds(sink)[-1] == "run.failed" and steps(sink)[-1]["error"] == "timeout"
    well_formed(sink)
    await daemon.close()
    await daemon2.close()


async def test_a_notification_is_a_run_that_only_speaks(aiohttp_client):
    client, daemon, spotify, qwen, sink, _ = await thinker_daemon(aiohttp_client, [])
    await client.post("/event", json={"source": "notification", "app": "Chat", "title": "Alex", "body": "lunch?"})
    await settle()
    assert kinds(sink) == ["speaking", "run.completed"]
    assert daemon.runs.recent()[0].source == "notification"
    await daemon.close()


# ----------------------------------------------------------------------------- stopping a run


async def test_a_cancel_mid_thought_ends_the_run_and_she_rests(aiohttp_client):
    client, daemon, spotify, qwen, sink, v1 = await thinker_daemon(aiohttp_client, ["[happy] never said"])
    daemon.thinker.chat = SlowQwen(["[happy] never said"], seconds=10.0)
    pending = asyncio.ensure_future(say(client, JAZZ))
    await until(lambda: "thinking" in kinds(sink))
    run_id = steps(sink)[0]["run_id"]
    assert daemon.runs.cancel(run_id, "stopped") is True
    body = await asyncio.wait_for(pending, 2.0)
    await settle()
    assert body["performance"] == {"state": "idle", "emotion": "neutral"}
    assert kinds(sink)[-1] == "run.cancelled" and steps(sink)[-1]["reason"] == "stopped"
    assert v1.got[-1] == {"state": "idle", "emotion": "neutral"}   # out of the thinking pose
    assert daemon.ledger.to_list() == [] and daemon.runs.cancel(run_id, "stopped") is False
    well_formed(sink)
    await daemon.close()


async def test_a_read_in_flight_is_dropped_at_once(aiohttp_client):
    slow_search = asyncio.Event()

    async def handler(name, arguments):
        if name == "search":
            await slow_search.wait()
        return await spotify.handle(name, arguments)

    client, daemon, spotify, qwen, sink, _ = await thinker_daemon(aiohttp_client, [[("search", {"q": "jazz"})], "x"],
                                                                  handler=lambda n, a: handler(n, a))
    pending = asyncio.ensure_future(say(client, JAZZ))
    await until(lambda: "tool.started" in kinds(sink))
    daemon.runs.cancel(steps(sink)[0]["run_id"], "stopped")
    await asyncio.wait_for(pending, 1.0)
    await settle()
    assert kinds(sink)[-2:] == ["tool.started", "run.cancelled"]
    slow_search.set()
    well_formed(sink)
    await daemon.close()


async def test_a_change_in_flight_finishes_and_she_says_so(aiohttp_client):
    async def handler(name, arguments):
        if name == "play":
            await asyncio.sleep(0.3)
        return await spotify.handle(name, arguments)

    client, daemon, spotify, qwen, sink, v1 = await thinker_daemon(aiohttp_client, [[("play", {})], "[happy] unused"],
                                                                   handler=lambda n, a: handler(n, a))
    pending = asyncio.ensure_future(say(client, JAZZ))
    await until(lambda: "tool.started" in kinds(sink))
    daemon.runs.cancel(steps(sink)[0]["run_id"], "stopped")
    await settle(3)
    assert "run.cancelled" not in kinds(sink)          # the play is still going: it finishes first
    body = await asyncio.wait_for(pending, 2.0)
    await settle()
    assert "play" in spotify.log and len(qwen.payloads) == 1   # no second round after the stop
    assert body["performance"]["text"] == "Stopped, but Spotify play had already gone through."
    assert kinds(sink)[-3:] == ["tool.completed", "speaking", "run.cancelled"] and steps(sink)[-3]["ok"] is True
    well_formed(sink)
    await daemon.close()


async def test_a_reflex_in_flight_finishes_before_the_stop(aiohttp_client):
    config = plain_config()

    async def slow(name, arguments):
        if name == "next":
            await asyncio.sleep(0.3)
        return await spotify.handle(name, arguments)

    spotify, toolbox, qwen, thinker = make([], handler=lambda n, a: slow(n, a))
    daemon, _ = voice_daemon(config, toolbox, thinker, gate=fake_gate(toolbox))
    sink = v2_sink(daemon)
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    pending = asyncio.ensure_future(say(client, "skip this track"))
    await until(lambda: "tool.started" in kinds(sink))
    daemon.runs.cancel(steps(sink)[0]["run_id"], "stopped")
    body = await asyncio.wait_for(pending, 2.0)
    await settle()
    assert body["performance"]["text"] == "Stopped, but Spotify skip had already gone through."
    assert kinds(sink)[-3:] == ["tool.completed", "speaking", "run.cancelled"]
    well_formed(sink)
    await daemon.close()


async def test_stop_stops_the_run_and_is_answered_briefly(aiohttp_client):
    client, daemon, spotify, qwen, sink, v1 = await thinker_daemon(aiohttp_client, ["[happy] never said"])
    daemon.thinker.chat = SlowQwen(["[happy] never said"], seconds=10.0)
    pending = asyncio.ensure_future(say(client, JAZZ))
    await until(lambda: "thinking" in kinds(sink))
    stopped = await asyncio.wait_for(say(client, "never mind"), 2.0)
    await asyncio.wait_for(pending, 1.0)
    await settle()
    by_run = well_formed(sink)
    first, second = list(by_run.values())
    assert first[-1]["type"] == "run.cancelled" and first[-1]["reason"] == "stopped"
    assert [e["type"] for e in second] == ["speaking", "run.completed"]    # no routing: never asked the gate
    assert stopped["performance"]["text"] == Daemon.STOPPED
    assert daemon.gate.last_route.text == JAZZ and daemon.thinker.chat.script == ["[happy] never said"]
    await daemon.close()


async def test_a_new_sentence_supersedes_the_one_she_is_on(aiohttp_client):
    client, daemon, spotify, qwen, sink, v1 = await thinker_daemon(aiohttp_client, [])
    daemon.thinker.chat = SlowQwen(["[happy] new answer"], seconds=10.0)   # the first reply never comes
    pending = asyncio.ensure_future(say(client, JAZZ))
    await until(lambda: "thinking" in kinds(sink))
    newer = await asyncio.wait_for(say(client, "what time is it"), 2.0)
    old = await asyncio.wait_for(pending, 1.0)
    await settle()
    first, second = list(well_formed(sink).values())
    assert first[-1]["reason"] == "superseded" and second[-1]["type"] == "run.completed"
    assert newer["performance"]["text"] == "new answer" and old["performance"]["state"] == "idle"
    talking = [m.get("text") for m in v1.got if m.get("state") == "talking"]
    assert talking == ["new answer"]
    await daemon.close()


async def test_with_supersede_off_the_newer_one_waits_its_turn(aiohttp_client):
    client, daemon, spotify, qwen, sink, v1 = await thinker_daemon(aiohttp_client, [])
    daemon.config.runs.supersede = False
    daemon.thinker.chat = SlowQwen(["[happy] first", "[happy] second"], seconds=0.3)
    pending = asyncio.ensure_future(say(client, JAZZ))
    await until(lambda: "thinking" in kinds(sink))
    second = await asyncio.wait_for(say(client, "what time is it"), 3.0)
    first = await pending
    assert first["performance"]["text"] == "first" and second["performance"]["text"] == "second"
    by_run = well_formed(sink)
    ends = [events[-1] for events in by_run.values()]
    assert [e["type"] for e in ends] == ["run.completed", "run.completed"]
    # One foreground run at a time: the second started thinking only after the first had ended.
    first_end = ends[0]["t"]
    second_thinking = [e for e in list(by_run.values())[1] if e["type"] == "thinking"][0]["t"]
    assert second_thinking >= first_end
    await daemon.close()


async def test_an_answer_to_her_question_never_supersedes(aiohttp_client):
    """Stage 2: the run that asked waits for the answer (approvals.py); a yes or no is no run of its own
    and never stops it: the asking run takes the answer and ends."""
    from tests.test_confirm import REMOVE, REMOVE_CALL, daemon_over

    spotify, qwen, daemon, _ = daemon_over([REMOVE_CALL])
    sink = v2_sink(daemon, HELLO_V2 | {"capabilities": {"phases": PHASES, "approvals": True}})
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await say(client, REMOVE)
    asking = daemon.runs.busy()
    assert asking is not None and asking.state == "awaiting_approval"
    # "cancel" is a no to her question, not a stop, while she waits for the answer.
    assert daemon.answers_question("cancel") and not daemon.answers_question("play some jazz")
    answer = await asyncio.wait_for(say(client, "cancel"), 3.0)
    assert answer["performance"]["text"] == LEFT_NO and spotify.removed == []
    assert daemon.runs.started == 1 and daemon.runs.busy() is None
    by_run = well_formed(sink)
    assert list(by_run) == [asking.run_id]
    assert kinds(sink) == ["routing", "thinking", "speaking", "approval.request", "approval.resolved", "speaking",
                           "run.completed"]
    resolved = steps(sink)[4]
    assert resolved["answer"] == "no" and resolved["by"] == "typed"
    await daemon.close()


async def test_shutdown_cancels_the_run_going_on(aiohttp_client):
    client, daemon, spotify, qwen, sink, v1 = await thinker_daemon(aiohttp_client, [])
    daemon.thinker.chat = SlowQwen(["[happy] never"], seconds=10.0)
    task = daemon.background(daemon.handle_event(_voice(JAZZ)), "typed")
    await until(lambda: "thinking" in kinds(sink))
    runs_queue = daemon.runs.subscribe()
    await daemon.close()
    await asyncio.gather(task, return_exceptions=True)
    end = [runs_queue.get_nowait() for _ in range(runs_queue.qsize())][-1]
    assert end["type"] == "run.cancelled" and end["reason"] == "shutdown"


def _voice(text: str):
    from strawberry_crab.events import Event

    return Event(source="voice", title=text)


# ----------------------------------------------------------------------------- privacy


async def test_no_argument_result_or_sentence_reaches_a_run_event(aiohttp_client, caplog):
    for name in ("", "strawberryd", "strawberryd.runs", "strawberryd.thinker", "strawberryd.hub", "strawberryd.ui"):
        caplog.set_level(logging.DEBUG, logger=name)
    sentence, argument, result, line = "CANARY-SENTENCE-3390", "CANARY-ARG-7781", "CANARY-RESULT-5512", "CANARY-LINE-9921"

    async def handler(name, arguments):
        from tests.test_tools import FakeContent, FakeResult

        if name == "search":
            return FakeResult([FakeContent(json.dumps({"tracks": [{"name": result}]}))])
        return await spotify.handle(name, arguments)

    client, daemon, spotify, qwen, sink, _ = await thinker_daemon(
        aiohttp_client, [[("search", {"query": argument}), ("play", {"uri": argument})], f"[happy] {line}"],
        handler=lambda n, a: handler(n, a), routes={sentence: jazz_route(sentence)})
    signed = await sign_in(client)
    await say(client, sentence)
    await settle()
    events = steps(sink)
    assert [e["type"] for e in events].count("tool.started") == 2
    on_the_bus = json.dumps(events) + json.dumps(await (await signed.get("/ui/api/runs")).json())
    on_the_bus += json.dumps([r.view() for r in daemon.runs.recent()])
    for canary in (sentence, argument, result, line):
        assert canary not in on_the_bus, canary
    run_lines = " ".join(r.getMessage() for r in caplog.records if r.name == "strawberryd.runs")
    assert run_lines and not any(c in run_lines for c in (sentence, argument, result, line))
    # Her line itself is a performance, which the bus has always carried.
    assert any(m.get("text") == line for m in sink.got if m.get("state") == "talking")
    await daemon.close()


# ----------------------------------------------------------------------------- the bus


async def test_v1_bodies_get_exactly_the_bytes_they_always_did(aiohttp_client):
    """Recorded from main before runs existed: a v1 body (no protocol in its hello) gets the same
    frames, byte for byte, with a v2 body connected beside it and a run.cancel it may not send."""
    config = plain_config()
    config.gate.enabled = config.tools.enabled = config.thinker.enabled = config.actions.mpris = False
    daemon = Daemon(reactor=CannedReactor(), config=config)
    client = await aiohttp_client(create_app(daemon))
    ws = await client.ws_connect("/ws")
    other = await client.ws_connect("/ws")
    await other.send_str(json.dumps(HELLO_V2))
    await ws.send_str(json.dumps({"type": "hello", "client": "old-body", "version": "dev"}))
    await client.post("/perform", json={"state": "talking", "text": "Hello there.", "emotion": "happy"})
    await client.post("/event", json={"source": "voice", "title": "how are you"})
    await client.post("/event", json={"source": "notification", "app": "Chat", "title": "Alex", "body": "lunch?"})
    await client.post("/event", json={"source": "git", "title": "strawberry", "body": "Add runs"})
    await client.post("/command", json={"command": "mute", "value": True})
    await client.post("/perform", json={"state": "dancing"})
    await ws.send_str(json.dumps({"type": "ping"}))
    await ws.send_str(json.dumps({"type": "run.cancel", "run_id": "r-1"}))
    await ws.send_str(json.dumps({"type": "ping"}))
    frames = []
    while True:
        try:
            message = await ws.receive(timeout=0.5)
        except asyncio.TimeoutError:
            break
        frames.append(message.data)
    assert frames == [
        '{"state": "talking", "emotion": "happy", "text": "Hello there."}',
        '{"state": "talking", "emotion": "neutral", "text": "You said: how are you", "reaction": "nod"}',
        '{"state": "talking", "emotion": "neutral", "text": "Chat: Alex", "reaction": "nod"}',
        '{"state": "talking", "emotion": "neutral", "anim": "notify_perk", "text": "Commit in strawberry: Add runs"}',
        '{"command": "mute", "value": true}',
        '{"state": "dancing", "emotion": "neutral"}',
        '{"type": "pong"}',
        '{"type": "pong"}',
    ]
    late = await client.ws_connect("/ws")
    assert (await late.receive(timeout=1.0)).data == '{"state": "dancing"}'
    for socket in (ws, other, late):
        await socket.close()


async def test_a_v2_hello_is_welcomed_and_pings_carry_the_clock(aiohttp_client):
    client, daemon, spotify, qwen, sink, _ = await thinker_daemon(aiohttp_client, ["[happy] Hi."])
    ws = await client.ws_connect("/ws")
    await ws.send_str(json.dumps(HELLO_V2 | {"capabilities": {"phases": ["tool", "run", "subagent"],
                                                              "sends": {"cancel": True}}}))
    welcome = json.loads((await ws.receive(timeout=1.0)).data)
    assert welcome["type"] == "welcome" and welcome["protocol"] == 2 and welcome["body_id"] == "t-1"
    assert welcome["accepted"] == {"phases": ["run", "tool"], "cancel": True} and welcome["rest_state"] == "idle"
    await ws.send_str(json.dumps({"type": "ping", "t": 12.5}))
    pong = json.loads((await ws.receive(timeout=1.0)).data)
    assert pong["type"] == "pong" and pong["t"] == 12.5 and pong["brain_t"] > 0
    await say(client, JAZZ)
    got = []
    while True:
        try:
            got.append(json.loads((await ws.receive(timeout=0.3)).data))
        except asyncio.TimeoutError:
            break
    assert [m["type"] for m in got if "type" in m] == ["run.completed"]    # only the families it accepted
    health = await (await client.get("/health")).json()
    assert {"protocol": 2, "id": "t-1", "phases": ["run", "tool"], "cancel": True} in health["bodies"]
    await ws.close()
    await daemon.close()


async def test_only_a_v2_body_that_declared_it_may_cancel_and_only_the_current_run(aiohttp_client):
    client, daemon, spotify, qwen, sink, _ = await thinker_daemon(aiohttp_client, [])
    daemon.thinker.chat = SlowQwen(["[happy] finally", "[happy] again"], seconds=0.4, slow=2)
    v1 = await client.ws_connect("/ws")
    await v1.send_str(json.dumps({"type": "hello", "client": "old", "version": "dev"}))
    mute = await client.ws_connect("/ws")
    await mute.send_str(json.dumps(HELLO_V2 | {"capabilities": {"phases": PHASES}}))
    able = await client.ws_connect("/ws")
    await able.send_str(json.dumps(HELLO_V2))
    await settle()
    pending = asyncio.ensure_future(say(client, JAZZ))
    await until(lambda: "thinking" in kinds(sink))
    run_id = steps(sink)[0]["run_id"]
    await v1.send_str(json.dumps({"type": "run.cancel", "run_id": run_id}))
    await mute.send_str(json.dumps({"type": "run.cancel", "run_id": run_id}))
    await able.send_str(json.dumps({"type": "run.cancel", "run_id": "r-999"}))
    body = await asyncio.wait_for(pending, 2.0)
    assert body["performance"]["text"] == "finally"          # none of them stopped it
    refused = [json.loads(m.data) for m in await _drain(able) if '"input.refused"' in m.data]
    assert refused == [{"type": "input.refused", "ref": "r-999", "reason": "not_current"}]
    # A finished run cannot be cancelled either; the run going on can, and nothing else happens.
    await able.send_str(json.dumps({"type": "run.cancel", "run_id": run_id}))
    pending = asyncio.ensure_future(say(client, "what time is it"))
    await until(lambda: kinds(sink).count("thinking") == 2)
    current = daemon.runs.busy().run_id
    await able.send_str(json.dumps({"type": "run.cancel", "run_id": current}))
    body = await asyncio.wait_for(pending, 2.0)
    await settle()
    assert body["performance"]["state"] == "idle" and steps(sink)[-1]["reason"] == "stopped"
    assert daemon.runs.started == 2       # the cancel started no run of its own
    for ws in (v1, mute, able):
        await ws.close()
    await daemon.close()


async def _drain(ws, timeout: float = 0.3) -> list:
    out = []
    while True:
        try:
            out.append(await ws.receive(timeout=timeout))
        except asyncio.TimeoutError:
            return out


# ----------------------------------------------------------------------------- the Brain UI


async def test_the_brain_ui_lists_runs_streams_their_steps_and_cancels(aiohttp_client):
    client, daemon, spotify, qwen, sink, _ = await thinker_daemon(aiohttp_client, [])
    daemon.thinker.chat = SlowQwen(["[happy] never"], seconds=10.0)
    signed = await sign_in(client)
    stream = await signed.get("/ui/api/events")
    pending = asyncio.ensure_future(say(client, JAZZ))
    step = await read_event(stream, "run")
    assert step["type"] == "routing" and step["source"] == "typed"   # /event without spoken: typed
    listed = await (await signed.get("/ui/api/runs")).json()
    assert listed["events"] is True and listed["runs"][0]["run_id"] == step["run_id"]
    assert listed["runs"][0]["outcome"] is None
    assert (await signed.post("/ui/api/cancel", {"run_id": step["run_id"]}, csrf=False)).status == 403
    assert (await signed.post("/ui/api/cancel", {"run_id": "<b>"})).status == 400
    assert (await signed.post("/ui/api/cancel", {"run_id": "r-77"})).status == 409
    response = await signed.post("/ui/api/cancel", {"run_id": step["run_id"]})
    assert response.status == 200 and (await response.json()) == {"cancelled": step["run_id"]}
    await asyncio.wait_for(pending, 2.0)
    while (event := await read_event(stream, "run"))["type"] not in runs.TERMINAL:
        pass
    assert event["type"] == "run.cancelled" and event["reason"] == "stopped"
    routes = await (await signed.get("/ui/api/routes")).json()
    assert routes["routes"][-1]["run_id"] == step["run_id"]
    stream.close()
    await daemon.close()


def test_the_runs_view_renders_with_text_only():
    from strawberry_crab import brainui
    from pathlib import Path

    script = (Path(brainui.__file__).parent / "ui" / "app.js").read_text(encoding="utf-8")
    assert "function renderRuns" in script and '"cancel"' in script and "innerHTML" not in script


async def test_perform_outside_a_run_sends_no_speaking():
    daemon = Daemon(reactor=CannedReactor(), config=plain_config())
    sink = v2_sink(daemon)
    await daemon.perform(Performance(state="talking", text="Hello.", emotion="happy"))
    await settle()
    assert steps(sink) == [] and "run_id" not in sink.got[-1]
