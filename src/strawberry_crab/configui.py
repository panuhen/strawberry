"""The Brain UI's Config file and Tools tabs (WIRING.md §17, §28): config.toml edited from the page.

    GET  /ui/api/config          the form's schema, the file's text and values (secrets hidden), what the page loaded
                                 (the file's hash and mtime) and a change waiting on her card
    POST /ui/api/config/check    a draft: the text (masked), and the form's edits on it. It comes back as text, checked
                                 as at start (config.load), with its diff against the file, what it loosens
                                 (configform.lowering) and what needs a restart
    POST /ui/api/config/save     a draft that checks out, against the hash the page loaded: written at once (backed up,
                                 one step) when it loosens nothing; else she asks on her card first, and it is written
                                 only after the user's yes there or out loud (202, and GET config says how it went)
    GET  /ui/api/tools           the tool servers (configured and built in) and each tool's tier, flags, whether the
                                 thinker is offered it; a url server's login command; the servers as the file says

The same session, CSRF header and Origin rules as every other write of the page (brainui.checked). A settings change
that loosens safety is asked about like a risky tool call (approvals.py): her card, held for a yes, or the user's own
spoken or typed yes; never a yes from this page, which is what asked (`yes_by`), and never a thumb. A No may come
from anywhere. Secrets in the file (a server's env, a token in its arguments or address, a key named like one) never
reach the page, the diff or the log: the page sees `<hidden N: kept as is>` and the daemon puts the value back.
"""

from __future__ import annotations

import asyncio
import logging
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from . import configform, runs
from .approvals import Approval
from .brainui import BrainUI, Session, _diff, _payload, checked
from .config import OFFERS, TRUST_FLAGS, ConfigError, default_path, default_toml, load
from .confirm import LEFT_CHANGED, Held
from .contract import Performance

log = logging.getLogger("strawberryd.ui")

MAX_TEXT = 200_000
CONFIRM_S = 60.0                       # how long her card waits for the user's yes to a settings change
YES_BY = frozenset({"voice", "typed", "body"})
ASK_LINE = "That settings change loosens my safety rules. Hold yes on my card if it was you."
SAVED_LINE = "Okay, the settings are saved."
LEFT_LINE = "Okay, I've left the settings as they were."
SILENT_LINE = "No answer, so I've left the settings as they were."
FAILED_LINE = "I couldn't save the settings."


@dataclass
class Pending:
    """A settings change waiting on her card: the text it would write (never sent to the page), the file it was
    checked against, and how it went."""

    approval: Approval
    text: str
    base_sha: str
    findings: list[configform.Finding]
    changed: list[str]
    state: str = "waiting"             # waiting | saved | left | changed | failed
    detail: str = ""
    backup: str | None = None
    applied: list[str] = field(default_factory=list)
    at: float = field(default_factory=time.time)
    file: str = "config.toml"          # or input.toml (the Input tab's settings: brainui.api_input_save)
    # After the yes: write(text, base_sha) -> the backup (raises configform.Stale when the file changed), then
    # apply() -> the sections in use at once. None: config.toml's (configform.write, apply_live).
    write: Callable[[str, str], Path | None] | None = None
    apply: Callable[[], list[str]] | None = None

    def view(self) -> dict[str, Any]:
        a = self.approval
        return {"approval_id": a.approval_id, "file": self.file, "state": self.state, "detail": self.detail or None,
                "lowering": [f.view() for f in self.findings], "at": round(self.at, 3), "backup": self.backup,
                "restart": [s for s in self.changed if s not in configform.LIVE_SECTIONS], "applied": self.applied,
                "expires_in": round(max(a.expires - time.monotonic(), 0.0), 1) if a.open else None,
                "outcome": a.outcome or None, "by": a.by or None}


class ConfigDesk:
    """The tabs' state in the daemon: each session's hidden values, and the one change waiting on her card."""

    def __init__(self) -> None:
        self.vaults: dict[str, configform.Vault] = {}
        self.pending: Pending | None = None

    def vault(self, ui: BrainUI, session: Session) -> configform.Vault:
        alive = {s.id for s in ui.sessions.sessions}
        for gone in [sid for sid in self.vaults if sid not in alive]:
            del self.vaults[gone]
        return self.vaults.setdefault(session.id, configform.Vault())

    def waiting(self) -> Pending | None:
        return self.pending if self.pending is not None and self.pending.state == "waiting" else None


DESK = web.AppKey("configdesk", ConfigDesk)


def config_path(ui: BrainUI) -> Path:
    return ui.daemon.config.path or default_path()


def _starting_text(base: dict[str, Any]) -> str:
    """What the draft starts from: the file, or the commented template for a file that is not there or is empty."""
    return base["text"] if base["text"].strip() else default_toml()


def config_view(ui: BrainUI, desk: ConfigDesk, session: Session) -> dict[str, Any]:
    path = config_path(ui)
    base = configform.base_of(path)
    vault = desk.vault(ui, session)
    error = None
    try:
        text: str | None = vault.mask(_starting_text(base))
        values = tomllib.loads(text)
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        text, values, error = None, {}, str(exc)
    return {"path": str(path), "exists": base["exists"], "base": {"sha256": base["sha256"], "mtime_ns": base["mtime_ns"]},
            "text": text, "values": values, "error": error, "schema": configform.schema(),
            "live": dict(configform.LIVE_SECTIONS),
            "pending": desk.pending.view() if desk.pending is not None else None}


def _old(ui: BrainUI, base: dict[str, Any]) -> Any:
    """The file as the daemon would read it now; when it does not load (broken under her), the settings she runs
    with, so a draft is never compared against something looser than what is in use."""
    try:
        return configform.load_text(base["text"])
    except ConfigError:
        return ui.daemon.config


def draft_check(ui: BrainUI, desk: ConfigDesk, session: Session, data: dict[str, Any]) -> dict[str, Any]:
    """The page's draft (masked text and the form's edits on it) checked as at start. `_real` (the text with the
    secrets in) stays here: configui strips it before anything goes to the page."""
    text = data.get("text")
    if not isinstance(text, str):
        raise web.HTTPBadRequest(text="text must be the draft's text")
    if len(text) > MAX_TEXT:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_TEXT, actual_size=len(text), text="that is too long")
    vault = desk.vault(ui, session)
    path = config_path(ui)
    base = configform.base_of(path)
    old_text = base["text"]
    out: dict[str, Any] = {"ok": False, "problems": [], "text": text, "values": None, "diff": [], "lowering": [],
                           "changed": [], "restart": [], "live": [], "same": False,
                           "stale": data.get("base") not in (None, base["sha256"]), "_real": None}
    try:
        real = vault.unmask(text.replace("\r\n", "\n"))
        if data.get("edits"):
            # An edit's value may carry hidden values too (a server's table as the page has it): put back as well.
            real = vault.unmask(configform.apply_edits(real, data["edits"]))
        masked = vault.mask(real)
    except ConfigError as exc:
        out["problems"] = [vault.scrub(str(exc))]
        return out
    try:
        old_masked = vault.mask(old_text) if old_text.strip() else ""
    except ConfigError:
        old_masked = "# (the file does not parse, so nothing of it is shown)\n"
    out.update(text=masked, values=tomllib.loads(masked), _real=real,
               diff=_diff(old_masked, masked, "config.toml (the file)", "config.toml (draft)"))
    try:
        new = configform.load_text(real)
    except ConfigError as exc:
        out["problems"] = [vault.scrub(str(exc))]      # a startup check may quote the value it refuses
        return out
    old = _old(ui, base)
    changed = configform.changed_sections(old, new)
    # What goes to the page and her card is scrubbed: a reason names a command, a host or an app as written.
    lowering = [{"key": vault.scrub(f.key), "why": vault.scrub(f.why)} for f in configform.lowering(old, new)]
    out.update(ok=True, problems=[vault.scrub(p) for p in configform.refused(old, new)], lowering=lowering,
               changed=changed, restart=[s for s in changed if s not in configform.LIVE_SECTIONS],
               live=[s for s in changed if s in configform.LIVE_SECTIONS], same=real == old_text)
    out["ok"] = not out["problems"]
    return out


def _public(checked_: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in checked_.items() if not k.startswith("_")}


def apply_live(ui: BrainUI, changed: list[str]) -> list[str]:
    """After a save: what the daemon takes at once. [notifications] as the Apply button does; [gestures] and [touch]
    with input.toml over them, as the Input tab's store does (its `enabled` stays: only the camera switch sets it).
    The rest is read at the next start."""
    d = ui.daemon
    applied = []
    if "notifications" in changed:
        d.reload_notifications()
        applied.append("notifications")
    if {"gestures", "touch"} & set(changed):
        fresh = load(config_path(ui))
        if not fresh.input_error:
            d.input.store.apply(fresh.gestures, fresh.touch)
            applied += [s for s in ("gestures", "touch") if s in changed]
    return applied


def _card_line(findings: list[configform.Finding]) -> str:
    more = f" (+{len(findings) - 1} more)" if len(findings) > 1 else ""
    return f"Save the settings? {findings[0].why}{more}."


async def _wait(ui: BrainUI, desk: ConfigDesk, pending: Pending, run: runs.Run) -> None:
    """The run that asked: her line, the wait for the answer, then the write after a yes (if the file is still the
    one the page loaded) or her line that she left it."""
    d = ui.daemon
    runs.active.set(run)
    approval = pending.approval
    try:
        await d.perform(Performance(state="talking", text=ASK_LINE, emotion="alert"))
        outcome = await d.approvals.wait(approval, busy=lambda: bool(getattr(d.listener, "busy", False)))
        line = None
        if outcome == "yes":
            if not d.approvals.verify(approval) or approval.held.arguments.get("sha256") != configform.sha256(pending.text):
                pending.state, pending.detail, line = "failed", "the change is not the one asked about", FAILED_LINE
            else:
                write = pending.write or (lambda text, base: configform.write(config_path(ui), text, base))
                try:
                    backup = await asyncio.to_thread(write, pending.text, pending.base_sha)
                except configform.Stale as exc:
                    pending.state, pending.detail, line = "changed", str(exc), LEFT_CHANGED
                except (ConfigError, OSError) as exc:
                    pending.state, pending.detail, line = "failed", f"not saved ({type(exc).__name__})", FAILED_LINE
                else:
                    approval.made = True
                    pending.state, pending.backup = "saved", str(backup) if backup else None
                    try:
                        pending.applied = pending.apply() if pending.apply else apply_live(ui, pending.changed)
                    except ConfigError:
                        pending.detail = "saved; it is read at the next start"
                    line = SAVED_LINE
                    log.info("ui: %s saved after a yes on %s (%d loosening change(s))%s", pending.file,
                             approval.approval_id, len(pending.findings), " (the old one backed up)" if backup else "")
        elif outcome in ("no", "timeout"):
            pending.state, pending.detail = "left", "no" if outcome == "no" else "no answer in time"
            line = LEFT_LINE if outcome == "no" else SILENT_LINE
        else:
            pending.state, pending.detail = "left", outcome
        if line is not None:
            performance = Performance(state="talking", text=line, emotion="neutral")
            approval.reply = performance, await d.perform(performance)
        if pending.state != "saved":
            log.info("ui: %s not saved: %s (%s)", pending.file, pending.state, pending.detail or outcome)
    except asyncio.CancelledError:
        d.approvals.resolve(approval, "cancelled")
        pending.state, pending.detail = "left", "cancelled"
        raise
    finally:
        d.runs.finish(run)


def ask(ui: BrainUI, desk: ConfigDesk, real: str, base_sha: str, findings: list[configform.Finding],
        changed: list[str], file: str = "config.toml", write: Callable[[str, str], Path | None] | None = None,
        apply: Callable[[], list[str]] | None = None) -> Pending:
    """Her card asks about the change (approvals.py, a `settings` run): a hold for a yes on the card, or the user's
    own yes; this page cannot say yes to it. Only `real`, the text checked here, is ever written after the yes."""
    d = ui.daemon
    run = d.runs.start("settings", foreground=False)
    prompt = _card_line(findings)
    held = Held("strawberry", "settings", {"file": file, "sha256": configform.sha256(real), "base": base_sha},
                question=prompt, risk="change", prompt=prompt)
    approval = d.approvals.request(run, held, timeout_s=CONFIRM_S, hold=True, yes_by=YES_BY)
    pending = Pending(approval, real, base_sha, findings, changed, file=file, write=write, apply=apply)
    desk.pending = pending
    task = d.background(_wait(ui, desk, pending, run), "a settings change waiting for a yes")
    run.continued, run.task = True, task
    log.info("ui: a %s change that loosens safety (%s) waits for a yes on her card (%s)", file,
             ", ".join(sorted({f.key for f in findings})), approval.approval_id)
    return pending


# ----------------------------------------------------------------------------- the routes


@checked("get")
async def api_config(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    desk = request.app[DESK]
    return web.json_response(await asyncio.to_thread(config_view, ui, desk, session))


@checked("post")
async def api_config_check(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    desk = request.app[DESK]
    data = await _payload(request)
    return web.json_response(_public(await asyncio.to_thread(draft_check, ui, desk, session, data)))


@checked("post")
async def api_config_save(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    """Save the draft: 200 when written, 202 when she asks on her card first (GET config's `pending` says how it
    went), 400 with the problems, 409 when the file changed since the page loaded it (`stale`) or a change already
    waits on her card."""
    desk = request.app[DESK]
    data = await _payload(request)
    base_sha = data.get("base")
    if not isinstance(base_sha, str) or len(base_sha) != 64:
        raise web.HTTPBadRequest(text="base must be the file's hash as the page loaded it")
    checked_ = await asyncio.to_thread(draft_check, ui, desk, session, data)
    if checked_["stale"]:
        return web.json_response({"error": "config.toml changed since this page loaded it (another editor, the tray or "
                                           "the camera switch). Reload it, then make the change again.",
                                  "stale": True}, status=409)
    if not checked_["ok"]:
        return web.json_response({"error": "the draft does not check out", "problems": checked_["problems"]}, status=400)
    if checked_["same"]:
        return web.json_response({"saved": False, "same": True})
    if desk.waiting() is not None:
        return web.json_response({"error": "a settings change already waits for your yes on her card; answer it first"},
                                 status=409)
    real = checked_["_real"]
    findings = [configform.Finding(f["key"], f["why"]) for f in checked_["lowering"]]
    if findings:
        pending = ask(ui, desk, real, base_sha, findings, checked_["changed"])
        return web.json_response({"pending": pending.view()}, status=202)
    path = config_path(ui)
    try:
        backup = await asyncio.to_thread(configform.write, path, real, base_sha)
    except configform.Stale as exc:
        return web.json_response({"error": f"{exc}. Reload it, then make the change again.", "stale": True}, status=409)
    except (ConfigError, OSError) as exc:
        return web.json_response({"error": f"not saved ({type(exc).__name__})"}, status=409)
    try:
        applied = apply_live(ui, checked_["changed"])
    except ConfigError:
        applied = []
    log.info("ui: config.toml saved (%s)%s", ", ".join(checked_["changed"]) or "comments only",
             " (the old one backed up)" if backup else "")
    return web.json_response({"saved": str(path), "backup": str(backup) if backup else None, "applied": applied,
                              "restart": checked_["restart"]}
                             | await asyncio.to_thread(config_view, ui, desk, session))


# ----------------------------------------------------------------------------- the Tools page


def _login(name: str) -> dict[str, Any]:
    from .remote import token_file   # local: remote brings the SDK's HTTP client

    try:
        saved = token_file(name).exists()
    except ValueError:
        saved = False
    return {"command": f"strawberry tools login {name}", "saved": saved}


def tools_view(ui: BrainUI, desk: ConfigDesk, session: Session) -> dict[str, Any]:
    """Every server she has (the configured ones and the built-in ones), each tool's tier and flags and whether the
    thinker is offered it, and the servers as config.toml says them now (secrets hidden), for the editor."""
    from . import systemone
    from .adapters import REGISTRY

    d = ui.daemon
    box = d.toolbox
    base = configform.base_of(config_path(ui))
    vault = desk.vault(ui, session)
    try:
        written = tomllib.loads(vault.mask(_starting_text(base)))
        real = tomllib.loads(_starting_text(base))
    except (ConfigError, tomllib.TOMLDecodeError):
        written, real = {}, {}
    in_file = ((written.get("tools") or {}).get("servers") or {}) if isinstance(written.get("tools"), dict) else {}
    real_servers = ((real.get("tools") or {}).get("servers") or {}) if isinstance(real.get("tools"), dict) else {}
    rows = []
    for name, server in box.servers.items():
        builtin = "command" not in server.config and "url" not in server.config
        offered = {spec.name for spec in server.tools}
        names = sorted(server.listed or offered)
        row = {"name": name, "builtin": builtin, "kind": "built in" if builtin else "url" if "url" in server.config
               else "command", "topic": server.topic, "adapter": getattr(server.adapter, "name", None),
               "flags": sorted(server.flags), "unknown": server.unknown, "offer": server.offer, "state": server.state,
               "error": server.failed or None, "in_file": name in in_file,
               "restart": name in real_servers and real_servers[name] != server.config and not builtin,
               "tools": [{"name": n, "tier": box.risk(name, n), "offered": n in offered, "confirm": n in server.confirm,
                          "careful": n in server.careful, "destructive_hint": n in server.destructive} for n in names]}
        if "url" in server.config:
            row["login"] = _login(name)
        rows.append(row)
    running = set(box.servers)
    for name in sorted(set(in_file) - running):
        rows.append({"name": name, "builtin": False, "kind": "url" if "url" in in_file[name] else "command",
                     "topic": in_file[name].get("topic", "other"), "adapter": None, "flags": [], "offer": None,
                     "state": "not running", "error": None, "in_file": True, "restart": True, "tools": [],
                     **({"login": _login(name)} if "url" in in_file[name] else {})})
    return {"enabled": d.config.tools.enabled, "servers": rows, "file": in_file, "path": str(config_path(ui)),
            "base": {"sha256": base["sha256"], "mtime_ns": base["mtime_ns"]},
            "options": {"topics": [o.name for o in systemone.TOPIC.options], "adapters": [a.name for a in REGISTRY],
                        "flags": list(TRUST_FLAGS), "offers": list(OFFERS)},
            "pending": desk.pending.view() if desk.pending is not None else None}


def tools_page(ui: BrainUI, desk: ConfigDesk, session: Session) -> dict[str, Any]:
    """tools_view with every secret the page's vault knows scrubbed out (a server's state or error, as it runs, may
    carry what its table says)."""
    vault = desk.vault(ui, session)

    def scrub(value: Any) -> Any:
        if isinstance(value, str):
            return vault.scrub(value)
        if isinstance(value, dict):
            return {scrub(k): scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value

    return scrub(tools_view(ui, desk, session))


@checked("get")
async def api_tools(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    desk = request.app[DESK]
    return web.json_response(await asyncio.to_thread(tools_page, ui, desk, session))


def add_routes(app: web.Application) -> None:
    app[DESK] = ConfigDesk()
    app.add_routes([
        web.get("/ui/api/config", api_config),
        web.post("/ui/api/config/check", api_config_check),
        web.post("/ui/api/config/save", api_config_save),
        web.get("/ui/api/tools", api_tools),
    ])
