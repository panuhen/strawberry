"""Approvals, hardened (WIRING.md §19): the grace period is capped, ids differ per start, the call is made
from a fresh copy checked against its digest, a stop between the yes and the call makes nothing and
says so, and a streamed reply that ends early is an error, never half an answer."""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from aiohttp import web

from strawberry_crab import confirm
from strawberry_crab.adapters.base import Adapter
from strawberry_crab.approvals import ApprovalBook, digest
from strawberry_crab.config import ApprovalsConfig, ThinkerConfig, ToolsConfig
from strawberry_crab.confirm import LEFT_CHANGED, LEFT_UNMADE, Held
from strawberry_crab.server import create_app
from strawberry_crab.thinker import Thinker, ThinkerError, read_stream
from strawberry_crab.tools import Toolbox
from tests.test_approvals import CARD, lines, say
from tests.test_confirm import REMOVE, REMOVE_CALL, daemon_over
from tests.test_runs import PHASES, kinds, until, v2_sink
from tests.test_tools import FakeContent, FakeResult, FakeSession, FakeTool, make_connect


async def test_a_listener_that_never_stops_cannot_hold_it_open():
    book = ApprovalBook(ApprovalsConfig(change_s=0.05, grace_s=0.2))
    approval = book.request(None, Held("s", "t", {}, "?", risk="change"))
    started = asyncio.get_running_loop().time()
    assert await asyncio.wait_for(book.wait(approval, busy=lambda: True), 2.0) == "timeout"
    waited = asyncio.get_running_loop().time() - started
    assert 0.24 <= waited < 0.6          # the wait, then one extension of grace_s, and no more
    none = ApprovalBook(ApprovalsConfig(change_s=0.05, grace_s=0.0))
    quick = none.request(None, Held("s", "t", {}, "?", risk="change"))
    assert await asyncio.wait_for(none.wait(quick, busy=lambda: True), 1.0) == "timeout"


def test_grace_is_in_the_config_and_checked(tmp_path):
    from strawberry_crab.config import ConfigError, default_toml, load

    path = tmp_path / "config.toml"
    path.write_text("[approvals]\ngrace_s = -1\n")
    with pytest.raises(ConfigError, match="grace_s"):
        load(path, env={})
    assert "grace_s = 10.0" in default_toml() and ApprovalsConfig().grace_s == 10.0


async def test_ids_carry_a_part_that_is_new_at_each_start():
    first, second = ApprovalBook(), ApprovalBook()
    one = first.request(None, Held("s", "t", {}, "?"))
    other = second.request(None, Held("s", "t", {}, "?"))
    assert re.fullmatch(r"a-[0-9a-f]{6}-1", one.approval_id) and re.fullmatch(r"a-[0-9a-f]{6}-1", other.approval_id)
    stale = "a-" + ("0" * 6 if first.boot != "0" * 6 else "1" * 6) + "-1"   # the same number, another start
    assert first.answer(stale, "yes", "body", hold=True) == "not_open" and one.open


async def test_the_call_is_made_from_a_fresh_copy_checked_against_the_digest():
    made: list[dict] = []

    async def handler(name, arguments):
        made.append(arguments)
        arguments["tracks"].append("added by the server")    # a server mutating what it was given
        return FakeResult([FakeContent('{"ok": true}')])

    toolbox = Toolbox(ToolsConfig(servers={"notes": {"topic": "notes", "command": "notes"}}, preconnect=False),
                      connect=make_connect({"notes": FakeSession([FakeTool("tidy")], handler)}))

    class Seen(Adapter):
        name = "seen"

        def done(self, name, arguments, result):
            arguments["tracks"].clear()                       # an adapter mutating what it was given
            return None

    held = Held("notes", "tidy", {"tracks": ["a"]}, "Tidy? Say yes.")
    expected = digest("notes", "tidy", {"tracks": ["a"]})
    outcome = await confirm.run(toolbox, Seen(), held, expected=expected)
    assert outcome.ok and made == [{"tracks": ["a", "added by the server"]}]
    assert held.arguments == {"tracks": ["a"]} and made[0] is not held.arguments   # the stored call is untouched
    changed = await confirm.run(toolbox, None, Held("notes", "tidy", {"tracks": ["b"]}, "?"), expected=expected)
    assert not changed.ok and changed.fact == LEFT_CHANGED and len(made) == 1
    await toolbox.close()


@pytest.mark.parametrize("how", ["cancelled", "flagged"])
async def test_a_stop_between_the_yes_and_the_call_makes_nothing_and_says_so(aiohttp_client, how):
    spotify, qwen, daemon, _ = daemon_over([REMOVE_CALL])
    sink = v2_sink(daemon, CARD | {"capabilities": {"phases": PHASES, "approvals": True}})
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await say(client, REMOVE)
    approval, run = daemon.approvals.open, daemon.runs.busy()
    assert daemon.approvals.answer(approval.approval_id, "yes", "ui") is None
    if how == "cancelled":
        daemon.runs.cancel(run.run_id, "stopped")   # lands before the waiting task wakes
    else:
        run.cancel_reason = "stopped"                # seen only by the last look before the call
    await until(lambda: run.done)
    assert spotify.removed == [] and "remove_from_playlist" not in spotify.log
    assert approval.outcome == "yes" and approval.made is False
    assert daemon.approvals.views()["history"][0]["made"] is False
    resolved = next(e for e in sink.got if e.get("type") == "approval.resolved")
    assert resolved["answer"] == "yes" and "tool.started" not in kinds(sink)
    assert kinds(sink)[-1] == "run.cancelled" and run.detail == "stopped"
    assert [m.get("text") for m in sink.got if m.get("state") == "talking"][-1] == LEFT_UNMADE
    assert 'you did "left remove_from_playlist undone after a yes"' in daemon.ledger.lines()[-1]
    await daemon.close()


async def test_stop_said_between_the_yes_and_the_call_is_answered_once(aiohttp_client):
    spotify, qwen, daemon, sink = daemon_over([REMOVE_CALL])
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await say(client, REMOVE)
    approval, run = daemon.approvals.open, daemon.runs.busy()
    daemon.approvals.answer(approval.approval_id, "yes", "ui")
    daemon.runs.cancel_all("stopped")    # what a whole-sentence "stop" does first (handle_voice)
    await until(lambda: run.done)
    from strawberry_crab.events import Event

    performance, sent = await daemon._stopped(Event(source="voice", title="stop"), run)
    assert performance.text is None and sent == 0     # the stopped run said it; no "Okay, stopped." over it
    talking = [m.get("text") for m in sink.got if m.get("state") == "talking"]
    assert talking[-1] == LEFT_UNMADE and spotify.removed == []
    await daemon.close()


async def test_a_newer_sentence_that_overtakes_a_yes_says_nothing_was_done(aiohttp_client):
    spotify, qwen, daemon, sink = daemon_over([REMOVE_CALL, "[neutral] Twenty past four."])
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await say(client, REMOVE)
    approval, run = daemon.approvals.open, daemon.runs.busy()
    daemon.approvals.answer(approval.approval_id, "yes", "ui")
    daemon.runs.cancel(run.run_id, "superseded")
    reply = await say(client, "what time is it")
    assert reply["performance"]["text"] == f"{LEFT_UNMADE} Twenty past four." and spotify.removed == []
    assert approval.made is False and run.detail == "superseded"
    await daemon.close()


async def raw_lines(*raw: bytes):
    for line in raw:
        yield line


async def test_a_stream_that_ends_early_or_breaks_is_an_error():
    with pytest.raises(ThinkerError, match="stream ended early"):
        await read_stream(lines({"message": {"role": "assistant", "content": "[happy] Half a"}, "done": False}))
    with pytest.raises(ThinkerError, match="stream ended early"):
        await read_stream(lines())

    async def too_long():
        yield (json.dumps({"message": {"content": "x"}, "done": False}) + "\n").encode()
        raise ValueError("Chunk too big")

    with pytest.raises(ThinkerError, match="ValueError"):
        await read_stream(too_long())
    with pytest.raises(ThinkerError, match="not JSON"):
        await read_stream(raw_lines(b"{not json\n"))


async def test_the_thinker_says_so_when_ollama_stops_half_way(aiohttp_server):
    async def chat(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await response.prepare(request)
        await response.write((json.dumps({"message": {"role": "assistant", "content": "[happy] Sure, I'll"},
                                          "done": False}) + "\n").encode())
        return response   # no done line

    app = web.Application()
    app.router.add_post("/api/chat", chat)
    server = await aiohttp_server(app)
    thinker = Thinker(ThinkerConfig(), Toolbox(ToolsConfig(servers={}, preconnect=False)), "qwen-test",
                      ollama_url=str(server.make_url("/")))
    await thinker.start()
    outcome = await thinker.run("how are you", tools=False)
    assert not outcome.ok and outcome.fact == "I tried, but my thinking part is not answering."
    await thinker.close()
