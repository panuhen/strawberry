"""The per-install bus secret (PROTOCOL.md §1.4, WIRING.md §19).

The daemon listens on loopback, and any process on the machine can reach loopback: another user's,
a sandboxed app's, a container's. The Origin rule keeps browsers out (§1.2); this keeps out everyone
who cannot read the user's own files. One random secret, 32 bytes as url-safe base64 (43 characters),
in a file only the user can read:

    Linux     $XDG_STATE_HOME/strawberry/bus-secret      (~/.local/state/strawberry/bus-secret), 0600
    Windows   %LOCALAPPDATA%\\strawberry\\state\\bus-secret  (the per-user profile's ACL)

The daemon makes it on its first start (`ensure`) and keeps it from then on; every first-party client
reads it (`read`) each time it talks to the daemon, so a daemon that made it after the client started
is no problem. What needs it:

    HTTP     every POST but the Brain UI's own API: the `X-Strawberry-Secret` header
    /ws      the hello's `secret` field: input (`heard`, `poked`, `run.cancel`, `approval.answer`) and
             approvals; without it a body still gets the performances and the plain run phases

It is compared in constant time (`matches`) and never logged, never sent back, never put on the bus.
What it does not stop: a process running as the user, which can read the file as the widget does.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
from pathlib import Path

from . import paths

log = logging.getLogger("strawberryd.secret")

HEADER = "X-Strawberry-Secret"     # the HTTP header a client presents it in
FIELD = "secret"                   # the hello field a body presents it in
FILENAME = "bus-secret"
BYTES = 32
VALID = re.compile(r"^[A-Za-z0-9_-]{43}$")   # token_urlsafe(32): 43 characters, no padding


def path() -> Path:
    """Where the secret is: the state dir (paths.state_dir), the same on every system."""
    return paths.bus_secret_file()


def read(where: Path | None = None) -> str | None:
    """The secret as a client reads it, or None when there is none yet (the daemon has never started) or the
    file does not hold one. Read afresh on every call: it is 43 bytes, and a stale copy would lock a
    long-running doorway out after the file was made again."""
    try:
        text = (where or path()).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text if VALID.match(text) else None


def ensure(where: Path | None = None) -> str:
    """The daemon's: the secret, made on the first start. An existing file is kept (made readable by the user
    alone again if it was not); a missing or broken one is replaced, atomically, so two daemons starting at
    once agree on one secret. Raises OSError when the state dir cannot be written."""
    target = where or path()
    existing = read(target)
    if existing is not None:
        _tighten(target)
        return existing
    target.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(BYTES)
    temporary = target.with_name(f".{FILENAME}.{os.getpid()}.{secrets.token_hex(4)}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        os.write(fd, (value + "\n").encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        if target.exists():
            # A file that holds no secret (empty, cut short, edited): replaced.
            os.replace(temporary, target)
        else:
            try:
                os.link(temporary, target)    # fails if another daemon made one a moment ago: theirs wins
            except FileExistsError:
                pass
            except OSError:
                os.replace(temporary, target)   # a file system without hard links
    finally:
        temporary.unlink(missing_ok=True)
    made = read(target)
    if made is None:
        raise OSError(f"{target} does not hold a secret after writing it")
    _tighten(target)
    log.info("secret: made %s (readable by this user only)", target)
    return made


def _tighten(target: Path) -> None:
    """0600 on POSIX. Windows has no mode bits that mean this: the file is in the user's %LOCALAPPDATA%,
    whose ACL lets in the user, SYSTEM and the administrators, as for the rest of the state dir."""
    if os.name != "posix":
        return
    try:
        mode = target.stat().st_mode & 0o777
        if mode != 0o600:
            os.chmod(target, 0o600)
            log.warning("secret: %s was readable by others (mode %o); made it 0600", target, mode)
    except OSError as exc:
        log.warning("secret: could not check the mode of %s (%s)", target, exc)


def matches(expected: str | None, given: object) -> bool:
    """Is `given` the secret? In constant time (hmac.compare_digest); False for anything that is not a
    string, and always False when the daemon has none."""
    if not expected or not isinstance(given, str) or not given:
        return False
    return hmac.compare_digest(expected.encode("ascii"), given.encode("utf-8", "replace"))


def headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """HTTP headers for a client's request: `extra` plus the secret, when there is one to send."""
    out = dict(extra or {})
    value = read()
    if value is not None:
        out[HEADER] = value
    return out
