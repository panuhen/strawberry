"""The apps she may open or close: the installed desktop entries, apps.toml and the Brain UI's Apps tab (apps.py,
appsui.py, WIRING.md §17, §27)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from strawberry_crab import apps, brainui, paths
from strawberry_crab.apps import App, AppStore, AppsError
from strawberry_crab.daemon import Daemon
from strawberry_crab.events import CannedReactor
from strawberry_crab.server import create_app
from tests.test_brainui import quiet_config, sign_in

CANARY = "zanzibarq"     # an alias: it may be in apps.toml and the page, never in a log line

SPOTIFY = """[Desktop Entry]
Type=Application
Name=Spotify
Name[fi]=Spotify FI
Exec=env BAMF_DESKTOP_FILE_HINT=/var/lib/snapd/desktop/applications/spotify_spotify.desktop /snap/bin/spotify %U
StartupWMClass=spotify
"""
FIREFOX = "[Desktop Entry]\nType=Application\nName=Firefox Web Browser\nExec=/snap/bin/firefox %u\n"
STEAM = "[Desktop Entry]\nType=Application\nName=Steam\nExec=/usr/games/steam %U\n"
HIDDEN = "[Desktop Entry]\nType=Application\nName=Helper\nNoDisplay=true\nExec=helper\n"
LINK = "[Desktop Entry]\nType=Link\nName=A web link\nURL=https://example.org\n"


def install(directory: Path, **entries: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in entries.items():
        (directory / name).write_text(text, encoding="utf-8")
    return directory


def user_apps_dir() -> Path:
    return Path(os.environ["XDG_DATA_HOME"]) / "applications"


def standard(directory: Path | None = None) -> Path:
    return install(directory or user_apps_dir(), **{"spotify_spotify.desktop": SPOTIFY,
                                                     "firefox_firefox.desktop": FIREFOX, "steam.desktop": STEAM,
                                                     "helper.desktop": HIDDEN, "link.desktop": LINK})


# ----------------------------------------------------------------------------- desktop ids and entries


@pytest.mark.parametrize("bad", [
    "/usr/share/applications/firefox.desktop", "../firefox.desktop", "sub/firefox.desktop", "firefox",
    "-firefox.desktop", "--help.desktop", "fire fox.desktop", "$(reboot).desktop", "a;rm -rf ~.desktop",
    "`id`.desktop", "a|b.desktop", ".hidden.desktop", "firefox.desktop\n", "x" * 200 + ".desktop", "",
])
def test_a_desktop_id_is_a_plain_file_name_and_nothing_else(bad):
    assert not apps.ID.match(bad)
    with pytest.raises(AppsError, match="not a desktop id"):
        apps.check_app(bad, {"open": True})


@pytest.mark.parametrize("good", ["spotify_spotify.desktop", "org.gnome.Nautilus.desktop", "code-url-handler.desktop",
                                  "com.spotify.Client.desktop", "gimp+extra.desktop"])
def test_ordinary_desktop_ids_pass(good):
    assert apps.ID.match(good) and apps.check_app(good, {"open": True}).open


def test_the_scan_follows_xdg_precedence_and_skips_what_is_not_an_app(tmp_path):
    first = install(tmp_path / "home/applications", **{"firefox_firefox.desktop": FIREFOX.replace("Firefox Web Browser",
                                                                                                "My Firefox")})
    second = standard(tmp_path / "usr/applications")
    install(second, **{"bad name.desktop": FIREFOX, "notes.txt": "x"})
    found = apps.scan([first, second, tmp_path / "missing"])
    assert found["firefox_firefox.desktop"].name == "My Firefox"            # the earlier dir wins
    assert found["spotify_spotify.desktop"].name == "Spotify"              # the unlocalised Name
    assert "spotify" in found["spotify_spotify.desktop"].classes           # StartupWMClass, Exec's program
    assert "firefox" in found["firefox_firefox.desktop"].classes
    assert found["helper.desktop"].hidden and not found["steam.desktop"].hidden
    assert "link.desktop" not in found and "bad name.desktop" not in found
    assert found["spotify_spotify.desktop"].data_dir == tmp_path / "usr"


def test_application_dirs_are_xdg_and_snapd(monkeypatch, tmp_path):
    monkeypatch.setattr(apps, "SNAP_DIR", tmp_path / "snap")
    dirs = apps.application_dirs({"XDG_DATA_HOME": "/h/share", "XDG_DATA_DIRS": "/a:/b:relative:/a"})
    assert dirs == [Path("/h/share/applications"), Path("/a/applications"), Path("/b/applications"), tmp_path / "snap"]


# ----------------------------------------------------------------------------- the list


def test_with_no_apps_toml_spotify_may_open_but_not_close(tmp_path):
    standard()
    store = AppStore(tmp_path / "apps.toml")
    assert store.apps() == [App("spotify_spotify.desktop", "Spotify", (), True, False, True)]
    assert store.status()["source"] == "default"
    empty = AppStore(tmp_path / "none" / "apps.toml", scanner=lambda: {})
    assert empty.apps() == []                                               # not installed: nothing by default


def test_apps_toml_round_trips_and_says_what_is_wrong():
    listed = [App("firefox_firefox.desktop", "Firefox", ("browser", "the web"), True, True, False),
              App("spotify_spotify.desktop", "Spotify", (), True, False)]
    text = apps.render(listed)
    assert apps.parse(text) == listed
    assert '[apps."firefox_firefox.desktop"]' in text and "close_asks = false" in text
    for bad, words in [
        ('[apps."x.desktop"]\nopen = "yes"\n', "true or false"),
        ('[apps."x.desktop"]\nopen = true\nrun = "rm -rf ~"\n', "unknown keys"),
        ('[apps."x.desktop"]\naliases = ["$(reboot)"]\n', "not a name"),
        ('[apps."/bin/sh"]\nopen = true\n', "not a desktop id"),
        ('[apps."a.desktop"]\nname = "Same"\n[apps."b.desktop"]\naliases = ["same"]\n', "used by two apps"),
        ('[config]\nx = 1\n', "tables only"),
        ("[apps\n", "does not parse"),
    ]:
        with pytest.raises(AppsError, match=words):
            apps.parse(bad)
    # close_asks left out asks: the safe default.
    assert apps.parse('[apps."x.desktop"]\nclose = true\n')[0].close_asks is True


def test_the_tab_may_add_only_installed_apps_and_drops_rows_with_nothing_ticked():
    installed = apps.scan([standard()])
    rows = [{"id": "firefox_firefox.desktop", "name": "Firefox", "aliases": ["browser"], "open": True, "close": True},
            {"id": "steam.desktop", "name": "Steam", "aliases": [], "open": False, "close": False}]
    text, listed = apps.from_view(rows, installed)
    assert [a.id for a in listed] == ["firefox_firefox.desktop"] and listed[0].close_asks is True
    with pytest.raises(AppsError, match="not installed"):
        apps.from_view([{"id": "gone.desktop", "name": "Gone", "open": True}], installed)
    assert apps.from_view([{"id": "gone.desktop", "name": "Gone", "open": True}], installed, kept=("gone.desktop",))
    with pytest.raises(AppsError, match="listed twice"):
        apps.from_view(rows[:1] * 2, installed)
    with pytest.raises(AppsError, match="must be a list"):
        apps.from_view({"id": "x"}, installed)


def test_the_store_reads_the_file_live_and_keeps_its_list_when_the_file_breaks(tmp_path):
    standard()
    path = tmp_path / "apps.toml"
    store = AppStore(path)
    path.write_text('[apps."firefox_firefox.desktop"]\nname = "Firefox"\naliases = ["browser"]\nclose = true\n')
    assert store.check() and [a.id for a in store.apps()] == ["firefox_firefox.desktop"]
    assert store.status()["source"] == "apps.toml"
    path.write_text('[apps."firefox_firefox.desktop"]\nopen = "maybe"\n')
    os.utime(path, ns=(0, path.stat().st_mtime_ns + 10**9))
    assert not store.check() and store.error and [a.id for a in store.apps()] == ["firefox_firefox.desktop"]
    path.unlink()
    assert store.check() and [a.id for a in store.apps()] == ["spotify_spotify.desktop"]   # the default again


def test_a_broken_apps_toml_never_falls_back_to_the_default(tmp_path):
    """Security review: the default (Spotify may open) stands only while there is no apps.toml. A file that does
    not check out at the start allows nothing, rather than the default the user may have taken away."""
    standard()
    path = tmp_path / "apps.toml"
    path.write_text('[apps."spotify_spotify.desktop"]\nopen = false\nclose = "nope"\n')
    store = AppStore(path)
    assert store.error and store.apps() == []
    assert store.find("spotify") is None
    # No file first (the default), then a broken one: the default goes too.
    other = AppStore(tmp_path / "other.toml")
    assert other.apps()
    (tmp_path / "other.toml").write_text("[apps\n")
    assert other.check() and other.apps() == [] and other.error


def test_names_aliases_and_ids_find_their_own_app_only(tmp_path):
    standard()
    path = tmp_path / "apps.toml"
    path.write_text('[apps."firefox_firefox.desktop"]\nname = "Firefox"\naliases = ["browser"]\nclose = true\n'
                    '[apps."steam.desktop"]\nname = "Steam"\naliases = ["firefox_firefox.desktop"]\nopen = true\n')
    store = AppStore(path)
    assert store.find("FIREFOX").app.id == "firefox_firefox.desktop"
    assert store.find("  Browser ").app.id == "firefox_firefox.desktop"
    assert store.find_said("the browser app").app.id == "firefox_firefox.desktop"
    # Security review: a desktop id names its own entry first, never an app with an alias that reads like it.
    assert store.find("firefox_firefox.desktop").app.id == "firefox_firefox.desktop"
    assert store.find("spotify") is None and store.find("") is None and store.find(None) is None
    assert store.find("x" * 500) is None


# ----------------------------------------------------------------------------- the Apps tab


@pytest.fixture
async def client(aiohttp_client):
    standard()
    return await aiohttp_client(create_app(Daemon(reactor=CannedReactor(), config=quiet_config())))


def daemon_of(client) -> Daemon:
    return client.server.app[brainui.UI].daemon


async def test_every_apps_write_needs_the_session_the_csrf_header_and_the_origin(client):
    signed = await sign_in(client)
    origin = f"http://127.0.0.1:{client.port}"
    for path in ("/ui/api/apps/check", "/ui/api/apps/save"):
        body = {"apps": [{"id": "steam.desktop", "name": "Steam", "open": True}]}
        assert (await client.post(path, json=body, headers={"Origin": origin})).status == 401
        assert (await signed.post(path, body, csrf=False)).status == 403
        assert (await signed.post(path, body, origin="http://127.0.0.1:1")).status == 403
        assert (await signed.post(path, body, origin=None)).status == 403
    assert (await signed.get("/ui/api/apps", origin="https://evil.example")).status == 403
    assert (await signed.get("/ui/api/apps", csrf=False)).status == 403
    assert not apps.apps_file().exists()


async def test_the_apps_tab_lists_checks_and_saves_apps_toml_never_config_toml(client, caplog):
    caplog.set_level(logging.DEBUG)
    signed = await sign_in(client)
    view = await (await signed.get("/ui/api/apps")).json()
    assert [a["id"] for a in view["apps"]] == ["spotify_spotify.desktop"] and view["file"]["source"] == "default"
    ids = [e["id"] for e in view["installed"]]
    assert "helper.desktop" not in ids and "steam.desktop" in ids             # NoDisplay is not offered
    rows = [{"id": "spotify_spotify.desktop", "name": "Spotify", "aliases": [], "open": True, "close": True,
             "close_asks": True},
            {"id": "firefox_firefox.desktop", "name": "Firefox", "aliases": [CANARY], "open": True, "close": True,
             "close_asks": False}]
    checked = await (await signed.post("/ui/api/apps/check", {"apps": rows})).json()
    assert checked["ok"] and f'+aliases = ["{CANARY}"]' in checked["diff"]
    bad = rows + [{"id": "../../bin/sh", "name": "sh", "open": True}]
    checked = await (await signed.post("/ui/api/apps/check", {"apps": bad})).json()
    assert not checked["ok"] and "not a desktop id" in checked["problems"][0]
    assert (await signed.post("/ui/api/apps/save", {"apps": bad})).status == 400
    missing = rows + [{"id": "nothere.desktop", "name": "Nothere", "open": True}]
    assert (await signed.post("/ui/api/apps/save", {"apps": missing})).status == 400
    assert not apps.apps_file().exists()
    saved = await (await signed.post("/ui/api/apps/save", {"apps": rows})).json()
    assert saved["backup"] is None and saved["file"]["in_use"] and saved["file"]["source"] == "apps.toml"
    daemon = daemon_of(client)
    assert daemon.apps.find(CANARY).app.close_asks is False                      # used at once
    rows[1]["close_asks"] = True
    again = await (await signed.post("/ui/api/apps/save", {"apps": rows})).json()
    assert again["backup"] and "close_asks = false" in apps.apps_file().with_name("apps.toml.bak").read_text()
    assert apps.parse(apps.apps_file().read_text())[1].close_asks is True        # the reader agrees
    assert not paths.config_file().exists()                                       # never config.toml
    assert CANARY not in caplog.text


async def test_the_page_has_an_apps_tab_under_settings(client):
    signed = await sign_in(client)
    page = await (await signed.get("/ui")).text()
    assert 'data-section="apps"' in page and '<section id="apps"' in page
    script = (Path(brainui.__file__).parent / "ui" / "app.js").read_text()
    assert '"apps"' in script and "apps/save" in script and "innerHTML" not in script
