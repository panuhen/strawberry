"""The Brain UI's Config file and Tools tabs (WIRING.md §17, §28): config.toml edited from the page.

    GET  /ui/api/config          the form's schema, the file's text and values (secrets hidden), what the page loaded
                                 (the file's hash and mtime) and a change waiting on her card
    POST /ui/api/config/check    a draft: the text (masked), and the form's edits on it. It comes back as text, checked
                                 as at start (config.load), with its diff against the file, what it loosens
                                 (configform.lowering) and what needs a restart
    POST /ui/api/config/save     a draft that checks out, against the hash the page loaded: written at once (backed up,
                                 one step) when it loosens nothing; else she asks on her card first, and it is written
                                 only after a held Yes there (202, and GET config says how it went)
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
import copy
import logging
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from . import configform, inputs, runs
from .approvals import Approval
from .brainui import BrainUI, Session, _diff, _payload, card_count, checked
from .config import OFFERS, TRUST_FLAGS, ConfigError, default_path, default_toml, load
from .confirm import LEFT_CHANGED, Held
from .contract import Performance

log = logging.getLogger("strawberryd.ui")

MAX_TEXT = 200_000
CONFIRM_S = 60.0                       # how long her card waits for the user's yes to a settings change
# Her card alone, held: never this page, a thumb, or a "yes" said or typed (which may have been meant for another
# question of hers, superseded by this one).
YES_BY = frozenset({"body"})
BUSY = "she is asking something else on her card; answer that first"
NO_CARD = ("her card isn't on screen, so nobody could hold Yes on it: start the crab (or another body that shows her "
           "card), then save again")
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
    base_tag: str
    findings: list[configform.Finding]
    changed: list[str]
    state: str = "waiting"             # waiting | saved | left | changed | failed
    detail: str = ""
    backup: str | None = None
    applied: list[str] = field(default_factory=list)
    at: float = field(default_factory=time.time)
    file: str = "config.toml"          # or input.toml (the Input tab's settings: brainui.api_input_save)
    # After the yes: write(text, base_tag) -> the backup (raises configform.Stale when the file changed), then
    # apply() -> the sections in use at once. None: config.toml's (configform.write, apply_live).
    write: Callable[[str, str], Path | None] | None = None
    apply: Callable[[], list[str]] | None = None

    def view(self) -> dict[str, Any]:
        """For the page: how it went, what it loosens, and the approval as her card shows it (its line, which
        _card_line built from fixed words and scrubbed names, and the time left), so every place on the page that
        shows this change shows it the same way."""
        a = self.approval
        return {"approval_id": a.approval_id, "run_id": a.run_id, "risk": a.risk, "prompt": a.prompt, "hold": a.hold,
                "yes_by": sorted(a.yes_by) if a.yes_by is not None else None,
                "file": self.file, "state": self.state, "detail": self.detail or None,
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

    def busy(self, ui: BrainUI) -> str | None:
        """Why a settings change cannot ask on her card now: one of ours waits, or she is asking anything else (a
        new question would supersede it, and an answer meant for that one could land on this). None: free."""
        if self.waiting() is not None:
            return "a settings change already waits for your yes on her card; answer it first"
        if ui.daemon.approvals.open is not None:
            return BUSY
        return None

    def refusal(self, ui: BrainUI) -> web.Response | None:
        """The 409 for a loosening save that cannot ask now: busy (above), or no body shows her card, so the
        question could only lapse (only a held Yes there takes it). None: she may ask."""
        why = self.busy(ui)
        if why is not None:
            return web.json_response({"error": why, "busy": True}, status=409)
        if card_count(ui.daemon) == 0:
            return web.json_response({"error": NO_CARD, "no_card": True}, status=409)
        return None


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
    return {"path": str(path), "exists": base["exists"], "base": {"tag": base["tag"], "mtime_ns": base["mtime_ns"]},
            "text": text, "values": values, "error": error, "schema": configform.schema(),
            "live": dict(configform.LIVE_SECTIONS), "cards": card_count(ui.daemon),
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
                           "stale": data.get("base") not in (None, base["tag"]), "_real": None}
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
    # input.toml replaces [gestures] (but `enabled`) and [touch] when it is there: what she would run with is the
    # file with it laid over. Both readings are compared; and a change that would make input.toml stop checking out
    # (a touch map whose server now asks first) is refused, never left to break it.
    old_eff, new_eff = copy.deepcopy(old), copy.deepcopy(new)
    inputs.overlay(old_eff, path)
    inputs.overlay(new_eff, path)
    known = known_tools(ui)
    findings = configform.merged(configform.lowering(old, new, known), configform.lowering(old_eff, new_eff, known))
    problems = configform.refused(old, new)
    if new_eff.input_error and not old_eff.input_error:
        problems.append(f"this change would make input.toml stop checking out ({new_eff.input_error}); change the "
                        "Input tab's settings first")
    # What goes to the page and her card is scrubbed: a reason names a command, a host or an app as written.
    lowering = [{"key": vault.scrub(f.key), "why": vault.scrub(f.why), "category": f.category,
                 "detail": vault.scrub(f.detail)} for f in findings]
    out.update(ok=True, problems=[vault.scrub(p) for p in problems], lowering=lowering,
               changed=changed, restart=[s for s in changed if s not in configform.LIVE_SECTIONS],
               live=[s for s in changed if s in configform.LIVE_SECTIONS], same=real == old_text)
    out["ok"] = not out["problems"]
    return out


def known_tools(ui: BrainUI) -> dict[str, Any]:
    """What the toolbox knows of each server now, for the tiers (configform.tier): the tools it lists and the ones it
    marks destructiveHint (empty until it has connected)."""
    return {name: {"listed": frozenset(server.listed) | {spec.name for spec in server.tools},
                   "destructive": frozenset(server.destructive)}
            for name, server in ui.daemon.toolbox.servers.items()}


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


CARD_MAX = 160                         # what her card shows of a line (runs.MAX_PROSE)
ITEM_MAX = 40                          # one command line or address on it, at most
CATEGORY_WORDS = {"runs": "runs", "egress": "reaches", "approvals": "fewer approvals", "privacy": "less privacy",
                  "gestures": "looser gestures"}


def _quoted(text: str, cap: int = ITEM_MAX) -> str:
    """Text from the draft (a command line, an address) as her card may show it: one line, no control characters, no
    double quotes or semicolons of its own (so it cannot close the quote or start a part of the line), cut to `cap`,
    in double quotes."""
    from .trust import CONTROL

    plain = " ".join(CONTROL.sub(" ", text).split()).replace('"', "'").replace(";", ",")
    if len(plain) > cap:
        plain = plain[:max(cap - 1, 1)].rstrip() + "…"
    return f'"{plain}"'


def _card_line(findings: list[configform.Finding]) -> str:
    """Her card's line for a settings change, in wording the draft cannot forge: the fixed words first, every kind of
    loosening there is, most severe first. What runs or connects is shown by its command line or address, each one
    quoted and cut to ITEM_MAX (at most two, then how many more); every other kind by how many settings it is, never by
    a name from the file. "Settings change: runs "notes-mcp --verbose"; approvals ×2; privacy ×1." At most CARD_MAX."""
    kinds = [c for c in configform.CATEGORIES if any(f.category == c for f in findings)]

    def line(cap: int) -> str:
        parts = []
        for kind in kinds:
            mine = [f for f in findings if f.category == kind]
            details = list(dict.fromkeys(f.detail for f in mine if f.detail)) if kind in ("runs", "egress") else []
            if details:
                shown = ", ".join(_quoted(d, cap) for d in details[:2])
                parts.append(f"{CATEGORY_WORDS[kind]} {shown}" + (f" (+{len(details) - 2} more)" if len(details) > 2 else ""))
            else:
                parts.append(f"{CATEGORY_WORDS[kind]} ×{len(mine)}")
        return "Settings change: " + "; ".join(parts) + "."

    # The fixed words always fit (every kind, its count): the quoted items shrink until the whole line does.
    cap = ITEM_MAX
    text = line(cap)
    while len(text) > CARD_MAX and cap > 4:
        cap -= 1
        text = line(cap)
    return text


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
                    backup = await asyncio.to_thread(write, pending.text, pending.base_tag)
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
    except Exception as exc:   # noqa: BLE001 - whatever went wrong, the page must not keep showing "waiting"
        d.approvals.resolve(approval, "cancelled")
        if pending.state == "waiting":
            pending.state, pending.detail = "failed", f"not saved ({type(exc).__name__})"
        log.warning("ui: a settings change failed while it waited (%s)", type(exc).__name__)
    finally:
        d.runs.finish(run)


def ask(ui: BrainUI, desk: ConfigDesk, real: str, base_tag: str, findings: list[configform.Finding],
        changed: list[str], file: str = "config.toml", write: Callable[[str, str], Path | None] | None = None,
        apply: Callable[[], list[str]] | None = None) -> Pending:
    """Her card asks about the change (approvals.py, a `settings` run): a held Yes on the card and nothing else (not
    this page, a thumb, or a yes said or typed, which may have been meant for another question). Only `real`, the text checked here, is ever written after the yes."""
    d = ui.daemon
    run = d.runs.start("settings", foreground=False)
    prompt = _card_line(findings)
    held = Held("strawberry", "settings", {"file": file, "sha256": configform.sha256(real), "base": base_tag},
                question=prompt, risk="change", prompt=prompt)
    approval = d.approvals.request(run, held, timeout_s=CONFIRM_S, hold=True, yes_by=YES_BY)
    pending = Pending(approval, real, base_tag, findings, changed, file=file, write=write, apply=apply)
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
    base_tag = data.get("base")
    if not isinstance(base_tag, str) or len(base_tag) != 64:
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
    real = checked_["_real"]
    findings = [configform.Finding(**f) for f in checked_["lowering"]]
    refused = desk.refusal(ui) if findings else None
    if refused is not None:
        return refused
    if findings:
        pending = ask(ui, desk, real, base_tag, findings, checked_["changed"])
        return web.json_response({"pending": pending.view()}, status=202)
    path = config_path(ui)
    try:
        backup = await asyncio.to_thread(configform.write, path, real, base_tag)
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
            "base": {"tag": base["tag"], "mtime_ns": base["mtime_ns"]},
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
