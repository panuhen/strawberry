"""The Brain UI's Her tabs (Persona, Profile), the timeline beside the runs and the Settings tab's additions
(WIRING.md §17, §21-§23): the same session, CSRF and Origin rules as every other route."""

from __future__ import annotations

import logging

import pytest

from strawberry_crab import brainui, paths, persona
from strawberry_crab.config import ConfigError
from strawberry_crab.daemon import Daemon
from strawberry_crab.events import CannedReactor
from strawberry_crab.server import create_app
from tests.test_brainui import Signed, origin, quiet_config, sign_in   # noqa: F401

SHIPPED = persona.shipped_path().read_text(encoding="utf-8")
CANARY = "kingfisher-draft-0b4e"


@pytest.fixture
async def client(aiohttp_client):
    return await aiohttp_client(create_app(Daemon(reactor=CannedReactor(), config=quiet_config())))


def daemon_of(client) -> Daemon:
    return client.server.app[brainui.UI].daemon


POSTS = [("/ui/api/persona/check", {"text": SHIPPED}), ("/ui/api/persona/save", {"text": SHIPPED}),
         ("/ui/api/persona/try", {"text": SHIPPED}), ("/ui/api/profile/save", {"text": "- x\n"}),
         ("/ui/api/profile/revert", {"id": "20261010T120000-000000"}), ("/ui/api/apply", {})]


@pytest.mark.parametrize("path, body", POSTS)
async def test_every_new_write_needs_the_session_the_csrf_header_and_the_origin(client, path, body):
    signed = await sign_in(client)
    assert (await client.post(path, json=body, headers={"Origin": origin(client)})).status == 401   # no session
    assert (await signed.post(path, body, csrf=False)).status == 403                       # no CSRF header
    assert (await signed.post(path, body, origin="http://127.0.0.1:1")).status == 403      # another origin
    assert (await signed.post(path, body, origin=None)).status in (403,)                  # no origin on a POST
    assert not paths.persona_file().exists() and not paths.profile_file().exists()


async def test_the_persona_tab_reads_checks_saves_with_a_backup_and_she_uses_it(client, caplog):
    caplog.set_level(logging.DEBUG)
    signed = await sign_in(client)
    view = await (await signed.get("/ui/api/persona")).json()
    assert view["text"] == SHIPPED and view["shipped"] == SHIPPED and view["status"]["source"] == "shipped"
    bad = SHIPPED.replace("  emotion: happy\n", "  emotion: smug\n", 1)
    checked = await (await signed.post("/ui/api/persona/check", {"text": bad})).json()
    assert not checked["ok"] and any("emotion must be one of" in p for p in checked["problems"])
    assert (await signed.post("/ui/api/persona/save", {"text": bad})).status == 400
    assert not paths.persona_file().exists()
    draft = SHIPPED.replace("- Okay, stopped.\n", f"- {CANARY}, stopped.\n")
    checked = await (await signed.post("/ui/api/persona/check", {"text": draft})).json()
    assert checked["ok"] and checked["sizes"]["Lines"] <= checked["caps"]["Lines"]
    assert f"+- {CANARY}, stopped." in checked["diff"] and "-- Okay, stopped." in checked["diff"]
    saved = await (await signed.post("/ui/api/persona/save", {"text": draft})).json()
    assert saved["backup"] is None and saved["status"]["source"] == "file"
    assert daemon_of(client).persona.line("stopped") == f"{CANARY}, stopped."
    again = await (await signed.post("/ui/api/persona/save", {"text": SHIPPED})).json()
    assert paths.persona_file().with_name("persona.md.bak").read_text(encoding="utf-8") == draft and again["backup"]
    assert CANARY not in "\n".join(r.getMessage() for r in caplog.records)
    big = await signed.post("/ui/api/persona/check", {"text": "x" * (persona.MAX_FILE_CHARS + 2000)})
    assert big.status == 413


async def test_try_it_runs_the_draft_through_the_reaction_model_and_refuses_without_one(client):
    signed = await sign_in(client)
    response = await signed.post("/ui/api/persona/try", {"text": SHIPPED})
    assert response.status == 409 and "reaction model is off" in (await response.json())["error"]
    seen = []

    class Reactor(CannedReactor):
        async def sample(self, draft, events):
            seen.append((draft.name, [e.source for e in events]))
            return [{"line": f"line {i}", "emotion": "happy", "s": 0.1} for i, _ in enumerate(events)]

    daemon_of(client).reactor = Reactor()
    draft = SHIPPED.replace("You are Strawberry,", "You are Bramble,", 1)
    result = await (await signed.post("/ui/api/persona/try", {"text": draft})).json()
    assert seen == [("Bramble", ["git", "notification", "media", "action"])]
    assert [s["label"] for s in result["samples"]] == ["a commit", "a message", "a track", "after a skip"]
    assert result["samples"][0]["line"] == "line 0" and result["samples"][0]["event"].startswith("source: git")
    assert not paths.persona_file().exists()                                 # nothing saved by a try


async def test_the_reactor_tries_a_draft_without_unloading_or_counting(aiohttp_server):
    from tests.test_brain import fake_ollama, make_reactor

    app, calls = fake_ollama({"line": "Draft line.", "emotion": "neutral"})
    reactor = await make_reactor(aiohttp_server, app)
    draft = persona.parse(SHIPPED.replace("You are Strawberry,", "You are Bramble,", 1))
    out = await reactor.sample(draft, [event for _l, event in brainui.SAMPLE_EVENTS])
    assert [o["line"] for o in out] == ["Draft line."] * 4 and reactor.stats()["calls"] == 0
    assert all(c["keep_alive"] == reactor.brain.keep_alive != 0 for c in calls["chat"])
    assert calls["chat"][0]["messages"][0]["content"].startswith("You are Bramble,")
    await reactor.close()


async def test_the_profile_tab_saves_shows_history_and_reverts(client, caplog):
    caplog.set_level(logging.DEBUG)
    signed = await sign_in(client)
    view = await (await signed.get("/ui/api/profile")).json()
    assert view["text"] == "" and view["history"] == [] and view["prompt"] == "" and view["cap"] == 400
    saved = await (await signed.post("/ui/api/profile/save", {"text": f"- Call me {CANARY}\n"})).json()
    assert saved["text"] == f"- Call me {CANARY}\n" and saved["prompt"].endswith(f"| - Call me {CANARY}")
    first = saved["history"][0]
    assert first["op"] == "edit" and first["by"] == "ui" and first["added"] == [f"Call me {CANARY}"]
    await signed.post("/ui/api/profile/save", {"text": "- Something else\n"})
    reverted = await (await signed.post("/ui/api/profile/revert", {"id": first["id"]})).json()
    assert reverted["text"] == "" and [c["op"] for c in reverted["history"]] == ["revert", "edit", "edit"]
    too_big = await signed.post("/ui/api/profile/save", {"text": "- " + "word " * 400})
    assert too_big.status == 409 and "400 tokens" in (await too_big.json())["error"]
    assert (await signed.post("/ui/api/profile/revert", {"id": "../../x"})).status == 400
    assert CANARY not in "\n".join(r.getMessage() for r in caplog.records)


async def test_runs_carry_the_timeline_with_sentences_only_while_logging_is_on(client):
    signed = await sign_in(client)
    ledger = daemon_of(client).ledger
    ledger.record("what was that", "A commit.")
    ledger.notice("git", 'a commit in strawberry: "Add websocket"', "Ooh.")
    timeline = (await (await signed.get("/ui/api/runs")).json())["timeline"]
    assert [e["kind"] for e in timeline] == ["notice", "turn"]               # newest first
    assert timeline[0]["about"] == 'a commit in strawberry: "Add websocket"' and timeline[0]["foreign"]
    assert "said" not in timeline[1] and timeline[1]["said_chars"] == len("what was that")
    daemon_of(client).config.learning.log_outcomes = True
    timeline = (await (await signed.get("/ui/api/runs")).json())["timeline"]
    assert timeline[1]["said"] == "what was that"


async def test_settings_show_the_effective_toml_what_applies_when_and_apply(client, monkeypatch):
    signed = await sign_in(client)
    settings = await (await signed.get("/ui/api/settings")).json()
    assert "[ledger]\nturns = 8" in settings["toml"] and "[notifications]" in settings["toml"]
    assert "persona.md" in settings["live"] and settings["persona_path"].endswith("persona.md")
    applied = await (await signed.post("/ui/api/apply", {})).json()
    assert applied == {"applied": "notifications", "body": "off"}

    def broken():
        raise ConfigError("bad")

    monkeypatch.setattr(daemon_of(client), "reload_notifications", broken)
    response = await signed.post("/ui/api/apply", {})
    assert response.status == 409 and "does not load" in (await response.json())["error"]


def test_the_page_has_the_new_groups_and_puts_text_in_as_text():
    ui = brainui.UI_DIR
    page = (ui / "index.html").read_text(encoding="utf-8")
    for group in ("Her", "Activity", "Learning", "Settings", "System"):
        assert f"<h3>{group}</h3>" in page
    for section in ("persona", "profile", "runs", "router", "learning", "data", "settings", "system"):
        assert f'data-section="{section}"' in page and f'<section id="{section}"' in page
    script = (ui / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script and "eval(" not in script
    assert 'id="attention"' in page
