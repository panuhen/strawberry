"""The notification doorway: parsing, filtering, coalescing, and one real D-Bus message.

Nothing here needs a bus. The Notify case is built as a jeepney message, serialised and
parsed back, so the variant unwrapping is exercised exactly as it is on the session bus.
"""

import asyncio

import pytest
from jeepney import DBusAddress, Parser, new_method_call

from strawberry_crab.config import NotificationsConfig
from strawberry_crab.doorways import notify_watch

parse_notify, allowed, to_event, summarise, clean = (
    notify_watch.parse_notify, notify_watch.allowed, notify_watch.to_event, notify_watch.summarise, notify_watch.clean
)

NOTIFICATIONS = DBusAddress("/org/freedesktop/Notifications", bus_name="org.freedesktop.Notifications",
                            interface="org.freedesktop.Notifications")


def notify(app="WhatsApp", summary="James", body="Are we still on for tonight?", urgency=1, replaces=0, **hints):
    h = {"urgency": urgency, **hints}
    return (app, replaces, "", summary, body, [], h, -1)


def bus_notify(app="WhatsApp", summary="James", body="Are we still on?", urgency=1, replaces=0,
               icon="", desktop_entry="whatsapp", category="im.received"):
    """A Notify method call as it really goes past a monitor connection: serialised, then parsed."""
    hints = {"urgency": ("y", urgency), "desktop-entry": ("s", desktop_entry), "category": ("s", category)}
    message = new_method_call(NOTIFICATIONS, "Notify", "susssasa{sv}i",
                              (app, replaces, icon, summary, body, [], hints, -1))
    parser = Parser()
    parser.add_data(message.serialise(serial=42))
    return parser.get_next_message()


def test_a_real_notify_message_becomes_an_event():
    message = bus_notify()
    assert notify_watch.is_call(message, "org.freedesktop.Notifications", "Notify")
    n = parse_notify(message.body)
    assert n == {
        "app": "WhatsApp", "desktop_entry": "whatsapp", "title": "James", "body": "Are we still on?",
        "urgency": "normal", "category": "im.received", "replaces_id": 0, "app_icon": "",
    }
    assert to_event(n, NotificationsConfig()) == {
        "source": "notification", "app": "WhatsApp", "title": "James", "urgency": "normal", "category": "im.received",
    }
    assert to_event(n, NotificationsConfig(body="react"))["body"] == "Are we still on?"


def test_the_watcher_batches_and_posts_what_it_hears(monkeypatch):
    """One notification in, one /event out, with the coalescing window shortened."""
    posted = []
    cfg = NotificationsConfig(coalesce_s=0.01)
    watcher = notify_watch.Watcher("http://127.0.0.1:1", cfg)

    async def fake_post(path, payload):
        posted.append((path, payload))
        return True

    monkeypatch.setattr(watcher.daemon, "post", fake_post)
    monkeypatch.setattr(notify_watch, "resolve_icon", lambda *a, **k: None)

    async def run():
        await watcher.handle(bus_notify(summary="James", body="Pub?"))
        await watcher.handle(bus_notify(summary="James", body="Pub?"))   # the relay's copy
        await watcher.handle(bus_notify(app="Slack", summary="#general", body="deploy done"))
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert len(posted) == 1
    path, payload = posted[0]
    assert path == "/event"
    assert payload["title"] == "2 notifications" and payload["app"] == "several apps"
    assert watcher.seen == 3


def test_desktop_icon_name_reads_the_desktop_file(tmp_path):
    apps = tmp_path / "applications"
    apps.mkdir()
    (apps / "whatsapp.desktop").write_text(
        "[Desktop Entry]\nName=WhatsApp\nIcon=whatsapp\n\n[Desktop Action new]\nIcon=other\n")
    assert notify_watch.desktop_icon_name("whatsapp", dirs=[apps]) == "whatsapp"
    assert notify_watch.desktop_icon_name("whatsapp.desktop", dirs=[apps]) == "whatsapp"
    assert notify_watch.desktop_icon_name("missing", dirs=[apps]) is None
    assert notify_watch.desktop_icon_name("", dirs=[apps]) is None


def test_parse_maps_the_notify_tuple():
    n = parse_notify(notify(category="im.received", **{"desktop-entry": "whatsapp"}))
    assert n == {
        "app": "WhatsApp", "desktop_entry": "whatsapp", "title": "James", "body": "Are we still on for tonight?",
        "urgency": "normal", "category": "im.received", "replaces_id": 0, "app_icon": "",
    }


def test_event_carries_category_and_icon_when_present():
    n = parse_notify(notify(category="im.received"))
    n["icon"] = "/tmp/whatsapp.png"
    event = to_event(n, NotificationsConfig())
    assert event["category"] == "im.received" and event["icon"] == "/tmp/whatsapp.png"
    assert "icon" not in to_event(parse_notify(notify()), NotificationsConfig())


@pytest.mark.linux_only     # POSIX icon paths and the XDG icon theme; Windows toasts carry their own
def test_resolve_icon_prefers_a_path_then_walks_the_theme(tmp_path):
    resolve_icon = notify_watch.resolve_icon
    direct = tmp_path / "direct.png"
    direct.write_bytes(b"x")
    assert resolve_icon(str(direct), "", "", search_dirs=[]) == str(direct)
    assert resolve_icon(f"file://{direct}", "", "", search_dirs=[]) == str(direct)
    apps = tmp_path / "hicolor" / "scalable" / "apps"
    apps.mkdir(parents=True)
    (apps / "whatsapp.svg").write_bytes(b"<svg/>")
    (apps / "com.slack.Slack.png").write_bytes(b"x")
    dirs = [apps]
    assert resolve_icon("whatsapp", "", "", search_dirs=dirs) == str(apps / "whatsapp.svg")
    assert resolve_icon("", "com.slack.Slack", "Slack", search_dirs=dirs) == str(apps / "com.slack.Slack.png")
    assert resolve_icon("", "", "WhatsApp", search_dirs=dirs) == str(apps / "whatsapp.svg")  # lower-cased app name
    assert resolve_icon("", "org.example.Nothing", "Nothing", search_dirs=dirs) is None
    # the .desktop file's declared icon wins over the id
    assert resolve_icon("", "org.slack", "x", search_dirs=dirs, desktop_icon=lambda de: "whatsapp") == str(apps / "whatsapp.svg")
    # snaps declare a full path in Icon=; use it directly (and only if it exists)
    assert resolve_icon("", "spotify_spotify", "Spotify", search_dirs=dirs, desktop_icon=lambda de: str(direct)) == str(direct)
    assert resolve_icon("", "spotify_spotify", "Spotify", search_dirs=dirs, desktop_icon=lambda de: "/gone/icon.png") is None


@pytest.mark.parametrize("raw, name", [(0, "low"), (1, "normal"), (2, "critical"), (7, "normal"), ("x", "normal")])
def test_urgency_byte_to_name(raw, name):
    assert parse_notify(notify(urgency=raw))["urgency"] == name


def test_missing_hints_and_empty_app_fall_back():
    n = parse_notify(("", 0, "", "Hi", "", [], {"desktop-entry": "org.telegram.desktop"}, -1))
    assert n["app"] == "org.telegram.desktop"
    assert n["urgency"] == "normal"


def test_clean_strips_markup_entities_and_truncates():
    assert clean("<b>Bold</b> &amp; <i>italic</i>\n\nnew   line", 100) == "Bold & italic new line"
    assert len(clean("x" * 500, 200)) == 200
    assert parse_notify(notify(body="<b>hi</b>"))["body"] == "hi"


def test_default_config_ignores_spotify_and_replacements():
    cfg = NotificationsConfig()
    assert allowed(parse_notify(notify()), cfg) is None
    assert "ignore_apps" in allowed(parse_notify(notify(app="Spotify", summary="Track")), cfg)
    assert "replaces" in allowed(parse_notify(notify(replaces=42)), cfg)
    assert "own" in allowed(parse_notify(notify(app="strawberry")), cfg)


def test_only_apps_and_urgency_floor():
    cfg = NotificationsConfig(only_apps=["Slack"], min_urgency="normal")
    assert "only_apps" in allowed(parse_notify(notify(app="WhatsApp")), cfg)
    assert allowed(parse_notify(notify(app="slack")), cfg) is None  # case-insensitive
    assert "below" in allowed(parse_notify(notify(app="Slack", urgency=0)), cfg)


def test_body_off_by_default_keeps_who_but_not_what():
    n = parse_notify(notify())
    private = to_event(n, NotificationsConfig())
    assert "body" not in private
    assert private["title"] == "James" and private["app"] == "WhatsApp"
    for mode in ("react", "glance"):
        assert to_event(n, NotificationsConfig(body=mode))["body"] == "Are we still on for tonight?"


def test_body_apps_override_the_default_by_app_or_desktop_entry():
    cfg = NotificationsConfig(body="off", body_apps={"slack": "glance", "org.telegram.desktop": "react"})
    assert "body" in to_event(parse_notify(notify(app="Slack")), cfg)
    assert "body" in to_event(parse_notify(notify(app="", **{"desktop-entry": "org.telegram.desktop"})), cfg)
    assert "body" not in to_event(parse_notify(notify(app="WhatsApp")), cfg)
    loud = NotificationsConfig(body="react", body_apps={"WhatsApp": "off"})
    assert "body" not in to_event(parse_notify(notify(app="whatsapp")), loud)
    assert "body" in to_event(parse_notify(notify(app="Slack")), loud)


def test_the_watcher_never_posts_or_logs_an_off_body(monkeypatch, caplog):
    """parse_notify -> forward decision, end to end in the watcher: mode off, the body stays here."""
    posted = []
    watcher = notify_watch.Watcher("http://127.0.0.1:1", NotificationsConfig(coalesce_s=0.01, body_apps={"Slack": "react"}))

    async def fake_post(path, payload):
        posted.append(payload)
        return True

    monkeypatch.setattr(watcher.daemon, "post", fake_post)
    monkeypatch.setattr(notify_watch, "resolve_icon", lambda *a, **k: None)

    async def run():
        await watcher.handle(bus_notify(app="WhatsApp", summary="James", body="canary-off-7f3a"))
        await asyncio.sleep(0.05)
        await watcher.handle(bus_notify(app="Slack", summary="Alex", body="canary-react-9c1d"))
        await asyncio.sleep(0.05)

    with caplog.at_level("DEBUG"):
        asyncio.run(run())
    assert "body" not in posted[0] and posted[0]["title"] == "James"
    assert posted[1]["body"] == "canary-react-9c1d"
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "canary" not in text and "body_len=15" in text


def test_long_bodies_are_cut_at_a_word_boundary():
    body = "word " * 300
    n = parse_notify(notify(body=body), max_body_chars=1000)
    assert len(n["body"]) <= 1000 and n["body"].endswith("word…")
    assert parse_notify(notify(body=body))["body"] == n["body"]           # 1000 is the default
    assert clean("the quick brown fox jumps", 18) == "the quick brown…"
    assert clean("short enough", 18) == "short enough"
    assert clean("supercalifragilistic", 10) == "supercali…"               # no space: a hard cut


def test_deduper_drops_the_relay_copy_but_not_a_later_repeat():
    d = notify_watch.Deduper(window_s=1.5)
    n = parse_notify(notify())
    assert d.seen(n, now=10.0) is False
    assert d.seen(dict(n), now=10.2) is True      # the relay's copy, same content
    assert d.seen(parse_notify(notify(body="different")), now=10.3) is False
    assert d.seen(dict(n), now=12.0) is False     # James really did write again


def test_single_notification_passes_through_unsummarised():
    cfg = NotificationsConfig(body="react")
    assert summarise([parse_notify(notify())], cfg) == {
        "source": "notification", "app": "WhatsApp", "title": "James", "body": "Are we still on for tonight?", "urgency": "normal",
    }


def test_burst_from_one_app_becomes_one_event():
    cfg = NotificationsConfig()
    batch = [parse_notify(notify(app="Slack", summary=f"#chan{i}", body="x")) for i in range(7)]
    batch[2]["icon"] = "/tmp/slack.png"
    event = summarise(batch, cfg)
    assert event["app"] == "Slack"
    assert event["title"] == "7 notifications from Slack"
    assert event["body"] == "#chan0 · #chan1 · #chan2 · #chan3 · #chan4 · and 2 more"
    assert event["icon"] == "/tmp/slack.png"


def test_burst_across_apps_keeps_the_highest_urgency():
    cfg = NotificationsConfig()
    batch = [parse_notify(notify(app="WhatsApp", summary="James")), parse_notify(notify(app="Power", summary="Battery low", urgency=2))]
    event = summarise(batch, cfg)
    assert event["app"] == "several apps"
    assert event["title"] == "2 notifications"
    assert event["body"] == "WhatsApp: James · Power: Battery low"
    assert event["urgency"] == "critical"


def test_the_match_rule_is_the_one_the_bus_needs():
    assert notify_watch.MATCH_RULE.serialise() == (
        "interface='org.freedesktop.Notifications',member='Notify',type='method_call'")


def test_main_reads_the_config_file_it_is_given(tmp_path, monkeypatch):
    """The tray passes its --config here, so "Message bodies" and the watcher read one file."""
    seen = []

    async def fake_watch(daemon, cfg):
        seen.append((daemon, cfg.body))
        return 0

    monkeypatch.setattr(notify_watch, "watch", fake_watch)
    config = tmp_path / "c.toml"
    config.write_text('[daemon]\nport = 8779\n[notifications]\nbody = "glance"\n')
    assert notify_watch.main(["--config", str(config)]) == 0
    assert notify_watch.main(["--config", str(config), "--daemon", "http://127.0.0.1:1"]) == 0
    assert seen == [("http://127.0.0.1:8779", "glance"), ("http://127.0.0.1:1", "glance")]
