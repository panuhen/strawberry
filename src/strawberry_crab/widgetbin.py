"""The widget as a program to run: which one, how, and fetching the released binary (WIRING.md §13).

Resolution order, the same for `strawberry widget` and the tray's widget child:

1. the exported binary in the data dir (`paths.widget_binary()`, put there by `fetch`);
2. developer mode: the Godot project in a source checkout, run with `godot` from PATH;
3. neither: a WidgetMissing that says how to get one (`strawberry widget --fetch`).

Either way she runs on the X11 backend (`--display-driver x11`): native on an X11 session,
XWayland on a Wayland one, because native Wayland clients on GNOME get neither always-on-top nor
self-positioning (§13).

`fetch` downloads `strawberry-widget-<version>-linux-x86_64` and its `.sha256` from the GitHub
release `v<version>`, checks the SHA-256 and installs it atomically. `strawberry setup`
(PACKAGING.md step 5) calls the same function. STRAWBERRY_RELEASE_URL replaces the release base
URL (tests serve a local directory with it).

Only the standard library here: `strawberry widget` starts before anything heavy is imported.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import paths

RELEASE_BASE = "https://github.com/panuhen/strawberry/releases/download"
RELEASE_ENV = "STRAWBERRY_RELEASE_URL"
PLATFORM = "linux-x86_64"
DISPLAY_ARGS = ("--display-driver", "x11")
CHUNK = 1 << 20


class WidgetMissing(Exception):
    """No binary and no checkout to run; the message says what to do."""


class FetchError(Exception):
    """A download that failed or did not match its checksum; nothing was installed."""


def asset_name(version: str) -> str:
    return f"strawberry-widget-{version}-{PLATFORM}"


def release_base() -> str:
    return (os.environ.get(RELEASE_ENV, "").strip() or RELEASE_BASE).rstrip("/")


def asset_url(version: str, suffix: str = "", base: str | None = None) -> str:
    return f"{(base or release_base()).rstrip('/')}/v{version}/{asset_name(version)}{suffix}"


# --- which widget ------------------------------------------------------------------

@dataclass(frozen=True)
class Widget:
    """What `strawberry widget` will run. `kind` is "binary" or "checkout"."""

    kind: str
    program: Path                 # the binary, or godot
    project: Path | None = None   # the checkout's widget/ (developer mode)

    def argv(self, port: int, extra: list[str] | tuple[str, ...] = ()) -> list[str]:
        user = ["--", f"--ws=ws://127.0.0.1:{port}/ws", *extra]
        if self.kind == "binary":
            return [str(self.program), *DISPLAY_ARGS, *user]
        return [str(self.program), *DISPLAY_ARGS, "--path", str(self.project), *user]


def is_runnable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def resolve(binary: Path | None = None, project: Path | None | str = "auto",
            godot: str | None = None) -> Widget:
    """The widget to run: the installed binary, else the checkout with godot, else WidgetMissing.

    The arguments are for tests; by default they are paths.widget_binary(),
    paths.widget_project() and `godot` on PATH ("" means no godot).
    """
    binary = binary if binary is not None else paths.widget_binary()
    if is_runnable(binary):
        return Widget("binary", binary)
    if project == "auto":
        project = paths.widget_project()
    if project is not None:
        found = godot if godot is not None else shutil.which("godot")
        if found:
            return Widget("checkout", Path(found), Path(project))
        raise WidgetMissing(
            f"no widget binary at {binary}, and developer mode needs godot (4.7) on PATH to run "
            f"{project}. Get the binary with: strawberry widget --fetch")
    raise WidgetMissing(f"no widget binary at {binary}. Get it with: strawberry widget --fetch "
                        f"(or strawberry setup)")


def strawberry_cli() -> str | None:
    """The absolute path of the `strawberry` executable, for STRAWBERRY_CLI in the widget's
    environment: its menu runs `$STRAWBERRY_CLI config --init` and `$STRAWBERRY_CLI restart`.

    An inherited STRAWBERRY_CLI (the tray's) wins; then the running script if it is `strawberry`;
    then the console script next to this interpreter (a venv or a uv tool env); then PATH.
    """
    inherited = os.environ.get("STRAWBERRY_CLI", "")
    if inherited and is_runnable(Path(inherited)):
        return inherited
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 is not None and argv0.name == "strawberry" and is_runnable(argv0):
        return str(argv0.absolute())
    sibling = Path(sys.executable).with_name("strawberry")
    if is_runnable(sibling):
        return str(sibling)
    return shutil.which("strawberry")


# --- fetching the release -------------------------------------------------------------

def _open(url: str, timeout: float):
    import urllib.request   # here: `strawberry widget` without --fetch never needs it

    request = urllib.request.Request(url, headers={"User-Agent": "strawberry-widget-fetch"})
    return urllib.request.urlopen(request, timeout=timeout)


def parse_sha256(text: str, name: str) -> str:
    """The hex digest from a `sha256sum` line ("<hex>  <name>"), or from a bare digest."""
    for line in text.splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        digest = parts[0].lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            continue
        if len(parts) == 1 or parts[-1].lstrip("*") == name:
            return digest
    raise FetchError(f"no SHA-256 for {name} in its .sha256 file")


def fetch(version: str, dest: Path | None = None, base: str | None = None, timeout: float = 30.0,
          say: Callable[[str], None] = print) -> Path:
    """Download the widget binary for `version`, check its SHA-256, install it at `dest`.

    Written to a temporary file beside `dest` and renamed over it only after the checksum
    matched, so a failed or interrupted fetch leaves any installed binary untouched. The version
    goes to paths.widget_version_file() (beside the default dest) for `setup` to compare.
    """
    import urllib.error

    dest = dest if dest is not None else paths.widget_binary()
    name = asset_name(version)
    sums_url = asset_url(version, ".sha256", base)
    url = asset_url(version, "", base)
    try:
        with _open(sums_url, timeout) as response:
            expected = parse_sha256(response.read(4096).decode("utf-8", "replace"), name)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"{sums_url}: HTTP {exc.code} (is there a release v{version}?)") from None
    except (urllib.error.URLError, OSError) as exc:
        raise FetchError(f"{sums_url}: {getattr(exc, 'reason', exc)}") from None

    dest.parent.mkdir(parents=True, exist_ok=True)
    say(f"downloading {url}")
    digest = hashlib.sha256()
    size = 0
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", dir=dest.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out:
            try:
                with _open(url, timeout) as response:
                    while chunk := response.read(CHUNK):
                        digest.update(chunk)
                        out.write(chunk)
                        size += len(chunk)
            except urllib.error.HTTPError as exc:
                raise FetchError(f"{url}: HTTP {exc.code}") from None
            except (urllib.error.URLError, OSError) as exc:
                raise FetchError(f"{url}: {getattr(exc, 'reason', exc)}") from None
        actual = digest.hexdigest()
        if actual != expected:
            raise FetchError(f"{name}: SHA-256 mismatch (expected {expected}, got {actual}); not installed")
        tmp.chmod(0o755)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    if dest == paths.widget_binary():
        paths.widget_version_file().write_text(version + "\n")
    say(f"installed {dest} ({size / 1e6:.1f} MB, sha256 {actual[:12]}…, version {version})")
    return dest


def installed_version() -> str | None:
    """What `fetch` recorded for the installed binary, or None (none installed, or put there by hand)."""
    if not is_runnable(paths.widget_binary()):
        return None
    try:
        return paths.widget_version_file().read_text().strip() or None
    except OSError:
        return None
