"""The Brain UI's Config file and Tools tabs (configform.py, configui.py, WIRING.md §28): the form from config.py's
schema, edits that keep the file's comments, the startup checks, a file changed under the page, what loosens safety
and her card's yes for it, the secrets that never reach the page, and the tool servers' listing."""

from __future__ import annotations

import asyncio
import json
import logging
import tomllib
from pathlib import Path

import pytest

from strawberry_crab import brainui, configform, configui, paths
from strawberry_crab.config import _SECTIONS, Config, ConfigError, default_toml, load
from strawberry_crab.daemon import Daemon
from strawberry_crab.events import CannedReactor
from strawberry_crab.server import create_app
from strawberry_crab.tools import ToolSpec, Toolbox
from tests.test_brainui import quiet_config, sign_in
from tests.test_runs import until

SECRET = "sk-kestrel-0429"          # a token: it may be in config.toml, never on the page, in a diff or in a log
FILE = f"""\
# the user's own comment
[daemon]
port = 8770   # keep this one

[notifications]
body = "off"          # quiet

[tools.servers.notes]   # a server of the user's
command = "notes-mcp"
args = ["--api-key", "{SECRET}", "--verbose"]
env = {{ NOTES_TOKEN = "{SECRET}", NOTES_HOME = "/srv/notes" }}
flags = ["private"]
# api_key = "{SECRET}"
"""


def write_config(text: str) -> Path:
    path = paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ----------------------------------------------------------------------------- the schema


def test_the_form_has_every_section_and_key_of_config_py_with_its_default_kind_and_the_codes_hint():
    sections = configform.schema()
    assert [s["name"] for s in sections] == list(_SECTIONS)
    json.dumps(sections)                                                       # the page gets it as JSON
    by = {(s["name"], f["key"]): f for s in sections for f in s["fields"]}
    assert len(by) == sum(len(cls.__dataclass_fields__) for cls in _SECTIONS.values())
    assert by[("daemon", "port")] | {} == by[("daemon", "port")] and by[("daemon", "port")]["kind"] == "int"
    assert by[("daemon", "port")]["default"] == 8770 and by[("speech", "speed")]["kind"] == "float"
    assert by[("speech", "enabled")]["kind"] == "bool" and by[("voice", "model")]["kind"] == "str"
    assert by[("notifications", "body")]["kind"] == "choice" and by[("notifications", "body")]["choices"] == ["off", "react", "glance"]
    assert by[("gestures", "hands")]["choices"] == [1, 2] and by[("approvals", "hold")]["kind"] == "multi"
    assert by[("media", "ignore")]["kind"] == "list" and by[("notifications", "body_apps")]["kind"] == "map"
    assert by[("gate", "examples")]["kind"] == "json" and by[("brain", "keep_alive")]["kind"] == "duration"
    assert "resume from suspend" in by[("daemon", "warm_on_wake")]["hint"]                  # the comment on its line
    assert "<sentence, 23 chars>" in by[("daemon", "log_sentences")]["hint"]                # the comment above it
    assert "until a restart" in by[("voice", "fallback_model")]["hint"]                     # the lines carrying it on
    assert by[("tools", "servers")]["elsewhere"]["tab"] == "tools"
    assert by[("gestures", "enabled")]["elsewhere"]["tab"] == "input"
    assert by[("brain", "persona")]["deprecated"] is True
    live = {s["name"]: s["live"] for s in sections}
    assert live["notifications"] and live["gestures"] and live["daemon"] is None


# ----------------------------------------------------------------------------- edits keep the file


def test_an_edit_keeps_every_comment_and_the_order_and_a_reset_takes_the_key_out():
    text = configform.apply_edits(FILE, [{"key": "daemon.port", "value": 8790}, {"key": "speech.speed", "value": 2},
                                         {"key": "notifications.body", "reset": True}])
    assert "# the user's own comment" in text and "port = 8790   # keep this one" in text
    assert "[tools.servers.notes]   # a server of the user's" in text and f'# api_key = "{SECRET}"' in text
    assert text.index("[daemon]") < text.index("[notifications]") < text.index("[tools.servers.notes]")
    assert 'body = "off"' not in text and "speed = 2.0" in text                 # an int where a float goes
    loaded = load_text(text)
    assert loaded.daemon.port == 8790 and loaded.notifications.body == "off" and loaded.speech.speed == 2.0
    assert configform.apply_edits(FILE, [{"key": "daemon.port", "value": 8770}]) == FILE   # the same value: untouched


def load_text(text: str) -> Config:
    return configform.load_text(text)


def test_a_section_that_is_only_a_parent_gets_its_own_header_and_servers_are_set_whole():
    text = configform.apply_edits(FILE, [{"key": "tools.enabled", "value": False}])
    assert text.count("[tools]") == 1 and text.count("# a server of the user's") == 1
    assert load_text(text).tools.enabled is False and "notes" in load_text(text).tools.servers
    added = configform.apply_edits(text, [{"key": "tools.servers.recall",
                                           "value": {"topic": "notes", "url": "https://recall.example/mcp",
                                                     "workspaces": ["Garden"]}}])
    assert '[tools.servers.recall]\ntopic = "notes"\nurl = "https://recall.example/mcp"' in added
    server = load_text(added).tools.servers["recall"]
    assert server == {"topic": "notes", "url": "https://recall.example/mcp", "workspaces": ["Garden"]}
    edited = configform.apply_edits(added, [{"key": "tools.servers.notes",
                                             "value": {"command": "notes-mcp", "args": ["--verbose"], "flags": ["private"]}}])
    assert "[tools.servers.notes]   # a server of the user's" in edited and "env =" not in edited.split("[tools.servers.recall]")[0]
    gone = configform.apply_edits(edited, [{"key": "tools.servers.recall", "reset": True}])
    assert "recall" not in load_text(gone).tools.servers
    fresh = configform.apply_edits("", [{"key": "tools.servers.web", "value": {"command": "/usr/bin/npx",
                                                                               "args": ["-y", "mcp-searxng"]}}])
    assert load_text(fresh).tools.servers == {"web": {"command": "/usr/bin/npx", "args": ["-y", "mcp-searxng"]}}


@pytest.mark.parametrize("edits, words", [
    ([{"key": "nope.port", "value": 1}], "not a setting"),
    ([{"key": "daemon.nope", "value": 1}], "not a setting"),
    ([{"key": "gestures.enabled", "value": True}], "camera switch"),
    ([{"key": "tools.servers", "value": {}}], "the Tools page"),
    ([{"key": "tools.servers.bad name", "value": {"command": "x"}}], "letters, digits"),
    ([{"key": "tools.servers.x", "value": {"command": "x", "shell": "sh"}}], "unknown keys"),
    ([{"key": "daemon.port"}], "needs a value"),
    ("daemon.port", "a list"),
])
def test_an_edit_the_form_cannot_make_is_refused_with_the_reason(edits, words):
    with pytest.raises(ConfigError, match=words):
        configform.apply_edits(FILE, edits)


# ----------------------------------------------------------------------------- secrets


def test_secrets_are_hidden_and_come_back_and_a_hidden_value_cannot_be_moved_or_forged():
    vault = configform.Vault()
    masked = vault.mask(FILE + '\n[brain]\nollama_url = "https://u:pw@ollama.example/?token=abc"\n')
    assert SECRET not in masked and "pw@" not in masked and "/srv/notes" not in masked      # every env value
    assert '"--api-key", "<hidden 1: kept as is>", "--verbose"' in masked
    assert "# <hidden comment" in masked and "port = 8770   # keep this one" in masked
    assert vault.unmask(masked) == FILE + '\n[brain]\nollama_url = "https://u:pw@ollama.example/?token=abc"\n'
    assert vault.mask(FILE) == vault.mask(FILE)                                 # the same numbers every time
    with pytest.raises(ConfigError, match="not one this page showed"):
        vault.unmask(masked.replace("<hidden 1:", "<hidden 99:"))
    with pytest.raises(ConfigError, match="changed by hand"):
        vault.unmask(masked.replace("<hidden 1: kept as is>", "x<hidden 1: kept as is>"))
    with pytest.raises(ConfigError, match="not one this page showed"):
        configform.Vault().unmask(masked)                                       # another page's numbers
    for arg_list, hidden in ((["--token=abc"], [0]), (["--api-key", "abc"], [1]), (["-v", "abc"], []),
                             (["Authorization: Bearer abc"], [0]), (["--api-key", "--other"], [])):
        found = configform.secret_paths({"tools": {"servers": {"x": {"args": arg_list}}}})
        assert [p[-1] for p in found] == hidden, arg_list


# ----------------------------------------------------------------------------- what loosens safety

SERVER = '[tools.servers.notes]\ncommand = "notes-mcp"\nflags = ["private"]\n'
RECALL = '[tools.servers.recall]\nurl = "https://recall.example/mcp"\nworkspaces = ["Garden"]\n'
SPOTIFY = '[tools.servers.spotify]\ncommand = "spotify-mcp"\n'

LOWERING = [
    # (the file, the draft, the keys that loosen; [] for an edit that is neutral or raises safety)
    ("", "[speech]\nspeed = 1.5\n", []),
    ("", "[daemon]\nhost = \"0.0.0.0\"\n", ["daemon.host"]),
    ("", "[daemon]\nhost = \"localhost\"\n", []),
    ("", "[daemon]\nlog_sentences = true\n", ["daemon.log_sentences"]),
    ("[daemon]\nlog_sentences = true\n", "", []),
    ("", "[learning]\nlog_outcomes = true\n", ["learning.log_outcomes"]),
    ("[messages]\nenabled = false\n", "", ["messages.enabled"]),
    ("", "[brain]\nollama_url = \"http://10.0.0.5:11434\"\n", ["brain.ollama_url"]),
    ("", "[brain]\nollama_url = \"http://127.0.0.1:11500\"\n", []),
    ("", "[notifications]\nbody = \"react\"\n", ["notifications.body"]),
    ("[notifications]\nbody = \"glance\"\n", "[notifications]\nbody = \"react\"\n", []),
    ("", "[notifications]\nbody_apps = { Slack = \"glance\" }\n", ["notifications.body_apps.Slack"]),
    ("[notifications]\nbody_apps = { Slack = \"glance\" }\n", "", []),
    ("[tools]\nenabled = false\n", "", ["tools.enabled"]),
    ("", SERVER, ["tools.servers.notes"]),
    (SERVER, "", []),
    (SERVER, SERVER.replace("notes-mcp", "/tmp/other"), ["tools.servers.notes.command"]),
    (SERVER, SERVER + 'args = ["--all"]\n', ["tools.servers.notes.args"]),
    (SERVER, SERVER + 'env = { HOME = "/" }\n', ["tools.servers.notes.env"]),
    (SERVER, SERVER + 'cwd = "/"\n', ["tools.servers.notes.cwd"]),
    (SERVER, SERVER.replace('["private"]', '["private", "foreign"]'), []),
    (SERVER.replace('["private"]', '["private", "egress"]'), SERVER, ["tools.servers.notes.flags"]),
    (SERVER, SERVER.replace('flags = ["private"]\n', ""), []),                 # no flags: all three
    (SERVER.replace('flags = ["private"]\n', ""), SERVER, ["tools.servers.notes.flags"]),
    (SERVER, SERVER + 'adapter = "web"\n', ["tools.servers.notes.adapter"]),
    (SERVER + 'careful = ["wipe"]\n', SERVER, ["tools.servers.notes.careful"]),
    (SERVER + 'confirm = ["wipe"]\n', SERVER + "confirm = []\n", ["tools.servers.notes.confirm"]),
    (SPOTIFY, SPOTIFY + "confirm = []\n", ["tools.servers.spotify.confirm"]),  # the adapter's two removals
    (SPOTIFY, SPOTIFY + 'confirm = ["remove_saved_tracks", "remove_from_playlist", "save_tracks"]\n', []),
    (SERVER + 'offer = "asked"\n', SERVER + 'offer = "topic"\n', ["tools.servers.notes.offer"]),
    (SERVER, SERVER + 'offer = "asked"\n', []),
    (RECALL, RECALL.replace('["Garden"]', '["Garden", "Work"]'), ["tools.servers.recall.workspaces"]),
    (RECALL, RECALL.replace('["Garden"]', "[]"), []),
    (RECALL, RECALL.replace('workspaces = ["Garden"]\n', ""), []),             # none listed: none readable
    (RECALL, RECALL.replace("recall.example", "elsewhere.example"), ["tools.servers.recall.url"]),
    (SERVER, SERVER + '[approvals]\nrisk = { "notes.wipe" = "read" }\n', ["approvals.risk.notes.wipe"]),
    (SERVER, SERVER + '[approvals]\nrisk = { "notes" = "playback" }\n', ["approvals.risk.notes"]),
    (SERVER, SERVER + '[approvals]\nrisk = { "notes.wipe" = "destructive" }\n', []),
    (SERVER + '[approvals]\nrisk = { "notes.wipe" = "sends" }\n', SERVER, ["approvals.risk.notes.wipe"]),
    (SERVER + '[approvals]\nrisk = { "notes.peek" = "read" }\n', SERVER, []),
    (SPOTIFY, SPOTIFY + '[approvals]\nrisk = { "spotify.pause" = "playback" }\n', []),   # its adapter's own tier
    ("", '[approvals]\nhold = ["destructive"]\n', ["approvals.hold"]),
    ("", '[approvals]\nhold = ["change", "sends", "destructive"]\n', []),
    ("", "[approvals]\nchange_s = 5.0\n", []),
    ('[gestures]\nwatch = "armed"\n', "", ["gestures.watch"]),
    ("", '[gestures]\nwatch = "armed"\n', []),
    ("[gestures]\napprovals = false\n", "", ["gestures.approvals"]),
    ("", "[ledger]\nforeign_minutes = 2.0\n", ["ledger.foreign_minutes"]),
    ("", "[ledger]\nforeign_minutes = 20.0\n", []),
    ("", "[gestures]\nhands = 1\nfps = 10.0\n", []),
    # the audit's: the ledger (F2), gestures (F6), the cheap ones (F11)
    ("", "[ledger]\nnotices = 0\n", ["ledger.notices"]),
    ("", "[ledger]\nnotices = 3\n", []),
    ("", "[ledger]\nwindow_minutes = 5.0\n", ["ledger.foreign_minutes"]),
    ("[ledger]\nforeign_minutes = 30.0\n", "[ledger]\nforeign_minutes = 30.0\nwindow_minutes = 20.0\n",
     ["ledger.foreign_minutes"]),
    ("", "[gestures]\nrequire_facing = false\n", ["gestures.require_facing"]),
    ("[gestures]\narming = true\n", "", ["gestures.arming"]),
    ("", "[gestures]\nhold_ms = 300.0\n", ["gestures.hold_ms"]),
    ("", "[gestures]\nzone = 0.9\n", ["gestures.zone"]),
    ("", "[gestures]\nmin_size = 0.05\n", ["gestures.min_size"]),
    ("", "[gestures]\narmed_s = 20.0\n", ["gestures.armed_s"]),
    ("", "[gestures]\nhold_ms = 600.0\nzone = 0.6\nmin_size = 0.2\narmed_s = 4.0\n", []),
    ('[daemon]\nhost = "10.0.0.2"\n', '[daemon]\nhost = "0.0.0.0"\n', ["daemon.host"]),
    ("", '[daemon]\nhost = "::"\n', ["daemon.host"]),
    ("", '[daemon]\nlog_level = "DEBUG"\n', ["daemon.log_level"]),
    ('[daemon]\nlog_level = "DEBUG"\n', "", []),
    ("", "[notifications]\nignore_apps = []\n", ["notifications.ignore_apps"]),
    ('[notifications]\nonly_apps = ["Signal"]\n', "", ["notifications.only_apps"]),
    ('[notifications]\nonly_apps = ["Signal"]\n', '[notifications]\nonly_apps = ["Signal", "Slack"]\n',
     ["notifications.only_apps"]),
    ("", '[notifications]\nonly_apps = ["Signal"]\n', []),
    ("", "[notifications]\nmax_body_chars = 2000\n", ["notifications.max_body_chars"]),
    ("", "[approvals]\nsends_s = 60.0\n", ["approvals.sends_s"]),
]


@pytest.mark.parametrize("old, new, keys", LOWERING)
def test_the_classifier_names_each_edit_that_loosens_safety_and_nothing_else(old, new, keys):
    found = configform.lowering(load_text(old), load_text(new))
    assert [f.key for f in found] == keys
    assert all(f.why for f in found)


def test_the_camera_is_never_switched_from_the_editor():
    assert configform.refused(load_text(""), load_text("[gestures]\nenabled = true\n"))
    assert configform.refused(load_text("[gestures]\nenabled = true\n"), load_text(""))
    assert not configform.refused(load_text("[gestures]\nenabled = true\n"), load_text("[gestures]\nenabled = true\nfps = 10.0\n"))


# ----------------------------------------------------------------------------- the routes


@pytest.fixture
async def client(aiohttp_client):
    return await aiohttp_client(create_app(Daemon(reactor=CannedReactor(), config=quiet_config())))


def daemon_of(client) -> Daemon:
    return client.server.app[brainui.UI].daemon


def desk_of(client) -> configui.ConfigDesk:
    return client.server.app[configui.DESK]


POSTS = [("/ui/api/config/check", {"text": ""}), ("/ui/api/config/save", {"text": "", "base": "0" * 64})]


@pytest.mark.parametrize("path, body", POSTS)
async def test_every_config_write_needs_the_session_the_csrf_header_and_the_origin(client, path, body):
    signed = await sign_in(client)
    origin = f"http://127.0.0.1:{client.port}"
    assert (await client.post(path, json=body, headers={"Origin": origin})).status == 401
    assert (await signed.post(path, body, csrf=False)).status == 403
    assert (await signed.post(path, body, origin="http://127.0.0.1:1")).status == 403
    assert (await signed.post(path, body, origin=None)).status == 403
    for get in ("/ui/api/config", "/ui/api/tools"):
        assert (await signed.get(get, origin="https://evil.example")).status == 403
        assert (await signed.get(get, csrf=False)).status == 403
    assert not paths.config_file().exists()


async def test_the_page_never_gets_a_secret_and_a_save_keeps_them(client, caplog):
    caplog.set_level(logging.DEBUG)
    path = write_config(FILE)
    signed = await sign_in(client)
    view = await (await signed.get("/ui/api/config")).json()
    assert SECRET not in json.dumps(view) and view["exists"] and view["error"] is None
    assert view["values"]["daemon"]["port"] == 8770 and view["text"].startswith("# the user's own comment")
    draft = view["text"].replace("port = 8770", "port = 8790")
    checked = await (await signed.post("/ui/api/config/check", {"text": draft, "base": view["base"]["tag"]})).json()
    assert checked["ok"] and checked["lowering"] == [] and checked["restart"] == ["daemon"]
    assert "-port = 8770   # keep this one" in checked["diff"] and SECRET not in json.dumps(checked)
    saved = await signed.post("/ui/api/config/save", {"text": draft, "base": view["base"]["tag"]})
    assert saved.status == 200
    body = await saved.json()
    assert body["backup"] and SECRET not in json.dumps(body) and body["restart"] == ["daemon"]
    text = path.read_text()
    assert text == FILE.replace("port = 8770", "port = 8790")                  # the secrets kept, the rest as written
    assert Path(body["backup"]).read_text() == FILE
    form = await signed.post("/ui/api/config/check", {"text": body["text"], "edits": [
        {"key": "tools.servers.notes", "value": body["values"]["tools"]["servers"]["notes"] | {"args": ["--verbose"]}}]})
    form = await form.json()
    assert form["ok"] and SECRET not in json.dumps(form)
    assert [f["key"] for f in form["lowering"]] == ["tools.servers.notes.args"]
    assert SECRET not in caplog.text


async def test_a_draft_that_would_not_load_is_refused_with_the_startup_reason(client):
    path = write_config(FILE)
    signed = await sign_in(client)
    view = await (await signed.get("/ui/api/config")).json()
    for draft, words in ((view["text"] + "\n[speech]\nspeed = 9.0\n", "speech.speed must be between"),
                         (view["text"] + "\n[daemon\n", "parse"),
                         (view["text"].replace('body = "off"', 'body = "shout"'), "notifications.body must be"),
                         (view["text"] + "\n[gestures]\nenabled = true\n", "camera switch")):
        checked = await (await signed.post("/ui/api/config/check", {"text": draft})).json()
        assert not checked["ok"] and words in " ".join(checked["problems"]), draft
        reply = await signed.post("/ui/api/config/save", {"text": draft, "base": view["base"]["tag"]})
        assert reply.status == 400
    assert path.read_text() == FILE


async def test_a_file_changed_under_the_page_is_never_overwritten(client):
    path = write_config(FILE)
    signed = await sign_in(client)
    view = await (await signed.get("/ui/api/config")).json()
    path.write_text(FILE + "\n[speech]\nenabled = true\n", encoding="utf-8")   # the tray, an editor, the camera switch
    draft = view["text"].replace("port = 8770", "port = 8790")
    checked = await (await signed.post("/ui/api/config/check", {"text": draft, "base": view["base"]["tag"]})).json()
    assert checked["stale"] is True
    reply = await signed.post("/ui/api/config/save", {"text": draft, "base": view["base"]["tag"]})
    assert reply.status == 409 and (await reply.json())["stale"] is True
    assert "[speech]" in path.read_text() and "port = 8790" not in path.read_text()
    with pytest.raises(configform.Stale):
        configform.write(path, FILE, configform.tag(FILE))


async def test_a_new_file_starts_from_the_template_and_a_safe_change_saves_at_once_and_applies(client):
    signed = await sign_in(client)
    view = await (await signed.get("/ui/api/config")).json()
    assert view["exists"] is False and view["text"] == default_toml()
    checked = await (await signed.post("/ui/api/config/check", {"text": view["text"], "edits": [
        {"key": "notifications.body", "value": "off"}, {"key": "notifications.ignore_apps", "value": ["Spotify", "Steam"]}]})).json()
    assert checked["ok"] and checked["live"] == ["notifications"] and checked["lowering"] == []
    saved = await (await signed.post("/ui/api/config/save", {"text": checked["text"], "base": view["base"]["tag"]})).json()
    assert saved["backup"] is None and saved["applied"] == ["notifications"]
    assert daemon_of(client).config.notifications.ignore_apps == ["Spotify", "Steam"]       # used at once
    assert load(paths.config_file()).notifications.ignore_apps == ["Spotify", "Steam"]
    assert "# Strawberry settings." in paths.config_file().read_text()


async def test_a_change_that_loosens_safety_waits_for_a_yes_on_her_card_never_from_the_page(client, caplog):
    caplog.set_level(logging.DEBUG)
    path = write_config(FILE)
    signed = await sign_in(client)
    daemon, desk = daemon_of(client), desk_of(client)
    view = await (await signed.get("/ui/api/config")).json()
    draft = view["text"].replace('body = "off"', 'body = "glance"')
    reply = await signed.post("/ui/api/config/save", {"text": draft, "base": view["base"]["tag"]})
    assert reply.status == 202
    pending = (await reply.json())["pending"]
    assert pending["state"] == "waiting" and pending["lowering"][0]["key"] == "notifications.body"
    approval = daemon.approvals.open
    assert approval is not None and approval.approval_id == pending["approval_id"] and approval.hold is True
    assert approval.timeout_s == configui.CONFIRM_S and approval.run.source == "settings"
    assert approval.prompt == "Settings: less privacy (notifications.body)."
    assert path.read_text() == FILE                                             # nothing written yet
    # The page that asked cannot say yes; a thumb cannot either; a second save waits its turn.
    refused = await signed.post("/ui/api/approval", {"approval_id": approval.approval_id, "answer": "yes"})
    assert refused.status == 409 and (await refused.json())["reason"] == "not_here"
    assert daemon.gestures.answerable() in (None, {"yes": False, "no": True})
    again = await signed.post("/ui/api/config/save", {"text": draft, "base": view["base"]["tag"]})
    assert again.status == 409
    # A tap on her card is not a yes for a hold; a held yes is.
    assert daemon.approvals.answer(approval.approval_id, "yes", "body", hold=False) == "hold_required"
    assert daemon.approvals.answer(approval.approval_id, "yes", "body", hold=True) is None
    await until(lambda: desk.pending.state != "waiting")
    assert desk.pending.state == "saved" and approval.made
    assert 'body = "glance"' in path.read_text() and SECRET in path.read_text()
    assert daemon.config.notifications.body == "glance"                          # applied at once
    after = await (await signed.get("/ui/api/config")).json()
    assert after["pending"]["state"] == "saved" and after["pending"]["by"] == "body"
    assert approval.run.done and SECRET not in caplog.text


async def test_a_no_or_no_answer_or_a_file_changed_meanwhile_writes_nothing(client):
    path = write_config(FILE)
    signed = await sign_in(client)
    daemon, desk = daemon_of(client), desk_of(client)
    view = await (await signed.get("/ui/api/config")).json()
    draft = view["text"].replace("[daemon]\n", "[daemon]\nlog_sentences = true\n")
    assert (await signed.post("/ui/api/config/save", {"text": draft, "base": view["base"]["tag"]})).status == 202
    no = await signed.post("/ui/api/approval", {"approval_id": daemon.approvals.open.approval_id, "answer": "no"})
    assert no.status == 200                                                     # a no from the page is fine
    await until(lambda: desk.pending.state != "waiting")
    assert desk.pending.state == "left" and path.read_text() == FILE
    assert (await signed.post("/ui/api/config/save", {"text": draft, "base": view["base"]["tag"]})).status == 202
    path.write_text(FILE + "\n# edited meanwhile\n", encoding="utf-8")
    daemon.approvals.answer(daemon.approvals.open.approval_id, "yes", "body", hold=True)
    await until(lambda: desk.pending.state != "waiting")
    assert desk.pending.state == "changed" and "log_sentences" not in path.read_text()
    base = configform.tag(path.read_text())
    draft = (await (await signed.get("/ui/api/config")).json())["text"].replace("[daemon]\n", "[daemon]\nlog_sentences = true\n")
    assert (await signed.post("/ui/api/config/save", {"text": draft, "base": base})).status == 202
    daemon.approvals.config.change_s = 0.05
    desk.pending.approval.expires = 0.0                                         # her wait ran out
    await until(lambda: desk.pending.state != "waiting", timeout=5.0)
    assert desk.pending.state == "left" and desk.pending.detail == "no answer in time"
    assert "log_sentences" not in path.read_text()


# ----------------------------------------------------------------------------- the Tools page


async def test_the_tools_page_lists_each_server_its_tools_tiers_flags_and_logins_without_secrets(aiohttp_client):
    write_config(FILE + RECALL)
    config = load(paths.config_file())
    config.brain.enabled = config.speech.enabled = config.voice.enabled = config.gate.enabled = False
    config.thinker.enabled = config.actions.mpris = False
    config.tools.preconnect = False
    daemon = Daemon(reactor=CannedReactor(), config=config, toolbox=Toolbox(config.tools))
    notes = daemon.toolbox.servers["notes"]
    notes.listed = frozenset({"peek", "wipe"})
    notes.tools = [ToolSpec("notes", "peek", "look", {})]
    notes.destructive = frozenset({"wipe"})
    client = await aiohttp_client(create_app(daemon))
    signed = await sign_in(client)
    view = await (await signed.get("/ui/api/tools")).json()
    assert SECRET not in json.dumps(view)
    rows = {row["name"]: row for row in view["servers"]}
    assert rows["notes"]["kind"] == "command" and rows["notes"]["flags"] == ["private"]
    assert rows["notes"]["tools"] == [
        {"name": "peek", "tier": "change", "offered": True, "confirm": False, "careful": False, "destructive_hint": False},
        {"name": "wipe", "tier": "destructive", "offered": False, "confirm": False, "careful": False, "destructive_hint": True}]
    assert rows["recall"]["kind"] == "url" and rows["recall"]["adapter"] == "recall"
    assert rows["recall"]["login"] == {"command": "strawberry tools login recall", "saved": False}
    assert rows["recall"]["flags"] == ["egress", "foreign", "private"]
    assert any(row["builtin"] for row in view["servers"])                      # her own (the desktop, messages)
    assert view["file"]["notes"]["env"]["NOTES_TOKEN"].startswith("<hidden ")
    assert "notes" in view["options"]["topics"] and "recall" in view["options"]["adapters"]
    await daemon.close()


def test_the_page_has_config_and_tools_tabs_under_settings_and_puts_text_in_as_text():
    page = (brainui.UI_DIR / "index.html").read_text(encoding="utf-8")
    for name in ("config", "tools"):
        assert f'data-section="{name}"' in page and f'<section id="{name}"' in page
    assert page.index('data-section="settings"') < page.index('data-section="config"') < page.index('data-section="system"')
    script = (brainui.UI_DIR / "app.js").read_text(encoding="utf-8")
    assert "config/check" in script and "config/save" in script and '"tools"' in script
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script and "eval(" not in script
    assert 'a.yes_by && !a.yes_by.includes("ui")' in script          # no Yes button for a change the page asked for


# ----------------------------------------------------------------------------- the security review's cases


async def test_a_hidden_value_moved_into_a_plain_key_is_refused_and_a_typed_one_stays_hidden_everywhere(client, caplog):
    """F8: a placeholder goes back only where it was shown. A secret typed in full where nothing hides it by name (a
    command) is still hidden in the draft, the reason, her card, the Tools view and the log."""
    caplog.set_level(logging.DEBUG)
    write_config(FILE)
    signed = await sign_in(client)
    view = await (await signed.get("/ui/api/config")).json()
    hidden = view["values"]["tools"]["servers"]["notes"]["env"]["NOTES_TOKEN"]
    for draft in (view["text"] + f'\n[voice]\nvocabulary = ["{hidden}"]\n',              # a plain key
                  view["text"] + f'\n[touch]\nmusic.flick = "{hidden}"\n',               # a check that would quote it
                  view["text"] + f'\n[tools.servers.leak]\ncommand = "{hidden}"\n',      # a reason that would name it
                  view["text"] + f'\n[brain]\nollama_url = "{hidden}"\n'):
        checked = await (await signed.post("/ui/api/config/check", {"text": draft})).json()
        assert not checked["ok"] and "moved" in checked["problems"][0] and SECRET not in json.dumps(checked), draft
        assert (await signed.post("/ui/api/config/save", {"text": draft, "base": view["base"]["tag"]})).status == 400
    leak = view["text"] + f'\n[tools.servers.leak]\ncommand = "{SECRET}"\n'
    checked = await (await signed.post("/ui/api/config/check", {"text": leak})).json()
    assert SECRET not in json.dumps(checked)
    assert checked["lowering"][0]["key"] == "tools.servers.leak" and "<hidden" in checked["lowering"][0]["why"]
    reply = await signed.post("/ui/api/config/save", {"text": leak, "base": view["base"]["tag"]})
    assert reply.status == 202 and SECRET not in await reply.text()
    approval = daemon_of(client).approvals.open
    assert SECRET not in approval.prompt and SECRET not in json.dumps(approval.view(prompt=True))
    tools = await (await signed.get("/ui/api/tools")).json()
    assert SECRET not in json.dumps(tools)
    assert SECRET not in caplog.text
    vault = configform.Vault()
    vault.mask('[x]\ntoken = "a\\"b\\\\c-0429"\n')
    assert "0429" not in vault.scrub(repr('a"b\\c-0429') + json.dumps('a"b\\c-0429'))   # quoted forms too


def test_a_secret_in_a_trailing_comment_or_a_plain_value_is_hidden_and_put_back():
    text = FILE + f'\n[voice]\nlanguage = "en"   # was {SECRET}\nvocabulary = ["{SECRET}"]\n'
    vault = configform.Vault()
    masked = vault.mask(text)
    assert SECRET not in masked and vault.unmask(masked) == text
    assert vault.scrub(f"voice.x = {SECRET!r} is wrong") == "voice.x = '<hidden 1: kept as is>' is wrong"


@pytest.mark.parametrize("text", [
    f'# api_key = "{SECRET}"\n',                                                # a commented-out key
    f'# args = ["--api-key", "{SECRET}"]\n',                                     # a commented-out flag and value
    f'# env = {{ NOTES = "{SECRET}" }}\n',                                       # a commented-out env table
    '# Authorization: Bearer sk-kestrel-0429\n',
    f'[voice]\nlanguage = "en"   # the old key was {SECRET}\n',                  # a trailing comment
    f'[voice]\nlanguage = "en"   # ghp_{"a1" * 12}\n',                           # a token-like word
    f'[brain]\nollama_url = "http://127.0.0.1:11434"   # was https://u:{SECRET}@ollama.example\n',
    f'[tools.servers.x]\ncommand = "x"\nenv = {{ A = """\n{SECRET}\n""" }}\n',   # a multi-line string in an inline table
    f'[tools.servers.x]\ncommand = "x"\nargs = [\n  "--token",\n  "{SECRET}",\n]\n',                # a multi-line array
    f'[gate.examples]\n"kind.request" = ["a"]\n[[gate.secrets]]\npassword = "{SECRET}"\n',          # an array of tables
])
def test_secrets_in_comments_inline_tables_arrays_and_long_strings_are_hidden_and_come_back(text):
    vault = configform.Vault()
    masked = vault.mask(text)
    secret = SECRET if SECRET in text else "a1" * 12
    assert secret not in masked, masked
    back = vault.unmask(masked)
    assert tomllib.loads(back) == tomllib.loads(text)                           # the same values (a long string may be
    assert back == text or '"""' in text                                      # written as a plain one)


# TOML's forms where a secret can sit (the parser-differential review): each must be hidden as the parser reads it.
FORMS = [
    '"token" = "{s}"\n',                                            # a quoted key
    'a.token = "{s}"\n',                                            # a dotted key
    '"tok\\u0065n" = "{s}"\n',                                      # a key spelled with an escape
    "[x]\npassword = '{s}'\n",                                      # a literal string
    '[x]\npassword = """\n{s}\n"""\n',                              # a multi-line basic string
    "[x]\npassword = '''\n{s}'''\n",                                # a multi-line literal string
    '[x]\nsecrets = {{ token = "{s}", other = 1 }}\n',               # an inline table
    '[[x.creds]]\nauth = "{s}"\n[[x.creds]]\nauth = "{s}2"\n',       # an array of tables
    '[tools.servers.x]\ncommand = "c"\nargs = [\n  "--token", # the token is next\n  "{s}",\n]\n',  # on the next line
    '[x]\nnote = """ # not a comment\n"""   # api_key = "{s}"\n',    # a comment after a multi-line string
    '[x]\nnote = "a # b"   # token: {s}\n',                          # a # inside a string, then a comment
    '[x]\nnote = "say \\" # still a string"  # was {s}-0a9b8c7d\n',  # an escaped quote before a #
    '[x]\r\npassword = "{s}"\r\n# api_key = "{s}"\r\n',              # CRLF
    '[x]\nlines = [\n  "a",  # the token was {s}\n  "b",\n]\n',      # a comment inside a multi-line array
]


@pytest.mark.parametrize("form", FORMS)
def test_every_toml_form_of_a_secret_is_hidden_as_the_parser_reads_it_and_comes_back(form):
    text = form.format(s=SECRET)
    vault = configform.Vault()
    masked = vault.mask(text)
    assert SECRET not in masked, masked
    for path in configform.secret_paths(tomllib.loads(masked)):
        node = tomllib.loads(masked)
        for part in path:
            node = node[part]
        assert configform.HIDDEN.fullmatch(node)
    back = vault.unmask(masked)
    assert tomllib.loads(back) == tomllib.loads(text)
    assert back == text or "'" in text or '"""' in text                        # a restored string may be requoted


def test_random_files_of_those_forms_never_show_a_secret():
    import random

    rng = random.Random(4471)
    for _ in range(60):
        forms = rng.sample(FORMS[3:], k=3)
        secrets = [f"sk-{rng.randrange(10**8):08d}-{rng.choice('abcdef') * 6}" for _ in forms]
        text = "".join(f"[t{i}]\n" + form.format(s=secret).replace("[x]", f"[t{i}.x]").replace("[[x.", f"[[t{i}.x.")
                       .replace("[tools.servers.x]", f"[tools.servers.s{i}]") for i, (form, secret) in enumerate(zip(forms, secrets)))
        text = text.replace("\r\n", "\n")
        vault = configform.Vault()
        masked = vault.mask(text)
        assert not any(secret in masked for secret in secrets), masked
        assert tomllib.loads(vault.unmask(masked)) == tomllib.loads(text)


def test_numbers_under_a_secrets_name_tokens_in_paths_commands_and_plain_values_are_hidden_too():
    text = ('[x]\npin = 482916\npassword = 7731.5\ntool_tokens = 4000\n'
            '[brain]\nollama_url = "https://hooks.example/services/T0A1B2C3D/B0A1B2C3D/abcdEFGH1234ijkl"\n'
            f'[tools.servers.s]\ncommand = "curl -H Authorization:Bearer {SECRET}"\ntopic = "ghp_{"a1" * 12}"\n')
    vault = configform.Vault()
    masked = vault.mask(text)
    for secret in ("482916", "7731.5", "abcdEFGH1234ijkl", SECRET, "a1" * 12):
        assert secret not in masked, secret
    assert "tool_tokens = 4000" in masked                                       # a count, not a secret
    back = tomllib.loads(vault.unmask(masked))
    assert back["x"]["pin"] == 482916 and isinstance(back["x"]["pin"], int) and back == tomllib.loads(text)


def test_a_hidden_comment_moved_into_a_value_or_a_secret_in_a_key_is_refused():
    vault = configform.Vault()
    masked = vault.mask(f'[voice]\nlanguage = "en"   # api_key = "{SECRET}"\n')
    sentinel = configform.HIDDEN_COMMENT.search(masked).group(0)
    with pytest.raises(ConfigError, match="moved out of a comment"):
        vault.unmask(f'[voice]\nlanguage = "{sentinel}"\n')
    vault.mask(f'[x]\ntoken = "{SECRET}"\n')
    with pytest.raises(ConfigError, match="cannot be shown here safely"):
        vault.mask(f'[x]\n"{SECRET}" = 1\n')                                   # a known secret as a key's name


def test_a_hidden_trailing_comment_leaves_its_key_editable_and_the_template_shows_whole():
    vault = configform.Vault()
    text = f'[voice]\nlanguage = "en"   # was {SECRET}\n'
    masked = vault.mask(text)
    assert masked.startswith('[voice]\nlanguage = "en"   # <hidden comment')
    edited = vault.unmask(masked.replace('"en"', '"fi"'))
    assert edited == text.replace('"en"', '"fi"')
    template = default_toml()
    assert "<hidden" not in configform.Vault().mask(template)                   # its examples are no secrets


async def test_the_settings_tab_hides_a_token_in_arguments_or_an_address_too(aiohttp_client):
    config = quiet_config()
    config.tools.servers = {"notes": {"command": "notes-mcp", "args": ["--api-key", SECRET],
                                      "url_note": f"https://u:{SECRET}@x.example"}}
    client = await aiohttp_client(create_app(Daemon(reactor=CannedReactor(), config=config)))
    signed = await sign_in(client)
    settings = await (await signed.get("/ui/api/settings")).text()
    assert SECRET not in settings and "notes-mcp" in settings


def test_a_placeholder_is_never_written(tmp_path):
    path = tmp_path / "config.toml"
    for text in ('[voice]\nlanguage = "<hidden 1: kept as is>"\n', "[voice]\n# <hidden comment 2: kept as is>\n"):
        with pytest.raises(ConfigError, match="placeholder"):
            configform.write(path, text, configform.tag(""))
    assert not path.exists()


async def test_a_loosening_edit_bundled_with_neutral_ones_still_asks(client):
    path = write_config(FILE)
    signed = await sign_in(client)
    view = await (await signed.get("/ui/api/config")).json()
    checked = await (await signed.post("/ui/api/config/check", {"text": view["text"], "edits": [
        {"key": "speech.speed", "value": 1.2}, {"key": "voice.language", "value": "en"},
        {"key": "approvals.hold", "value": ["destructive"]}]})).json()
    assert [f["key"] for f in checked["lowering"]] == ["approvals.hold"]
    reply = await signed.post("/ui/api/config/save", {"text": checked["text"], "base": view["base"]["tag"]})
    assert reply.status == 202 and path.read_text() == FILE


async def test_only_the_text_she_asked_about_is_written(client):
    path = write_config(FILE)
    signed = await sign_in(client)
    daemon, desk = daemon_of(client), desk_of(client)
    view = await (await signed.get("/ui/api/config")).json()
    draft = view["text"].replace('body = "off"', 'body = "react"')
    assert (await signed.post("/ui/api/config/save", {"text": draft, "base": view["base"]["tag"]})).status == 202
    desk.pending.text = desk.pending.text.replace("[daemon]\n", "[daemon]\nhost = \"0.0.0.0\"\n")   # swapped meanwhile
    daemon.approvals.answer(daemon.approvals.open.approval_id, "yes", "body", hold=True)
    await until(lambda: desk.pending.state != "waiting")
    assert desk.pending.state == "failed" and path.read_text() == FILE


async def test_a_broken_file_is_compared_with_what_she_runs_not_with_the_defaults(aiohttp_client):
    config = quiet_config()
    config.messages.enabled = False
    client = await aiohttp_client(create_app(Daemon(reactor=CannedReactor(), config=config)))
    write_config("[messages]\nenabled = false\n[daemon\n")                        # broken under her
    signed = await sign_in(client)
    checked = await (await signed.post("/ui/api/config/check", {"text": "[speech]\nspeed = 1.2\n"})).json()
    assert "messages.enabled" in [f["key"] for f in checked["lowering"]]


async def test_the_input_tab_asks_on_her_card_before_a_thumb_may_answer_it(aiohttp_client):
    from strawberry_crab import inputs
    from tests.test_gestures import gesture_daemon

    daemon, _ = gesture_daemon(approvals=False)
    client = await aiohttp_client(create_app(daemon))
    signed = await sign_in(client)
    settings = (await (await signed.get("/ui/api/input")).json())["settings"]
    calm = {**settings, "gestures": {**settings["gestures"], "hold_ms": 500}}
    assert (await signed.post("/ui/api/input/save", {"settings": calm})).status == 200          # neutral: at once
    loose = {**calm, "gestures": {**calm["gestures"], "approvals": True}}
    reply = await signed.post("/ui/api/input/save", {"settings": loose})
    assert reply.status == 202 and (await reply.json())["pending"]["file"] == "input.toml"
    assert "approvals = false" in inputs.input_file().read_text() and daemon.config.gestures.approvals is False
    refused = await signed.post("/ui/api/approval", {"approval_id": daemon.approvals.open.approval_id, "answer": "yes"})
    assert refused.status == 409
    daemon.approvals.answer(daemon.approvals.open.approval_id, "yes", "body", hold=True)
    desk = client.server.app[configui.DESK]
    await until(lambda: desk.pending.state != "waiting")
    assert desk.pending.state == "saved" and daemon.config.gestures.approvals is True
    assert "approvals = true" in inputs.input_file().read_text()
    await daemon.close()


# ----------------------------------------------------------------------------- the audit's findings


async def test_a_settings_change_never_asks_over_another_question_and_takes_only_a_held_yes_on_her_card(client):
    """F1: a yes meant for another card cannot approve a settings change. While she asks anything else a loosening
    save is refused (it would supersede that question); her own settings question takes a hold on her card only."""
    from strawberry_crab.approvals import CARD_ONLY
    from strawberry_crab.confirm import Held
    from strawberry_crab.events import Event

    path = write_config(FILE)
    signed = await sign_in(client)
    daemon, desk = daemon_of(client), desk_of(client)
    view = await (await signed.get("/ui/api/config")).json()
    draft = view["text"].replace('body = "off"', 'body = "react"')
    run = daemon.runs.start("typed")
    other = daemon.approvals.request(run, Held("notes", "wipe", {}, question="Wipe? Say yes.", risk="destructive"))
    busy = await signed.post("/ui/api/config/save", {"text": draft, "base": view["base"]["tag"]})
    assert busy.status == 409 and (await busy.json())["busy"] and daemon.approvals.open is other
    neutral = view["text"].replace("port = 8770", "port = 8771")
    assert (await signed.post("/ui/api/config/save", {"text": neutral, "base": view["base"]["tag"]})).status == 200
    daemon.approvals.resolve(other, "no")
    daemon.runs.finish(run)
    view = await (await signed.get("/ui/api/config")).json()
    draft = view["text"].replace('body = "off"', 'body = "react"')
    assert (await signed.post("/ui/api/config/save", {"text": draft, "base": view["base"]["tag"]})).status == 202
    approval = daemon.approvals.open
    for by in ("voice", "typed", "ui", "gesture"):
        assert daemon.approvals.answer(approval.approval_id, "yes", by) == "not_here"
    performance, _sent = await daemon.handle_voice(Event(source="voice", title="yes"))
    assert performance.text == CARD_ONLY and approval.open and 'body = "off"' in path.read_text()
    assert daemon.approvals.answer(approval.approval_id, "yes", "body", hold=True) is None
    await until(lambda: desk.pending.state != "waiting")
    assert desk.pending.state == "saved" and 'body = "react"' in path.read_text()


async def test_an_error_while_she_waits_never_leaves_the_page_saying_waiting(client, monkeypatch):
    """F12."""
    write_config(FILE)
    signed = await sign_in(client)
    daemon, desk = daemon_of(client), desk_of(client)

    async def broken(*_a, **_k):
        raise RuntimeError("the speaker fell over")

    monkeypatch.setattr(daemon, "perform", broken)
    view = await (await signed.get("/ui/api/config")).json()
    draft = view["text"].replace('body = "off"', 'body = "react"')
    assert (await signed.post("/ui/api/config/save", {"text": draft, "base": view["base"]["tag"]})).status == 202
    await until(lambda: desk.pending.state != "waiting")
    assert desk.pending.state == "failed" and daemon.approvals.open is None


async def test_the_page_gets_a_keyed_tag_never_the_files_plain_hash(client):
    """F9."""
    write_config(FILE)
    signed = await sign_in(client)
    view = await (await signed.get("/ui/api/config")).json()
    tools = await (await signed.get("/ui/api/tools")).json()
    assert configform.sha256(FILE) not in json.dumps(view) + json.dumps(tools)
    assert view["base"]["tag"] == configform.tag(FILE) == tools["base"]["tag"] and "sha256" not in view["base"]
    with pytest.raises(configform.Stale):
        configform.write(paths.config_file(), FILE, configform.sha256(FILE))   # the plain hash is not a tag


@pytest.mark.linux_only
def test_config_toml_and_its_backups_are_the_users_alone_and_ten_are_kept(tmp_path):
    """F10: 0600 for the file and each backup from the first byte, no temporary file left, the last ten backups."""
    import os
    import stat

    path = tmp_path / "config.toml"
    path.write_text(FILE, encoding="utf-8")
    os.chmod(path, 0o644)
    text = FILE
    made = []
    for i in range(13):
        new = FILE.replace("8770", str(8700 + i))
        made.append(configform.write(path, new, configform.tag(text)))
        text = new
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.read_text() == text
    backups = sorted(tmp_path.glob("config.toml.bak-*"))
    assert len(backups) == configform.KEEP_BACKUPS and all(stat.S_IMODE(b.stat().st_mode) == 0o600 for b in backups)
    assert made[-1] in backups and not list(tmp_path.glob(".config.toml.*.tmp"))



def test_tiers_follow_the_servers_own_precedence_with_its_destructive_tools():
    """F5: a whole-server entry never takes a tool below its destructiveHint or its adapter's tier; a per-tool entry is
    taken as written; each effective lowering is named."""
    known = {"notes": {"listed": {"wipe", "peek"}, "destructive": {"wipe"}}}
    whole = load_text(SERVER + '[approvals]\nrisk = { "notes" = "read" }\n')
    found = [f.key for f in configform.lowering(load_text(SERVER), whole, known)]
    assert found == ["approvals.risk.notes", "approvals.risk.notes.peek"]          # wipe stays destructive
    assert configform.tier(whole, "notes", "wipe", frozenset({"wipe"})) == "destructive"
    own = load_text(SERVER + '[approvals]\nrisk = { "notes.wipe" = "read" }\n')
    assert [f.key for f in configform.lowering(load_text(SERVER), own, known)] == ["approvals.risk.notes.wipe"]
    spotify = load_text(SPOTIFY + '[approvals]\nrisk = { "spotify" = "read" }\n')
    keys = [f.key for f in configform.lowering(load_text(SPOTIFY), spotify, {})]
    assert "approvals.risk.spotify" in keys and "approvals.risk.spotify.pause" not in keys   # its own playback stays


async def test_input_toml_laid_over_is_read_too_and_a_change_that_would_break_it_is_refused(client):
    """F4: what she runs with is config.toml with input.toml over [gestures] and [touch]."""
    from strawberry_crab import inputs

    path = write_config('[tools.servers.spotify]\ncommand = "x"\n')
    inputs.input_file().write_text('[touch]\nmusic.flick = "skip"\n', encoding="utf-8")
    signed = await sign_in(client)
    view = await (await signed.get("/ui/api/config")).json()
    breaks = view["text"] + 'confirm = ["next"]\n'
    checked = await (await signed.post("/ui/api/config/check", {"text": breaks})).json()
    assert not checked["ok"] and "input.toml stop checking out" in " ".join(checked["problems"])
    assert (await signed.post("/ui/api/config/save", {"text": breaks, "base": view["base"]["tag"]})).status == 400
    assert "confirm" not in path.read_text()
    # A loosening in input.toml's own [gestures] is moot in config.toml, and one in config.toml that input.toml
    # does not cover is still caught.
    inputs.input_file().write_text("[gestures]\nrequire_facing = true\n", encoding="utf-8")
    shadowed = await (await signed.post("/ui/api/config/check", {"text": view["text"] + "\n[gestures]\nrequire_facing = false\n"})).json()
    assert "gestures.require_facing" in [f["key"] for f in shadowed["lowering"]]    # the file alone is read too


def test_her_card_names_every_kind_most_severe_first_with_the_command_cut_to_fit():
    """F3."""
    from strawberry_crab.configui import CARD_MAX, _card_line

    findings = configform.lowering(load_text(""), load_text(
        '[daemon]\nlog_sentences = true\n[approvals]\nhold = []\n[gestures]\nzone = 0.95\n'
        '[tools.servers.x]\ncommand = "/opt/x/bin/x-mcp"\nargs = ["--root", "/", "--yes\\u0007"]\n'
        '[brain]\nollama_url = "https://gpu.example:11434"\n'))
    assert [f.category for f in findings][:2] == ["runs", "egress"]
    line = _card_line(findings)
    assert line.startswith("Settings: runs /opt/x/bin/x-mcp --root / --yes; reaches gpu.example")
    for words in ("fewer approvals (approvals.hold)", "less privacy (daemon.log_sentences)", "looser gestures"):
        assert words in line, line
    assert "\x07" not in line and len(line) <= CARD_MAX
    many = [configform.Finding(f"tools.servers.s{i}", "x", "runs", f"/bin/server-{i} " + "--flag " * 20) for i in range(9)]
    long = _card_line(many + [configform.Finding("daemon.log_level", "x", "privacy")])
    assert len(long) <= CARD_MAX and long.startswith("Settings: runs /bin/server-0") and "less privacy" in long
