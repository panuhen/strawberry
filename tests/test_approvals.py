"""Approvals with risk tiers (approvals.py, WIRING.md §19, PROTOCOL.md §13b): the call bound to its
digest, the tiers and who sets them, the timeouts, who may answer and how, what a cancel does, the
card for a body that reconnects, what the bus carries, and the stage 2 gauges (`listening`,
`token_rate`) and the `didnt_catch` end."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass
from typing import Any

import pytest
from aiohttp import web

from strawberry_crab import runs
from strawberry_crab.adapters.base import Adapter
from strawberry_crab.approvals import ApprovalBook, digest, needed
from strawberry_crab.config import ApprovalsConfig, ConfigError, ThinkerConfig, ToolsConfig, default_toml, load
from strawberry_crab.confirm import LEFT_CHANGED, LEFT_NO, LEFT_OTHER, LEFT_SILENT, Held, card_line
from strawberry_crab.runs import RunBook
from strawberry_crab.server import create_app
from strawberry_crab.thinker import Meter, Thinker, read_stream
from strawberry_crab.tools import Toolbox
from strawberry_crab.voice import DIDNT_CATCH
from tests.fake_spotify import TRACKS
from tests.test_brainui import read_event, sign_in
from tests.test_confirm import REMOVE, REMOVE_CALL, RUNNING, daemon_over
from tests.test_runs import HELLO_V2, PHASES, kinds, settle, steps, until, v2_sink, well_formed
from tests.test_thinker import FakeQwen, ScriptedGate, plain_config, voice_daemon
from tests.test_tools import FakeContent, FakeResult, FakeSession, FakeTool, make_connect
from tests.test_voice import fake_recording, make_daemon

FEELING_GOOD = TRACKS[0]["uri"]
CARD = {"type": "hello", "client": "test-card", "version": "dev", "protocol": 2, "body": {"id": "card"},
        "capabilities": {"phases": ["run", "speaking"], "approvals": True, "sends": {"heard": True, "approval": True}}}
CANARY = "CANARY-ARG-4471"


@dataclass
class AnnotatedTool(FakeTool):
    annotations: Any = None


@dataclass
class Hints:
    readOnlyHint: bool | None = None
    destructiveHint: bool | None = None


def answer_msg(approval_id: str, answer: str, **extra: Any) -> str:
    return json.dumps({"type": "approval.answer", "approval_id": approval_id, "answer": answer, **extra})


async def say(client, text: str) -> dict:
    return await (await client.post("/event", json={"source": "voice", "title": text})).json()


async def drain(ws, timeout: float = 0.3) -> list[dict]:
    out = []
    while True:
        try:
            message = await ws.receive(timeout=timeout)
        except asyncio.TimeoutError:
            return out
        if message.type.name != "TEXT":
            return out
        out.append(json.loads(message.data))


def notes_daemon(script: list, risk: dict[str, str] | None = None, tools: list[FakeTool] | None = None,
                 adapter: Adapter | None = None, **approvals_config: Any):
    """A daemon over a plain "notes" server (no adapter unless given) whose tools the thinker is offered:
    send_message (sends, by the config), shred (destructive, by the config), note (change)."""
    calls: list[tuple[str, dict]] = []

    async def handler(name: str, arguments: dict) -> FakeResult:
        calls.append((name, dict(arguments)))
        return FakeResult([FakeContent(json.dumps({"done": name}))])

    session = FakeSession(tools or [FakeTool("send_message"), FakeTool("shred"), FakeTool("note")], handler)
    toolbox = Toolbox(ToolsConfig(servers={"notes": {"topic": "notes", "command": "notes"}}, preconnect=False),
                      connect=make_connect({"notes": session}), adapters={"notes": adapter} if adapter else {})
    config = plain_config()
    config.thinker = ThinkerConfig(ack_after_s=30.0, still_on_it_s=60.0)
    config.approvals = ApprovalsConfig(risk={"notes.send_message": "sends", "notes.shred": "destructive"} | (risk or {}),
                                       **approvals_config)
    qwen = FakeQwen(script)
    thinker = Thinker(config.thinker, toolbox, "qwen-test", chat=qwen)
    daemon, v1 = voice_daemon(config, toolbox, thinker, gate=ScriptedGate({}))
    return daemon, calls, qwen


SEND = [("send_message", {"to": "Sam", "text": CANARY})]
SHRED = [("shred", {"name": CANARY})]


# ----------------------------------------------------------------------------- the pieces


def test_a_yes_is_needed_for_the_confirm_list_and_for_sends_and_destructive():
    assert needed(True, "change") and needed(True, "read")
    assert needed(False, "sends") and needed(False, "destructive")
    assert not needed(False, "change") and not needed(False, "read")
    # Stage 3's hook: every call that is not a read once foreign text is in. Nothing passes it yet.
    assert needed(False, "change", foreign=True) and not needed(False, "read", foreign=True)


def test_the_digest_binds_the_server_the_tool_and_the_arguments():
    one = digest("spotify", "remove_from_playlist", {"playlist": "a", "track": "b"})
    assert one == digest("spotify", "remove_from_playlist", {"track": "b", "playlist": "a"})   # key order is not the call
    assert len(one) == 64 and one != digest("spotify", "remove_from_playlist", {"playlist": "a", "track": "c"})
    assert one != digest("spotify", "remove_saved_tracks", {"playlist": "a", "track": "b"})
    assert one != digest("other", "remove_from_playlist", {"playlist": "a", "track": "b"})


async def test_the_stored_call_is_a_copy_checked_against_its_digest():
    book = ApprovalBook()
    arguments = {"playlist": "Gym", "tracks": ["a"]}
    held = Held("spotify", "remove_from_playlist", arguments, "Remove it? Say yes.", risk="change", prompt="Remove it?")
    approval = book.request(None, held)
    arguments["tracks"].append("b")            # the thinker's dict changes afterwards: the stored call does not
    assert approval.held.arguments == {"playlist": "Gym", "tracks": ["a"]} and book.verify(approval)
    approval.held.arguments["playlist"] = "Other"    # the stored call itself tampered with: caught
    assert not book.verify(approval)


async def test_one_open_at_a_time_and_the_first_answer_wins():
    book = ApprovalBook()
    first = book.request(None, Held("s", "a", {}, "A?", risk="change"))
    second = book.request(None, Held("s", "b", {}, "B?", risk="change"))
    assert first.outcome == "superseded" and book.open is second
    assert book.answer(first.approval_id, "yes", "voice") == "resolved"       # stale: already ended
    assert book.answer("a-99", "yes", "voice") == "not_open"
    assert book.answer(None, "yes", "voice") == "not_open"
    assert book.answer(second.approval_id, "maybe", "voice") == "bad_answer" and book.open is second
    assert book.answer(second.approval_id, "no", "voice") is None
    assert book.answer(second.approval_id, "yes", "body", hold=True) == "resolved"
    assert second.outcome == "no" and second.by == "voice" and book.open is None
    assert [a["outcome"] for a in book.views()["history"]] == ["no", "superseded"]
    assert book.stats()["refused"] == 5


async def test_a_body_must_hold_for_the_hold_tiers_and_nobody_else_must():
    book = ApprovalBook()
    for risk, hold in (("change", False), ("sends", True), ("destructive", True)):
        approval = book.request(None, Held("s", "t", {}, "?", risk=risk))
        assert approval.hold is hold and approval.timeout_s == {"change": 10.0}.get(risk, 30.0)
        if hold:
            assert book.answer(approval.approval_id, "yes", "body") == "hold_required" and approval.open
            assert book.answer(approval.approval_id, "yes", "body", hold="yes") == "hold_required"
        assert book.answer(approval.approval_id, "yes", "body", hold=True) is None
    # A no never needs the hold; the user's own sentence and the Brain UI never need it.
    for by, answer in (("body", "no"), ("voice", "yes"), ("typed", "yes"), ("ui", "yes")):
        approval = book.request(None, Held("s", "t", {}, "?", risk="destructive"))
        assert book.answer(approval.approval_id, answer, by) is None and approval.outcome == answer
    # Which tiers want the hold is the config's.
    own = ApprovalBook(ApprovalsConfig(hold=["change"]))
    assert own.request(None, Held("s", "t", {}, "?", risk="change")).hold
    assert not own.request(None, Held("s", "t", {}, "?", risk="sends")).hold


async def test_timeouts_per_tier_and_none_while_the_user_is_speaking():
    book = ApprovalBook(ApprovalsConfig(change_s=0.05, sends_s=0.1, destructive_s=0.15))
    assert [book.timeout_for(r) for r in ("read", "change", "sends", "destructive")] == [0.05, 0.05, 0.1, 0.15]
    approval = book.request(None, Held("s", "t", {}, "?", risk="sends"))
    speaking = {"on": True}
    waiting = asyncio.ensure_future(book.wait(approval, busy=lambda: speaking["on"]))
    await asyncio.sleep(0.3)
    assert not waiting.done() and approval.open            # past its expiry, but the answer is on its way
    speaking["on"] = False
    assert await asyncio.wait_for(waiting, 1.0) == "timeout" and approval.outcome == "timeout"


def test_the_card_line_is_one_short_line_without_say_yes():
    assert card_line("Remove 'Teardrop' from Gym? Say yes.") == "Remove 'Teardrop' from Gym?"
    assert card_line("Delete it?\nSay yes") == "Delete it?"
    assert card_line("a\x1b[31mb‮c") == "a [31mb c"
    assert len(card_line("x" * 500)) == 160 and card_line("x" * 500).endswith("…")


def test_the_approval_events_keep_only_their_fields():
    request = runs.clean("approval.request", {"approval_id": "a-1", "risk": "loud", "prompt": "Send 'hi' to Sam?\n" + "y" * 300,
                                              "timeout_s": 30, "expires_t": 12.34567, "hold": "yes", "arguments": {"x": 1},
                                              "tool": "notes.send"})
    assert request == {"approval_id": "a-1", "prompt": "Send 'hi' to Sam? " + "y" * 141 + "…", "timeout_s": 30.0,
                       "expires_t": 12.346}
    resolved = runs.clean("approval.resolved", {"approval_id": "a-1", "answer": "maybe", "by": "orbs", "text": "yes"})
    assert resolved == {"approval_id": "a-1"}
    assert runs.clean("approval.resolved", {"answer": "superseded", "by": "ui"}) == {"answer": "superseded", "by": "ui"}
    assert runs.clean("token_rate", {"tokens_per_s": math.inf, "tokens": 12.7}) == {"tokens": 12}
    assert runs.clean("listening", {"phase": "talking", "seconds": 1.5, "speech": 1, "text": "hello"}) == {"seconds": 1.5}


# ----------------------------------------------------------------------------- the tiers


def test_the_config_reads_approvals_and_checks_them(tmp_path):
    def loads(text: str):
        path = tmp_path / "config.toml"
        path.write_text(text)
        return load(path, env={})

    config = loads('[approvals]\nsends_s = 20.0\nhold = ["destructive"]\nrisk = { "notes.send" = "sends", notes = "read" }\n')
    assert config.approvals.sends_s == 20.0 and config.approvals.hold == ["destructive"]
    assert config.approvals.risk == {"notes.send": "sends", "notes": "read"}
    # Unquoted, a dotted key is a nested table in TOML: read as "server.tool" all the same.
    nested = loads('[approvals.risk]\nspotify.remove_saved_tracks = "destructive"\nnotes = "read"\n')
    assert nested.approvals.risk == {"spotify.remove_saved_tracks": "destructive", "notes": "read"}
    with pytest.raises(ConfigError, match="approvals.risk"):
        loads('[approvals]\nrisk = { notes = "scary" }\n')
    with pytest.raises(ConfigError, match="approvals.hold"):
        loads('[approvals]\nhold = ["loud"]\n')
    with pytest.raises(ConfigError, match="destructive_s"):
        loads("[approvals]\ndestructive_s = 0\n")
    assert "[approvals]" in default_toml() and loads(default_toml()).approvals == ApprovalsConfig()


async def test_a_tier_comes_from_the_config_then_the_adapter_and_an_annotation_only_raises():
    class Notes(Adapter):
        name = "notes"
        reads = ("peek", "wipe")
        risks = {"post": "sends"}

    tools = [AnnotatedTool("post"), AnnotatedTool("peek"), AnnotatedTool("edit"),
             AnnotatedTool("wipe", annotations=Hints(destructiveHint=True)),             # the adapter says read
             AnnotatedTool("claims", annotations=Hints(readOnlyHint=True)),              # "only reads", unproven
             AnnotatedTool("both", annotations=Hints(readOnlyHint=True, destructiveHint=True)),
             AnnotatedTool("loud", annotations=Hints(destructiveHint=True))]
    session = FakeSession(tools, lambda n, a: None)
    toolbox = Toolbox(ToolsConfig(servers={"notes": {"topic": "notes", "command": "notes"}}, preconnect=False),
                      connect=make_connect({"notes": session}), adapters={"notes": Notes()})
    await toolbox.servers["notes"].ensure()
    tier = {t.name: toolbox.risk("notes", t.name) for t in tools}
    assert tier == {"post": "sends", "peek": "read", "edit": "change", "wipe": "destructive", "claims": "change",
                    "both": "destructive", "loud": "destructive"}
    assert toolbox.reads("notes", "claims")      # a cancel may still drop it: that is about waiting, not a yes
    # The config is taken as written, above or below what the adapter and the annotations say.
    toolbox.risks = {"notes.loud": "change", "notes": "sends", "notes.peek": "read"}
    assert toolbox.risk("notes", "loud") == "change" and toolbox.risk("notes", "edit") == "sends"
    assert toolbox.risk("notes", "peek") == "read"
    assert toolbox.needs_approval("notes", "edit") and not toolbox.needs_approval("notes", "loud")
    assert toolbox.risk("nobody", "x") == "change"
    await toolbox.close()


def test_spotify_and_web_tiers():
    from strawberry_crab.adapters.spotify import SPOTIFY
    from strawberry_crab.adapters.web import WEB

    # The removals stay `change` (a 10 s wait, no hold), asked about through the confirm list as before.
    assert SPOTIFY.risk("remove_from_playlist") is None and SPOTIFY.risk("search") == "read"
    assert WEB.risk("search") == "read"


# ----------------------------------------------------------------------------- the daemon


async def test_the_asking_run_waits_and_a_typed_yes_makes_exactly_the_stored_call(aiohttp_client):
    spotify, qwen, daemon, _ = daemon_over([REMOVE_CALL])
    sink = v2_sink(daemon, CARD | {"capabilities": {"phases": PHASES, "approvals": True}})
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    asked = await say(client, REMOVE)
    assert asked["performance"]["text"] == "Remove 'Feeling Good' from Running? Say yes."
    run = daemon.runs.busy()
    assert run is not None and run.state == "awaiting_approval" and not run.done
    request = steps(sink)[-1]
    assert request["type"] == "approval.request" and request["risk"] == "change" and request["hold"] is False
    assert request["prompt"] == "Remove 'Feeling Good' from Running?" and request["timeout_s"] == 10.0
    assert request["expires_t"] == pytest.approx(request["t"] + 10.0, abs=0.01)
    spotify.index = 1    # the song changes before the answer: the stored call still names the one she asked about
    reply = await say(client, "yes, go ahead")
    assert spotify.removed == [(RUNNING, FEELING_GOOD)]
    assert reply["performance"]["text"].startswith("Removed Feeling Good by Nina Simone from Running.")
    assert kinds(sink) == ["routing", "thinking", "speaking", "approval.request", "approval.resolved",
                           "tool.started", "tool.completed", "speaking", "run.completed"]
    resolved, started = steps(sink)[4], steps(sink)[5]
    assert resolved == resolved | {"answer": "yes", "by": "typed", "approval_id": request["approval_id"]}
    assert started["tool"] == "spotify.remove_from_playlist" and started["label"] == "Spotify: remove from playlist"
    assert daemon.runs.started == 1 and len(qwen.payloads) == 1
    well_formed(sink)
    await daemon.close()


async def test_a_tampered_call_is_never_made(aiohttp_client):
    spotify, qwen, daemon, sink = daemon_over([REMOVE_CALL])
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await say(client, REMOVE)
    daemon.approvals.open.held.arguments["track"] = "spotify:track:" + "z" * 22
    reply = await say(client, "yes")
    assert reply["performance"]["text"] == LEFT_CHANGED and spotify.removed == []
    assert "remove_from_playlist" not in spotify.log
    assert daemon.runs.recent()[0].outcome == "failed"
    await daemon.close()


async def test_no_and_timeout_say_she_left_it_and_the_run_completes(aiohttp_client):
    spotify, qwen, daemon, _ = daemon_over([REMOVE_CALL, REMOVE_CALL], confirm_s=0.1)
    sink = v2_sink(daemon, CARD | {"capabilities": {"phases": PHASES, "approvals": True}})
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await say(client, REMOVE)
    assert (await say(client, "no"))["performance"]["text"] == LEFT_NO
    daemon.dropped_at = -1e9   # the same sentence again is a new request, not a late answer
    await say(client, REMOVE)
    await until(lambda: daemon.runs.busy() is None)
    assert [m.get("text") for m in sink.got if m.get("state") == "talking"][-1] == LEFT_SILENT
    outcomes = [e["answer"] for e in steps(sink) if e["type"] == "approval.resolved"]
    assert outcomes == ["no", "timeout"] and spotify.removed == []
    assert [r.outcome for r in daemon.runs.recent()] == ["completed", "completed"]
    assert "by" not in [e for e in steps(sink) if e["type"] == "approval.resolved"][1]
    well_formed(sink)
    await daemon.close()


@pytest.mark.parametrize("script, risk, wait", [(SEND, "sends", 30.0), (SHRED, "destructive", 30.0)])
async def test_sends_and_destructive_always_ask_and_wait_longer(aiohttp_client, script, risk, wait):
    daemon, calls, qwen = notes_daemon([script])
    sink = v2_sink(daemon, CARD)
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    asked = await say(client, "do the thing")
    name = script[0][0]
    assert asked["performance"]["text"] == f"Shall I go ahead with {name.replace('_', ' ')}? Say yes." and calls == []
    request = next(m for m in sink.got if m.get("type") == "approval.request")
    assert request["risk"] == risk and request["timeout_s"] == wait and request["hold"] is True
    assert request["prompt"] == f"Shall I go ahead with {name.replace('_', ' ')}?"
    await say(client, "yes")
    assert calls == [script[0]]
    await daemon.close()


async def test_a_change_off_the_confirm_list_is_made_at_once(aiohttp_client):
    daemon, calls, qwen = notes_daemon([[("note", {"text": "x"})], "[happy] Noted."])
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    reply = await say(client, "note this")
    assert reply["performance"]["text"] == "Noted." and calls == [("note", {"text": "x"})]
    assert daemon.approvals.asked == 0
    await daemon.close()


async def test_an_adapter_tier_asks_and_the_adapter_describes_the_card(aiohttp_client):
    class Mail(Adapter):
        name = "mail"
        risks = {"note": "sends"}

        async def ask(self, toolbox, server, name, arguments):
            return "Send your note to Sam? Say yes.", dict(arguments)

        def describe(self, name, arguments, question):
            return "Send a note to Sam"

    daemon, calls, qwen = notes_daemon([[("note", {"text": CANARY})]], adapter=Mail())
    sink = v2_sink(daemon, CARD)
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    asked = await say(client, "send it")
    assert asked["performance"]["text"] == "Send your note to Sam? Say yes."
    request = next(m for m in sink.got if m.get("type") == "approval.request")
    assert request["prompt"] == "Send a note to Sam" and request["risk"] == "sends"
    await say(client, "no")
    assert calls == []
    await daemon.close()


# ----------------------------------------------------------------------------- the bus


async def test_a_card_answers_over_the_bus_with_a_hold_and_first_answer_wins(aiohttp_client, caplog):
    caplog.set_level(logging.DEBUG)
    daemon, calls, qwen = notes_daemon([SHRED])
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    card = await client.ws_connect("/ws")
    await card.send_str(json.dumps(CARD))
    welcome = (await drain(card))[0]
    assert welcome["type"] == "welcome" and welcome["accepted"] | {} == {"phases": ["run", "speaking"], "cancel": False,
                                                                         "approvals": True, "approval": True}
    await say(client, "shred it")
    got = await drain(card)
    request = next(m for m in got if m.get("type") == "approval.request")
    approval_id = request["approval_id"]
    await card.send_str(answer_msg(approval_id, "yes"))                  # no hold: refused, still open
    await card.send_str(answer_msg("a-77", "no"))                       # not the open one
    await card.send_str(answer_msg(approval_id, "perhaps", hold=True))   # not an answer
    refused = [m for m in await drain(card) if m.get("type") == "input.refused"]
    assert refused == [{"type": "input.refused", "ref": approval_id, "reason": "hold_required"},
                       {"type": "input.refused", "ref": "a-77", "reason": "not_open"},
                       {"type": "input.refused", "ref": approval_id, "reason": "bad_answer"}]
    assert daemon.approvals.open is not None and calls == []
    await card.send_str(answer_msg(approval_id, "yes", hold=True))
    await until(lambda: daemon.runs.busy() is None)
    await card.send_str(answer_msg(approval_id, "no"))                   # too late: the first answer won
    after = await drain(card)
    assert calls == [("shred", {"name": CANARY})]
    assert {"type": "input.refused", "ref": approval_id, "reason": "resolved"} in after
    resolved = next(m for m in got + after if m.get("type") == "approval.resolved")
    assert resolved["answer"] == "yes" and resolved["by"] == "body"
    assert any(m.get("type") == "run.completed" for m in after)
    assert 'the user said "(answered yes on her card)"' in daemon.ledger.lines()[-1]
    # Nothing of the call's arguments in a log line of the approvals.
    assert not any(CANARY in r.getMessage() for r in caplog.records if r.name == "strawberryd.approvals")
    await card.close()
    await daemon.close()


async def test_only_a_v2_body_that_declared_it_may_answer(aiohttp_client):
    daemon, calls, qwen = notes_daemon([SEND])
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    v1 = await client.ws_connect("/ws")
    await v1.send_str(json.dumps({"type": "hello", "client": "old", "version": "dev"}))
    shows = await client.ws_connect("/ws")       # shows the card but did not say it may answer
    await shows.send_str(json.dumps(CARD | {"capabilities": {"approvals": True, "sends": {"heard": True}}}))
    blind = await client.ws_connect("/ws")       # may answer, but never shows a card: not accepted either
    await blind.send_str(json.dumps(CARD | {"capabilities": {"sends": {"approval": True}}}))
    await settle()
    await say(client, "send it")
    approval_id = daemon.approvals.open.approval_id
    for ws in (v1, shows, blind):
        await drain(ws, 0.1)
        await ws.send_str(answer_msg(approval_id, "yes", hold=True))
    assert await drain(v1) == []                                          # a v1 body: ignored, no bytes
    for ws in (shows, blind):
        assert [m for m in await drain(ws) if m.get("type") == "input.refused"] == [
            {"type": "input.refused", "ref": approval_id, "reason": "not_declared"}]
    assert daemon.approvals.open is not None and calls == []
    health = await (await client.get("/health")).json()
    rows = {row.get("id"): row for row in health["bodies"] if row["protocol"] == 2}
    assert rows["card"]["approvals"] in (True, False) and health["approvals"]["open"] == approval_id
    await say(client, "no")
    for ws in (v1, shows, blind):
        await ws.close()
    await daemon.close()


async def test_a_body_that_reconnects_while_she_waits_gets_the_card(aiohttp_client):
    spotify, qwen, daemon, _ = daemon_over([REMOVE_CALL])
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await say(client, REMOVE)
    late = await client.ws_connect("/ws")
    await late.send_str(json.dumps(CARD))
    got = await drain(late)
    assert [m.get("type") for m in got] == ["welcome", "approval.request"]
    assert got[1]["approval_id"] == daemon.approvals.open.approval_id and got[1]["run_id"] == daemon.runs.busy().run_id
    # A body that does not show approvals is not sent it; nor is a v1 body.
    plain = await client.ws_connect("/ws")
    await plain.send_str(json.dumps(HELLO_V2))
    assert [m.get("type") for m in await drain(plain)] == ["welcome"]
    v1 = await client.ws_connect("/ws")
    await v1.send_str(json.dumps({"type": "hello", "client": "old", "version": "dev"}))
    assert await drain(v1) == []
    await late.send_str(answer_msg(got[1]["approval_id"], "yes"))
    await until(lambda: daemon.runs.busy() is None)
    assert spotify.removed == [(RUNNING, FEELING_GOOD)]
    for ws in (late, plain, v1):
        await ws.close()
    await daemon.close()


async def test_a_typed_heard_answers_and_a_v1_body_sees_only_performances(aiohttp_client):
    spotify, qwen, daemon, _ = daemon_over([REMOVE_CALL])
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    v1 = await client.ws_connect("/ws")
    await v1.send_str(json.dumps({"type": "hello", "client": "old", "version": "dev"}))
    await say(client, REMOVE)
    await v1.send_str(json.dumps({"type": "heard", "text": "yes please"}))
    await until(lambda: daemon.runs.busy() is None and spotify.removed)
    frames = await drain(v1)
    assert frames and all("state" in m and "type" not in m and "run_id" not in m for m in frames)
    assert daemon.approvals.history[-1].by == "typed"
    await v1.close()
    await daemon.close()


# ----------------------------------------------------------------------------- cancel and supersede


async def test_the_x_cancels_the_open_approval_and_makes_nothing(aiohttp_client):
    spotify, qwen, daemon, _ = daemon_over([REMOVE_CALL])
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    card = await client.ws_connect("/ws")
    await card.send_str(json.dumps(CARD | {"capabilities": {"phases": PHASES, "approvals": True,
                                                            "sends": {"cancel": True, "approval": True}}}))
    await say(client, REMOVE)
    run_id = daemon.runs.busy().run_id
    approval_id = daemon.approvals.open.approval_id
    await card.send_str(json.dumps({"type": "run.cancel", "run_id": run_id}))
    await until(lambda: daemon.runs.busy() is None)
    got = await drain(card)
    resolved = next(m for m in got if m.get("type") == "approval.resolved")
    assert resolved["answer"] == "cancelled" and "by" not in resolved
    assert got[-1]["type"] == "run.cancelled" and got[-1]["reason"] == "stopped"
    await card.send_str(answer_msg(approval_id, "yes", hold=True))
    assert [m["reason"] for m in await drain(card) if m.get("type") == "input.refused"] == ["resolved"]
    assert spotify.removed == [] and daemon.held is None
    await card.close()
    await daemon.close()


async def test_another_sentence_supersedes_the_question_even_with_supersede_off(aiohttp_client):
    spotify, qwen, daemon, _ = daemon_over([REMOVE_CALL, "[neutral] Twenty past four."])
    daemon.config.runs.supersede = False
    sink = v2_sink(daemon, CARD | {"capabilities": {"phases": PHASES, "approvals": True}})
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await say(client, REMOVE)
    reply = await asyncio.wait_for(say(client, "what time is it"), 3.0)
    assert reply["performance"]["text"] == f"{LEFT_OTHER} Twenty past four." and spotify.removed == []
    by_run = well_formed(sink)
    first, second = by_run.values()
    assert [e for e in first if e["type"] == "approval.resolved"][0]["answer"] == "superseded"
    assert first[-1]["type"] == "run.cancelled" and first[-1]["reason"] == "superseded"
    assert second[-1]["type"] == "run.completed"
    await daemon.close()


async def test_stop_while_she_waits_is_a_no_and_shutdown_cancels(aiohttp_client):
    spotify, qwen, daemon, sink = daemon_over([REMOVE_CALL, REMOVE_CALL])
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await say(client, REMOVE)
    assert (await say(client, "stop"))["performance"]["text"] == LEFT_NO
    daemon.dropped_at = -1e9
    await say(client, REMOVE)
    approval = daemon.approvals.open
    queue = daemon.runs.subscribe()
    await daemon.close()
    sent = [queue.get_nowait() for _ in range(queue.qsize())]
    assert approval.outcome == "cancelled" and spotify.removed == []
    assert [m["type"] for m in sent][-2:] == ["approval.resolved", "run.cancelled"] and sent[-1]["reason"] == "shutdown"


async def test_a_stop_after_the_yes_lets_the_call_finish(aiohttp_client):
    gate = asyncio.Event()
    spotify, qwen, daemon, _ = daemon_over([REMOVE_CALL])
    original = spotify.handle

    async def slow(name, arguments):
        if name == "remove_from_playlist":
            await gate.wait()
        return await original(name, arguments)

    daemon.toolbox.servers["spotify"].connect = make_connect({"spotify": FakeSession(
        [FakeTool(n) for n in ("remove_from_playlist", "find_playlist", "get_current_track", "next")], slow)})
    sink = v2_sink(daemon, CARD | {"capabilities": {"phases": PHASES, "approvals": True}})
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await say(client, REMOVE)
    answer = asyncio.ensure_future(say(client, "yes"))
    await until(lambda: "tool.started" in kinds(sink))
    assert daemon.runs.cancel(daemon.runs.busy().run_id, "stopped")
    gate.set()
    reply = await asyncio.wait_for(answer, 3.0)
    assert spotify.removed and reply["performance"]["text"] == "Stopped, but Spotify remove from playlist had already gone through."
    assert kinds(sink)[-1] == "run.cancelled"
    await daemon.close()


# ----------------------------------------------------------------------------- privacy


async def test_no_argument_reaches_the_bus_the_ui_or_the_log(aiohttp_client, caplog):
    for name in ("", "strawberryd", "strawberryd.approvals", "strawberryd.runs", "strawberryd.ui"):
        caplog.set_level(logging.DEBUG, logger=name)
    daemon, calls, qwen = notes_daemon([SEND])
    sink = v2_sink(daemon, CARD | {"capabilities": {"phases": PHASES, "approvals": True}})
    client = await aiohttp_client(create_app(daemon))
    signed = await sign_in(client)
    await daemon.start()
    await say(client, "message Sam")
    listed = await (await signed.get("/ui/api/runs")).json()
    assert listed["approvals"]["open"]["prompt"] == "Shall I go ahead with send message?"
    await say(client, "yes")
    listed = await (await signed.get("/ui/api/runs")).json()
    on_the_bus = json.dumps([m for m in sink.got if "type" in m]) + json.dumps(listed)
    on_the_bus += json.dumps(daemon.approvals.views()) + json.dumps((await (await client.get("/health")).json())["approvals"])
    assert CANARY not in on_the_bus and "Sam" not in on_the_bus
    assert calls == [("send_message", {"to": "Sam", "text": CANARY})]
    logged = " ".join(r.getMessage() for r in caplog.records if r.name in ("strawberryd.approvals", "strawberryd.runs",
                                                                           "strawberryd.ui", "strawberryd.http"))
    assert CANARY not in logged
    await daemon.close()


# ----------------------------------------------------------------------------- the Brain UI


async def test_the_brain_ui_shows_the_open_approval_and_answers_it(aiohttp_client):
    spotify, qwen, daemon, _ = daemon_over([REMOVE_CALL])
    client = await aiohttp_client(create_app(daemon))
    signed = await sign_in(client)
    await daemon.start()
    stream = await signed.get("/ui/api/events")
    await say(client, REMOVE)
    while (event := await read_event(stream, "run"))["type"] != "approval.request":
        pass
    listed = await (await signed.get("/ui/api/runs")).json()
    pending = listed["approvals"]["open"]
    assert pending["approval_id"] == event["approval_id"] and pending["tool"] == "spotify.remove_from_playlist"
    assert pending["prompt"] == "Remove 'Feeling Good' from Running?" and 0 < pending["expires_in"] <= 10
    approval_id = pending["approval_id"]
    assert (await signed.post("/ui/api/approval", {"approval_id": approval_id, "answer": "yes"}, csrf=False)).status == 403
    assert (await signed.post("/ui/api/approval", {"approval_id": "<b>", "answer": "yes"})).status == 400
    assert (await signed.post("/ui/api/approval", {"approval_id": approval_id, "answer": "sure"})).status == 400
    assert (await signed.post("/ui/api/approval", {"approval_id": "a-000000-55", "answer": "yes"})).status == 409
    assert (await signed.post("/ui/api/approval", {"approval_id": "a-55", "answer": "yes"})).status == 400
    response = await signed.post("/ui/api/approval", {"approval_id": approval_id, "answer": "yes"})
    assert response.status == 200 and (await response.json()) == {"answered": approval_id, "answer": "yes"}
    again = await signed.post("/ui/api/approval", {"approval_id": approval_id, "answer": "no"})
    assert again.status == 409 and (await again.json())["reason"] == "resolved"
    await until(lambda: daemon.runs.busy() is None)
    assert spotify.removed == [(RUNNING, FEELING_GOOD)]
    history = (await (await signed.get("/ui/api/runs")).json())["approvals"]
    assert history["open"] is None and history["history"][0]["outcome"] == "yes" and history["history"][0]["by"] == "ui"
    assert "prompt" not in history["history"][0] and "arguments" not in json.dumps(history)
    stream.close()
    await daemon.close()


def test_the_runs_view_has_the_approval_and_its_history_as_text():
    from pathlib import Path

    from strawberry_crab import brainui

    script = (Path(brainui.__file__).parent / "ui" / "app.js").read_text(encoding="utf-8")
    assert "function pendingApproval" in script and '"approval"' in script and "innerHTML" not in script
    assert '"Approvals"' in script and "token_rate" in script


# ----------------------------------------------------------------------------- listening, token_rate, didnt_catch


async def test_a_voice_capture_sends_listening_started_and_ended_to_the_bodies_that_asked():
    daemon, v1 = make_daemon("hello there")
    sink = v2_sink(daemon, HELLO_V2 | {"capabilities": {"phases": ["listening", "run"]}})
    await daemon.start()
    await daemon.listener.loaded()
    daemon.listen()
    await daemon.listen_task
    await settle()
    gauges = [m for m in sink.got if m.get("type") == "listening"]
    assert [g["phase"] for g in gauges] == ["started", "ended"]
    assert gauges[1]["seconds"] == 2.0 and gauges[1]["speech"] is True
    assert all(set(g) <= {"type", "t", "phase", "seconds", "speech"} for g in gauges)
    assert not any("type" in m for m in v1.got)
    await daemon.close()


async def test_an_empty_capture_is_a_run_that_ends_didnt_catch():
    daemon, v1 = make_daemon("", recording=fake_recording(speech=0.0, stopped_by="max"))
    sink = v2_sink(daemon, HELLO_V2 | {"capabilities": {"phases": PHASES + ["listening"]}})
    await daemon.start()
    await daemon.listener.loaded()
    daemon.listen()
    await daemon.listen_task
    await settle()
    assert v1.got[-1]["text"] == DIDNT_CATCH
    assert kinds(sink) == ["speaking", "run.cancelled"] and steps(sink)[-1]["reason"] == "didnt_catch"
    gauges = [m for m in sink.got if m.get("type") == "listening"]
    assert gauges[-1]["speech"] is False
    run = daemon.runs.recent()[0]
    assert run.outcome == "cancelled" and run.detail == "didnt_catch" and not run.foreground
    await daemon.close()


async def test_an_empty_capture_leaves_her_question_open(aiohttp_client):
    spotify, qwen, daemon, _ = daemon_over([REMOVE_CALL])
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await say(client, REMOVE)
    approval = daemon.approvals.open
    await daemon.didnt_catch(DIDNT_CATCH)
    assert daemon.approvals.open is approval and daemon.runs.busy() is approval.run
    await say(client, "no")
    await daemon.close()


async def test_token_rate_is_a_gauge_at_most_four_a_second():
    book = RunBook()
    queue = book.subscribe()
    run = book.start("typed")
    assert book.rate(run, 31.25, 10) is not None
    assert book.rate(run, 40.0, 12) is None                         # too soon
    forced = book.rate(run, 35.0, 20, force=True)
    assert forced == {"type": "token_rate", "run_id": run.run_id, "t": forced["t"], "tokens_per_s": 35.0, "tokens": 20}
    run.rate_at -= 0.3
    assert book.rate(run, 30.0, 25) is not None
    sent = [queue.get_nowait() for _ in range(queue.qsize())]
    assert len(sent) == 3 and all("seq" not in m for m in sent) and run.seq == 0 and run.events == []
    book.finish(run)
    assert book.rate(run, 1.0, 1, force=True) is None
    quiet = RunBook(events=False)
    assert quiet.rate(quiet.start("typed"), 1.0, 1, force=True) is None


async def lines(*chunks: dict):
    for chunk in chunks:
        yield (json.dumps(chunk) + "\n").encode()


async def test_a_streamed_reply_is_put_back_together_and_counted():
    book = RunBook()
    queue = book.subscribe()
    run = book.start("typed")
    meter = Meter(run)
    reply = await read_stream(lines(
        {"message": {"role": "assistant", "content": "[happy] "}, "done": False},
        {"message": {"role": "assistant", "content": "Hello"}, "done": False},
        {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "next", "arguments": {}}}]},
         "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True, "eval_count": 40, "eval_duration": 2_000_000_000}),
        meter)
    assert reply["message"] == {"role": "assistant", "content": "[happy] Hello",
                                "tool_calls": [{"function": {"name": "next", "arguments": {}}}]}
    assert reply["eval_count"] == 40 and meter.tokens == 3
    meter.round_done(reply)
    assert meter.tokens == 40
    sent = [queue.get_nowait() for _ in range(queue.qsize())]
    assert sent[-1]["tokens_per_s"] == 20.0 and sent[-1]["tokens"] == 40
    with pytest.raises(Exception, match="model is busy"):
        await read_stream(lines({"error": "model is busy"}))


async def test_the_thinker_streams_from_ollama_and_sends_token_rate(aiohttp_server):
    received: list[dict] = []

    async def chat(request: web.Request) -> web.StreamResponse:
        received.append(await request.json())
        response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await response.prepare(request)
        for word in ["[happy] ", "All ", "done ", "now."]:
            await response.write((json.dumps({"message": {"role": "assistant", "content": word}, "done": False}) + "\n").encode())
            await asyncio.sleep(0.06)
        await response.write((json.dumps({"message": {"role": "assistant", "content": ""}, "done": True,
                                          "eval_count": 4, "eval_duration": 250_000_000}) + "\n").encode())
        return response

    app = web.Application()
    app.router.add_post("/api/chat", chat)
    server = await aiohttp_server(app)
    toolbox = Toolbox(ToolsConfig(servers={}, preconnect=False))
    thinker = Thinker(ThinkerConfig(), toolbox, "qwen-test", ollama_url=str(server.make_url("/")))
    await thinker.start()
    book = RunBook()
    queue = book.subscribe()
    run = book.start("typed")
    outcome = await thinker.run("how are you", tools=False, run=run)
    assert outcome.ok and outcome.fact == "All done now." and received[0]["stream"] is True
    rates = [m for m in (queue.get_nowait() for _ in range(queue.qsize())) if m["type"] == "token_rate"]
    assert 1 <= len(rates) <= 3 and rates[-1] == rates[-1] | {"tokens_per_s": 16.0, "tokens": 4}
    assert all(set(m) == {"type", "run_id", "t", "tokens_per_s", "tokens"} for m in rates)
    # Off in the config: one reply at the end, as before, and still counted from Ollama's numbers.
    thinker.config.stream = False
    await thinker.close()
    await thinker.start()
    with pytest.raises(Exception):
        await thinker._ollama_chat({"model": "x", "messages": []})   # the fake streams: not one JSON body
    assert received[-1]["stream"] is False
    await thinker.close()


async def test_a_thinker_reply_with_counts_sends_token_rate_to_the_bodies_that_asked(aiohttp_client):
    daemon, calls, qwen = notes_daemon([])

    async def counted(payload: dict) -> dict:
        return {"message": {"role": "assistant", "content": "[happy] Fine."}, "eval_count": 30, "eval_duration": 1_000_000_000}

    daemon.thinker.chat = counted
    wants = v2_sink(daemon, HELLO_V2 | {"capabilities": {"phases": ["token_rate", "run"]}})
    other = v2_sink(daemon, HELLO_V2 | {"capabilities": {"phases": PHASES}})
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await say(client, "how are you")
    await settle()
    rates = [m for m in wants.got if m.get("type") == "token_rate"]
    assert rates == [rates[0] | {"tokens_per_s": 30.0, "tokens": 30}] and "seq" not in rates[0]
    assert not any(m.get("type") == "token_rate" for m in other.got)
    well_formed(other)
    await daemon.close()
