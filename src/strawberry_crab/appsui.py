"""The Brain UI's Apps tab (WIRING.md §17, §27): which apps she may open or close, saved to apps.toml.

    GET  /ui/api/apps          the list in use, the installed apps for the picker (NoDisplay and Hidden ones left
                               out), the file and its status
    POST /ui/api/apps/check    the tab's list checked as the daemon reads apps.toml, as the file's text and its diff
    POST /ui/api/apps/save     a list that checks out written as apps.toml (atomic, the old file kept as
                               apps.toml.bak) and used at once; never config.toml

The same session, CSRF header and Origin rules as every other write of the page (brainui.checked). Only an
installed desktop id can be added; she launches nothing from here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from . import apps as applist
from . import inputs
from .brainui import BrainUI, Session, _diff, _payload, checked

log = logging.getLogger("strawberryd.ui")


def apps_view(ui: BrainUI) -> dict[str, Any]:
    store = ui.daemon.apps
    installed = store.installed(fresh=True)
    picker = sorted(({"id": e.id, "name": e.name} for e in installed.values() if not e.hidden),
                    key=lambda row: (row["name"].lower(), row["id"]))
    return {"apps": [applist.with_entry(app, installed.get(app.id)) for app in store.apps()],
            "installed": picker, "file": store.status() | {"text": store.text()},
            "limits": {"apps": applist.MAX_APPS, "aliases": applist.MAX_ALIASES, "name": applist.NAME_CHARS,
                       "alias": applist.ALIAS_CHARS}}


def _apps_draft(ui: BrainUI, data: dict[str, Any]) -> dict[str, Any]:
    """The tab's list checked as the daemon reads apps.toml (apps.from_view): the file's text and its diff against
    the file there now. An app already in the file may stay while it is not installed; a new one must be."""
    store = ui.daemon.apps
    kept = tuple(app.id for app in store.apps()) if store.from_file else ()
    try:
        text, _apps = applist.from_view(data.get("apps"), store.installed(), kept)
    except applist.AppsError as exc:
        return {"ok": False, "problems": [str(exc)], "text": None, "diff": []}
    return {"ok": True, "problems": [], "text": text,
            "diff": _diff(store.text(), text, "apps.toml (the file)", "apps.toml (draft)")}


@checked("get")
async def api_apps(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    return web.json_response(await asyncio.to_thread(apps_view, ui))


@checked("post")
async def api_apps_check(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    data = await _payload(request)
    return web.json_response(await asyncio.to_thread(_apps_draft, ui, data))


@checked("post")
async def api_apps_save(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    """A list that checks out written as apps.toml (atomic, the old file kept as apps.toml.bak) and used at once.
    400 with the problems when it does not check out."""
    data = await _payload(request)
    draft = await asyncio.to_thread(_apps_draft, ui, data)
    if not draft["ok"]:
        return web.json_response({"error": "the list does not check out", "problems": draft["problems"]}, status=400)
    store = ui.daemon.apps
    try:
        backup = await asyncio.to_thread(inputs.save, draft["text"], store.path)
    except OSError as exc:
        return web.json_response({"error": f"not saved ({type(exc).__name__})"}, status=409)
    store.check(force=True)
    listed = store.apps()
    log.info("ui: apps.toml saved (%d app(s): %d may open, %d may close)%s", len(listed), sum(a.open for a in listed),
             sum(a.close for a in listed), " (the old one kept as apps.toml.bak)" if backup else "")
    return web.json_response({"saved": str(store.path), "backup": str(backup) if backup else None}
                             | await asyncio.to_thread(apps_view, ui))


def add_routes(app: web.Application) -> None:
    app.add_routes([
        web.get("/ui/api/apps", api_apps),
        web.post("/ui/api/apps/check", api_apps_check),
        web.post("/ui/api/apps/save", api_apps_save),
    ])
