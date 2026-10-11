"""The Brain UI's Mood view (WIRING.md §17, §29): her living mood, read-only, and a way back to her baseline.

    GET  /ui/api/mood          the two dials now, where they rest (her temperament, shifted by the hour and idle
                               time), the last hours as a graph's points, the reasons (each nudge's source and size,
                               never a sentence), the table of what moves her and its caps
    POST /ui/api/mood/reset    the dials back to where they rest now (a reason `reset`); the view again

The same session, CSRF header and Origin rules as every other route of the page (brainui.checked). Nothing here
changes what she decides: the mood is expression only (mood.py).
"""

from __future__ import annotations

import logging

from aiohttp import web

from .brainui import BrainUI, Session, checked

log = logging.getLogger("strawberryd.ui")


@checked("get")
async def api_mood(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    return web.json_response(ui.daemon.mood.view())


@checked("post")
async def api_mood_reset(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    """Back to her baseline now. 409 with the mood off."""
    mood = ui.daemon.mood
    if not mood.enabled:
        return web.json_response({"error": "the mood is off ([mood] enabled = false)"}, status=409)
    energy, feeling = mood.reset()
    log.info("ui: mood reset to her baseline (energy %.2f, feeling %.2f)", energy, feeling)
    return web.json_response(mood.view())


def add_routes(app: web.Application) -> None:
    app.add_routes([
        web.get("/ui/api/mood", api_mood),
        web.post("/ui/api/mood/reset", api_mood_reset),
    ])
