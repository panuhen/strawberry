"""Which widget runs (binary, checkout, neither), and fetching the binary from a release, served
here by a local HTTP server standing in for GitHub (WIRING.md §13)."""

from __future__ import annotations

import hashlib
import http.server
import os
import threading
from functools import partial
from pathlib import Path

import pytest

from strawberry_crab import __version__, cli, paths, widgetbin


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for name in ("CONFIG", "DATA", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.delenv(widgetbin.RELEASE_ENV, raising=False)
    monkeypatch.delenv("STRAWBERRY_CLI", raising=False)


def executable(path: Path, body: bytes = b"#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(0o755)
    return path


# --- resolution -------------------------------------------------------------------

def test_the_installed_binary_comes_first(tmp_path):
    binary = executable(paths.widget_binary())
    assert binary == tmp_path / "data" / "strawberry" / "widget" / "strawberry-widget"
    widget = widgetbin.resolve(project=tmp_path / "checkout" / "widget", godot="/usr/bin/godot")
    assert widget == widgetbin.Widget("binary", binary)
    assert widget.argv(8776, ["--capture=/tmp/x.png"]) == [
        str(binary), "--display-driver", "x11", "--", "--ws=ws://127.0.0.1:8776/ws", "--capture=/tmp/x.png"]


def test_a_binary_that_is_not_executable_does_not_count(tmp_path):
    paths.widget_binary().parent.mkdir(parents=True)
    paths.widget_binary().write_bytes(b"half a download")
    widget = widgetbin.resolve(project=tmp_path / "widget", godot="/opt/godot")
    assert widget.kind == "checkout"


def test_without_a_binary_a_checkout_and_godot_is_developer_mode(tmp_path):
    project = tmp_path / "widget"
    widget = widgetbin.resolve(project=project, godot="/opt/godot")
    assert widget == widgetbin.Widget("checkout", Path("/opt/godot"), project)
    assert widget.argv(8770) == ["/opt/godot", "--display-driver", "x11", "--path", str(project), "--",
                                 "--ws=ws://127.0.0.1:8770/ws"]


def test_neither_says_how_to_get_the_widget(tmp_path):
    with pytest.raises(widgetbin.WidgetMissing, match="godot .* on PATH.*--fetch"):
        widgetbin.resolve(project=tmp_path / "widget", godot="")
    with pytest.raises(widgetbin.WidgetMissing, match="strawberry widget --fetch"):
        widgetbin.resolve(project=None)


def test_the_cli_says_so_too(monkeypatch, capsys):
    monkeypatch.setattr(paths, "widget_project", lambda: None)
    assert cli.main(["widget"]) == 2
    assert "strawberry widget --fetch" in capsys.readouterr().err


def test_the_cli_execs_the_binary_with_the_x11_driver_and_the_port(monkeypatch):
    binary = executable(paths.widget_binary())
    monkeypatch.setenv("STRAWBERRY_TRAY", "1")          # the tray owns the daemon: start nothing
    monkeypatch.setenv("STRAWBERRYD_PORT", "8779")
    monkeypatch.setenv("STRAWBERRY_CLI", "")            # restored afterwards: cmd_widget sets it
    monkeypatch.setattr(cli, "daemon_up", lambda here: True)
    exec_calls = []

    def fake_execv(program, argv):
        exec_calls.append((program, argv))
        raise SystemExit(0)

    monkeypatch.setattr(os, "execv", fake_execv)
    with pytest.raises(SystemExit):
        cli.main(["widget", "--capture=/tmp/c.png"])
    assert exec_calls == [(str(binary), [str(binary), "--display-driver", "x11", "--",
                                         "--ws=ws://127.0.0.1:8779/ws", "--capture=/tmp/c.png"])]


def test_strawberry_cli_prefers_what_the_tray_passed(monkeypatch, tmp_path):
    fake = executable(tmp_path / "bin" / "strawberry")
    monkeypatch.setenv("STRAWBERRY_CLI", str(fake))
    assert widgetbin.strawberry_cli() == str(fake)
    monkeypatch.setenv("STRAWBERRY_CLI", str(tmp_path / "gone"))
    assert widgetbin.strawberry_cli() != str(tmp_path / "gone")


# --- fetching ------------------------------------------------------------------------

@pytest.fixture
def release(tmp_path):
    """A directory laid out like a GitHub release download URL, served on localhost."""
    root = tmp_path / "release"
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

    handler = partial(Quiet, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def publish(version: str, body: bytes, digest: str | None = None) -> None:
        folder = root / f"v{version}"
        folder.mkdir(parents=True, exist_ok=True)
        name = widgetbin.asset_name(version)
        (folder / name).write_bytes(body)
        (folder / f"{name}.sha256").write_text(f"{digest or hashlib.sha256(body).hexdigest()}  {name}\n")

    publish.base = f"http://127.0.0.1:{server.server_address[1]}"
    yield publish
    server.shutdown()


def test_fetch_installs_a_binary_whose_checksum_matches(release):
    body = b"\x7fELF pretend widget" * 1000
    release("0.1.0", body)
    installed = widgetbin.fetch("0.1.0", base=release.base, say=lambda _: None)
    assert installed == paths.widget_binary()
    assert installed.read_bytes() == body
    assert os.access(installed, os.X_OK)
    assert widgetbin.installed_version() == "0.1.0"
    assert widgetbin.resolve(project=None).kind == "binary"
    assert [p.name for p in installed.parent.iterdir() if p.name.startswith(".")] == []   # no temp left


def test_a_bad_checksum_installs_nothing_and_keeps_the_old_binary(release):
    old = executable(paths.widget_binary(), b"the old widget")
    release("0.2.0", b"tampered", digest="0" * 64)
    with pytest.raises(widgetbin.FetchError, match="SHA-256 mismatch"):
        widgetbin.fetch("0.2.0", base=release.base, say=lambda _: None)
    assert old.read_bytes() == b"the old widget"
    assert sorted(p.name for p in old.parent.iterdir()) == ["strawberry-widget"]


def test_a_missing_release_is_a_clear_error(release):
    with pytest.raises(widgetbin.FetchError, match=r"HTTP 404 \(is there a release v9.9.9\?\)"):
        widgetbin.fetch("9.9.9", base=release.base, say=lambda _: None)
    assert not paths.widget_binary().exists()


def test_the_cli_fetches_from_the_overridden_base(release, monkeypatch, capsys):
    release(__version__, b"widget for this package")
    release("0.3.1", b"another version")
    monkeypatch.setenv(widgetbin.RELEASE_ENV, release.base)
    assert cli.main(["widget", "--fetch"]) == 0
    assert paths.widget_binary().read_bytes() == b"widget for this package"
    assert cli.main(["widget", "--fetch", "--version", "0.3.1"]) == 0
    assert paths.widget_binary().read_bytes() == b"another version"
    assert "installed" in capsys.readouterr().out
    assert cli.main(["widget", "--version", "0.3.1"]) == 2     # --version alone is a mistake
    monkeypatch.setenv(widgetbin.RELEASE_ENV, release.base + "/nothing-here")
    assert cli.main(["widget", "--fetch"]) == 1
    assert "widget fetch failed" in capsys.readouterr().err


def test_the_default_url_is_the_github_release():
    assert widgetbin.asset_url("0.1.0") == (
        "https://github.com/panuhen/strawberry/releases/download/v0.1.0/strawberry-widget-0.1.0-linux-x86_64")
    assert widgetbin.asset_url("0.1.0", ".sha256", "http://x/").endswith("/v0.1.0/strawberry-widget-0.1.0-linux-x86_64.sha256")


def test_sha256_files_parse_like_sha256sum():
    digest = "ab" * 32
    assert widgetbin.parse_sha256(f"{digest}  strawberry-widget-1-x\n", "strawberry-widget-1-x") == digest
    assert widgetbin.parse_sha256(f"{digest} *strawberry-widget-1-x", "strawberry-widget-1-x") == digest
    assert widgetbin.parse_sha256(digest.upper(), "anything") == digest
    with pytest.raises(widgetbin.FetchError):
        widgetbin.parse_sha256(f"{digest}  some-other-file", "strawberry-widget-1-x")
    with pytest.raises(widgetbin.FetchError):
        widgetbin.parse_sha256("<html>not found</html>", "strawberry-widget-1-x")
