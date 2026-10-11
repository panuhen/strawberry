"""The Brain UI's History view (historyui.py, WIRING §17, §30): days, rows, search, forget, digests and reminders."""

from __future__ import annotations

import json
import logging
import time

import pytest
from aiohttp.test_utils import TestServer

from strawberry_crab import brainui, historyui
from strawberry_crab.daemon import Daemon
from strawberry_crab.events import CannedReactor
from strawberry_crab.history import day_of, day_start, shift
from strawberry_crab.server import QuietAccessLogger, create_app
from tests.test_brainui import origin, quiet_config, sign_in

CANARY = "wren-history-5d1e"


def daemon_of(client) -> Daemon:
    return client.server.app[brainui.UI].daemon


@pytest.fixture
async def client(aiohttp_client):
    return await aiohttp_client(create_app(Daemon(reactor=CannedReactor(), config=quiet_config())))


def days_ago(n: int) -> str:
    return shift(day_of(time.time()), -n)


def seed(daemon: Daemon) -> dict[str, int]:
    """Today (open), yesterday (no digest: pending), two days ago (a digest), three days ago (the user's own
    digest), ten days ago (a digest only: its rows gone); a pending, a fired and a missed reminder."""
    h = daemon.history
    ids = {}
    at = lambda n, hour: day_start(days_ago(n)) + hour * 3600   # noqa: E731
    ids["today"] = h.turn(f"what is {CANARY}", "A word.", source="typed", at=time.time() - 5)
    ids["notice"] = h.notice("notification", f"Signal, from {CANARY}-sender", foreign=True, at=time.time() - 3)
    ids["yesterday"] = h.turn("plan the SQLite store", "Sounds good.", source="voice", at=at(1, 6))
    ids["play"] = h.play("Spotify", "New Order — Blue Monday", at=at(1, 7))
    ids["run"] = h.run_ended("voice", ["lights.turn_on"], "change", "completed", at=at(2, 5))
    ids["secret"] = h._insert("run", None, "voice", False, "r-9", {"tools": ["x.y"], "args": {"token": CANARY}},
                              at=at(2, 6))
    h.approval("lights.turn_on", "change", "yes", "body", at=at(2, 5.5))
    h.store_digest(days_ago(2), "Talked about the lamp.")
    h.turn("old words", "Old reply.", at=at(3, 3))
    h.store_digest(days_ago(3), "My own note.", edited=True)
    h.store_digest(days_ago(10), "A day long gone.")
    ids["pending"] = h.add_timer(time.time() + 600, f"call {CANARY}")
    ids["fired"] = h.add_timer(time.time() - 600, "stretch")
    h.set_timer_state(ids["fired"], "fired")
    ids["missed"] = h.add_timer(time.time() - 7200, "water the plants")
    h.set_timer_state(ids["missed"], "missed")
    return ids


async def test_the_view_has_the_retention_the_days_with_their_state_and_the_reminders(client):
    signed = await sign_in(client)
    daemon = daemon_of(client)
    ids = seed(daemon)
    view = await (await signed.get("/ui/api/history")).json()
    assert view["enabled"] and view["open"] and view["error"] is None
    assert view["retention"] == {"keep_days": 14, "digest_months": 6, "sentences": True, "lookback_tokens": 500}
    assert view["limits"]["digest_chars"] == 700 and view["limits"]["confirm"] == "forget"
    states = {d["day"]: (d["state"], d["edited_by_user"]) for d in view["days"]}
    assert states == {days_ago(0): ("open", False), days_ago(1): ("pending", False), days_ago(2): ("digest", False),
                      days_ago(3): ("digest", True), days_ago(10): ("digest_only", False)}
    assert [d["day"] for d in view["days"]] == sorted(states, reverse=True) and view["more"] is None
    assert next(d for d in view["days"] if d["day"] == days_ago(0))["foreign"] == 1
    assert [t["id"] for t in view["timers"]["pending"]] == [ids["pending"]]
    assert [t["id"] for t in view["timers"]["recent"]] == [ids["fired"], ids["missed"]]   # newest first
    assert view["counts"]["timers_pending"] == 1 and view["today"] == days_ago(0)


async def test_a_day_gives_its_rows_newest_first_with_the_foreign_marked_and_no_argument(client):
    signed = await sign_in(client)
    ids = seed(daemon_of(client))
    today = await (await signed.get(f"/ui/api/history/day?day={days_ago(0)}")).json()
    assert [r["id"] for r in today["rows"]] == [ids["notice"], ids["today"]] and today["more"] is None
    notice, turn = today["rows"]
    assert notice["foreign"] and notice["kind"] == "notice" and notice["text"] == f"Signal, from {CANARY}-sender"
    assert not turn["foreign"] and (turn["said"], turn["reply"]) == (f"what is {CANARY}", "A word.")
    two = await (await signed.get(f"/ui/api/history/day?day={days_ago(2)}")).json()
    assert two["digest"]["text"] == "Talked about the lamp." and not two["digest"]["edited_by_user"]
    secret = next(r for r in two["rows"] if r["id"] == ids["secret"])
    assert secret["meta"] == {"tools": ["x.y"]} and CANARY not in json.dumps(two)     # only the kind's own fields
    run = next(r for r in two["rows"] if r["id"] == ids["run"])
    assert run["meta"] == {"tools": ["lights.turn_on"], "tier": "change", "outcome": "completed"}
    assert next(r for r in two["rows"] if r["kind"] == "approval")["meta"] == {"tool": "lights.turn_on",
                                                                               "risk": "change", "outcome": "yes"}
    gone = await (await signed.get(f"/ui/api/history/day?day={days_ago(10)}")).json()
    assert gone["rows"] == [] and gone["total"] == 0 and gone["digest"]["text"] == "A day long gone."
    for bad in ("2026-13-01", "yesterday", "", "2026-10-10' OR 1=1"):
        assert (await signed.get(f"/ui/api/history/day?day={bad}")).status == 400


async def test_a_turn_whose_words_hold_the_separator_still_splits_by_its_length(client):
    signed = await sign_in(client)
    h = daemon_of(client).history
    h.turn('say "; you said "hi', 'Fine "ok".', at=time.time() - 1)
    rows = (await (await signed.get(f"/ui/api/history/day?day={days_ago(0)}")).json())["rows"]
    assert (rows[0]["said"], rows[0]["reply"]) == ('say "; you said "hi', 'Fine "ok".')


async def test_days_and_rows_come_a_page_at_a_time(client, monkeypatch):
    signed = await sign_in(client)
    h = daemon_of(client).history
    for n in range(35):
        h.store_digest(days_ago(n + 1), f"day {n + 1}")
    first = await (await signed.get("/ui/api/history")).json()
    assert len(first["days"]) == historyui.DAYS_PAGE and first["more"] == days_ago(30)
    second = await (await signed.get(f"/ui/api/history?before={first['more']}")).json()
    assert [d["day"] for d in second["days"]] == [days_ago(n) for n in range(31, 36)] and second["more"] is None
    assert (await signed.get("/ui/api/history?before=nope")).status == 400
    monkeypatch.setattr(historyui, "ROWS_PAGE", 3)
    for i in range(5):
        h.notice("git", f"commit {i}", foreign=False, at=time.time() - 10 + i)
    page = await (await signed.get(f"/ui/api/history/day?day={days_ago(0)}")).json()
    assert [r["text"] for r in page["rows"]] == ["commit 4", "commit 3", "commit 2"] and page["more"] == 3
    rest = await (await signed.get(f"/ui/api/history/day?day={days_ago(0)}&offset=3")).json()
    assert [r["text"] for r in rest["rows"]] == ["commit 1", "commit 0"] and rest["more"] is None
    assert (await signed.get(f"/ui/api/history/day?day={days_ago(0)}&offset=-1")).status == 400


async def test_the_search_is_hers_grouped_by_day_with_the_digests(client):
    signed = await sign_in(client)
    seed(daemon_of(client))
    found = await (await signed.get("/ui/api/history/search?q=lamp SQLite")).json()
    assert [g["day"] for g in found["groups"]] == [days_ago(1), days_ago(2)]
    yesterday, two = found["groups"]
    assert yesterday["digest"] is None and yesterday["rows"][0]["said"] == "plan the SQLite store"
    assert two["digest"] == "Talked about the lamp." and two["rows"] == []
    assert found["words"] == ["lamp", "sqlite"] and found["rows"] == 1 and found["digests"] == 1
    empty = await (await signed.get("/ui/api/history/search?q=the what")).json()   # filler only: no words
    assert empty["groups"] == [] and empty["words"] == []
    assert (await signed.get("/ui/api/history/search?q=" + "x" * 201)).status == 400


async def test_forget_a_row_a_day_and_everything_keeping_the_reminders(client):
    signed = await sign_in(client)
    h = daemon_of(client).history
    ids = seed(daemon_of(client))
    assert (await (await signed.post("/ui/api/history/forget/row", {"id": ids["today"]})).json()) == {"forgotten": ids["today"]}
    assert [r.id for r in h.rows(days_ago(0))] == [ids["notice"]]
    assert (await signed.post("/ui/api/history/forget/row", {"id": ids["today"]})).status == 404
    for bad in ({"id": "1"}, {"id": True}, {"id": -1}, {}):
        assert (await signed.post("/ui/api/history/forget/row", bad)).status == 400
    day = await (await signed.post("/ui/api/history/forget/day", {"day": days_ago(2)})).json()
    assert day == {"day": days_ago(2), "rows": 3} and h.rows(days_ago(2)) == [] and h.digest_of(days_ago(2)) is None
    assert (await signed.post("/ui/api/history/forget/day", {"day": "today"})).status == 400
    assert (await signed.post("/ui/api/history/forget/all", {"confirm": "yes"})).status == 400
    assert (await signed.post("/ui/api/history/forget/all", {"confirm": "forget", "timers": "no"})).status == 400
    assert h.days()                                                      # nothing went on a refusal
    done = await (await signed.post("/ui/api/history/forget/all", {"confirm": "forget"})).json()
    assert done["rows"] > 0 and not done["timers"] and h.days() == [] and len(h.timers()) == 3
    await (await signed.post("/ui/api/history/forget/all", {"confirm": "forget", "timers": True})).json()
    assert h.timers() == []


async def test_edit_a_digest_marks_it_the_users_and_checks_its_length(client):
    signed = await sign_in(client)
    h = daemon_of(client).history
    seed(daemon_of(client))
    saved = await (await signed.post("/ui/api/history/digest", {"day": days_ago(1), "text": " Planned  it.\n\nThen tea. "})).json()
    assert saved["digest"]["text"] == "Planned it.\nThen tea." and saved["digest"]["edited_by_user"]
    assert not h.store_digest(days_ago(1), "The digester's own.")       # the digester never overwrites it
    assert h.digest_of(days_ago(1))["text"] == "Planned it.\nThen tea."
    view = await (await signed.get("/ui/api/history")).json()
    assert next(d for d in view["days"] if d["day"] == days_ago(1))["edited_by_user"]
    for text, status in (("", 400), ("  \n ", 400), ("x" * 201, 400), ("\n".join(["y" * 150] * 5), 400), (3, 400)):
        assert (await signed.post("/ui/api/history/digest", {"day": days_ago(1), "text": text})).status == status
    assert (await signed.post("/ui/api/history/digest", {"day": "2001-01-01", "text": "made up"})).status == 404
    assert h.digest_of("2001-01-01") is None and h.digest_of(days_ago(1))["text"] == "Planned it.\nThen tea."


async def test_cancel_a_pending_reminder_only(client):
    signed = await sign_in(client)
    h = daemon_of(client).history
    ids = seed(daemon_of(client))
    assert (await (await signed.post("/ui/api/history/timer/cancel", {"id": ids["pending"]})).json()) == {"cancelled": ids["pending"]}
    assert {t["id"]: t["state"] for t in h.timers()}[ids["pending"]] == "cancelled"
    again = await signed.post("/ui/api/history/timer/cancel", {"id": ids["pending"]})
    assert again.status == 409 and "cancelled" in (await again.json())["error"]
    assert (await signed.post("/ui/api/history/timer/cancel", {"id": ids["fired"]})).status == 409
    assert {t["id"]: t["state"] for t in h.timers()}[ids["fired"]] == "fired"
    assert (await signed.post("/ui/api/history/timer/cancel", {"id": 999999})).status == 404


GETS = ["/ui/api/history", "/ui/api/history/day?day=2026-10-10", "/ui/api/history/search?q=plan"]
POSTS = [("/ui/api/history/forget/row", {"id": 1}), ("/ui/api/history/forget/day", {"day": "2026-10-10"}),
         ("/ui/api/history/forget/all", {"confirm": "forget"}), ("/ui/api/history/digest", {"day": "2026-10-10", "text": "x"}),
         ("/ui/api/history/timer/cancel", {"id": 1})]


@pytest.mark.parametrize("path", GETS)
async def test_every_read_needs_the_session_and_the_csrf_header(client, path):
    signed = await sign_in(client)
    assert (await client.get(path)).status == 401
    assert (await signed.get(path, csrf=False)).status == 403
    assert (await signed.get(path, origin="http://evil.example")).status == 403
    assert (await signed.get(path, **{"Sec-Fetch-Site": "cross-site"})).status == 403
    assert (await signed.get(path)).status == 200


@pytest.mark.parametrize("path, body", POSTS)
async def test_every_write_needs_the_session_the_csrf_header_and_the_origin(client, path, body):
    signed = await sign_in(client)
    h = daemon_of(client).history
    h.turn("keep me", "Kept.", at=day_start("2026-10-10") + 3600)
    h.store_digest("2026-10-10", "kept")
    h.add_timer(time.time() + 60, "kept")
    assert (await client.post(path, json=body, headers={"Origin": origin(client)})).status == 401   # no session
    assert (await signed.post(path, body, csrf=False)).status == 403
    assert (await signed.post(path, body, origin="http://127.0.0.1:1")).status == 403
    assert (await signed.post(path, body, origin=None)).status == 403
    assert len(h.rows("2026-10-10")) == 1 and h.digest_of("2026-10-10")["text"] == "kept"
    assert h.timers()[0]["state"] == "pending"


async def test_with_the_history_off_the_view_says_so_and_nothing_acts(aiohttp_client):
    config = quiet_config()
    config.history.enabled = False
    client = await aiohttp_client(create_app(Daemon(reactor=CannedReactor(), config=config)))
    signed = await sign_in(client)
    view = await (await signed.get("/ui/api/history")).json()
    assert view["enabled"] is False and view["open"] is False and "days" not in view
    assert view["retention"]["keep_days"] == 14
    for path in GETS[1:]:
        assert (await signed.get(path)).status == 409
    for path, body in POSTS:
        response = await signed.post(path, body)
        assert response.status == 409 and "off" in (await response.json())["error"]


async def test_a_file_that_did_not_open_is_said_and_nothing_acts(client):
    signed = await sign_in(client)
    h = daemon_of(client).history
    await h.call(h.close)
    h.error = "OperationalError"
    view = await (await signed.get("/ui/api/history")).json()
    assert view["enabled"] and not view["open"] and view["error"] == "OperationalError"
    response = await signed.post("/ui/api/history/forget/all", {"confirm": "forget"})
    assert response.status == 409 and "not open" in (await response.json())["error"]


async def test_no_row_digest_search_or_reminder_text_reaches_a_log(aiohttp_client, caplog):
    caplog.set_level(logging.DEBUG)
    # The daemon's own access log (server.serve's runner): a /ui request without its query, a good API read not at all.
    server = TestServer(create_app(Daemon(reactor=CannedReactor(), config=quiet_config())))
    await server.start_server(access_log_class=QuietAccessLogger)
    client = await aiohttp_client(server)
    signed = await sign_in(client)
    ids = seed(daemon_of(client))
    await signed.get("/ui/api/history")
    await signed.get(f"/ui/api/history/day?day={days_ago(0)}")
    await signed.get(f"/ui/api/history/search?q={CANARY}")
    await signed.get(f"/ui/api/history/search?q={CANARY * 20}")                                   # refused
    await signed.post("/ui/api/history/digest", {"day": days_ago(1), "text": f"about {CANARY}"})
    await signed.post("/ui/api/history/digest", {"day": days_ago(1), "text": CANARY * 40})        # refused
    await signed.post("/ui/api/history/timer/cancel", {"id": ids["pending"]})
    await signed.post("/ui/api/history/forget/row", {"id": ids["today"]})
    await signed.post("/ui/api/history/forget/day", {"day": days_ago(1)})
    await signed.post("/ui/api/history/forget/all", {"confirm": "forget", "timers": True})
    said = [r.getMessage() for r in caplog.records]
    assert any("GET /ui/api/history/search" in m and " 400" in m for m in said)
    assert any("forgotten" in m for m in said) and any("reminder cancelled" in m for m in said)
    assert not [m for m in said if CANARY in m or "Blue Monday" in m or "SQLite store" in m]


def test_the_page_registers_the_view_under_activity_as_text_only():
    script = (brainui.UI_DIR / "app.js").read_text(encoding="utf-8")
    block = script[script.index("activity: history (historyui.py"):script.index("// activity: history end")]
    page = (brainui.UI_DIR / "index.html").read_text(encoding="utf-8")
    group = page[page.index("<h3>Activity</h3>"):page.index("<h3>Learning</h3>")]
    assert 'data-section="history"' in group and '<section id="history"' in page
    assert "RENDER.history = renderHistory" in block and "REFRESH.history = refreshHistory" in block
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script and "outerHTML" not in script
