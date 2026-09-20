"""The notification doorway's pure parts: parsing, filtering, coalescing (no bus needed)."""

import importlib.util
import sys
from pathlib import Path

import pytest

from strawberryd.config import NotificationsConfig

_spec = importlib.util.spec_from_file_location(
    "notify_watch", Path(__file__).resolve().parents[2] / "doorways" / "notify_watch.py"
)
notify_watch = importlib.util.module_from_spec(_spec)
sys.modules["notify_watch"] = notify_watch
_spec.loader.exec_module(notify_watch)

parse_notify, allowed, to_event, summarise, clean = (
    notify_watch.parse_notify, notify_watch.allowed, notify_watch.to_event, notify_watch.summarise, notify_watch.clean
)


def notify(app="WhatsApp", summary="James", body="Are we still on for tonight?", urgency=1, replaces=0, **hints):
    h = {"urgency": urgency, **hints}
    return (app, replaces, "", summary, body, [], h, -1)


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


def test_include_body_false_keeps_who_but_not_what():
    n = parse_notify(notify())
    assert to_event(n, NotificationsConfig())["body"] == "Are we still on for tonight?"
    private = to_event(n, NotificationsConfig(include_body=False))
    assert "body" not in private
    assert private["title"] == "James" and private["app"] == "WhatsApp"


def test_deduper_drops_the_relay_copy_but_not_a_later_repeat():
    d = notify_watch.Deduper(window_s=1.5)
    n = parse_notify(notify())
    assert d.seen(n, now=10.0) is False
    assert d.seen(dict(n), now=10.2) is True      # the relay's copy, same content
    assert d.seen(parse_notify(notify(body="different")), now=10.3) is False
    assert d.seen(dict(n), now=12.0) is False     # James really did write again


def test_single_notification_passes_through_unsummarised():
    cfg = NotificationsConfig()
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
