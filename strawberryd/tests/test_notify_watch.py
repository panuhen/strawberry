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
        "urgency": "normal", "category": "im.received", "replaces_id": 0,
    }


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
    event = summarise(batch, cfg)
    assert event["app"] == "Slack"
    assert event["title"] == "7 notifications from Slack"
    assert event["body"] == "#chan0 · #chan1 · #chan2 · #chan3 · #chan4 · and 2 more"


def test_burst_across_apps_keeps_the_highest_urgency():
    cfg = NotificationsConfig()
    batch = [parse_notify(notify(app="WhatsApp", summary="James")), parse_notify(notify(app="Power", summary="Battery low", urgency=2))]
    event = summarise(batch, cfg)
    assert event["app"] == "several apps"
    assert event["title"] == "2 notifications"
    assert event["body"] == "WhatsApp: James · Power: Battery low"
    assert event["urgency"] == "critical"
