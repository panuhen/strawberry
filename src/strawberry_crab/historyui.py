"""The Brain UI's History view (WIRING.md §17, §30): her history by day, searched, forgotten and her digests edited.

    GET  /ui/api/history               the retention in use, the counts, the reminders, and a page of days (newest
                                       first: each with its digest, or why it has none yet); `?before=DAY` for the
                                       page after it
    GET  /ui/api/history/day?day=DAY   that day's rows, newest first, a page at a time (`&offset=N`)
    GET  /ui/api/history/search?q=     the rows and digests carrying the words (the same search she uses), by day
    POST /ui/api/history/forget/row    {id}: that row gone
    POST /ui/api/history/forget/day    {day}: the day's rows and its digest gone
    POST /ui/api/history/forget/all    {confirm: "forget", timers}: every row and digest (and the reminders only
                                       with `timers`)
    POST /ui/api/history/digest        {day, text}: the user's own digest for the day (`edited_by_user`: the digester
                                       never overwrites it)
    POST /ui/api/history/timer/cancel  {id}: a pending reminder cancelled

The same session, CSRF header and Origin rules as every other route of the page (brainui.checked). Forgetting needs
no card: it only ever makes her know less. A longer retention is a config.toml edit, made in the Config file tab and
asked about on her card there (§28); this module writes no setting.

Row text and digests are the user's own words: they go to the page and nowhere else. A log line here carries counts
and ids only. A row's meta reaches the page only as the fields the store keeps for its kind (`META`), and the store
keeps no tool argument.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from aiohttp import web

from . import history as hist
from .brainui import BrainUI, Session, _payload, checked

log = logging.getLogger("strawberryd.ui")

DAYS_PAGE = 30                 # days a page
ROWS_PAGE = 200                # a day's rows a page
SEARCH_ROWS = 60               # rows a search shows at most
QUERY_CHARS = 200
RECENT_TIMERS = 20             # fired, missed and cancelled reminders shown
CONFIRM = "forget"
META = {"turn": ("did", "withheld"), "run": ("tools", "tier", "outcome", "detail"),
        "approval": ("tool", "risk", "outcome")}
SAID, REPLY = 'the user said "', '"; you said "'      # a turn's text (History.turn), split where meta.said says
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _store(ui: BrainUI) -> hist.History | None:
    """The store when it is on and open; None otherwise (the routes answer 409, the view says why)."""
    store = ui.daemon.history
    return store if store is not None and store.conn is not None else None


def _day(value: Any) -> str:
    """A YYYY-MM-DD day from the page, or a 400."""
    if not isinstance(value, str) or not DAY.fullmatch(value):
        raise web.HTTPBadRequest(text="day must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise web.HTTPBadRequest(text="day must be a real date") from None
    return value


def _id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2 ** 62:
        raise web.HTTPBadRequest(text="id must be a positive whole number")
    return value


def row_view(row: hist.Row) -> dict[str, Any]:
    """A row for the page: its fields, a turn split into the user's words and her reply where it can be, and only
    the meta its kind keeps (never an argument: the store has none)."""
    out = {"id": row.id, "at": row.at, "day": row.day, "kind": row.kind, "source": row.source,
           "foreign": row.foreign, "text": row.text, "run_id": row.run_id,
           "meta": {k: row.meta[k] for k in META.get(row.kind, ()) if k in row.meta}}
    said, text = row.meta.get("said"), row.text
    if row.kind == "turn" and text is not None and isinstance(said, int) and not isinstance(said, bool):
        end = len(SAID) + said
        if text.startswith(SAID) and text.startswith(REPLY, end) and text.endswith('"') and len(text) > end + len(REPLY):
            out["said"], out["reply"] = text[len(SAID):end], text[end + len(REPLY):-1]
    return out


def _state(entry: dict[str, Any], now: float) -> str:
    """digest | digest_only (its raw rows gone) | pending (finished, the digester has not got to it) | open (the day
    has not ended yet: it ends at 04:00)."""
    if entry["digest"] is not None:
        return "digest" if entry["events"] else "digest_only"
    return "pending" if hist.finished(entry["day"], now) else "open"


def history_view(ui: BrainUI, before: str | None = None) -> dict[str, Any]:
    """The view's head: the retention in use, the counts, the reminders and one page of days. Sync: on the store's
    writer thread (History.call)."""
    config = ui.daemon.config.history
    out: dict[str, Any] = {"enabled": config.enabled, "open": False, "error": None,
                           "retention": {"keep_days": config.keep_days, "digest_months": config.digest_months,
                                         "sentences": config.sentences, "lookback_tokens": config.lookback_tokens},
                           "limits": {"digest_chars": hist.DIGEST_CHARS, "digest_line_chars": hist.DIGEST_LINE_CHARS,
                                      "query_chars": QUERY_CHARS, "confirm": CONFIRM}}
    store = ui.daemon.history
    if store is None:
        return out
    stats = store.stats()
    out |= {"open": bool(stats["open"]), "error": stats["error"]}
    if store.conn is None:
        return out
    now = store.clock()
    days = store.days()
    later = [d for d in days if before is None or d["day"] < before]
    page = later[:DAYS_PAGE]
    for entry in page:
        entry["state"] = _state(entry, now)
    timers = store.timers()
    pending = [t for t in timers if t["state"] == "pending"]
    recent = sorted((t for t in timers if t["state"] != "pending"), key=lambda t: (-t["due"], -t["id"]))[:RECENT_TIMERS]
    digester = ui.daemon.digester
    return out | {"today": hist.day_of(now), "now": now, "fts": stats["fts"],
                  "counts": {k: stats[k] for k in ("events", "days", "digests", "timers_pending")},
                  "days": page, "more": page[-1]["day"] if len(later) > DAYS_PAGE else None,
                  "timers": {"pending": pending, "recent": recent},
                  "digester": digester.stats() if digester is not None else None}


def day_view(store: hist.History, day: str, offset: int) -> dict[str, Any]:
    rows = store.rows(day, newest_first=True)
    shown = rows[offset:offset + ROWS_PAGE]
    entry = store.digest_of(day)
    return {"day": day, "total": len(rows), "offset": offset, "rows": [row_view(r) for r in shown],
            "more": offset + ROWS_PAGE if offset + ROWS_PAGE < len(rows) else None, "digest": entry}


def search_view(store: hist.History, query: str) -> dict[str, Any]:
    """The rows (best match first, as she gets them) and the digests carrying the words, grouped by day, newest day
    first."""
    words = hist.search_words(query)
    rows = store.search(query, limit=SEARCH_ROWS) if words else []
    digests = store.search_digests(query) if words else []
    groups: dict[str, dict[str, Any]] = {}
    for day, text in digests:
        groups.setdefault(day, {"day": day, "digest": None, "rows": []})["digest"] = text
    for row in rows:
        groups.setdefault(row.day, {"day": row.day, "digest": None, "rows": []})["rows"].append(row_view(row))
    return {"query": query, "words": words, "rows": len(rows), "digests": len(digests), "capped": len(rows) >= SEARCH_ROWS,
            "groups": sorted(groups.values(), key=lambda g: g["day"], reverse=True)}


def _off(ui: BrainUI) -> web.Response:
    if ui.daemon.history is None:
        return web.json_response({"error": "the history is off ([history] enabled = false)"}, status=409)
    return web.json_response({"error": "the history file is not open this run"}, status=409)


# ----------------------------------------------------------------------------- reading


@checked("get")
async def api_history(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    before = request.query.get("before")
    if before is not None:
        before = _day(before)
    store = ui.daemon.history
    if store is None:
        return web.json_response(history_view(ui))
    return web.json_response(await store.call(history_view, ui, before))


@checked("get")
async def api_history_day(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    day = _day(request.query.get("day"))
    try:
        offset = int(request.query.get("offset", "0"))
    except ValueError:
        raise web.HTTPBadRequest(text="offset must be a whole number") from None
    if not 0 <= offset < 10 ** 7:
        raise web.HTTPBadRequest(text="offset out of range")
    store = _store(ui)
    if store is None:
        return _off(ui)
    return web.json_response(await store.call(day_view, store, day, offset))


@checked("get")
async def api_history_search(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    query = request.query.get("q", "").strip()
    if len(query) > QUERY_CHARS:
        raise web.HTTPBadRequest(text=f"a search is at most {QUERY_CHARS} characters")
    store = _store(ui)
    if store is None:
        return _off(ui)
    return web.json_response(await store.call(search_view, store, query))


# ----------------------------------------------------------------------------- forgetting and editing


@checked("post")
async def api_forget_row(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    row_id = _id((await _payload(request)).get("id"))
    store = _store(ui)
    if store is None:
        return _off(ui)
    if not await store.call(store.forget_row, row_id):
        return web.json_response({"error": "no such row (already forgotten?)"}, status=404)
    log.info("ui: a history row forgotten (id %d)", row_id)
    return web.json_response({"forgotten": row_id})


@checked("post")
async def api_forget_day(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    day = _day((await _payload(request)).get("day"))
    store = _store(ui)
    if store is None:
        return _off(ui)
    rows = await store.call(store.forget_day, day)
    log.info("ui: a history day forgotten (%d rows and its digest)", rows)
    return web.json_response({"day": day, "rows": rows})


@checked("post")
async def api_forget_all(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    """Everything: every row and digest; the reminders too only with `timers` true. The page has the user type the
    word first; the route wants it too."""
    data = await _payload(request)
    timers = data.get("timers", False)
    if data.get("confirm") != CONFIRM or not isinstance(timers, bool):
        raise web.HTTPBadRequest(text=f'forgetting everything needs {{"confirm": "{CONFIRM}"}} and a true or false '
                                      '"timers"')
    store = _store(ui)
    if store is None:
        return _off(ui)
    rows = await store.call(store.forget_all, timers)
    log.info("ui: the whole history forgotten (%d rows%s)", rows, ", the reminders too" if timers else "")
    return web.json_response({"rows": rows, "timers": timers})


def _digest_text(value: Any) -> str:
    """The user's digest as written, or a 400: some text, no line over DIGEST_LINE_CHARS, at most DIGEST_CHARS in all
    (the store would cut it silently; the page says so instead)."""
    if not isinstance(value, str):
        raise web.HTTPBadRequest(text="text must be a string")
    lines = [" ".join(line.split()) for line in value.replace("\r\n", "\n").split("\n")]
    text = "\n".join(line for line in lines if line)
    if not text:
        raise web.HTTPBadRequest(text="a digest needs some text (to drop the day, forget it)")
    if any(len(line) > hist.DIGEST_LINE_CHARS for line in lines):
        raise web.HTTPBadRequest(text=f"a line is at most {hist.DIGEST_LINE_CHARS} characters")
    if len(text) > hist.DIGEST_CHARS:
        raise web.HTTPBadRequest(text=f"a digest is at most {hist.DIGEST_CHARS} characters")
    return text


def _edit(store: hist.History, day: str, text: str) -> dict[str, Any] | None:
    """The edit, on the writer thread: only a day the history has (rows or a digest)."""
    with store.lock:
        known = store.digest_of(day) is not None or store._query("SELECT 1 FROM events WHERE day = ? LIMIT 1", (day,))
        if not known:
            return None
        return store.edit_digest(day, text)


@checked("post")
async def api_digest(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    data = await _payload(request)
    day, text = _day(data.get("day")), _digest_text(data.get("text"))
    store = _store(ui)
    if store is None:
        return _off(ui)
    try:
        saved = await store.call(_edit, store, day, text)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    if saved is None:
        return web.json_response({"error": "the history has nothing of that day"}, status=404)
    log.info("ui: a digest edited by the user (%d chars)", len(saved.get("text") or ""))
    return web.json_response({"digest": saved})


def _cancel(store: hist.History, timer_id: int) -> str | None:
    """Cancel a pending timer, on the writer thread. Its state before, or None when there is no such timer."""
    with store.lock:
        found = store._query("SELECT state FROM timers WHERE id = ?", (timer_id,))
        if not found:
            return None
        if found[0][0] == "pending":
            store.set_timer_state(timer_id, "cancelled")
        return found[0][0]


@checked("post")
async def api_timer_cancel(request: web.Request, ui: BrainUI, session: Session) -> web.Response:
    timer_id = _id((await _payload(request)).get("id"))
    store = _store(ui)
    if store is None:
        return _off(ui)
    was = await store.call(_cancel, store, timer_id)
    if was is None:
        return web.json_response({"error": "no such reminder"}, status=404)
    if was != "pending":
        return web.json_response({"error": f"that reminder is not pending ({was})"}, status=409)
    log.info("ui: a reminder cancelled (id %d)", timer_id)
    return web.json_response({"cancelled": timer_id})


def add_routes(app: web.Application) -> None:
    app.add_routes([
        web.get("/ui/api/history", api_history),
        web.get("/ui/api/history/day", api_history_day),
        web.get("/ui/api/history/search", api_history_search),
        web.post("/ui/api/history/forget/row", api_forget_row),
        web.post("/ui/api/history/forget/day", api_forget_day),
        web.post("/ui/api/history/forget/all", api_forget_all),
        web.post("/ui/api/history/digest", api_digest),
        web.post("/ui/api/history/timer/cancel", api_timer_cancel),
    ])
