"""The Windows notification doorway (doorways/toast_watch.py) on a fake listener shaped like
Windows' UserNotificationListener: toasts in, the same events out as the D-Bus doorway posts.
No WinRT, so it runs on any system; tests/conftest.py keeps the real listener out of reach."""

from __future__ import annotations

import asyncio
import logging

import pytest

from strawberry_crab.config import NotificationsConfig
from strawberry_crab.doorways import notifications, notify_watch, toast_watch
from strawberry_crab.doorways.toast_watch import Toast, Toasts, Watcher, parse_toast, read_toast

ALLOWED, DENIED, UNSPECIFIED = 1, 2, 0
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 16


# --- the fake, shaped like the WinRT objects ----------------------------------------------

class FakeText:
    def __init__(self, text):
        self.text = text


class FakeBinding:
    def __init__(self, texts):
        self.texts = texts

    def get_text_elements(self):
        return [FakeText(t) for t in self.texts]


class FakeVisual:
    def __init__(self, texts):
        self.binding = FakeBinding(texts) if texts is not None else None

    def get_binding(self, template):
        return self.binding if template == "ToastGeneric" else None


class FakeDisplayInfo:
    def __init__(self, name):
        self.display_name = name

    def get_logo(self, size):
        return None                      # a desktop app's, as Windows gives it


class FakeAppInfo:
    def __init__(self, aumid, name):
        self.app_user_model_id = aumid
        self.display_info = FakeDisplayInfo(name)


class FakeNotification:
    """A UserNotification: an Id, the app it came from, and its visual's text elements."""

    def __init__(self, id, app="Slack", aumid="com.squirrel.slack.slack", texts=("Alex", "Lunch at noon?")):
        self.id = id
        self.app_info = FakeAppInfo(aumid, app)
        self.notification = type("ToastNotification", (), {"visual": FakeVisual(texts)})()


class FakeListener:
    def __init__(self, *toasts, status=ALLOWED, grant=ALLOWED):
        self.toasts = list(toasts)
        self.status = status
        self.grant = grant
        self.requests = 0
        self.fail = False
        self.kinds = []

    def get_access_status(self):
        return self.status

    async def request_access_async(self):
        self.requests += 1
        self.status = self.grant
        return self.status

    async def get_notifications_async(self, kinds):
        self.kinds.append(kinds)
        if self.fail:
            raise OSError("-2147024891 access denied")
        return list(self.toasts)

    def add_notification_changed(self, handler):
        raise OSError("[WinError -2147023728] Element not found")   # what an unpackaged process gets


class FakeDaemon:
    url = "http://127.0.0.1:1"

    def __init__(self):
        self.calls = []

    async def post(self, path, payload):
        self.calls.append((path, payload))
        return True


def watcher_with(*toasts, cfg=None, **listener) -> tuple[Watcher, FakeListener]:
    fake = FakeListener(*toasts, **listener)
    watcher = Watcher("http://127.0.0.1:1", cfg or NotificationsConfig(coalesce_s=0.01), Toasts(fake))
    watcher.daemon = FakeDaemon()
    return watcher, fake


async def settle():
    await asyncio.sleep(0.05)


# --- reading a toast -------------------------------------------------------------------

def test_a_user_notification_reads_as_a_toast_whose_text_stays_out_of_repr():
    toast = read_toast(FakeNotification(7, texts=("Alex", "canary-repr-41d0")))
    assert (toast.id, toast.app, toast.app_id, toast.texts) == (7, "Slack", "com.squirrel.slack.slack",
                                                                ("Alex", "canary-repr-41d0"))
    assert "canary" not in repr(toast) and "Alex" not in repr(toast)
    assert read_toast(FakeNotification(8, texts=None)).texts == ()          # no ToastGeneric binding


def test_parse_toast_is_parse_notifys_dict():
    n = parse_toast(Toast(1, "Slack", "com.squirrel.slack.slack", ("Alex", "Lunch", "at  noon &amp; after?")))
    assert n == {
        "app": "Slack", "desktop_entry": "slack", "title": "Alex", "body": "Lunch at noon & after?",
        "urgency": "normal", "category": "", "replaces_id": 0, "app_icon": "",
    }
    assert set(n) == set(notify_watch.parse_notify(("Slack", 0, "", "Alex", "Lunch", [], {}, -1)))
    assert parse_toast(Toast(2, "", "Spotify.exe", ()))["app"] == "spotify"    # no display name: the key
    assert parse_toast(Toast(3, "App", "x", ("Only a title",)))["body"] == ""
    long = parse_toast(Toast(4, "App", "x", ("t", "word " * 300)), max_body_chars=100)["body"]
    assert len(long) <= 100 and long.endswith("word…")


@pytest.mark.parametrize("mode", ["off", "react", "glance"])
@pytest.mark.parametrize("app, title, body", [("Slack", "Alex", "Lunch at noon?"), ("Spotify", "Track", "x"),
                                              ("Mail", "Your code", "482913 is your code")])
def test_the_same_notification_makes_the_same_event_on_both_systems(mode, app, title, body):
    """Whatever the system, one notification is one decision: forwarded or not, body or not."""
    cfg = NotificationsConfig(body=mode)
    linux = notify_watch.parse_notify((app, 0, "", title, body, [], {}, -1))
    windows = parse_toast(Toast(1, app, f"{app}.exe", (title, body)))
    assert notifications.allowed(linux, cfg) == notifications.allowed(windows, cfg)
    assert notifications.to_event(linux, cfg) == notifications.to_event(windows, cfg)


def test_both_doorways_share_one_set_of_rules():
    for name in ("clean", "allowed", "forwards_body", "to_event", "summarise", "Deduper"):
        assert getattr(notify_watch, name) is getattr(notifications, name)
    assert issubclass(notify_watch.Watcher, notifications.Forwarder)
    assert issubclass(toast_watch.Watcher, notifications.Forwarder)


# --- polling and posting ----------------------------------------------------------------

async def test_what_is_there_at_start_is_not_news_and_a_new_toast_is_posted_once():
    watcher, fake = watcher_with(FakeNotification(1))
    await watcher.poll_once()
    await settle()
    assert watcher.daemon.calls == []
    fake.toasts.append(FakeNotification(2, texts=("Sam", "Pub?")))
    await watcher.poll_once()
    await watcher.poll_once()                                   # still there: not new again
    await settle()
    assert watcher.daemon.calls == [("/event", {"source": "notification", "app": "Slack", "title": "Sam",
                                                "urgency": "normal"})]
    assert fake.kinds and watcher.seen == 1


async def test_a_toast_that_goes_and_a_new_one_that_comes_are_told_apart_by_id():
    watcher, fake = watcher_with()
    await watcher.poll_once()
    fake.toasts = [FakeNotification(5, texts=("Sam", "one"))]
    await watcher.poll_once()
    await settle()
    fake.toasts = []                                          # dismissed
    await watcher.poll_once()
    fake.toasts = [FakeNotification(6, texts=("Sam", "two"))]
    await watcher.poll_once()
    await settle()
    assert [c[1]["title"] for c in watcher.daemon.calls] == ["Sam", "Sam"]


async def test_the_config_filters_as_on_linux():
    cfg = NotificationsConfig(coalesce_s=0.01, body="off", body_apps={"slack": "react"})
    watcher, fake = watcher_with(cfg=cfg)
    await watcher.poll_once()
    fake.toasts = [FakeNotification(1, app="Spotify", aumid="Spotify.exe", texts=("Track", "Artist"))]
    await watcher.poll_once()
    await settle()
    assert watcher.daemon.calls == []                         # ignore_apps = ["Spotify"] by default
    fake.toasts.append(FakeNotification(2, texts=("Alex", "Lunch?")))
    await watcher.poll_once()
    await settle()
    fake.toasts.append(FakeNotification(3, app="Mail", aumid="microsoft.windowscommunicationsapps_8wekyb3d8bbwe!microsoft.windowslive.mail",
                                        texts=("Sam", "Minutes attached")))
    await watcher.poll_once()
    await settle()
    slack, mail = (c[1] for c in watcher.daemon.calls)
    assert slack["body"] == "Lunch?"                          # body_apps by the app's key
    assert "body" not in mail and mail["title"] == "Sam"      # body "off": the body stays here


async def test_toasts_inside_the_window_coalesce_into_one_event():
    watcher, fake = watcher_with()
    await watcher.poll_once()
    fake.toasts = [FakeNotification(i, texts=(f"#chan{i}", "x")) for i in range(3)]
    await watcher.poll_once()
    await settle()
    assert watcher.daemon.calls == [("/event", {"source": "notification", "app": "Slack",
                                                "title": "3 notifications from Slack",
                                                "body": "#chan0 · #chan1 · #chan2", "urgency": "normal"})]


async def test_no_body_in_any_log_line_and_off_bodies_never_posted(caplog):
    watcher, fake = watcher_with(cfg=NotificationsConfig(coalesce_s=0.01, body_apps={"Slack": "react"}))
    await watcher.poll_once()
    with caplog.at_level(logging.DEBUG):
        fake.toasts = [FakeNotification(1, app="Mail", aumid="Mail.exe", texts=("James", "canary-off-7f3a"))]
        await watcher.poll_once()
        await settle()
        fake.toasts.append(FakeNotification(2, texts=("Alex", "canary-react-9c1d")))
        await watcher.poll_once()
        await settle()
        fake.toasts.append(FakeNotification(3, app="Spotify", aumid="Spotify.exe", texts=("T", "canary-drop-11aa")))
        await watcher.poll_once()                                      # dropped, at DEBUG
        await settle()
    posted = [c[1] for c in watcher.daemon.calls]
    assert "body" not in posted[0] and posted[0]["title"] == "James"
    assert posted[1]["body"] == "canary-react-9c1d"
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "canary" not in text and "body_len=15" in text and "body_len=17" in text


# --- the app's logo ---------------------------------------------------------------------

async def test_the_apps_logo_becomes_the_events_icon_once_per_app(monkeypatch, tmp_path):
    reads = []

    async def fake_logo(display_info, size=64):
        reads.append(display_info.display_name)
        return PNG if display_info.display_name == "Slack" else b"GIF89a"

    monkeypatch.setattr(toast_watch, "read_logo", fake_logo)
    watcher, fake = watcher_with()
    watcher.icons_dir = tmp_path / "icons"
    await watcher.poll_once()
    fake.toasts = [FakeNotification(1, texts=("Alex", "x")), FakeNotification(2, app="Other", aumid="Other.exe")]
    await watcher.poll_once()
    await settle()
    fake.toasts.append(FakeNotification(3, texts=("Sam", "y")))
    await watcher.poll_once()
    await settle()
    icon = str(tmp_path / "icons" / "slack.png")
    assert (tmp_path / "icons" / "slack.png").read_bytes() == PNG
    assert watcher.daemon.calls[0][1]["app"] == "several apps"            # a burst across apps has no badge
    assert watcher.daemon.calls[1][1]["icon"] == icon
    assert reads == ["Slack", "Other"]                                    # once per app, not per toast
    assert watcher.icons == {"com.squirrel.slack.slack": icon, "Other.exe": None}   # not a PNG: no badge


async def test_a_logo_that_cannot_be_read_is_no_badge(monkeypatch):
    async def broken(display_info, size=64):
        raise OSError("gone")

    monkeypatch.setattr(toast_watch, "read_logo", broken)
    watcher, fake = watcher_with()
    await watcher.poll_once()
    fake.toasts = [FakeNotification(1)]
    await watcher.poll_once()
    await settle()
    assert "icon" not in watcher.daemon.calls[0][1]


# --- access and the loop ----------------------------------------------------------------

async def test_access_already_allowed_is_not_asked_again():
    fake = FakeListener()
    assert await Toasts(fake).access() == "allowed" and fake.requests == 0


async def test_access_not_yet_given_is_asked_for():
    fake = FakeListener(status=UNSPECIFIED, grant=ALLOWED)
    assert await Toasts(fake).access() == "allowed" and fake.requests == 1


async def test_access_denied_exits_and_says_where_to_allow_it(caplog):
    watcher, fake = watcher_with(status=DENIED, grant=DENIED)
    with caplog.at_level(logging.ERROR):
        assert await watcher.run(asyncio.Event()) == 3
    assert "denied" in caplog.text and "Privacy & security > Notifications" in caplog.text
    assert fake.kinds == []                                               # nothing was read


async def test_the_real_listener_is_out_of_reach_in_tests(caplog):
    watcher = Watcher("http://127.0.0.1:1", NotificationsConfig())
    with caplog.at_level(logging.ERROR):
        assert await watcher.run(asyncio.Event()) == 3
    assert "out of bounds in tests" in caplog.text


async def test_the_loop_polls_until_stopped(monkeypatch):
    monkeypatch.setattr(toast_watch, "POLL_S", 0.01)
    watcher, fake = watcher_with(FakeNotification(1))
    stopping = asyncio.Event()
    task = asyncio.ensure_future(watcher.run(stopping))
    await asyncio.sleep(0.05)
    fake.toasts.append(FakeNotification(2, texts=("Sam", "Pub?")))
    for _ in range(100):
        if watcher.daemon.calls:
            break
        await asyncio.sleep(0.01)
    stopping.set()
    assert await task == 0
    assert [c[1]["title"] for c in watcher.daemon.calls] == ["Sam"]


async def test_reads_that_keep_failing_warn_once_then_exit(monkeypatch, caplog):
    monkeypatch.setattr(toast_watch, "POLL_S", 0.0)
    monkeypatch.setattr(toast_watch, "FAILURES_TO_EXIT", 5)
    watcher, fake = watcher_with()
    fake.fail = True
    with caplog.at_level(logging.WARNING):
        assert await watcher.run(asyncio.Event()) == 3
    assert caplog.text.count("could not read the notifications") == 1
    assert "5 times in a row" in caplog.text


def test_main_reads_the_config_file_it_is_given(tmp_path, monkeypatch):
    """The tray passes its --config here too, so "Message bodies" and the watcher read one file."""
    seen = []

    async def fake_watch(daemon, cfg):
        seen.append((daemon, cfg.body))
        return 0

    monkeypatch.setattr(toast_watch, "watch", fake_watch)
    config = tmp_path / "c.toml"
    config.write_text('[daemon]\nport = 8779\n[notifications]\nbody = "glance"\n')
    assert toast_watch.main(["--config", str(config)]) == 0
    assert toast_watch.main(["--config", str(config), "--daemon", "http://127.0.0.1:1"]) == 0
    assert seen == [("http://127.0.0.1:8779", "glance"), ("http://127.0.0.1:1", "glance")]


def test_icon_names_are_safe_file_names():
    assert toast_watch.icon_name("com.squirrel.slack.slack") == "slack.png"
    assert toast_watch.icon_name("C:\\Program Files\\App One\\app one.exe") == "app_one.png"
    assert toast_watch.icon_name("") == "app.png"
