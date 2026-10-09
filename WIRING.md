# Strawberry — Harness & Wiring Spec

**Target:** the glue that makes the finished crab react. OS notifications, git events, and voice all flow through one backend into a Godot desktop widget, which speaks (TTS), listens (STT), and shows a speech bubble the local model writes.

**For:** an agent (Claude Code / Codex) and the human running it.

**Assumes done:** `model/strawberry_v2.glb` — rig, seven clips, morphs, cel shader, verified in Godot 4.7.2 (see `model/README.md`). This spec references those names in §9; they must match.

**Assumes running:** Ollama serving Qwen3.8 on `127.0.0.1:11434`; dunst as the notification daemon; faster-whisper and Piper installed. (As of Phase 1 only Ollama is present on the machine.)

---

## 0. The one idea

Every feature is the same shape: **an event becomes one JSON message to Godot.** Notifications, git, voice: different doorways into the same hallway. Build the message path once; everything else plugs in.

There are **two hops**, and the common mistake is skipping the first:

```
short-lived event scripts ──HTTP──▶  strawberryd (daemon)  ──websocket──▶  Godot widget
(dunst script, git hook)             (brain, TTS, STT, orchestration)      (the performer)
```

Event scripts are short-lived. They must **not** each open a websocket to Godot. They fire a quick HTTP POST at the always-running daemon. The daemon holds the single Godot connection.

---

## 1. The message contract (built first, both sides implement it)

Daemon → Godot, one JSON object per performance:

```json
{
  "state": "talking",
  "anim": "notify_perk",
  "text": "Someone pushed to main again…",
  "audio": "/tmp/strawberry_line.wav",
  "emotion": "alert"
}
```

| Field | Req | Meaning |
|---|---|---|
| `state` | yes | `idle` \| `listening` \| `thinking` \| `talking` \| `dancing` → the five loops |
| `anim` | no | one-shot reaction played over the state: `alert_snap` \| `notify_perk` |
| `text` | no | speech-bubble text (omit = no bubble) |
| `audio` | no | path to a wav; **present** = play + drive claws, **absent** = silent mode |
| `emotion` | no | `neutral` \| `happy` \| `alert` \| `angry` — tints bubble / picks `anim` |
| `reaction` | no | procedural recipe layered over the clip: `wave` \| `peek` \| `shiver` \| `double_hop` \| `nod` (§13) |
| `icon` | no | path to an image shown as a badge beside the bubble (the notifying app's icon) |
| `hop` | no | `true`: the window itself bounces on the desktop |

Optional fields are omitted on the wire, never sent as `null`. Unknown fields, unknown states, and missing audio files are rejected by the daemon with a 400 and a reason.

**Godot's behaviour on receipt:** apply `state` (crossfade to its loop); if `anim` present, fire it as a one-shot and resume the state's loop when it ends; if `text`, show the bubble; if `audio`, play it through the analysed bus (§6). When speech/bubble finishes and the state was `talking`, **auto-return to her resting state**: `dancing` if she was dancing before the line, otherwise `idle`. The daemon doesn't send a follow-up. `listening`/`thinking` are pipeline transients and are not remembered; `dancing` persists until the next `idle`. A performance with no line (no `text`, no `audio`) never stops the wav she is playing: its state is taken at once and she finishes the sentence in it (the media doorway's `dancing` arrives whenever the music starts). A new line replaces the one playing, and `listening` stops it, since her voice would go into the microphone.

**Transport:** the **daemon is the websocket server**, Godot is a **client** that connects out and auto-reconnects with backoff (1 s doubling to 8 s). localhost only. One port carries both HTTP and the websocket (`/ws`).

**Liveness, both ends.** On shutdown the daemon closes every widget socket with `1001 going away` and exits within 2 s, so a restart is noticed immediately (aiohttp would otherwise hold the handler for up to 60 s and the widget would sit on a dead socket). The widget also sends `{"type":"ping"}` every 5 s, gets `{"type":"pong"}`, and drops and reconnects after 12 s of silence, covering a half-open socket after sleep/wake. `scripts/check_reconnect.sh` (and `SIGNAL=KILL …`) proves both paths: drop noticed at 0.0 s, reconnected in 2.6 s.

**Hello and the version handshake.** On connecting the widget sends `{"type": "hello", "client": "strawberry-widget", "version": …, "godot": …}`. `version` is `application/config/version`, which `scripts/build_widget.sh` stamps into the exported binary from `pyproject.toml`; a source run has none and says `dev`. The daemon compares it with its own package version (`server.version_verdict`): `dev` is always served; the same major with another minor or patch is served with a warning in the log; another major is **refused**: an error line (`refusing widget 1.0.0: its major version differs from strawberryd 0.1.0 …`) and the socket closed with code `4001` and the reason `widget X, daemon Y`. The widget shows that reason once in her bubble and retries every 60 s instead of 1–8 s, in case the daemon is upgraded underneath it. A hello without a readable version is served with a warning. `/health` lists `widget_versions` (one per open socket that said hello) and `version` (the daemon's).

Source of truth: `src/strawberry_crab/contract.py` and the constants at the top of `widget/widget.gd`.

**The full wire protocol** (every message both ways, the HTTP routes, the handshake, and the proposed v2 additions for other bodies) is [PROTOCOL.md](PROTOCOL.md). Write a new body from that file.

---

## 2. Backend daemon — `strawberryd`

Long-running Python (asyncio, aiohttp). One port, default **8770**:

- `POST /event` — accepts `{source, app, title, body, urgency}`. This is what the hooks hit. `source` ∈ `notification|git|voice|manual`; `urgency` is normalised from dunst's `LOW|NORMAL|CRITICAL`.
- `POST /command` — `{command, value}` from the tray, broadcast to every widget (§14). The name and the value's type are checked here, so a typo is a 400 and not a silent nothing.
- `POST /perform` — accepts a raw contract blob. For `curl`, tests, and callers that already know what they want.
- `GET /health` — `{ok, widgets, performed, uptime_s, state, rest_state, brain: {model, loaded, calls, fallbacks, last_latency_s}}`. `state` is what she is doing now (a transient older than 6 s reads as her resting state, because the widget returns to it on its own); the tray's status row is this field. `tempo` is the fresh beat estimate (§4c) and `tempo_age_s` the seconds since beat_watch last posted one (`null`: never since the daemon started); `strawberry doctor` reads it to tell a watcher that stopped from one with nothing playing.
- `GET /config` — the effective settings (§15).
- `POST /probe` — `strawberry doctor --talk`: the daemon's own two scripted sentences (`Daemon.PROBE_LINES`) through the gate, the desktop voice (Gemma), the brain (Qwen, offered no tools so nothing changes), Piper and whisper (reading Piper's wav back), each timed. It takes no text, performs nothing, records nothing in the ledger, logs only the times, and answers a loopback client only.
- `GET /ws` — the widget connects here.
- `POST /ui-token`, `/ui/...` — the Brain UI (§17): a one-time login token for `strawberry ui`, and the page with its own API.
- **Core function:** `Daemon.perform(performance)` builds the blob, (Phase 4) runs TTS, and sends to Godot. Everything routes through it.

**What she does is owned by the daemon**, so the model never names a clip or a recipe; it only picks the emotion. `strawberry/reactions.py` maps *(source, app/category, urgency, emotion)* to a one-shot clip, a recipe (§13), and whether the window hops:

| Event | anim | reaction | hop |
|---|---|---|---|
| notification from a messaging app (`im.*`/`email.*` category, or WhatsApp/Telegram/Slack/Thunderbird/…) | – | `wave` | yes |
| notification, critical urgency or `angry` | `alert_snap` | `shiver` | – |
| coalesced burst ("N notifications…") | – | `double_hop` | – |
| other notification, `alert` / `happy` / `neutral` | – / `notify_perk` / – | `peek` / – / `nod` | – |
| git `post-commit` | `notify_perk` (`alert_snap` if angry) | – | – |
| git `pre-push`, media track change, voice | – | `nod` | – |

Critical beats message (a critical WhatsApp still shivers). The app icon resolved by the doorway rides along as `icon`.

**Local tools only.** Requests carrying a browser `Origin` header are refused (403) on every route but the Brain UI's under `/ui` (§17), including `/ws`, `/health` and `/ui-token` (one middleware, `server.local_only`, so a new route cannot miss it; `/health` holds the ledger), and POSTs with a body must be `Content-Type: application/json` (415 otherwise). Godot, curl, and the doorways never send `Origin`; browsers always do. Without this a web page could make her talk, open `/ws` and read notification text as it goes past, or read the user's recent sentences from `/health`.

**Shutdown.** `server.serve` runs the app itself rather than through `web.run_app`: SIGTERM or SIGINT only sets a stop event, then `runner.cleanup()` runs to the end: widgets are closed with GOING_AWAY, then `Daemon.close()` closes every part (each one even if another fails). The journal says `SIGTERM: shutting down` and `shut down; sessions closed`. Stopping the tray unit signals its whole cgroup and the tray also terminates its children, so the daemon sees SIGTERM twice within a millisecond; the second one is logged (`SIGTERM during shutdown ignored`) and changes nothing. run_app could not do this: its handler raises GracefulExit out of the loop, and a second signal read while it was still closing the listening socket, before our first shutdown hook ran, raised it again in the middle of the cleanup, cancelling it and leaving the brain, gate and thinker sessions to the garbage collector ("Unclosed client session" in the journal). The handlers stay until the loop closes. The port opens when `Daemon.start` returns (`strawberryd listening on …, N.Ns after the start` is logged then; before 2026-09-23 the line came first, while nothing listened yet), and since 2026-09-25 that is at once: no model is awaited (below). A SIGTERM during the start cancels it, which closes what was opened.

**Nothing waits for a model at start** (2026-09-25). With the gate on ONNX in-process the start had grown to ~5 s (1.7 s to load the model, ~3.8 s to embed the ~357 examples on the CPU), on top of Piper and the reaction model's warm-up, all before the port opened. Now `Daemon.start` calls each part's `begin()`, which starts the load as a task and returns, and the port answers in ~0.3 s (measured on a throwaway daemon: port 0.3 s, Piper ready 2.1 s, the gate 4.7 s). What arrives first is handled as it would be with that part unavailable, or waits a little:

| part | while it loads | `/health` |
|---|---|---|
| gate (`Gate.begin`) | a sentence waits up to `Gate.START_WAIT_S` (2 s) for it, then is chat (`gate: still starting after 2.0s; treating a sentence as chat`), as when the gate is down; a notification body waits up to `retry_timeout_s` (15 s), as for a reload, and is read; a resume during the start joins it | `gate.ready: false`, `gate.starting: true` |
| Piper (`Speaker.begin`) | a line waits for the voice in `say()` (a second or two, local disk), so the first line is not silent | `speech.loading: true` |
| reaction model (`OllamaReactor.begin`) | the usual 1.5 s call; a timeout is the canned line and joins the warm-up (`schedule_rewarm`), whose long call keeps Ollama's load going | `brain.loaded: false` |
| whisper (`Listener.start`, §7) | `/listen` says she is still getting her ears on | `voice.phase: "loading"` |

A part without `begin()` (the tests' stand-ins) is started in full. `close()` cancels and awaits a load still running, so a stop during it closes cleanly. The MCP servers already connected in the background (`preconnect`), and the thinker's session opens at once. Tests: `tests/test_background_start.py`. `tests/test_server.py` stops the tray's own daemon command with a second SIGTERM during the site stop and counts the sessions left open.

**On Windows** SIGTERM is TerminateProcess, which nothing can catch, so the daemon also waits on a named event, `Local\strawberry-stop-<key>` (`winproc.py`), and setting it is the same stop (`stop requested: shutting down`). The key is its own pid and the one its parent passes in `STRAWBERRY_STOP_EVENT` (a venv's `python.exe` starts the real interpreter as a child, so the pid the parent holds is not the interpreter's). The event is in the logon session's own namespace with the user's default DACL; nothing new listens on a port. `strawberry stop` and the tray set it and fall back to TerminateProcess after a timeout (§14).

**The access log** (2026-09-22) is aiohttp's line per request at INFO, minus the polling: a successful `GET /health` (the tray, every 2 s) or `POST /tempo` (beat_watch, every `interval_s`) is not logged, and a failing one still is (`server.QuietAccessLogger`, `QUIET_ROUTES`). The line is the request line, status, size and user agent; no request body is ever logged.

Run: `strawberryd [--port 8770]` (the console script; in a checkout `.venv/bin/strawberryd` after `uv sync --inexact --group gpu`), or `strawberry daemon`, which also starts the doorways.

---

## 3. The brain — two paths, don't conflate them

- **Reaction path (no tools):** a notification or commit needs a fast one-liner, not tool use. `OllamaReactor` (`strawberry/brain.py`) calls `POST /api/chat` on a **small, always-resident model** with the persona as system prompt, five example exchanges, and the output **forced to a JSON schema** `{"line": str, "emotion": neutral|happy|alert|angry}`. Thinking mode off, 1.5 s timeout, then the canned line instead. The model is loaded at daemon start with `keep_alive = -1` so the first event never waits for a cold load.
- **Action path (tools, and her voice):** everything the user says — a command like "skip the track", a question, or small talk — goes to the big model with the MCP tools (§8), except the bare reflexes that need no model at all. It answers as her; nothing is added on top.

Both paths end by calling `perform(...)`. Route notifications/git/media → reaction path; voice → action path (reaction path again when `[thinker] enabled = false`).

**Model choice (bake-off, `scripts/reactor_bakeoff.py`, results in `scripts/bakeoff_*.json`).** The reflex needs speed and residency, not intelligence: the 27B would cold-load for 10–20 s after a quiet half hour and hog the GPU. Among ~1B models on the Ollama library, **`gemma3:1b`** won: 815 MB, ~0.5 s warm, 0/32 schema misses, short lines (median 7 words), emotions right, and it does not invent details. `qwen3.5:0.8b` was as fast and livelier but hallucinated specifics ("Dinner Friday at 7 PM" for "dinner on Sunday?"), leaked the examples, and ran long. For a mascot reading your real notifications, saying less beats saying wrong. The decisive lever was **few-shot examples**: with the description alone Gemma answered "Interesting…" to everything; with five example exchanges it became a crab. The examples live in config (§15), so her voice is tunable without code.

**Keeping a 1B model from repeating itself.** Left alone it finds a pet adjective and puts it in every line ("lovely" twelve times in one evening, after the persona once listed it as an example word: never name favourite words in the persona). Three counters in `brain.py`: the example order is shuffled per call, so no single example is the template; the reactor remembers her last eight lines and, when a new line reuses a content word from two or more of them (or the same opener three times), asks once more with those words banned and the temperature raised by 0.3, keeping the second line if it is less stale, within the same time budget; and the persona asks for varied sentence shapes. `/health` counts the `retries`.

The interface is `Reactor` (`strawberry/events.py`): `async react(event) -> Performance`. `CannedReactor` (fixed line per source, no model) remains as the fallback and as `brain.enabled = false`.

---

## 4. Doorway: notifications — `strawberry/doorways/notify_watch.py`

**On this machine the notification daemon is GNOME Shell, not dunst**, and dunst cannot run beside it (both claim `org.freedesktop.Notifications`). So there is no script hook. Instead the watcher opens a private **monitor connection** to the session bus (`org.freedesktop.DBus.Monitoring.BecomeMonitor`, unprivileged for the user's own bus) with a match on `Notify` method calls, and sees every notification as it goes past. GNOME still shows it normally; the watcher only listens.

**On jeepney, not PyGObject** (2026-09-22). Both D-Bus watchers are modules in the daemon's package and run on its interpreter (`strawberry-doorway notify_watch`, or `python -m strawberry_crab.doorways.notify_watch` as the tray starts it), so a packaged install needs no system `gi`. `strawberry/bus.py` is the whole of the plumbing: jeepney hands over raw messages, and it matches replies to calls by serial, queues everything else for a consumer task, and **notices when the socket dies** — a watcher whose bus went away exits non-zero and is started again rather than going quietly deaf.

Three things the monitor connection insists on:

- **It is a connection of its own, and it may only listen.** `BecomeMonitor` is an ordinary method call (`asu`: the match rules and a flags word that must be 0) sent right after the Hello handshake; from the reply onwards the bus stops routing normal traffic to it, and a monitor that *sends* anything is disconnected. That is why the daemon is reached over HTTP from here and never from the bus connection. (With GDBus this was also the reason the filter had to return `None` and swallow the message; jeepney never answers anything, so there is nothing to swallow.)
- **Every Notify crosses the bus twice** on this desktop: the app sends it to a relay, and the relay re-sends it to the shell with a new sender and serial. Only the content is shared, so repeats are dropped by content inside a 1.5 s window (`Deduper`).
- **The match rule is `type='method_call',interface='org.freedesktop.Notifications',member='Notify'`** — calls, not signals: the notification never comes back as one.

Without PyGObject there is no `Gio.DesktopAppInfo`, so the `.desktop` file's `Icon=` is read by hand from the XDG application directories (`desktop_icon_name`).

`Notify(app_name, replaces_id, app_icon, summary, body, actions, hints, expire_timeout)` becomes `{source: "notification", app, title: summary, body, urgency, category, icon}`; urgency comes from the `urgency` hint byte (0/1/2 → low/normal/critical), markup and entities are stripped from the body, and `desktop-entry` stands in when an app sends no name. `icon` is the app's icon resolved on disk: `app_icon` if it is a path, else the `.desktop` file's `Icon=`, the desktop-entry id, or the app name, looked up under `~/.local/share/icons`, `/usr/share/icons/{hicolor,Yaru,Adwaita}`, `/var/lib/snapd/desktop/icons` and `/usr/share/pixmaps` (SVG or PNG). No icon found means no badge, nothing else changes.

**What gets forwarded** is `[notifications]` in the config (§15):

| Setting | Default | Effect |
|---|---|---|
| `ignore_apps` | `["Spotify"]` | music is already covered by the media doorway |
| `only_apps` | `[]` | non-empty: forward these apps only |
| `min_urgency` | `"low"` | drop anything below |
| `body` | `"off"` | what she does with a message body: `off` \| `react` \| `glance` (below) |
| `body_apps` | `{}` | per app, case-insensitive, by app name or desktop entry: `{ Slack = "glance", Signal = "off" }` |
| `max_body_chars` | `1000` | cut at a word boundary, with an ellipsis |
| `ignore_replacements` | `true` | updates to an existing notification (download progress) |
| `coalesce_s` | `2.0` | several inside the window become one event: "7 notifications from Slack" / "3 notifications", highest urgency wins |

Her own notifications (app `strawberry`) are always ignored. This is also how WhatsApp / Messenger reach her: you react to the **desktop notification**, never their APIs. A machine running dunst could post the same event from a `dunstrc` `script =` rule.

**Message bodies** (2026-09-22). The monitor sees every notification any app posts, IM bodies included. With the old default (`include_body = true`) a test Slack message's code word went into Gemma's prompt and she said it aloud, and `client.py` wrote every body into the journal. Nothing left the machine; it is still not a default. Three modes, per app:

| mode | what crosses to the daemon | what Gemma sees | what she says |
|---|---|---|---|
| `off` (default) | app, sender (title), urgency, category | app and sender | her line about *who* wrote ("Sam's written to you."); canned fallback `App: Sender` |
| `react` | + the body | + the body, with the rule never to repeat its words, names, numbers or links | her own one-liner about it |
| `glance` | + the body | call 1, no persona: the body, for a gist; call 2, her voice: the gist only | the gist ("Alex asks about lunch at noon.", ≤ 12 words, third person), then her quip (≤ 8 words) |

`off` is enforced twice: the watcher does not put the body in the POST (it never crosses even localhost HTTP), and the daemon drops a body that arrives anyway (an old watcher, a `curl`). A burst's body is the senders' titles, not message text, so it stays in every mode. **Switching the mode live**: the tray's Message bodies ▸ Off / React / Glance (§14) writes `body` into config.toml, then the daemon re-reads `[notifications]` and the watcher restarts, in that order; each reader applies the new mode from its next notification, and nothing needs restarting by hand. Glance is `Daemon.report()`'s shape: the fact first, then the quip, and the quip's prompt never contains the body. The gist is refused (and she reacts instead) if it carries any digit, link or address; a `react` line that carries a link, a number from the body, or four body words in a row is replaced by the canned `App: Sender`. The old `include_body = true|false` still reads, as `react|off`, with one deprecation warning.

**The sensitive filter, fail closed** (`strawberry/privacy.py`). Before any body reaches Gemma, in `react` or `glance`, the daemon (it owns the gate) runs two checks; either one says yes and the body is dropped and she says only `"<app> sent something private."`, whatever the mode:

1. **Patterns**, plain code, on the title and the body: a 4–8 digit run (or `G-123456`, `123-456`) within 60 characters of *code, OTP, PIN, passcode, verification, one-time, 2FA, MFA, security/login/sign-in/confirmation code*; a body that is only a code; reset and magic-login wording or links (`token=`, `/reset`, `verify` in a URL); "do not share this code", "code expires". PR numbers (`#4821`), times, decimals and a number with no code word near it pass. The title is checked in `off` too: some apps put the code in the summary.
2. **`IS_SENSITIVE`**, a Noul on the gate's embedder (§8a; `systemone.py`): yes-examples are verification codes, password changed/reset, new sign-in from a device, confirm this login, bank and card transactions, account locked or security notices; no-examples are chat, work messages, CI results, calendar reminders, updates, music, battery. It reads `"<title>: <body>"`; p(yes) ≥ 0.5 drops. A separate question on the same embedder, embedded at start with the routing examples; it does not touch the voice questions (`gate_check.py` still 71/71).

**Fail closed**: a gate that is disabled, still warming, or erroring counts as a yes. So `react` and `glance` need `[gate]`; without it every body is "something private". Cost, measured 2026-09-22: one embedding call per body, **151 ms median, 182 ms max** (`scripts/sensitive_check.py`, warm); on the live daemon 141–174 ms. A glance is one more Gemma call (~0.6 s for the gist); end to end ~0.8 s for `react`, ~1.5 s for `glance`, 0 ms extra for `off`. Once, the first two calls after the gate's start hit the 2 s timeout (3.8 s; the GPU was busy): those bodies were dropped as private, as designed, and the gate reloaded in the background.

**Slow is not down** (0.1.1). After a resume from suspend Ollama had unloaded embeddinggemma; the next notification's check hit the 2 s timeout, the body was dropped as private, and the model finished loading 9 s later. A timeout now means "loading", not "down": `Gate.sensitive()` waits for the gate's one background reload (`Gate.warm()`, the 120 s warm-up allowance) for up to `[gate] retry_timeout_s` (15 s), then asks once more with what is left of that budget (never less than `timeout_s`). A notification may be late; it is not dropped for being slow. It still fails closed:

| what happened | what the check does |
|---|---|
| timeout (`GateTimeout`: Ollama took longer than `timeout_s`) | wait for the shared reload, ask once more; a second timeout, or a reload still running after `retry_timeout_s`, counts as sensitive |
| hard error (`GateError`: connection refused, HTTP 4xx/5xx, a malformed reply) | counts as sensitive at once, no retry: Ollama not running, or a runner that failed to load the model (Ollama's 500), will not be fixed in 15 s |
| a reload already running when the notification arrives (a burst at wake) | skips the doomed short call and waits for that reload, so N notifications share one wait instead of N in a row |
| `retry_timeout_s = 0` | the old behaviour: a timeout counts as sensitive, and the reload starts in the background |

The reload is shielded: a notification that gives up does not cancel it, so the next one finds the model warm. Nothing else waits for it: each POST `/event` is its own aiohttp task, so a commit, a track change or a spoken sentence arriving meanwhile is handled at once (`tests/test_gate_retry.py`). The voice path never retries (§8a). The watcher waits up to 30 s for the daemon's reply (`notify_watch.POST_TIMEOUT_S`; the post runs in a thread, so the bus reader keeps reading), where a slow reply used to be logged as "strawberryd unreachable". The journal gets the timings and nothing of the message: `is_sensitive timed out after 2.0s (TimeoutError); waiting up to 15s for embeddinggemma to load, then asking once more`, `embeddinggemma reloaded after a timeout in 8.7s`, `is_sensitive answered on the retry, 10.9s after the notification arrived`. `/health.gate` counts `retries` and `warmups` and says whether a reload is `warming`.

**Warm on wake** (`strawberry_crab/wake.py`). The daemon also watches logind's `org.freedesktop.login1.Manager.PrepareForSleep` on the **system** bus (jeepney, its own connection, match on `sender='org.freedesktop.login1'`). On `PrepareForSleep(false)`, the resume, `Daemon.warm_models("on resume")` starts the gate's reload (a one-word embedding) and the reaction model's (`OllamaReactor.schedule_rewarm`: an empty `/api/generate` with the configured `keep_alive`) in the background, before a notification needs them; the thinker's big model is left to load on demand. Each part keeps at most one load in flight and joins it when asked again, so a doubled signal or a timeout meanwhile starts nothing new. No system bus or no logind (a container, CI, the unit tests) is one `wake: no system bus (…); no model warm-up on resume` line and nothing else; a system bus that drops later is reconnected every 30 s. `[daemon] warm_on_wake = false` turns it off; `/health.wake` shows `watching` and the count of `resumes`. Tests: a fake system bus carrying the real serialised signal (`tests/test_wake.py`).

**Warm on wake on Windows** (`strawberry_crab/winwake.py`, WINDOWS.md step 7). `wake.watcher()` picks the system's watcher at runtime; on Windows it is `PowerWatcher`, which registers a callback with `PowerRegisterSuspendResumeNotification(DEVICE_NOTIFY_CALLBACK)` (powrprof, ctypes). The daemon has no window, so a callback fits it better than `WM_POWERBROADCAST` on a hidden one; the event codes are the same. Windows calls it on a thread of its own; the callback only hands the code to the daemon's loop (`call_soon_threadsafe`) and returns, because Windows waits for it on the way into a suspend. `PBT_APMRESUMEAUTOMATIC`, which comes on every resume (from sleep or hibernation, a user there or not), is the resume and calls the same `Daemon.warm_models("on resume")`; `PBT_APMRESUMESUSPEND`, which follows it when a user is there, is the same resume and is ignored; `PBT_APMSUSPEND` is one log line. After that everything is shared: the gate's one reload and "slow is not down" above, so a notification right after a resume waits for the reload and is read, not dropped as private. A registration that fails is one `wake: no power notifications (…); no model warm-up on resume` line; the registration is undone when the daemon stops. `/health.wake` is the same. Tests (`tests/test_winwake.py`): a fake registration firing suspend and resume from another thread, the notification after a resume read normally against a cold fake Ollama, and on Windows the real registration, whose callback is called through the very function pointer Windows holds; `tests/conftest.py` refuses the real registration everywhere else. The machine was never suspended for them.

`scripts/sensitive_check.py` is the contract, like `gate_check.py`: it builds the gate the way the daemon does and runs the whole filter over `scripts/sensitive_phrases.json` (38 notifications, 20 sensitive, 18 not, including "meeting at 1400", "PR #4821 merged", "3 tests failed"); it prints the gate-alone tally too and exits 1 on a miss. 38/38, the gate alone 38/38. A notification she misjudges goes in the file, and the fix goes in `IS_SENSITIVE` or the patterns.

**No message text in logs, anywhere.** `client.py` logs an event's source, app, keys and `body_len`; the watcher logs app, title and `body_len`; the daemon logs the privacy decision (`private, pattern: one-time code`, `body read, mode glance, clear 0.02 (149 ms)`) and lengths; the gate logs p(yes) at DEBUG, never the text; the prompt is never logged. Her own generated line is logged, as for every event. The user's own sentences are not either, unless `[daemon] log_sentences` is on (§15). `tests/test_privacy.py` runs a canary body through watcher → HTTP → daemon → gate → Gemma (fake) with every logger at DEBUG and fails if the canary is in any record, once through each doorway (the D-Bus one, and the Windows one below).

**On Windows (the port, `WINDOWS.md`): `doorways/toast_watch.py`.** The same event, from the toasts in Windows' notification centre, read with `Windows.UI.Notifications.Management.UserNotificationListener` (WinRT). Each toast gives the app's display name and `AppUserModelId`, and its `ToastGeneric` binding's text elements: the first is the title, the rest joined are the body. Everything after the reading is shared with the D-Bus doorway in `doorways/notifications.py` (`Forwarder`): `clean`, the content deduper, `allowed` (own notifications, `only_apps`, `ignore_apps`, `min_urgency`, replacements), the body decision (`body` / `body_apps`), the coalescing window and its summary, the log line with `body_len` and never the body, and the 30 s POST. The daemon's privacy checks do not know which system sent the event. `desktop_entry` holds the app's short key from its `AppUserModelId` (`smtc.app_key`: `slack`, `msteams`), so `ignore_apps`, `only_apps` and `body_apps` match by display name or key, as they match by app name or desktop entry on Linux. Toasts carry no urgency or category (urgency is `normal`), and an update to a toast is a new toast (`replaces_id` 0). `icon` is the app's logo (`AppDisplayInfo.GetLogo`), written once per app as a PNG to `%LOCALAPPDATA%\strawberry\cache\app-icons\`; desktop apps usually have none, and no logo means no badge.

Two things the listener insists on, seen on Windows 11 build 26200 (2026-09-23):

- **Access.** An unpackaged Python process may use it. `GetAccessStatus` says `Allowed` (1) while Settings > Privacy & security > Notifications > "Let apps access your notifications" is on (`HKCU\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\userNotificationListener`, `Value = Allow`), and `RequestAccessAsync` then answers `Allowed` at once, with no prompt. The watcher asks only when the status is not `Allowed`; `Denied` (2) or `Unspecified` (0) is one error line saying where to turn it on, and exit 3.
- **No change events without package identity.** Adding a `NotificationChanged` handler from an unpackaged process fails with `0x80070490` (Element not found). So the watcher polls `GetNotificationsAsync(Toast)` every second (a read takes about 160 ms and 4 ms of CPU) and diffs by the toast's `Id`; the toasts already there at start are not news. A toast that appears and is dismissed within one poll can be missed. 30 failed reads in a row (access turned off meanwhile) exit 3, so the supervisor starts it again.

Tests: `tests/test_toast_watch.py` on a fake listener shaped like the WinRT one (it runs on any system), including one notification giving the same event on both systems; `tests/conftest.py` makes the real listener unreachable in every test. Seen live 2026-09-23: the watcher, pointed at a throwaway daemon, opened the real listener without any identity (access `Allowed`, the toasts already there counted, then polled every second), and a fake toast driven over HTTP into the daemon came out as "Chat sent something private." with the gate down, and as "Chat: Sam" with bodies off.

## 4b. Doorway: media (MPRIS) — `strawberry/doorways/mpris_watch.py`

Any player that speaks MPRIS (Spotify, VLC, Rhythmbox, browser tabs with media) registers as `org.mpris.MediaPlayer2.<name>` on the session bus. The watcher follows **all of them at once** with jeepney, no extra tools, and reacts to the union:

| Change | Daemon call |
|---|---|
| any player → Playing | `POST /perform {"state":"dancing"}` |
| nothing playing / player quits | `POST /perform {"state":"idle"}` |
| a playing player changes track | `POST /event {"source":"media","app":"<player Identity>","title":"Artist — Title"}` |

Two subscriptions carry it: `PropertiesChanged` on `org.mpris.MediaPlayer2.Player` (matched by path, `/org/mpris/MediaPlayer2`) for what a known player is doing, and `NameOwnerChanged` with `arg0namespace='org.mpris.MediaPlayer2'` for players appearing and disappearing — a player is tracked by its **unique owner name** (`:1.494`), which is what a signal's sender field carries. Every value arrives as a variant, `Metadata` as a variant holding a whole `a{sv}`, so `bus.plain()` unwraps them before anything reads `xesam:title`.

Changes are debounced 400 ms (players fire several property updates per track), a track is announced once per player, and un-pausing the same song is not an announcement. A dance/idle post the daemon could not take (it restarts at the same moment as the watcher under the tray) is retried every 2 s until it lands; track announcements are not. The daemon in turn remembers her resting state (`idle`|`dancing`, shown in `/health`) and sends it to any widget the moment it connects, so a relaunched widget does not stand still while the music plays. `--only spotify,vlc` or `--ignore firefox` narrow it. `strawberry daemon` (or the tray) starts the watcher next to the daemon. This is why the widget returns to `dancing` rather than `idle` after a line (§1): the music context outlives the reaction.

**On Windows (the port, `WINDOWS.md`): `doorways/smtc_watch.py`.** The same three daemon calls, from the System Media Transport Controls: every player that registers a session with Windows' `GlobalSystemMediaTransportControlsSessionManager` (Spotify, the browsers, Media Player; the media card in the volume flyout shows the same list). `SessionsChanged` on the manager and `PlaybackInfoChanged`/`MediaPropertiesChanged` on each session arrive on a WinRT thread, so a handler only pokes the event loop, which re-reads every session after the same 400 ms debounce; a 5 s poll re-reads too, for a player that does not raise every event. The `Changing` status between two tracks keeps the previous one. SMTC has no track id, so a track is its title, artist and album together. The app's name is its `SourceAppUserModelId` made readable (`smtc.app_name`): `Spotify.exe` and Spotify's Store id both say "Spotify", `Chrome` "Google Chrome", `MSEdge` "Microsoft Edge", Firefox's install-folder hash "Firefox", an unknown app its own id ("Tidal.exe" -> "Tidal"). `--only`/`--ignore` take the short lowercase key (`smtc.app_key`: `spotify`, `chrome`, `msedge`, `firefox`), and the Chromium-based browsers also answer to `chromium`, as on MPRIS. `doorways.for_system()` runs it on Windows beside the notification doorway (§4), for `strawberry daemon` and the tray's children alike. Seen live 2026-09-23 on a throwaway daemon, with a silent test session of its own: dancing on play, a track change announced about 1 s after it, idle on pause and when the session closed, driven by the events alone with the poll turned off.

---

## 4c. Doorway: the beat — `strawberry/doorways/beat_watch.py` + `beat_track.py`

MPRIS says *that* music plays; this says *how it goes*. Like the other two it is a package module on the same interpreter (`strawberry-doorway beat_watch`; numpy is a normal dependency). The watcher captures the player's own PipeWire output stream (`pw-record --target <node>`: never the microphone, never the whole mixer, so her voice, calls and system sounds stay out) and feeds it to a pure-numpy beat tracker: spectral flux at ~43 frames/s in three bands (kick < 150 Hz, mid, high), each normalised by its own mean so the kick's few bins count as much as the hats' many; autocorrelation over the last 8 s for the period (55–215 BPM, ac(lag) + ½·ac(2·lag), log-Gaussian prior around 118 BPM, half/double of the winner re-scored side by side); hysteresis (a new tempo must score 15 % better twice in a row), a second vote from the last 4 s alone so a tempo change re-locks in two estimates, and the median of the last three estimates as the reported BPM; a comb at the fractional period over the last 4 s for the phase, led by the kick band in linear magnitude so she lands on the kick rather than on off-beat hats or an off-beat bass, blended with the grid the previous estimate predicted. Every 2 s it posts to `POST /tempo`:

```json
{"bpm": 128.4, "period_s": 0.467, "confidence": 0.71, "next_beat": 1789935826.592,
 "evenness": 0.62, "low_ratio": 0.55, "density": 4.1, "loudness_db": -18.0, "steady": true}
```

or `{"silent": true}`. `next_beat` is wall-clock, so the widget computes the beat phase itself every frame. `steady` is true once the same tempo has held for two estimates and two seconds since the last lock, with confidence ≥ 0.3 and sound in the last second; it is optional in the protocol (the daemon accepts estimates without it, a widget treats a missing flag as steady), so an older watcher still works. `evenness` (autocorrelation at 1, 2 and 4 periods: four-on-the-floor scores high), `low_ratio` (energy below 150 Hz), `density` (onsets/s) and loudness are the style features. The daemon validates ranges, forwards `{"tempo": {...}}` to the widgets, shows the fresh estimate in `/health`, and hands it to a widget that connects while it is fresh (6 s). Stream discovery is automatic (a running `Stream/Output/Audio` node from a known player, else any running stream that is not ours) or pinned with `[beat].target`. Only a *running* stream is ever targeted, and after the first second of data the watcher reads the graph (`pw-dump` links into its own `strawberry-beat` node) to confirm the bytes come from that stream: Spotify keeps a second, idle stream node, and when the watcher once targeted it at a track change PipeWire quietly linked the capture to the default sink's monitor instead. From then on the beat followed the headset, went dead across an A2DP→handsfree profile switch, and came back as 8 kHz telephone audio (2026-09-22). A capture fed by an `Audio/Sink` node is dropped and the next strategy tried; the explicit `sink-monitor` strategy is the last resort and says so in the log.

**On Windows (the port, `WINDOWS.md` step 5): `doorways/beat_loopback.py`.** `beat_watch.py` is the same doorway on both systems: it holds the tracker driving, the `/tempo` posts and when to let a capture go, and `beat_watch.backend()` picks the capture at runtime, `beat_pipewire.py` (everything above) on Linux and `beat_loopback.py` on Windows. The player is the System Media Transport Controls session that plays (§4b; `[beat].target` pins one by its short name, `spotify`, `chrome`), and its process is found among the output devices' audio sessions, which name their process: a packaged app's by its AppUserModelId (`GetApplicationUserModelId`), a desktop app's by its image name (`Spotify.exe`, `chrome.exe`), an active session before an idle one, never our own processes or the widget. From there the capture climbs to the topmost ancestor with the same image name (a browser above its audio service) and records that process tree with WASAPI process loopback (`ActivateAudioInterfaceAsync` on `VAD\Process_Loopback`, Windows 10 2004 and later; `wasapi.py`, ctypes only): the audio engine converts it to mono float32 at 22050 Hz, so the tracker gets what `pw-record` gives it on Linux. It hears the player after its session volume and mute, never anything else. A capture ends when the player's process does, is reconnected after 12 s of silence while the session still says Playing, and is let go after 4 s of silence when another session has started playing. The tray and `strawberry stop` stop it through its stop event (§2). Seen live 2026-09-23 on a throwaway daemon with a test player of its own: 124, 100 and 140 BPM loops came out at 123.5, 99.7 and 139.8.

**Dance styles (`widget/dance_style.gd`).** While she is dancing with a fresh estimate the node runs `dance_loop` at the music's tempo (one leg lift per beat: the clip's natural rate is 119 BPM, halved or doubled to stay within 0.65–1.6×) and layers beat-locked moves over it, the same bone-offset-after-the-AnimationPlayer technique as the reactions. The rule table, in order:

| style | when | moves |
|---|---|---|
| `sway` | confidence < 0.3, or < 76 BPM, or quieter than −48 dB, or `steady` is false | slow roll, clip at 0.75×, happy eyes; no beat lock |
| `rave` | ≥ 118 BPM, evenness ≥ 0.45, low_ratio ≥ 0.25 (techno, house, trance, hardstyle) | stomp squash on every beat, arms pump alternately, eyes wide |
| `headbang` | ≥ 132 BPM, density ≥ 4 (rock, metal) | forward nod on the beat, claws half up, squint |
| `groove` | ≤ 108 BPM, low_ratio ≥ 0.2 (hip hop, funk) | two-beat roll, claw pumps on alternate beats |
| `bounce` | everything else with a beat | squash and a small nod on the beat |

A new style must win two estimates in a row (4 s) before she switches, so borderline songs do not flicker. Silence or a stale estimate resets speed and layers. The thresholds are constants at the top of the script and the watcher logs the same features per song, so tuning is: play the song, read the journal, adjust. First live reading: Schrotthagen at 161 BPM, evenness 0.5, low 0.77 → rave. Not built yet: a build-up/drop detector for rave (energy rising over bars, then a crouch and a drop back in) and a genre hint from the brain.

**Measuring the tracker (`scripts/beat_eval.py`).** An offline harness feeds wav files through `BeatTracker` exactly as the watcher does (2048-sample chunks, an estimate every 2 s) and scores every estimate against the true beat grid: `acc1` (within 4 % of the true tempo), `octave` (within 4 % of half or double), `acc2` (either), time to lock (first correct estimate that the next two confirm), jitter (mean BPM change between locked estimates), phase (locked `next_beat` within 70 ms of a true beat), phase jumps (the grid moved more than 0.2 beat between estimates), beatless claims (a beat claimed over noise, silence or bare pads: `steady`, or confidence ≥ 0.3 for a tracker without the flag) and CPU per second of audio. `scripts/beat_eval.py gen DIR` writes the generated test set (35 clips of 30 s: click tracks at 70–175 BPM; house, techno, trance, rock, punk, swung hip hop and funk, half-time trap, drum and bass, one-drop, swung jazz, a waltz; loud pads and arpeggios over quiet drums; pink noise at 0 dB SNR; a breakdown, a silence gap, abrupt 100→128 and 140→92 changes, a 118→134 ramp; noise, pads and hum without a beat); `run DIR --tracker OLD.py` compares another version on the same set. No recorded audio is committed. `tests/test_beat_eval.py` runs the set at 20 s per clip and holds the scores.

| on the generated set | first tracker | now |
|---|---|---|
| acc1 (right tempo) | 0.56 | 0.84 |
| octave errors | 0.31 | 0.09 |
| acc2 (right up to an octave) | 0.87 | 0.92 |
| other errors | 0.06 | 0.00 |
| time to lock, mean over sections (median) | 13.3 s (4.1 s) | 6.3 s (4.1 s) |
| jitter while locked | 0.12 BPM | 0.08 BPM |
| phase on the beat (±70 ms) | 0.82 | 0.91 |
| phase jumps between estimates | 0.067 | 0.018 |
| beats claimed where there are none | 0.023 | 0.000 |
| estimates `steady` / of those right | — | 0.85 / 0.90 |
| CPU per second of audio (one core) | 1.96 ms | 2.04 ms |

The first estimate of every clip comes at 2 s and is always empty (the tracker needs 4 s), which is why acc1 tops out at 0.93 per clip and time to lock bottoms out at 4 s. What the old tracker got wrong: 120 and 140 BPM click tracks came out at 60 and 70 (the beat period fell between two integer frame lags, the doubled lag did not), four-on-the-floor techno and trance halved, swung hip hop doubled by the hats, and the phase landed on an off-beat bass. What the new one still gets "wrong" is the metrical level of half-time material: trap at 140 comes out at 70, drum and bass at 174 as 87, a one-drop at 75 as 150, all counted as octave errors though a dancer could pick either. Its phase still goes to the off-beat on a funk pattern whose kicks mostly sit off the beat (the snare would say otherwise, but weighting the mid band lets off-beat hats and bass win on minimal techno, which matters more), flips on one syncopated hip-hop pattern, and trails a steep tempo ramp by about a fifth of a beat. The set is synthetic; the numbers say which mistakes are gone, not how she does on a given record.

## 5. Doorway: git — `strawberry git-event`

Global by construction: hooks run inside the `git` binary when the commit or push happens, so a terminal, Claude Code, Codex, opencode, or an IDE all fire the same hook. One line makes them apply to every repo on the machine, present and future:

```bash
strawberry git-hooks install   # writes the hooks into ~/.config/git/hooks and sets core.hooksPath
strawberry git-hooks remove
```

| Hook | Event posted |
|---|---|
| `post-commit` | `{source:"git", app:"post-commit", title:"<repo>", body:"<commit subject>"}` |
| `pre-push` | `{source:"git", app:"pre-push", title:"<repo>", body:"pushing N commits on <branch> to <remote>"}` (skipped for branch deletions) |

`app` carries the hook name so the reactor can shade the reaction. Each hook is one line, `exec <strawberry> git-event <hook> "$@"`, with the absolute path of the `strawberry` that installed it (and a guard, so an uninstalled strawberry leaves git alone). `strawberry git-event` (in `cli.py`) reads the repo, branch and subject from git, counts the pushed commits from pre-push's stdin, posts the event from a forked, session-less child that gives up after one second, and never fails because of it: a hook must never slow or break git, and a stopped daemon just means she doesn't react. It posts to `STRAWBERRYD_URL`, else the configured port. Until 2026-09-22 the hooks were symlinks to bash scripts in the checkout that used `jq` and `curl`; `git-hooks install` replaces those symlinks (and removes the old `strawberry-git-event` helper link) and leaves any other file alone.

Two things the hooks take care of:

- **A global `core.hooksPath` replaces per-repo `.git/hooks`**, so `git-event` ends by handing over to the repo's own hook of the same name if one exists (pre-push replays its stdin to it and returns its verdict, so a repo's pre-push can still refuse the push). Repos that set `core.hooksPath` locally (husky-style) win over the global one; add `strawberry git-event post-commit` to their hook if you want her there too.
- **Only this machine's git fires.** Commits elsewhere, GitHub web merges, and CI results reach her through the notification doorway (§4). `post-merge`/`post-rewrite` are deliberately not installed for now; they'd make her chatty.

---

## 6. TTS + speech bubble (Godot side)

The reply text goes **two places at once**: to Piper (wav) and into the blob's `text`.

- **Piper (`strawberry/speech.py`):** `Daemon.perform()` hands any line without `audio` to `Speaker.say()`, which runs the `piper-tts` Python package (onnxruntime, CPU) in a worker thread and writes a wav into a per-daemon temp dir; the path goes in `audio`. A medium voice loads in ~0.9 s at startup (in the background; a line that comes first waits for it, §2) and voices a line in 60–150 ms, so the bubble lands a tenth of a second later than silent mode. The last three wavs are kept so a widget still playing one is not cut off. Silence is never an error: speech off, quiet hours, no voice installed, or a synthesis failure all mean "send the blob without `audio`", and `/health` says why under `speech.reason`. A caller that supplies its own `audio` keeps it. Emoji and dashes are stripped before synthesis; the bubble still shows them.
- **Voices** are Piper `.onnx` files in `~/.local/share/strawberry/voices/`. `strawberry voices` lists them, `strawberry voices en_US-amy-medium` downloads one (catalogue: rhasspy.github.io/piper-samples), `strawberry audition` plays a sample line in each, `strawberryd --say TEXT [--voice V]` writes a wav for one. Installed: `en_GB-alba-medium` (default; a Scottish voice that reads British English lines well), `en_GB-jenny_dioco-medium` (RP), `en_US-amy-medium` (US), `en_GB-alan-medium` (Scottish male). The persona is British English with a dry wit and no dialect words; the accent comes from the voice alone. The persona speaks of "the user's desktop", never a name, so the same defaults ship to everyone.
- **Bubble:** a `Label3D` billboarded above the crab (`widget/bubble.gd`). Reveals characters over time, timed to the **audio duration** if `audio` present, or a text-length heuristic if silent (≈18 chars/s, clamped 1.6–9 s). Holds, fades, auto-hides, and signals `finished`. Tinted by `emotion`.
- **Audio-reactive claws (`widget/speech_player.gd`):** an `AudioStreamPlayer` on its own `Speech` bus carrying an `AudioEffectSpectrumAnalyzer`. Each frame it reads the 90–4000 Hz magnitude, maps −52…−16 dB to 0…1 through an envelope follower (fast attack, slower release), and writes it to the `claw_open_L/R` blend shapes at process priority 160, after the reaction recipes. When playback ends it writes 0 once and stops writing, so the claw controller's dance/think gestures own the morphs again. **The wav must play through Godot**; that's the only way the analyser sees it. Don't also send it to the system mixer. Godot's dummy audio driver still mixes, so the headless acceptance check measures the clack (0.78 mid-tone, 0 after).

**Silent mode falls out for free:** omit `audio` and you get a talking crab with a bubble and no sound. The model still writes the line. This is "quiet hours" (`speech.quiet_hours = "22:00-08:00"` in config, or `speech.enabled = false`), and it's the mode built **first** (§10).

---

## 7. Doorway: voice (STT) — `strawberry/voice.py`

Hotkey → she listens → **faster-whisper** → the transcript is a `source: voice` event → the brain answers in her voice. Voice lives *inside the daemon* (not a fourth watcher) so the whisper model loads once at start (~1.6 s for `small`, int8, CPU) and the hotkey is a bare `POST /listen`.

**The load is in the background** (2026-09-23). `Listener.start` starts it in a daemon thread of its own and returns, so the port opens without waiting for it. Before, the daemon loaded whisper before it opened its port, and a model not in the Hugging Face cache is downloaded first: the first start after `strawberry setup --no-download` had written `[voice] model = "medium"` spent ~4.5 minutes downloading 1.5 GB, and in that time nothing answered (connections to 8770 stayed in SYN_SENT, the widget said "not connected to strawberryd", every doorway logged "strawberryd unreachable … timed out"). While it loads, `/health.voice` has `phase: "loading"`, `ready: false`, `reason: "loading whisper medium (first use: downloading ~1.5 GB from Hugging Face)"` (the download part only when the model is not cached) and `load_s`, the seconds so far; `/listen` answers 200 `{"listening": false, "loading": <reason>}` at once and she says "I'm still getting my ears on." (at most every 5 s, after the same debounce as a session); the tray's status row reads "Idle (loading whisper)" and `strawberry doctor` shows the reason as ✓, not as a fault. Whether it will download is read from the cache first (`voice.whisper_cached`: faster-whisper's `download_model(local_files_only=True)` and a `model.bin` in the snapshot, nothing sent anywhere), and the log says both ends: `voice: whisper tiny (cpu/int8) is not in the cache; downloading ~75 MB first, in the background`, then `voice: whisper tiny (cpu/int8) ready in 26.0s (downloaded and loaded)` (or `(loaded from the cache)`). A load that fails (no network, a bad device) leaves voice off with `reason: "whisper failed to load: …"`, as before. A CUDA error other than running out of memory (a missing cuBLAS usually shows at the first transcription, not at the load) has no CPU fallback; out of memory has, below. A stop during the download does not wait for it: huggingface_hub's download threads would hold the interpreter's exit until the model is complete, so when the load is still running after the shutdown the daemon flushes its log and leaves with `os._exit(0)` (`whisper is still loading; exiting without waiting for it`); the partial file is resumed on the next start. Measured on Windows with throwaway dirs and `HF_HUB_CACHE`: `tiny` (75 MB) downloaded and loaded in 26 s with `/health` answering in 1-25 ms throughout; a stop 0.3 s into a download of `base` exited 0.1 s after the request (34 s before this). `strawberry setup` fetches the model up front (its step 4), so a normal first start is a load from disk. The rest of the startup (the brain's warm-up, Piper, the gate's examples) was still awaited before the port opened, seconds each (brain 0.0 s, Piper 1.6 s, gate 3-7 s on the Windows machine) and up to their 120 s allowances when Ollama was slow; since 2026-09-25 they load in the background too (§2, "Nothing waits for a model at start").

One session (`Listener.session`):

1. `{state: "listening"}` → `pw-record` from the microphone at 16 kHz mono. The mic is `[voice].source` (a `pactl` source-name fragment) or the first non-monitor input; the machine's *default* source is the speaker monitor, which would make her hear the music instead of you, so the default is never used blindly.
2. Recording ends after `silence_s` (1.1 s) of quiet following at least `min_speech_s` of speech, on a second poke (`/listen` again = stop early), or at `max_seconds` (15). Speech is any 0.1 s chunk louder than the room's quietest chunk + 12 dB and than `level_db` (−40 dBFS).
3. `{state: "thinking"}` → whisper in a worker thread (`vad_filter`, `beam_size` 1, language detect or pinned with `language = "en"`).
4. Empty transcript → `{state: "talking", text: "Sorry, I didn't catch that."}`. Otherwise `Event(source="voice", title=<transcript>)` goes through `handle_event`, so Gemma writes the reply and Piper speaks it. `describe()` renders voice events as "the user is talking to you; reply to them", and the persona carries two voice examples, so she answers rather than narrates.

`/health` shows `voice` (model and device actually in use, fallback, ready, reason, phase (`loading` until whisper is in), load_s, sessions, empty, last_transcript, last_ms). If whisper cannot load (no model, no network for the first download), voice is disabled with the reason and the rest of the daemon is unaffected. `scripts/check_config.toml` disables voice for the acceptance run; the flow is unit-tested with fake recorder and transcriber (`tests/test_voice.py`).

**Names.** Whisper `small` hears "Daft Punk" as Dothpunk, Duff Punk, Dove Punk (2026-09-21, three tries; the thinker then played a real artist called Dovepunk). faster-whisper's `hotwords` biases decoding toward given names, so the daemon keeps a vocabulary: `[voice] vocabulary` from the config (your own names) plus whatever a configured server's adapter can offer (none ships configured; with the optional Spotify server, in order of likelihood: the artist playing now, favourites, the last 50 saved tracks' artists, playlist names — `adapters/spotify.py`, refreshed every `vocabulary_refresh_s` = 10 min, first 2 s after start). The first `max_hotwords` (60) go to every transcription. Without such a server the list is just your own `vocabulary`. An artist you have never saved gets no help from this; `[voice] model = "medium"` is the next lever. Measured on Piper-synthesised phrases (8 artist sentences): small 3/8 plain, 7/8 with hotwords, 1.5 s; medium 5/8 plain, 7/8 with hotwords, 3.2 s per sentence on the CPU. A real microphone and a non-native accent are harder than Piper (`small` heard "Kashmir by Led Zeppelin" as "Cosmere Pie, Let's Cheppelin'"), so `medium` is the recommendation. Whisper on CUDA (`[voice] device = "cuda"`, `compute_type = "int8_float16"`; `uv sync --inexact --group gpu` installs the cuBLAS and cuDNN wheels in a checkout, `uv tool install 'strawberry[gpu]'` for an installed one, and `voice.preload_cuda_libraries` loads them by path) (the scripts sync with `--inexact --group gpu` so `bin/strawberry` or the acceptance run does not prune that group again; a bare `uv sync` does, and whisper then logs `No module named 'nvidia'` and falls back to the CPU) is 0.13 s per sentence for `medium` and takes 1.2 GB of VRAM; measured next to Qwen and both Gemmas it fits with ~3.7 GB spare and nothing evicted. `/health.voice.hotwords` is the count.

**Out of GPU memory (2026-09-25).** Whisper runs in the daemon, outside Ollama, and Ollama cannot free VRAM for it: when Qwen fills the card first (27B at a large `num_ctx` is ~21 GB of a 24 GB card), a CUDA load or a transcription fails. CTranslate2 raises a `RuntimeError` for it, `CUDA failed with error out of memory` from its `CUDA_CHECK` (the text is `cudaGetErrorString`'s) or `cuBLAS failed with status CUBLAS_STATUS_ALLOC_FAILED`, at `WhisperModel(...)` or while `transcribe()`'s segments are read (they are a generator; the GPU work happens there). `voice.Whisper` catches exactly those (`cuda_out_of_memory`; a `MemoryError` is host RAM and is not caught) and loads the model again on the CPU with `int8`: `[voice] fallback_model`, or the configured size when that is empty or not in the Hugging Face cache (a download mid-sentence would keep the user waiting minutes). A transcription that failed is run again on the CPU, so the sentence is not lost; a load that failed ends ready on the CPU. It is logged once (`voice: CUDA out of memory while transcribing (…); whisper medium now runs on the CPU (int8) until the daemon restarts`), `/health.voice` has `device: "cpu/int8"` and `fallback: "CUDA out of memory while transcribing; on the CPU until the daemon restarts"`, and `strawberry doctor` warns with it. It stays on the CPU until a restart: going back would mean loading a second model on a card that was full a moment ago, in the middle of a sentence. A load that fails with OOM is not retried as a download (the download retry is for a model not in the cache). Tests: fake constructors raising the real message (`tests/test_voice.py`); the message format was checked against the installed CTranslate2 4.8 (a bad `device_index` gives `CUDA failed with error invalid device ordinal`, a `RuntimeError`).

**Hotkey.** `strawberry hotkey [COMBO]` writes a GNOME custom keyboard shortcut (`gsettings`, default `<Super><Shift>space`; `<Super><Alt>s` is GNOME's screen-reader toggle) that runs `strawberry listen` (the absolute path of the CLI that set it; a bare-socket `POST /listen`, ~40 ms). GNOME owns the key, so it is identical on X11 and Wayland and the daemon never grabs keyboard input. `--remove` undoes it. Phase 6 routes voice through the action model with tools; today it goes to the reaction path like everything else.

**On Windows** (`WINDOWS.md` step 6) the session is the same and two ends differ. *The microphone* is `winmic.py`: WASAPI through PortAudio (`sounddevice`, a Windows-only dependency imported only when she listens), a shared-mode stream at 16 kHz mono s16 with `auto_convert`, so Windows converts from the device's own format (48 kHz, usually; without the flag PortAudio refuses 16 kHz on a 48 kHz device) and whisper gets what it gets on Linux. The samples go from PortAudio's callback through a queue into `voice.capture()`, the loop both systems share (the chunking, the noise floor, the silence, the poke, `max_seconds`, and giving up after 3 s without data). The input is `[voice] source` as a fragment of the device's name, else Windows' default recording device: there the default is a real input, not the speaker monitor, so no search is needed. `bluetooth` changes nothing: Windows switches a headset to its hands-free profile itself when its microphone is opened. No input at all is "I can't find a microphone.", as on Linux. *The hotkey* is the tray's (§14): no Windows setting runs a command on a key, so the tray registers `[voice] hotkey` with `RegisterHotKey` and `strawberry hotkey [COMBO] [--remove]` only writes that setting (`hotkey.py`: GNOME's syntax, `<Control><Alt>space`, and `Ctrl+Alt+Space` read too; "" is the default, "off" none; a combination that does not parse is refused before anything is written). The default is `<Control><Alt>space`, not Linux's: Windows keeps Win-key combinations for itself, and Win+Shift+Space (back through the input languages) fails with `ERROR_HOTKEY_ALREADY_REGISTERED` on Windows 11. *Whisper on CUDA*: ctranslate2 on Windows loads `cublas64_12.dll` by name, which the loader looks for on PATH and not in the gpu wheels' `nvidia\cublas\bin`; a CUDA toolkit on PATH hides this. `preload_cuda_libraries` loads `cublasLt64_12.dll` and `cublas64_12.dll` from there by path and adds the wheels' `bin` directories to PATH and the DLL search path. cuDNN is not preloaded: ctranslate2's Windows wheel carries its own `cudnn64_9.dll`, and whisper did not load the rest of cuDNN. Measured 2026-09-23 on an RTX 3090 with only the wheels (the toolkit taken off PATH), a 4.5 s synthetic sentence: `small`, `int8_float16`, 1.2 s to load, 0.22-0.30 s per transcription with the VAD after a 0.4-0.5 s first one, the sentence word for word; the CPU (`int8`) took 1.4 s.

---

## 8. Action path — the gate, then MCP (Phase 6)

Voice is the only doorway whose events can be *requests*. Everything else stays on the reaction path for good. So Phase 6 is two pieces: a **gate** that decides what a spoken sentence is, and a **tool loop** on the big model behind it.

### 8a. The gate: a local "System One"

The shape we want is the one TypeSafe AI sells as a hosted API (docs.typesafe.ai): a *System One* model that "makes fast, structured decisions that software can use directly" and "does not write replies, produce code, or generate explanations". Their logic, captured here so we can run it locally:

- **State** is free text (or JSON). **Questions** are typed and evaluated in parallel against the same state in one call; "each question asks one specific, well-scoped thing… a gut-check determination"; complex judgements are decomposed into atomic questions and combined in code.
- **Choice**: options with one-line descriptions (include "other" when the list may not be exhaustive; use `what` / `not_for` / `examples` when options are confused) → `choice`, `probabilities` (sum 1), `confidence`.
- **Score**: an ordered rubric of 2–10 *situations* ("broken but a workaround exists", not "moderately severe"), each level scored independently → `score` = Σ level·p, `probabilities`, `legend`, `confidence`.
- **Noul**: a yes/no → a single probability of yes (0…1), no separate confidence.
- **Confidence** collapses the distribution by how peaked it is, not entropy: for three options `(3·p_max − 1)/2`, i.e. in general `(n·p_max − 1)/(n − 1)`: 0 for uniform, 1 for a single spike.
- **Acting on it**: thresholds per consequence, not per model. > 0.9 act on your own; the middle confirms or flags; < 0.5 don't act. "Different actions within the same system should be gated at different levels depending on the consequences of getting it wrong." Thresholds are tuned on your own data, starting conservative.

Nothing in that needs a hosted model; it needs a local scorer that turns (state, option) into a probability. Bake-off on 16 phrases in three classes (request / chat / question), 2026-09-21:

| backend | correct | per text | notes |
|---|---|---|---|
| **`embeddinggemma` (Ollama embeddings) + labelled example centroids, softmax at T = 0.05** | **16/16** | **16 ms** | the winner; examples live in config like the persona's |
| `all-minilm` (Ollama) + centroids | 15/16 | 137 ms | tiny (45 MB) but slower here and weaker |
| `Xenova/nli-deberta-v3-xsmall` zero-shot NLI (onnxruntime) | 12/16 | 34 ms | "put on some jazz" read as chat |
| `nli-deberta-v3-small` | 11/16 | 37 ms | |
| `nli-deberta-v3-base` (quantised) | 7/16 | 57 ms | |

**Built (Phase 6a, 2026-09-21): `strawberry/systemone.py`.** `SystemOne.ask(state, *questions)` with the three primitives, computed from embedding similarity; `Gate` on top runs the routing questions and logs every decision. Two things changed from the bake-off design once the whole phrase set was measured:

| scorer (embeddinggemma, T = 0.05) | scripts/gate_phrases.json |
|---|---|
| mean-pooled centroids, no prefixes | 29/34 |
| nearest examples (mean of top 2), no prefixes | 32/34 |
| **nearest examples + embeddinggemma's prompt prefixes** (`task: classification \| query: ` on the sentence, `title: none \| text: ` on the examples) | **34/34**, then 10/13 on a held-out batch written afterwards; 47/47 after three examples were added |

An option's score is the mean similarity of its two nearest examples (one odd example cannot carry it; one close example is enough), softmax at T = 0.05 → probabilities, confidence by the formula above. Score = expected level over the rubric, 1-based, with a legend. Noul = Choice between the `yes` and `no` examples, reported as p(yes). A single Ollama embedding call costs ~165 ms regardless of batch size, so a sentence is routed in ~170 ms (the bake-off's 16 ms was amortised over 16 texts). The examples are embedded once at start, in the background (warm-up gets the same 120 s allowance as the brain; a sentence that comes first waits up to 2 s for it and is then chat, §2); if Ollama is down the gate reports `disabled_reason` in `/health.gate` and every sentence is chat. The reload on resume from suspend (§4) embeds them then and brings the gate up.

**Timeouts: slow is not down, but only a notification waits** (0.1.1). Every embedding call carries its own budget (a context variable in `systemone.py`, so a 120 s reload and a 2 s sentence can be in flight together without sharing one). A call that times out raises `GateTimeout`; anything else (connection refused, an HTTP error) raises plain `GateError`. On the **voice path** (`Gate.route`) either one reads the sentence as chat at once, with no retry: someone is waiting for her answer, chat is a safe reading of any sentence, and a reflex missed once costs a few seconds of Qwen. A timeout also starts the one shared background reload, so the next sentence finds the model warm. The **notification check** (`Gate.sensitive`) waits for that reload and asks once more (§4, "Slow is not down"), because there the fallback throws the message away.

**In-process, on the CPU (2026-09-25): `strawberry/embedder.py`.** Nearly all of those ~170 ms were Ollama's per-call overhead, so embeddinggemma now runs in the daemon's own process: `onnx-community/embeddinggemma-300m-ONNX` (the Google repo is gated; this export is not), fp32, ONNX Runtime's CPU provider, `onnx_threads` intra-op threads (4). The graph does the pooling, the dense layers and the normalisation and outputs `sentence_embedding`; its tokenizer adds `<bos>`/`<eos>` as Ollama does, and the prefixes stay the gate's. Measured against Ollama's embeddinggemma: vectors at cosine 0.99998+ (the similarities the gate scores agree to ~1e-3, `tests/gate_parity.json`), `gate_check.py` 87/87 and `sensitive_check.py` 38/38 unchanged; ~19 ms an embedding, a whole route 18–24 ms median over 60–100 sentences (p95 ~45 ms), against 125–150 ms through Ollama on the same machine; the model takes ~0.7–0.8 GB of RAM and 1.7 s to load, and embedding the ~360 examples at start ~4 s more. fp16 gives NaN and q4 changes 15 routes, so only the fp32 files are fetched; all 32 threads gave a worse p95 than 4–8. The call runs in a worker thread (`asyncio.to_thread`), so the examples at start do not hold up the event loop, and an ONNX Runtime session and a tokenizer are safe to share between threads. With the embed in-process, the scoring was the next 5 ms: the similarities are one numpy product of the sentence with every example now (float64; the old `math.sumprod` scores to 1e-9, `tests/test_systemone.py`), ~0.3 ms.

The model is not shipped. `strawberry setup` (step 3) runs `python -m strawberry_crab.embedder --fetch`, which downloads `onnx/model.onnx`, `onnx/model.onnx_data` and `tokenizer.json` at a pinned revision into `paths.gate_model_dir()` (`<data>/models/embeddinggemma-300m-onnx`) with Hugging Face's `local_dir` and checks each against its size and SHA-256: they must be real files, because ONNX Runtime refuses external weights reached through the Hugging Face cache's symlinks. When the files are missing (or ONNX Runtime refuses them) the gate falls back to `model` through Ollama at start, logs a warning and says so in `/health.gate.embedder` (`backend`, `configured`, `dir`, `threads`, `load_s`, `fallback`); it never fails to start over it. `strawberry doctor` says where the gate runs, counts an unpulled Ollama embeddinggemma as a warning (the fallback) rather than a failure while the ONNX model is in place, and warns when the running daemon fell back. The in-process calls cannot time out the way an Ollama call does, so `timeout_s` and the reload after a timeout only apply to the Ollama backend.

**Router v2, a trained head (2026-09-26): `strawberry/gatehead.py`, the default scorer.** An evaluation on sentences the examples had never been tuned against found the nearest examples at 206/249 fields (83%) and 37/54 sentences strict; their 87/87 on `scripts/gate_phrases.json` is a training score, since the examples were written against those phrases. The weak spots were `is_about_her` (worse than always "no"), `is_urgent` ("resume playback" urgent at 0.90), calendar edits read as questions, "soita jotain Sibeliusta" read as `resume`, "turn on do not disturb" as music. So each question now also has a head: a multinomial logistic regression on the sentence's unit vector (the same embedding, query prefix included), `softmax((W·v + b) / T)`.

- **The data** (`strawberry_crab/data/gate_train.jsonl`, how it was made in `data/README.md`): the gate's own examples and the Spotify adapter's phrases (236 distinct sentences), plus 1883 sentences the local `qwen3.8:27b` wrote per option (hard negatives included, Finnish in calls of their own) and a second, targeted round (recommendations asked as questions, music for a mood or activity, greetings and goodbyes, asides not meant for her), each labelled for every question by the same model and then reviewed by hand (`scripts/gate_data/review.json`): 80 sentences dropped (broken Finnish, nonsense, passwords and codes), 108 fixed (mostly Finnish), 211 labels set by hand, and a few consistent rules where the labeller was noisy (`gate_build.tidy`: `has_argument` on bare player commands and fact questions, `is_urgent` = yes only where the sentence was written for urgency, a question about her is chat). Exact and near duplicates (cosine > 0.97) go (104 and 143). 1797 sentences, 224 of them Finnish (12%: the model's Finnish was the part the review dropped or rewrote most); `is_sensitive` has 176 notification texts.
- **The held-out set** (`data/gate_heldout.json`, 199 sentences, 35 Finnish, and 42 notifications): the first evaluation's 54 + 12, with names and places made neutral, and a separate generation organised by situation rather than by option, labelled by hand after the model's first pass. No held-out sentence is within cosine 0.95 of a training one: a training sentence that close to the first evaluation's is dropped, a new held-out one that close to training is dropped.
- **The fit**: numpy only (L-BFGS on the convex loss, ~15 s for all nine questions), features standardised for the fit and folded back into W and b, balanced class weights, the gate's own examples counted twice (hand-written; a weight of 1, 2 or 3 moves the held-out score by 2 fields of 1338). L2 per question from a grid (0.003–0.3) by out-of-fold log loss over five folds grouped by generation call, so a paraphrase never validates its twin; T per question on those out-of-fold logits (kind 0.82, topic 0.82, is_urgent 1.06, is_about_her 0.82, has_argument 0.91, wants_library_change 0.62, music_tool 0.84, needs_catalogue 0.78; `is_sensitive` had no out-of-fold error to calibrate against and keeps 1.0).
- **The thresholds belong to the head**: the TypeSafe confidence is taken on the calibrated probabilities, and `act` is the lowest 0.05 step from which the out-of-fold request/question readings are right 98% of the time, `offer` the same at 90%: **act 0.65, offer 0.05** (1161 out-of-fold readings at act, 19 wrong). `[gate] act`/`offer` stay the nearest examples' (0.6, 0.3). A head file carries its weights, temperatures and thresholds together, so a new head (or a rollback) changes all three at once. `[actions] reflex` (0.6 on the tool) is unchanged: on the held-out set the head fires 30 reflexes and 3 are wrong ("quiet please the baby is napping" as pause, "stop this now and switch to something instrumental" as pause, "mute the mic" as the music), the nearest examples 37 with 8 wrong.

| held-out (199 + 42) | fields | strict | Finnish fields / strict | conf right / wrong | AUROC | is_sensitive |
|---|---|---|---|---|---|---|
| nearest examples | 1042/1338 (78%) | 123/199 (62%) | 172/229 / 18/35 | 0.680 / 0.465 | 0.709 | 39/42 |
| **head** | **1270/1338 (95%)** | **168/199 (84%)** | **212/229 / 26/35** | 0.915 / 0.510 | **0.929** | 39/42 |
| first evaluation's 54 + 12 only: nearest / head | 206/249 / 238/249 | 37/54 / 48/54 | 37/44 / 41/44 | | 0.743 / 0.981 | 11/12 / 11/12 |

Per field on the whole held-out set, nearest → head: kind 171 → 183/199, topic 157 → 171/184, tool 48 → 62/67, decision 45 → 47/49, catalogue 142 → 160/163, urgent 75 → 142/148, about_her 140 → 193/198, argument 105 → 119/132, library 120 → 154/156. `scripts/gate_phrases.json`: 85/87 for both without a music server (the head misses "queue one more time" as catalogue and "what are the best Daft Punk albums" as a request; the nearest examples the two adapter phrases), 87/87 for the nearest examples and 85/87 for the head with the Spotify adapter. `scripts/sensitive_check.py` 38/38 either way. The scorer costs ~0.1 ms a route (0.13 ms median for the head, 0.22 ms for the nearest examples, the embedding excluded); a route is still ~20 ms.

**Privacy fails closed** (`systemone.FAIL_CLOSED`): under the head, `is_sensitive` takes whichever reading, the head's or the nearest examples', gives "private" more. The head alone read "Signal: Your Signal registration code: 552-019" as clear at 0.05 (the nearest examples 0.57; the pattern catches it anyway); together they miss no private body on either set, and three clear held-out notifications are dropped as private, as the nearest examples alone drop them.

**Extra examples still fix a phrase.** The head never saw `[gate.examples]` or an adapter's phrases. They are embedded again as queries at start (`SystemOne.anchors`), and a sentence within cosine 0.93 of one (`ANCHOR_SIMILARITY`: "save this song" ~ "save this track" 0.97, but "play the beatles" ~ "play radiohead" 0.92) is answered for that question by the nearest examples, which include it. `/health.gate.scorer` counts the anchors and how often one answered.

**Where heads live.** `gatehead.resolve`: `[gate] head` when set (a file; missing is a fallback, never a quiet substitute), else the user's current head (`<data>/gate/heads/current` names a `head-<version>.npz` beside it), else the one shipped in the package (`data/gate_head.npz`). The learning loop (§8d) writes versions there and moves `current`, and the running gate follows it without a restart (`Gate.reload_head`); a broken current head falls back to the shipped one, with a warning. At start the gate checks the head against the embedder (`embeddinggemma`, the ONNX export or Ollama's, parity above), the query prefix, the vector size and every question's options; anything that does not fit keeps the nearest examples, says why in the journal and in `/health.gate.scorer` (`configured`, `in_use`, `head` with version, source, path and data-set hash, `act`, `offer`, `anchors`, `anchored`, `fallback`), and `strawberry doctor` warns about it. The examples are embedded at start either way, for the anchors, the privacy rule and the fallback.

**By hand.** `strawberry gate train [--data FILE] [--activate] [--output FILE]` embeds the data set (vectors cached in `<cache>/gate/`), fits a head, writes it as a new version in the heads dir and prints the held-out table for the nearest examples, the head in use and the new one; `--activate` puts it in use after a restart, and `strawberry gate use VERSION|shipped` at once (through the learning loop's switch, §8d, so a running daemon follows it within seconds and `strawberry learning rollback` comes back from it). `strawberry gate eval [--scorer both|head|nearest] [--set FILE] [--misses]` scores the configured gate; `scripts/gate_heldout_check.py` is the same table from the repo (`--json` keeps every miss). `scripts/gate_data.py` regenerates the data (`data/README.md`). Tests: `tests/test_gatehead.py` (the fit, the gradient, calibration, thresholds, the file, the heads dir, the fallbacks, anchors, the privacy rule, the CLI) on the fake embedder.

Config `[gate]`: `enabled`, `embedder` (`onnx`, the default, or `ollama`), `onnx_dir` (empty: the data dir), `onnx_threads` (4), `model` (the Ollama model: `embedder = "ollama"` and the fallback), `scorer` (`head`, the default, or `nearest`), `head` (a head file; empty: the data dir's current head, else the shipped one), `query_prefix`/`document_prefix` (empty both for a model without conventions), `neighbours`, `temperature` and `act`/`offer` for the nearest examples, `topic_min`, `timeout_s` (2 s, one call), `retry_timeout_s` (15 s, notification checks only, below), and `[gate.examples]` with `"kind.request" = [...]`-style extra phrases: a sentence she misreads goes under the option it belongs to. Tools: `strawberry route "…"` prints one sentence's full reading; `scripts/gate_check.py` runs the phrase set (add to it; it exits 1 on a miss). Unit tests run on a bag-of-words fake embedder (`tests/test_systemone.py`).

**The routing questions** (all asked at once, atomic, combined in code):

- `kind` Choice: `request` (asks her to do something), `question` (wants information looked up), `chat` (small talk, feelings, banter), `other`.
- `topic` Choice: `music`, `calendar`, `notes`, `system`, `other` — picks which MCP servers to load.
- `is_urgent` Noul; `is_about_her` Noul (she answers those herself, whatever the kind).
- `needs_catalogue` Noul: particular music to find and play or queue (an artist, a song, a genre, a playlist) rather than one of the player's buttons. With no music server configured it decides the no-catalogue line (§8b).

Then in code (`decide()`): `act` at confidence ≥ `act` on a `request`/`question`, `chat` below `offer`, the middle band in between (the head's 0.65 and 0.05; the nearest examples' 0.6 and 0.3). Since 2026-09-22 the reading is used for exactly two things (§8b): a clear `act` with a sure tool and nothing to fill in fires a **reflex**; `wants_library_change` ≥ 0.5 decides whether Qwen is offered the careful tools. Everything else the user says goes to Qwen whatever the decision says, so the three labels are a journal entry now, not three code paths. Every decision is still logged with its probabilities so the thresholds can be tuned on real sentences.

### 8b. The tool loop, and the one brain behind it

**Built so far (2026-09-21): the MCP client, `strawberry/tools.py`.** `[tools.servers.<name>]` in the config lists the servers with a `topic` (one of the gate's) and a launch `command`; the servers you list are the servers, and none is listed by default (ADAPTERS.md). Each runs as a child process over stdio through the official `mcp` SDK (2.x); the SDK's transport has to be opened and closed from one task, so every server gets a task that owns the connection and serves calls from a queue. Servers connect in the background at start (`preconnect`) or lazily, are skipped with a warning when they fail, and are retried on the next use; a hung call drops the connection instead of blocking the ones behind it. `Toolbox.tools_for(topic)` returns `ToolSpec`s with an Ollama-ready `for_ollama()` (names prefixed with the server on a collision); results are text cut to `result_chars` (2000) and `ok` reads both the MCP error flag and the `{"error": …}` body some servers return instead. Measured with the Spotify server: connect 0.45 s, 25 tools, a call 0.1–0.3 s. `strawberry tools [TOPIC]` lists, `strawberry tool spotify next` calls; `/health.tools` shows each server's state. Tests: a fake session for the logic and a real stdio round trip against `tests/mcp_echo_server.py`.

**Built (2026-09-21): the reflex tier, `strawberry/actions.py`.** The gate asks two more questions on the same embedding (§8a, chained): the topic's *tool* Choice (`music_tool`: skip, previous, pause, resume, volume_down, volume_up, now_playing, other) and a *has_argument* Noul (a specific song, artist, amount or time the embedding cannot extract). When the tool is sure (`[actions] reflex`, 0.6) and there is nothing to fill in (`argument`, 0.5), the reflex runs directly: no model in the loop.

**Who does it (2026-09-22): MPRIS, or a server's adapter.** `Actor.reflex_for` asks two questions in order. Does a configured server for the sentence's topic have a reflex for this tool? Then it, because a server usually knows more than the desktop does — Spotify's Web API can name the *next* track before the player's metadata catches up, and its volume is the active device's rather than the app's. Otherwise the bare music commands go to **MPRIS** (`strawberry/mpris.py`; on Windows `smtc.py`, below), which every desktop player answers on the session bus, so skip, previous, pause, resume, volume and "what's playing" work with nothing configured at all — no server, no account, no tokens. `[actions] mpris = false` turns that off; the thresholds and `carries_argument` are the same either way, so "play daft punk" still goes to Qwen rather than resuming whatever was paused.

**No catalogue, no pretending (2026-09-22).** MPRIS has the player's buttons and nothing else: it cannot search for Daft Punk, open the liked songs or queue a track. Before this, with no server configured, Qwen answered those from `NO_TOOLS` and got them wrong both ways: "play daft punk", "play some acid techno" and "put on my liked songs" got "I can't control the player from here" (15/15 runs, and false: skip and pause work), and "queue one more time" got "Queued again." (5/5, nothing was queued). Now `Actor.needs_catalogue` checks the gate's `needs_catalogue` (>= 0.5, on a music `request`/`question`, never for a bare "play"), and when no configured server has the `music` topic the daemon says one of `actions.NO_CATALOGUE` ("I only have the player's buttons. Choosing what to play needs a music add-on, like the Spotify one.") with no model asked and no quip; the journal line points at ADAPTERS.md. Measured on a throwaway daemon with no servers and a fake MPRIS player: 20/20 of those four sentences get the line, "play the next song" and "what song is this" stay `mpris.skip`/`mpris.now_playing` (10/10). A sentence the gate misses ("save this song" reads as chat) falls to Qwen, whose `NO_TOOLS` prompt now says the same thing (20/20 on the four sentences with the guard bypassed). Tests: `tests/test_catalogue.py`.

**…but talking about music needs no catalogue (2026-09-23).** Recommending music and talking about it are hers to answer from what she knows, with or without a server; only playing, queueing, opening or saving particular music needs one. Seen live with no server: "can you recommend some techno artists" (gate: `needs_catalogue` 0.12, so the thinker) got "I can't recommend artists without a music add-on." — `NO_TOOLS`'s add-on sentence, over-applied by the model, not the guard. Measured on a throwaway daemon with `mistral-small:24b` as the thinker, no servers, MPRIS off, every sentence 5× in a fresh context: before, "any good techno from <a country>?" got the add-on sentence 5/5, a "good <region> techno" question 4/5, "can you recommend some techno artists" dodged 2/5 ("I don't know much about techno artists"), and "what's a good album to start with Radiohead" read `needs_catalogue` 0.55 and got the fixed line 5/5. Two fixes. `NO_TOOLS` names recommending and talking about artists, genres, albums and songs as her own knowledge ("naming a few real artists or records you are sure of; asked about one you have never heard of, say so rather than invent it") and keeps the add-on sentence for playing, queueing, opening or saving. The gate's `needs_catalogue` "no" side has eight recommendation and knowledge examples. After, on 17 such sentences (85 runs): no add-on sentence and no fixed line; the well-known ones are answered with real names every time (Detroit pioneers, jazz albums, artists like Daft Punk, "tell me about Aphex Twin"). A name the model does not know gets "Never heard of them." 3/5 and a vague invention 2/5 (it invented an 18th-century composer 5/5 before the clause on names); niche scenes still get a wrong name now and then. That is the limit of what the model knows. Still the fixed line 35/35: "play daft punk", "play some acid techno", "put on my liked songs", "queue one more time", "play something by <an artist>", "play something by Radiohead", "play something like Daft Punk". With the guard bypassed (`strawberry think`), the thinker says those need a music add-on 25/25 and claims nothing. The live gate readings through `Actor.reflex_for` with a stand-in MPRIS player keep "play the next song", "what song is this" and "pause" as `mpris.skip`/`now_playing`/`pause`. `scripts/gate_phrases.json` has 13 new cases: 85/87 without a server, and the two misses are the Spotify-adapter ones below.

MPRIS in detail (jeepney, asyncio, one connection reopened after a failure): `ListNames` for `org.mpris.MediaPlayer2.*`, one `GetAll` per player, and the active one is a player that is `Playing`, else the one she acted on last, else the first — so "pause" and "play" keep talking to the same player. Volume is a double, stepped by ±0.15 and clamped; `Next`/`Previous` are followed by a 0.4 s settle and a re-read, because a player reports the old track for a moment. A player that does not implement something says so in the sentence ("I tried to skip, but Mozilla Firefox won't skip from here.", "…won't say where the volume is.") instead of failing silently. Measured live 2026-09-22 against Spotify's own MPRIS: `now_playing` 9 ms, `pause` 7 ms, `skip` 403 ms (the settle); the same sentences through the Spotify server are 463 ms, 425 ms and 1201 ms.

On Windows the same reflexes run over the System Media Transport Controls: `smtc.Smtc` is `Mpris` with the bus swapped for Windows' media sessions, and `media.controls()` picks one for the daemon, so `Actor` and the daemon import neither. Each session reads as an MPRIS-shaped player (the app id where the bus name is, the enabled buttons as `CanGoNext` and friends), Windows' current session first among equals, and Play/Pause fall back to the play/pause toggle for a player that has only that button. SMTC has no volume, so "turn it up" gets "…won't say where the volume is." Config and names stay `mpris` (`[actions] mpris`, the `mpris.*` action names) on both systems. Tests: `tests/test_smtc.py` on a fake session manager; `tests/conftest.py` makes the real one unreachable in every test.

**Adapters (2026-09-22, ADAPTERS.md).** Everything that was Spotify-specific now lives in `strawberry/adapters/spotify.py`: its reflexes, its situation line, the recogniser's vocabulary, the rewording of its 403s and 404s, its gate phrases and its `common_tools`. An adapter is loaded only when a configured server matches it, by the name in `[tools.servers.<name>]` or by an explicit `adapter = "…"` there. `ToolsConfig.servers` is empty by default, `tools.clarify_error` is a hook that asks the matching adapter (and leaves a server without one exactly as it answered), and the generic music phrases stay in `systemone.py` because music control is core now — only the library ones ("save this song", "play my running playlist") come with the Spotify adapter. Those two are the only sentences in `scripts/gate_phrases.json` that need it: 71/71 with a Spotify server configured, 69/71 without, and both misreadings are harmless (one is a name the thinker handles, the other is chat, which also goes to Qwen).

What she says is split by reliability: **code writes the fact** in one plain sentence ("Skipped. Now Blue Monday by New Order.", "Volume down to 65.", "I tried to skip, but Spotify said: no active device.") and the reaction path adds **one short quip** in her voice after it (an `action` event: `brain.describe` quotes the fact and asks for at most 8 words; `Daemon.report` joins them, forces `alert` on a failure). Measured first: a 1B model given the structured result composed lines like "Let's go. A bit more fun, perhaps?" for a skip and a cheerful line for a failure, so the fact never depends on it. The MPRIS doorway's reaction to a track change she caused is swallowed for 8 s (`quiet_media_until`, set before the action runs because the watcher is faster than the confirmation). `/health.actions` keeps the last action with its tool calls. Anything the reflex tier does not cover (an argument, `other`, a topic without reflexes) is counted as `deferred` in the journal and handed to the thinker.

**Built (2026-09-21), rebuilt (2026-09-22): the thinker, `strawberry/thinker.py`.** Everything the user says that is not a bare reflex is now ONE Qwen call (`brain.action_model`, `qwen3.8:27b`): small talk, questions, and requests with something to fill in. It gets the sentence, the *situation* (the date, "Now playing on Spotify: … (album: …)" so "this song" means something — from the server's adapter, or from MPRIS when no server can say — the library names, the ledger) and **the configured servers' tools** as Ollama specs from `Toolbox.tools_for` — the careful ones (save, remove, add to a playlist) only when the gate's `wants_library_change` is ≥ 0.5.

**How many tools (2026-09-22).** Measured on the live `qwen3.8:27b` with Ollama's `prompt_eval_count`: her system prompt and one sentence are 326 tokens, and the Spotify server's 25 tool schemas add **2382** on top — 29 % of `num_ctx` 8192, not the ~360 this section claimed before anyone counted (that number was the prompt *without* the tools). Its ten `common_tools` cost 1304. So `[thinker] max_tools` (30) caps the schemas: over the cap, `Thinker.tools` orders the topics with the gate's first, each server's adapter `common_tools` first inside it, cuts the tail and logs what was left out. One Spotify server is under the cap; two or three servers are not. While everything fits, the order is the same for every sentence (since 2026-10-07): Ollama reuses its cached prompt up to the first token that differs, the tool schemas come right after the system prompt, and putting the gate's topic first re-read ~2700 tokens whenever the topic changed, 2.1–2.7 s against ~0.3 s for the same prompt (measured on `qwen3.8:27b` with `prompt_eval_duration`, Spotify plus the web server).

It calls tools until it answers; each call goes through the same client with truncated results; after `max_rounds` (6) the tools are withdrawn and it must say honestly what it did. `think = false` (Ollama accepts false / "low" / "medium" / true = xhigh, not "high"), `keep_alive = "30m"` so she is rarely cold, `num_ctx = 8192`, temperature 0.2, one `timeout_s` (45 s) over the whole request. Every call is a fresh conversation; nothing accumulates but the ledger.

**The prompt must fit `num_ctx` (2026-09-25).** Ollama does not compress a prompt longer than `num_ctx`; it drops the oldest tokens without an error, and the oldest tokens are the system prompt (her voice, the tool rules). So before every round (tool results make each round longer) `Thinker.fit_prompt` estimates the prompt and trims it to `num_ctx - num_predict`. Ollama has no tokenize endpoint and no tokenizer is a dependency, so `prompt_tokens` counts the characters of the messages' and tool schemas' JSON at 3 a token, plus 250 for the template's tool instructions when tools are offered. Measured on `qwen3.8:27b` with `prompt_eval_count`: messages run 4.1-4.5 characters a token, tool schemas ~3.7, and offering any tool adds ~200 tokens; the estimate was 1.3-1.5× the real count (552 for 371, 1136 for 828, 1805 for 1367 with ten schemas). The ledger reaches the thinker as lines of its own (`Ledger.lines`, `Thinker.run(recent=…)`), not inside the situation text, so the trimming is: the oldest ledger turns first, then the tool results, oldest first, each cut to what is needed but never below 200 characters and keeping its start (` …(cut to fit)` marks the cut). The system prompt, the tool schemas, the situation and the sentence are never cut. If they alone do not fit, the request is not sent: `PromptTooLong` (a `ThinkerError`) is logged (`prompt does not fit: ~N tokens with nothing left to trim, M fit (the system prompt and K tool schemas alone ~F)`) and she says "That's more than I can hold in my head at once. Sorry." as a failed outcome. A trim logs one line of counts only (`dropped 3 of 6 ledger turns, shortened 1 of 2 tool results`), never content: a tool result can carry a message. `thinker.num_predict` must be between 1 and half of `num_ctx`. With the defaults it rarely triggers: the system prompt, 25 Spotify schemas and six turns come to roughly 4000 by this estimate, of the 7892 that fit.

**The reply is hers.** Qwen's system prompt is her voice (`thinker.VOICE`: small, dry, warm, British, ≤ 15 words for small talk, two or three sentences when there are facts, never the user's name) plus the tool-use rules (`TOOLS_GUIDE`: be decisive, a misheard name is probably a library name, 'play' means play, report only what a tool did) — or, with no servers answering, `NO_TOOLS` (answer from memory, no internet, say so when the question needs today's news), or with only a web search, `LOOKUP_ONLY` (`NO_TOOLS`'s music clauses without "no internet") — followed by the paragraph each offered server's adapter brings (`guide`), or the one for a configured server that is not answering (`unavailable`: "say search isn't available"). The line is performed as it stands: no Gemma quip on top of a Qwen answer. It is asked to start with its mood in square brackets, `[happy] Skipped. Blue Monday next.`, which `split_emotion` parses off into the performance's `emotion`; a missing or unknown tag is `neutral`, and a tool that failed forces `alert`. `tidy_sentence` takes the first paragraph, strips markdown and caps it at 380 chars (`speech.max_chars` is 400).

Cover for the wait scales with it: `Daemon.think` puts her in the `thinking` pose at once and says nothing (a warm round is ~2 s, and "On it." before the answer to "what's up?" read odd), speaks a random acknowledgement from `[thinker] acks` only if the reply has not come after `ack_after_s` (2.5 s), says "Still on it." once after `still_on_it_s` (8 s), then her line. Measured: cold load 7–17 s (the first request of a session), a warm round ~2 s, prompt 326 tokens plus 2382 for 25 Spotify tools. Loading Qwen can evict Gemma and the embedding model from VRAM; both re-warm themselves in the background after a timeout (see §3). `strawberry think "…"` runs it by hand and prints the calls; `/health.thinker` keeps the last one. Tests on a scripted fake Ollama and the fake Spotify (`tests/test_thinker.py`).

**Web search (2026-10-07): `adapters/web.py`, ADAPTERS.md.** A SearXNG instance the user runs, behind `mcp-searxng` (`[tools.servers.web]`, topic `other`; the README has the block). Its adapter offers the search and the page reader only, with short schemas, compacts a listing to numbered results, rewords its failures without the query, logs a result count and never a result, and allows three calls a sentence.

*Routing: offered with every sentence, told when to search.* Every sentence that is not a reflex already went to the thinker with every server's tools (the `tools=False` call is the probe's), so the question was only which sentences get the web tools. Measured on a throwaway daemon's thinker with `qwen3.8:27b`, the 25-schema stand-in for the Spotify server and the real SearXNG, by `prompt_eval_count`/`prompt_eval_duration`: the system prompt and Spotify's tools are 2239 tokens; the two web schemas and the guide add ~490 (2730, 2751–2755 with a note). Leaving the web tools out of small talk (the gate's `chat`, or `is_about_her` ≥ 0.5) saves those tokens, but the prompt then changes between chat and questions, and each switch re-read the whole of it: 2.1–2.7 s, against ~0.3 s when the prompt is the one Ollama has cached. Offered every time, the guide alone kept small talk, questions about her and a music recommendation from searching (0/8, later 0/24 live). So the web tools go with every sentence and the system prompt never depends on the sentence; what does is a line under the sentence (the adapter's `nudge`): "They asked for a web search: search first" for an explicit request (a regex: "search the web/online", "look … up", "google", "find/check … online", "hae netistä"…; not a bare "search for", which is the music catalogue's), and "This may depend on current facts: unless the situation above answers it, search" for a question (as the gate reads it, not about her) with "now", "today", "tomorrow", "latest", "newest", "weather", "price", "score", "open/close"… in it. The guide alone left "what's the newest iPhone" answered from memory 4/4 and "what's the population of <a country> now" 1/2; with the note 16/16 searched, and "what is the capital of Australia" and "what time is it now" (in the situation) still 0/4. Weather and sports took five or six searches and page reads (17–45 s, one over the 45 s timeout) until the guide said "at most twice and one page" and `max_calls` = 3.

*Prompts.* `TOOLS_GUIDE`'s "Tools are for the player, not for facts" (it kept Qwen from searching the catalogue to answer "who is Aphex Twin") reads, when a web search is offered, "The player's tools are for the player, not for facts … unless the web search rules below say to search"; every other clause is unchanged, and with no web server the prompts are what they were. The guide asks for two or three spoken sentences, the site at most and never an address (the thinker also turns any address in her line into its site), and "say so plainly" when the search fails or finds nothing.

*Untrusted results.* One conversation would otherwise hold the user's private context (the ledger, what is playing, library names, other tools' results), strangers' text (snippets, pages) and a way out (queries and page addresses), and a page could ask her to fetch `http://evil/?q=<what the user said>` or call `save_tracks`. The structural guards, in code, whatever the model does: once a result from an `untrusted` adapter is in, the ledger, the situation but the date (`public_context`) and the other servers' results are taken out of the conversation, every other server's tool is refused, and three rounds remain; a page is read only by a URL that, in one strict canonical form (scheme, IDNA host in lower case, the port only when not the default, one percent-encoding, no fragment), equals a URL field of that question's own results, and the result's canonical URL is what is sent; a URL two parsers could read differently is refused outright (a login part, backslashes, whitespace or control characters, an encoded host, a trailing dot or empty label, any IP notation, internal and single-label hosts, other schemes, over 2048 characters); one page a question and no search after it; a query is one plain line of at most 200 characters with no other arguments; any call to a tool not offered for that sentence, from any server, is refused, and at most six calls a reply are made; a sentence that asks for a library change (`careful`) is offered no web search. As a second layer only, a web call after a result that carries a phrase of the private context (decoded, squashed) is refused. Her answer from web results is logged as its length, kept in the ledger as a placeholder (the next sentence's prompt would otherwise carry page text untainted), and `/health` keeps a web result's size only. A server listing the web tools gets this adapter whatever its name or `adapter` key. Only the user's sentences reach the thinker: notifications, media, git and action events go to the reaction path, which has no tools. Since 2026-10-07 the name is also looked up before a page is read (`WebAdapter.screen`, on the URL `forward` will send): `canonical` refuses IP literals and internal names, but a public-looking name can resolve to a private address (`localtest.me` is 127.0.0.1, `*.nip.io` is whatever is written into it, an attacker's DNS says what it likes). The event loop's `getaddrinfo` (a thread, so nothing blocks) has 1.5 s; the read is refused when the name does not resolve in time or when any address it gives is not globally routable (loopback, private, link-local, CGNAT 100.64/10, multicast, reserved, unspecified, unique-local fc00::/7, and the IPv4-mapped and NAT64 forms of those; 6to4 and Teredo outright). DNS rebinding, an answer that changes between this lookup and the fetch, stays the fetching server's job: mcp-searxng's own reader also refuses private addresses at connect time (its DNS lookup hook), which stays as a second layer. Tests: `tests/test_adapter_web.py`.

*Measured live* on a throwaway daemon (port 8781, temp dirs, no speech, no widget, MPRIS off, the Spotify stand-in), each sentence 3×: explicit searches 9/9, current-fact questions 9/9, small talk, "who are you" and "recommend some techno" 0 searches in 12, the music reflexes 12/12 and "play some daft punk" 3/3 through their own paths. A search turn took 4.5–17 s (one search ~5 s, the weather and a shop's opening hours with two searches and a page 12–18 s); small talk 1.2–2.0 s, as before. With SearXNG unreachable she said search was down 4/4 (3.4–4.8 s); with `npx` missing the server was skipped with a warning and she said search isn't available 4/4 (1.2–2.0 s). The SearXNG engines matter more than anything here: during the runs Google CSE and Brave suspended the instance for too many requests and DuckDuckGo answered with a CAPTCHA, after which most searches came back empty and she said so rather than guess.

**The Spotify library by name (2026-10-08, ADAPTERS.md).** The Spotify server gained `like_current`, `add_current_to_playlist`, `find_playlist`, `remove_from_playlist`, `create_playlist` and a `playlist` argument for `play`, with the playlist matched by name inside the server, and one error shape, `{"error", "code", "status"?, "details"?}`. Thirty tools now, ~2500 tokens by their schemas' size (estimated at ~3.7 characters a token, not measured live), plus ~200 for the template; `common_tools` keeps thirteen (~1150), the three new easy-to-undo ones among them. `like_current` and `add_current_to_playlist` are not careful (a word undoes them and the server skips duplicates); `remove_from_playlist` and `create_playlist` are. Routing, each measured on a fake server with the real schemas and the live `qwen3.8:27b`, so nothing reached the user's Spotify:
- *"I like this", "save this song"*: the gate has no "like" option and a new one would mean retraining its head, and it reads "I like this" as chat (`wants_library_change` 0.35). So an adapter can now name reflexes by the whole sentence's words (`Adapter.said_reflexes`, asked by `Actor.said_reflex_for` before the gate's decision, for a server of the sentence's topic that lists the tool the reflex needs, never for `kind` other). Spotify's is "like": "I like this", "like this track", "save this song please", "add this to my liked songs", nothing longer and nothing with a name, then `like_current` and "Liked: Blue Monday by New Order." 15/15 in about a second. The local favourites stayed for "add this to my favourites" (through the thinker: `favorite_current` 5/5 in the last run, 0/3 to 4/5 in earlier ones, the rest a claim with no call), until they were retired the same day (below). With an older server that has no `like_current`, the sentence goes to the thinker as before.
- *Playlists by name* go to the thinker, which gets the adapter's `guide` (once, in the system prompt) and, under a playlist sentence, a `nudge`: "They asked to add the playing track to their playlist 'gym': do it with a tool call now, passing the name as heard. Say it is done only if a tool did it." The guide alone left "take this off my gym playlist" answered as done with no call 4/5 and "play my running playlist" 3/6 (and a guide clause for `play` was worse than none). With both, 40/40 over eight sentences called the right tool with the name as heard: `add_current_to_playlist(playlist="gym")`, `play(playlist="schranz")`, `remove_from_playlist(playlist="chill", track="current")`, `find_playlist(query="gym")`. Small talk stayed tool-free (10/10). The removal's line is only added when the removal is offered (`wants_library_change` ≥ 0.5).
- *Several playlists match*: `clarify_error` turns the server's "Several playlists match 'workout': … Which one?" into an instruction to ask which one, naming at most three; Qwen asked 5/5 ("Workout, Workout Mix or Old Workout. Which one?"). A reflex says the same: "…but several playlists match: A, B or C. Which one?"
- *Errors*: `_failed` and `clarify_error` branch on `code`: `network` is "I can't reach Spotify right now", `auth` "Spotify needs signing in again", `no_active_device` and `restricted` keep their lines, and the rest are the server's sentence without its hints for a terminal or a model. An older server's errors are read by Spotify's `details` and their wording as before; "…Spotify said: Could not reach Spotify. Check the internet connection and try again.." is gone.
Read-only on a throwaway daemon (port 8783, temp dirs, every tool that changes anything marked careful): "what's playing" was the reflex (0.14–0.28 s), and "do I have a playlist called gym" called `find_playlist` and answered from it (3–5 s).

**Favourites are Liked Songs (2026-10-08, ADAPTERS.md).** The Spotify server's local favourites (a JSON file and five tools: `favorite_current`, `get_favorites`, `remove_favorite`, `play_favorites`, `clear_favorites`) existed because Spotify's library writes once failed; they work now, so the list is gone and "favourites" means Liked Songs. The server gained `play_liked`: it starts a page of the saved tracks as a `uris` list (a random page of 50, shuffled, by default), because Spotify documents only albums, artists and playlists as playback contexts and has refused `spotify:user:<id>:collection`. 26 tools, ~2300 tokens by their schemas' size (same estimate as above); `common_tools` keeps fourteen (~1250), `play_liked` among them; an ordinary sentence is offered 21 (~1775). Routing, measured on the real gate and the live `qwen3.8:27b` against a fake server with the real schemas:
- *"Add this to my favourites"*, "favourite this track", "could you add this one to my favourites" are the like reflex (`like_current`), and *"play my favourites"*, "can you play my favourites" a second said reflex, `play_liked`: 18/18 for the like reflex ("I like this" among them) and 9/9 for `play_liked`, no model. This ends the "Saved to your favourites" with no call.
- Forced through the thinker instead, "add this to my favourites" called `like_current` 5/5 and "play my favourites" `play_liked` 5/5 (the old tools: `favorite_current` 5/5, `play_favorites` 5/5). Sentences the reflexes do not take get a line under them, as playlist sentences do ("They asked to play their favourites, which are their Liked Songs: do it with a tool call now. …"): "I think you should add this one to my favourites" and "put on some of my favourite songs while I work" 8/8. Naming the tool in that line ("call like_current now") was worse: 1/12. "Hey could you save this song to my favourites for later" was answered "Saved" with no call 4/6 even with the line: Qwen reads "for later" as no call now, so the reflexes take polite wrappers ("can/could/will you", "please") and "for later"; "would you like this song" stays a question. Small talk ("what's your favourite song", "would you like this song") made no call 6/6; "what are my favourites" read `get_saved_tracks` 3/3.
- Known gap, not new: "I like this" forced through the thinker is answered "Saved" with no call (4/5 now, 5/5 on the old tools). Live it never gets there: the reflex takes it whenever the server lists `like_current`.

**A spoken yes before a removal (2026-10-08, `strawberry/confirm.py`).** `careful` decides only whether the thinker is *offered* a tool, on the gate's `wants_library_change`; once offered, a misread sentence (or "remove this from my gym playlist" said loosely) removed at once with nothing said first. Now each server has a `confirm` list too: the config's `[tools.servers.<name>] confirm`, else its adapter's `confirm` (Spotify: `remove_from_playlist`, `remove_saved_tracks`), else none; `confirm = []` turns it off. In `Thinker._run`, a call to a listed tool that has passed every other check (offered, the guards, `forward`, `screen`) is not made: `confirm.hold` asks the adapter's `ask` for her line and the arguments to keep, the thinker returns at once with that line as her reply and the `Held` call on the `Outcome` (the rest of that reply's calls are not made, no second round); the ledger keeps a placeholder for the line, not her words. The Spotify `ask` pins what could change by the answer: `track = "current"` (the server's own words for it) becomes the playing track's URI, a playlist name with exactly one `find_playlist` match becomes its URI and is said by its own name ("Remove 'Teardrop' from Gym? Say yes."); both are read-only calls. The daemon keeps one `held` call and a timer of `[actions] confirm_s` (10 s) from when she asked, paused while the listener is busy (the answer is being recorded or transcribed). The next sentence through `_handle_voice`, before the gate, is the answer: `confirm.answer` reads the whole sentence against word lists (yes / no / filler phrases, multi-word first), so "yes, go ahead" is a yes, "okay, never mind" a no, and "yes and play some jazz" or "thank you" (what whisper often hears in silence) neither. A yes makes the held call exactly (`toolbox.call(server, name, arguments)`), and the adapter's `done` writes the fact ("Removed Teardrop by Massive Attack from Gym.") with the usual quip after it; no model is asked again. A no says "Okay, I've left it." and stops there. Any other sentence drops the held call and is handled as usual, with "I've left that, then." said first as part of the same line (the widget cuts a line short when the next arrives, so a reflex 0.3 s later would have swallowed a separate one). No answer: "No answer, so I've left it.", also kept in the ledger. A bare yes or no in the minute after a timeout or a no gets a fixed line ("I've already left that one. Ask me again if you still want it.") and not the thinker: live, with her question in the ledger, Qwen answered a late "yes" with "Removed Teardrop from Gym." and a `play` call, removing nothing. Only `source: voice` events reach `_handle_voice` (spoken, typed into the widget, or posted by a local process as the user, which could as well have asked for the removal itself); notifications, media, git and action events, tool results and web pages have no path to it. `/health.confirm` has the held tool (never its arguments) and the wait; the log names the tool only. `strawberryd --think` never makes a held call. Measured live on a throwaway daemon (port 8791, temp dirs, a stand-in MCP server with the Spotify tool names, `qwen3.8:27b`): "take this off my gym playlist" and "remove this from my liked songs" were held and asked about by the playing track's name; "Sure, do it." made exactly the held call; "skip this" while waiting said "I've left that, then. Skipped. Now Blue Monday by New Order."; a notification titled "yes" left the question open; silence dropped it after 10 s. Qwen first sent `remove_saved_tracks` a made-up ID ("spotify:track:teardrop-massive-attack"); the adapter's `guard` now sends it back to look the track up (`get_current_track`), and it then asked about the right one 2/2. With her question in the ledger in her words, Qwen copied it ("Remove Blue Monday from your Liked Songs? Say yes.") with no call held, so the ledger keeps it as `confirm.LEDGER_HELD` instead; after that, three rounds of ask/no, ask/silence/late yes, ask/yes held 9/9 asks and removed only after the three yeses. Tests: `tests/test_confirm.py` (fake Ollama, fake Spotify). *Since brain step 6, stage 2 (§19):* the run that asked waits for the answer instead of ending with the question, the timer is `[approvals] change_s` (the old `[actions] confirm_s` is read as it), and the answer can also come from her card on a body or the Brain UI; what she says at each turn is unchanged.

**Why one brain (2026-09-22).** The tiers between the reflex and chat were where every live failure came from: a wrong reflex on a sentence that carried a name, "it's already playing" when she had misheard, and offers nobody had asked for. Gemma keeps what it is good at — the desktop events (§3, §4) and the quip after a reflex — and the user gets one voice for everything else. `[thinker] enabled = false` (the unit tests, `scripts/check_config.toml`, a machine without Qwen) falls back to the old Gemma chat path, so voice still works with a 1B model and no MCP servers.

**The ledger (built 2026-09-21).** `strawberry/ledger.py` is her only memory across turns: the last `[actions] ledger_turns` (6) exchanges no older than `ledger_age_s` (10 min), each "the user said …; you did … and said …". Qwen gets all of them in the situation, Gemma the last three when it is the fallback, so "the other one" and "skip this one too" resolve; nothing else accumulates. `/health` shows it. **Typed input:** `strawberry talk` posts each terminal line as a voice event, so a typed sentence and a spoken one take the same path and she answers in both places; the prompt prints the gate's reading and the reflex or the thinker's tool calls with its mood.

### 8c. The learning loop, first half: outcomes (`strawberry/outcomes.py`)

The gate's examples are the whole router (§8a), and today a misread sentence is fixed by hand. A trainer learns from what happened after each route (§8d): this half collects the data, and §8d turns it into labels and candidate heads. What the trainer retrains is the gate's head (§8a): it writes a new version into the heads dir and, once accepted, moves `current`; a rollback moves it back. **Off by default** (`[learning] log_outcomes = false`): on, the sentences the user says or types are kept on disk, and the project is open source.

`Daemon.handle_voice` calls `OutcomeLog.heard(text, route, source)` right after the gate and `acted(record, path, ok, …)` once the sentence is handled (reflex, thinker, no_catalogue or chat); both are no-ops when it is off. A record is written to `<state>/outcomes.jsonl` (`paths.outcomes_file()`) once its outcome is known, from the next sentence or from nothing coming:

| outcome | what the user did | strength |
|---|---|---|
| `correction` | within `rephrase_s` (10 s), a sentence the phrase check `CORRECTION` matches: "no, I meant…", "not that one", "I meant", "ei kun", "tarkoitin" | strong: the route was wrong |
| `undo` | within `undo_s` (10 s) of a reflex, the opposite one (skip ↔ previous, pause ↔ resume, volume up ↔ down; the gate's tool at ≥ 0.5) | strong negative for that reflex |
| `rephrase` | within `rephrase_s`, a sentence whose embedding has cosine ≥ `rephrase_similarity` (0.8) with this one | medium |
| `repeat` | the same reflex again (skip, skip) | neutral: two were wanted |
| `silence` | nothing for `silence_s` (30 s) | weak positive |
| `moved_on` | another sentence, none of the above | none |
| `none` | the daemon stopped first | none |

A thinker record also gets `teacher`: the thinker called exactly one tool, it succeeded and had no arguments, and it is one a reflex covers (`REFLEX_TOOLS`: `pause`, `next`, `play`, `get_current_track`…) — System Two saying this sentence should have been a reflex. The new sentence's record says what it was to the one before (`follows: {"id", "as"}`). The correction check is small and conservative on purpose ("no more music" and "no worries" are not corrections); a false one mislabels a good route, and the trained router is meant to learn corrections from the rephrases. The 0.8 is from embeddinggemma with the gate's query prefix, 2026-09-25: paraphrases 0.79–0.98 ("skip this song" ~ "next track please" 0.82, "pause" ~ "stop the music" 0.79), unrelated sentences 0.24–0.57, "turn it up" ~ "turn it down" 0.77 (an undo, checked first), but "play the beatles" ~ "play radiohead" 0.92, which is why a rephrase is only medium.

The vector is the gate's own: `SystemOne.read` returns it with the answers and `Route.vector` carries it (not in `to_dict`), so relating two sentences costs no embedding call. `IS_SENSITIVE` (§4) is asked on the same vector in `Gate.route`, also for free.

**Guards.** Only outcomes are stored as labels; the route is kept as the gate read it, never as "the router was sure". A sentence the gate reads as `kind=other` (the TV, someone else) is ignored altogether: not kept, and it does not break a silence. A sentence that is private (`privacy.pattern`, or `IS_SENSITIVE` ≥ 0.5; a route without that answer counts as private) is not kept, though it still settles the one before. A sentence the gate could not route is not kept (there is no reading to learn from). Tool results, arguments and her replies are never stored: a call is `{server, name, arguments: bool, ok}`. The journal gets the path and the label (`outcomes: voice reflex -> undo`), never a sentence; `tests/test_outcomes.py` runs a canary sentence through the gate, the reflex tier and the thinker with every logger at DEBUG and fails if it is in any record of `strawberryd.outcomes`. (The gate, the actions and the thinker log only its length unless `[daemon] log_sentences` is on, §15; this canary is about this module, `tests/test_log_sentences.py` about the rest.) The file is created 0600, rewritten 0600 when pruned, and pruned at start and every 200 records to `max_days` (30) and `max_records` (5000).

A record, one line of JSON (shortened; `answers` has every routing question):

```json
{"v": 1, "id": "70cd40b63998", "ts": 1790316749.383, "at": "2026-09-25T09:12:29+0300", "source": "voice",
 "text": "skip this song",
 "route": {"kind": "request", "topic": "music", "confidence": 0.9956, "decision": "act", "tool": "skip",
           "tool_confidence": 0.9715, "has_argument": 0.0108, "library_change": 0.2097, "catalogue": 0.011,
           "is_urgent": 0.9472, "is_about_her": 0.0127,
           "answers": {"kind": {"confidence": 0.9956, "probabilities": {"request": 0.9967, "question": 0.0017, "chat": 0.0015, "other": 0.0001}, "choice": "request"}, "…": {}}},
 "path": "reflex", "reflex": "mpris.skip", "ok": true, "calls": [], "teacher": null, "follows": null,
 "outcome": "undo", "after_s": 2.1, "by": "282bcd8300af"}
```

`strawberry outcomes [--last N] [--clear]` reads the file (the daemon need not run): counts per signal, path and source, the teacher count, and the last N records with their sentences, printed to the user's terminal. `/health.learning` has `enabled` and `records`, and when on `written`, `waiting`, `signals`, `skipped` (other, private, unrouted), `errors` and the path. The first-run privacy note (§15) says whether sentences are kept, and where.

### 8d. The learning loop, second half: labels, candidates, the switch (`labels.py`, `learnfit.py`, `learning.py`)

§8c keeps what came of each routed sentence. This half turns those records into labelled sentences, trains a *candidate* head on the shipped data set plus them, holds it to the held-out set and puts it in use only when the user accepts it (or at once with `[learning] auto_switch = true`). It improves the answers to the questions the gate already asks; a new question or option still needs examples and a code change. `learning.py` is the API the CLI (`learncmd.py`), the daemon, the tray and the Brain UI (§17) call; nothing in it prints.

**The labels** (`labels.extract`). Each record says something about its own sentence, if anything, and only from what happened next or from what the thinker did: never from the gate being sure.

| signal | when | labels the sentence | weight |
|---|---|---|---|
| `undo` | the gate's reflex T, undone (§8c) | avoid `music_tool` T | 1.0 |
| `correction` | the gate's reflex T, then "no, I meant…" | avoid `music_tool` T | 1.0 |
| `rephrase` | the gate's reflex T, then the same said again | avoid `music_tool` T | 0.6 |
| `hint` | after a correction or rephrase, the next sentence's own reflex R, or the thinker's bare R (teacher), that nothing objected to (its outcome silence, moved_on, none or repeat) | the reflex labels of R | 0.3 |
| `teacher` | the thinker called R's tool alone, with no arguments, and nothing objected | the reflex labels of R | 1.0 |
| `silence` | the gate's reflex T and nothing said for `silence_s`; not when the gate was already sure (kind and tool confidence >= 0.95) | the reflex labels of T | 0.25 |
| `silence` | "that needs a music add-on" (§8b) and nothing said | `needs_catalogue` yes | 0.25 |
| `repeat`, `moved_on`, `none` | | nothing | |

The reflex labels of R: `kind` request (question for `now_playing`, as the data set has it), `topic` music, `music_tool` R, `has_argument` no, `needs_catalogue` no. Only the gate's own reflexes count: the path was a reflex on the gate's `act` decision with that music tool, the reflex was named after it, and it worked. A reflex read off the words (an adapter's "I like this"), one that failed, or a thinker answer says nothing about the gate's tool, so an undo or correction after it labels nothing. Why each:

- An undo or a correction says the reflex was wrong but not what was meant, so it is an *avoid*: the row's loss is −log(1 − p(T)) (`gatehead.Sample.avoid`, still convex), which pushes T down and leaves the other options to the rest of the data. A hard label "other" would claim the user wanted no button at all, which a correction to "pause" contradicts.
- A rephrase is the same avoid at 0.6: §8c measured "play the beatles" ~ "play radiohead" at 0.92, so a rephrase can be a new request.
- The follow-up's reflex hints what was meant, but the user may have changed their mind or said something else entirely, so it is the weakest positive (0.3), and only when nobody objected to the follow-up itself.
- Teacher is the strongest positive: System Two read the sentence in full and did exactly what a reflex does (§8c). It is dropped when the user corrected or rephrased afterwards: then System Two was wrong too.
- Silence after a reflex is the only label that confirms the gate's own reading, so it is weak and skipped where the gate was already sure: there it teaches nothing and would only make the head surer of itself.

**The store** (`labels.ExampleStore`, `<data>/gate/learned.json`, 0600). One example per distinct sentence (case, spacing and end punctuation do not count), with its evidence (record id, signal, labels, avoid, weight; a record counts once however often the file is read, so extraction is safe to run again and needs no cursor). Labels are resolved per question when asked: each option's positive weight minus its avoid weight; the best positive wins when the runner-up has less than half of it, a closer runner-up drops that question for that sentence (`conflicts`), and with no positive left the most avoided option is the avoid; weights cap at 1; ties go by the question's option order. So the same evidence in any order gives the same labels. Never stored: `kind` other, `privacy.pattern` (codes, sign-ins), records older than the last `forget`. A sentence taken out (rejected in review, or read as private by the trainer) loses its text and keeps its key under `gone`, so the same sentence is never taken in again. At most 3000 examples (the oldest go) and 20 pieces of evidence each.

**The candidate** (`Learning.train`, `learnfit.run`). Every usable example (labelled, not rejected) is embedded with the gate's own embedder and query prefix, with the data set, the held-out set, the examples and the anchors, through the same vector cache as `strawberry gate train` (`<cache>/gate/vectors-<model>-<backend>.npz`, so a retrain embeds only new sentences). Then, on the vectors:

1. *Privacy again*: IS_SENSITIVE on the vector with the head in use, failing closed (§8a); a sentence that reads as private is deleted from the store.
2. *The held-out set stays out*, both ways: a learned sentence within cosine 0.95 of a held-out one is left out (§8a's rule for the data set), and the held-out file is package data that nothing here writes. Auto-labels never enter it.
3. *Near duplicates*: two learned sentences within 0.97 with the same labels are one (the higher weights kept).
4. *Balance* (`learnfit.balance`): per question and option, the learned weight at most `max_share` (0.25) of the data set's rows with that option, and per question at most `max_share` of its rows; over a cap the rows involved are scaled down together. The data set has 41–96 rows per player button, so a few dozen labels cannot swamp one; the fit's balanced class weights count only rows that say what a sentence is.
5. The fit is `gatehead.train` on the data set plus the learned rows (group `learned:<key>`, so a sentence validates only out of its own fold), L2 and T and the thresholds chosen as for the shipped head. A run with no learned rows trains nothing: retrained on the data set alone, the head is the shipped one to the digit (measured: same 1270/1338, 168/199, 30 reflexes 3 wrong).

**The held-out gate** (`learnfit.better_or_equal`). The candidate and the head in use (the shipped one, a user head, or the nearest examples when no head fits) are scored on `gate_heldout.json` by `gateeval`, each through a gate built like the daemon's (its questions, extra examples and anchors, the candidate's own thresholds). It passes only with at least as many fields right, as many sentences strictly right, as many notifications read right for privacy, and no more wrong reflexes. A candidate that fails is kept and marked `rejected` with the reason. `scripts/gate_phrases.json` is scored too when there is a checkout, and reported, never gated on (step 4 tuned the data against it).

**The switch.** Every head is kept: `heads/head-<version>.npz` and beside it `head-<version>.json`, its manifest (0600): the store keys and label digests it was trained with (never the sentences), the counts (data set, learned, left out by reason, scaled by the balance), the data set's hash, the held-out scores of it and of the head it was compared with, `parent` (the head in use when it was built), the trigger (idle or by hand) and what became of it (`candidate`, `accepted`, `rejected`, `superseded`, and `rolled_back_at`). `learning.json` (0600, no sentences) has the candidate waiting, the history of switches and a stack of the heads in use before, events for the report, and what the last run had (so "new labels" means new since then). `accept` needs a passing candidate whose parent is still the head in use (else "train again": the comparison is stale). `rollback` pops the stack: the head in use before this one, then the one before that, down to the shipped head. `strawberry gate use` goes through the same switch, so a rollback comes back from it too. With `[gate] head` set to a file the loop's heads are not used, and `status` says so.

**No restart.** `Gate.reload_head` loads the head `current` names and swaps it in (weights, temperatures and thresholds together; the anchors stay), after the same fit check as at start; a head that does not fit is not used and the one in use stays. The daemon's `IdleTrainer` looks at `current` every 5 s, so an accept or a rollback from the CLI or the tray reaches the running daemon within seconds. Measured on a throwaway daemon: the switch 4–5 s after the command, both ways.

**When it trains** (`IdleTrainer`, in the daemon). With `idle_train` on (the default) and outcome logging on: once nobody has spoken for `idle_minutes` (20) and at least `min_new_labels` (10) labelled sentences are new since the last run, at most once in 15 minutes. "Spoken" is a sentence being handled (`Daemon.voice_handling`, `voice_at`) or the listener recording or transcribing. The run never holds up a sentence: the embedding goes through the gate's own embedder (a worker thread, as a route's does) 16 sentences a call, and between two calls it stops as soon as the user speaks (`gatecmd.EmbedInterrupted`; what was embedded stays in the cache); the fit and the scoring run in a child process (`python -m strawberry_crab.learnfit`, the job and its sentences through a pipe, never a file) at nice 19 (below normal on Windows) with two BLAS threads. Measured on a throwaway daemon with the real model: a run with the vectors cached took 30 s; routes during the child's fit took 22–26 ms against 21 ms idle. A first run embeds ~2500 sentences (the data set, the held-out set, the examples), about a minute. `strawberry learning train` does the same in the foreground with no idle check.

**Reporting.** `strawberry learning report` (and `Learning.report`): "learned 7 new examples, 5 rejected, held-out 94.9% → 94.9%", the signals, the candidates built, accepted and rejected, rollbacks, and the head in use then and now. With `weekly_line = true` she says one line a week in her voice ("Learned three new things this week. I've got better at pausing the music."), written by code from the report: only when something was learned, never in quiet hours (`[speech] quiet_hours`), with a widget connected and two minutes of quiet; the first week starts when it is turned on. `/health.learning` adds, to §8c's counts: `head` (the version in use), `candidate` (version, passed, held-out before and after), `examples`, `new`, `last_train`, `rollback` (where a rollback would go, or null), `auto_switch`, `versions` and `trainer` (`phase`, `waiting_for`, runs, interruptions, errors); counts and versions only, re-read only when a file changed. The tray shows **Router: roll back to previous** while `rollback` is set (§14).

**Privacy.** Sentences live in two files only, both 0600 in the user's dirs: `outcomes.jsonl` (§8c) and `learned.json`. Everything else names a sentence by its store key (a hash). What is derived from them is the user's alone too: the vector cache (`<cache>/gate/`, embeddings keyed by a hash of the text), the heads, their manifests and the `current` pointer are written 0600 (`paths.open_private`) in directories made 0700 (`paths.private_dir`: `gate/`, `gate/heads/`, the cache's `gate/`). Damaged files never switch anything off: a head that does not load (an empty file, a broken zip, a meta of another shape) is a `HeadError` and the gate falls back to the shipped head; a `current` pointer naming anything but a `head-*.npz` in the heads dir is ignored; a broken vector cache is made again; an outcome line or store entry of another shape is skipped (and a sentence over 500 characters is never an example); version names are checked (`[A-Za-z0-9._-]`, no `..`) before any path is built. The locks (`labels.file_lock`) hold a token (pid and a random part); one whose holder is gone is taken over by renaming it aside first, a live holder keeps its lock however long it runs, and on the way out a process removes the lock only while it still holds its own token. The child has 30 minutes and is killed after that. `/health.learning.trainer.last_error`, the child's reported error and the tick's log line carry an exception's type only, never its message, and the child's stderr is never read back. The log says counts, signals, versions and scores; `tests/test_learning.py` runs a canary sentence through extraction, a real training run, accept, rollback, the report, the status and `/health` with every logger at DEBUG and fails if it is in any record, in `learning.json` or in a manifest. `strawberry learning forget` first marks the outcome records written so far as ignored and drops the candidate waiting, then (holding the training lock when it comes within 5 s) deletes the store and the vector caches; `--all` also deletes the heads the loop made (the shipped head is in use again). A run that started before the forget is discarded when it ends, head file and all, so nothing trained on forgotten sentences is ever offered or switched to, `auto_switch` or not. `strawberry outcomes --clear` is separate.

**Measured end to end** (2026-10-08, a throwaway daemon on port 8797 with temp dirs, the real ONNX model, 14 synthetic records): 13 labels, 12 examples (the record with a code in it never became one). The first candidate scored 94.9% → 95.0% on fields and 84.4% → 84.9% strict but fired one more wrong reflex on the held-out set ("stop it sam" as pause, learned from three pause teachers), and was rejected; with those examples rejected in review it scored the same as the shipped head and waited; accept and rollback switched the running daemon's head within 5 s; the idle trainer then built two more candidates in its child process (the second superseding the first).

Tests: `tests/test_learning.py` (every signal on synthetic records, privacy, dedupe, conflicts, balance, the gate's rule, manual and auto mode, a worse candidate rejected in both, rollback, the held-out set never written and its sentences left out, the canary, the child process, the idle trainer not starting while voice is active and stopping when the user speaks, the gate following `current`, the weekly line and quiet hours, the CLI); `tests/test_gatehead.py` for the avoid rows.

---

## 9. Naming contract (must match the GLB — do not rename)

```
states → clips:   idle→idle_loop  listening→listen_loop  thinking→think_loop
                  talking→talk_base  dancing→dance_loop
one-shots:        alert_snap   notify_perk
shape keys:       blink  squint  eye_wide  happy  claw_open_L  claw_open_R  squash  leg_tuck
bones:            root  body  eyestalk_L  eyestalk_R  claw_arm_L  claw_arm_R
materials:        mat_shell  mat_shell_dark  mat_claw  mat_cream  mat_eye  mat_ink
```

`talk_base` is the neutral talking pose; the clack is driven live via `claw_open_*`, **not** baked into the clip. The widget reuses the v2 runtime controllers (`blink_controller.gd`, `claw_controller.gd`, `cel_style.gd`, `skin_palettes.gd`) unchanged.

---

## 10. Build order (each phase independently testable)

1. **Contract + transport.** ✅ Daemon skeleton: HTTP intake + ws server + `perform()` that only forwards. Godot widget as ws client in a transparent always-on-top window. Test: `curl` a blob → crab changes state. No brain, no audio.
2. **Reaction path, silent.** ✅ git `post-commit` → `/event` → Ollama one-liner + emotion → blob with `text`, no `audio` → **bubble appears above the crab on commit.** Proves event → brain → face end to end with zero audio risk.
3. **Notifications doorway.** ✅ D-Bus monitor → same intake. Anything that notifies (including WhatsApp/Messenger) makes her react.
4. **TTS.** ✅ Piper → `audio` field → Godot plays it through the analysed bus → claws clack in time. `[speech]` in config turns it on.
5. **Voice in.** ✅ Hotkey → faster-whisper → transcript → the reaction path answers (§7). The action path with tools is Phase 6.
6. **The gate.** ✅ Every spoken sentence goes through the local System One (§8a): kind, topic, urgency, is-it-about-her, the tool and its argument, with probabilities and a decision in the log and `/health.gate`.
7. **MCP actions, reflex tier.** ✅ The MCP client and the reflexes (§8b): "skip this song", "pause", "louder", "what song is this" are done in about a second with no model in the loop, and she reports the fact plus a quip.
8. **The thinker.** ✅ Qwen with the topic's tools for sentences that carry an argument or need several steps ("play some Nina Simone"), acknowledged and covered while it loads (§8b).
9. **Ledger, then one brain.** ✅ Short rolling memory for both models; then (2026-09-22) everything the user says but a bare reflex goes to Qwen in her own voice, with the tools and the ledger (§8b).

Stop after any phase and you still have something that works.

---

## 11. Acceptance criteria

- [x] `curl` posting a contract blob to the daemon makes Godot change state / play a one-shot. (`scripts/check_phase1.sh`)
- [x] Bubble reveal is timed to text length when silent, to the wav length when spoken, and auto-hides.
- [x] Speech ends → crab returns to `idle` with no follow-up message from the daemon.
- [x] A `/event` round-trips through the reactor to the widget. (Canned reactor; model in Phase 2.)
- [x] Play/pause in any MPRIS media player makes her dance/idle; a track change shows a "Now playing" bubble and she returns to dancing. (`strawberry/doorways/mpris_watch.py`)
- [x] The beat of the playing music picks a dance style and the clip runs at its tempo; synthetic 128/92/168 BPM drums are tracked to within 2 BPM with the phase on the kick; an unsteady tempo sways. Scored offline by `scripts/beat_eval.py` (§4c). (`strawberry/doorways/beat_watch.py`, `check_phase1.sh` step 14, `tests/test_beat_track.py`, `tests/test_beat_eval.py`)
- [x] Browser origins are refused on HTTP and `/ws`; POSTs need the JSON content type.
- [x] A git commit makes Strawberry show a model-written bubble (silent). (`gemma3:1b` via `OllamaReactor`)
- [x] A desktop notification (any app) triggers a reaction. (`notify-send -a WhatsApp James "…"` → Gemma line in the bubble)
- [x] With TTS on, the wav plays through Godot and the claws clack to the audio; `claw_open_*` is 0 when idle. (`check_phase1.sh` step 11: 0 → 0.78 → 0; a missing wav is a 400)
- [x] A voice command routed through MCP successfully calls one real tool. (Live 2026-09-22: "skip this" → `spotify.next` + `get_current_track` in 1.1 s as a reflex; "play some daft punk" → Qwen `search` + `play` in 6.4 s, answered in her own voice.)
- [x] The same plain music commands work with no server configured at all. (Live 2026-09-22, a daemon on port 8772 with an empty `[tools.servers]`: "what song is this" → `mpris.now_playing` 9 ms, "skip this" → `mpris.skip` 403 ms, "pause" → `mpris.pause` 7 ms, "turn it down"/"turn it up" → `mpris.volume_*` 1–2 ms; with the Spotify server back, the same sentences went to `spotify.*`.)

---

## 12. Notes for the human

- **Two hops, not one.** Hooks hit the daemon over HTTP; only the daemon talks to Godot. Don't let a git hook open a websocket.
- **Godot is the ws client**, daemon is the server. Simpler, and it reconnects cleanly if you restart either side while iterating.
- **Silent-first** is the whole de-risking trick: you can prove the entire loop before touching audio.
- **One audio rule:** Piper's wav plays *through Godot*, or the analyser is blind and the clack dies.
- **CPU/GPU split:** whisper + Piper on CPU, Qwen keeps the GPU.
- **The MCP fork (§8)** is the one open design decision. Recommend the Python-SDK-in-daemon route so the action path stays in one process.

---

## 13. Desktop widget shell (Godot project `widget/`)

Strawberry lives on the desktop as a pet: a **frameless, transparent, always-on-top** window with **click-through** outside her silhouette. Four properties, set in `project.godot` and again in `widget.gd::setup_window()`:

| Property | Godot | Why |
|---|---|---|
| Transparent | `display/window/size/transparent`, `per_pixel_transparency/allowed`, viewport `transparent_bg`, clear colour alpha 0 | Only the crab is drawn |
| Borderless | `Window.borderless` | No frame or title bar |
| Always-on-top | `Window.always_on_top` | Floats over other windows |
| Click-through | `Window.mouse_passthrough_polygon` = padded convex hull of her meshes | Clicks beside her reach the desktop; clicks on her drag the window |

**Two ways to run her.** *Installed:* the exported binary `~/.local/share/strawberry/widget/strawberry-widget` (`paths.widget_binary()`), put there by `strawberry widget --fetch [--version V]`: it downloads `strawberry-widget-<V>-linux-x86_64` and its `.sha256` from the GitHub release `v<V>` (default: the package's version), checks the SHA-256, writes the file next to the target and renames it over it only after the check passes, marks it executable and records the version in `strawberry-widget.version` beside it. `STRAWBERRY_RELEASE_URL` replaces the release base URL (the tests serve a local directory). *Developer mode:* without that binary, the Godot project in the checkout's `widget/` with `godot` 4.7 from PATH (imported on first run). Neither: `strawberry widget` exits 2 with a message naming `--fetch`. `widgetbin.resolve()` makes that choice for both `strawberry widget` and the tray (§14). The same validators run in both: `scripts/check_phase1.sh` for the source project, `WIDGET=dist/strawberry-widget-… scripts/check_phase1.sh` for the binary.

**The export.** `widget/export_presets.cfg` has a preset per system; `Linux`: x86_64, the PCK embedded in the executable, `export_filter = all_resources` (every scene, script, shader and the GLB, and the `validate_*.gd` scripts, so the binary can run its own acceptance) plus `desktop_idle.py` by `include_filter`. `scripts/build_widget.sh` copies `widget/` to a staging directory, stamps `config/version` from `pyproject.toml` there (the checkout's `project.godot` stays unversioned, so a source run is `dev`), exports headless with the official 4.7.2 export templates (`~/.local/share/godot/export_templates/4.7.2.stable/`) and writes `dist/strawberry-widget-<version>-linux-x86_64` and its `.sha256` (`sha256sum` format). About 75 MB, nearly all of it the Godot runtime. What changes once exported, and how the widget copes: `res://` is inside the executable, so a file an outside program needs (the idle helper run by `/usr/bin/python3`, the evidence icon whose path goes to the daemon) is copied out to `$XDG_CACHE_HOME/strawberry/widget/` (`paths.gd::on_disk`); `res://` is read-only, so `validate_widget.gd` takes `--report=<path>`; and release templates have no `--script`, so the binary runs a validator with `strawberry-widget --headless -- --acceptance=res://validate_widget.gd …`: `widget.gd` gives the SceneTree the validator's script and steps aside before building anything. The window properties below all come through the export unchanged (checked 2026-09-22 on X11: 32-bit ARGB visual, `_NET_WM_STATE_ABOVE`, Motif hints with no decorations, a 117-rectangle input shape covering a third of the window, sound through PulseAudio on the analysed bus, and a capture whose background pixels have alpha 0).

**The Windows export** (`WINDOWS.md` step 8) is the second preset, `Windows`: the same filters and embedded pack, `application/icon` the berry (the export turns `icon.png` into the .exe's icon, which Explorer and Task Manager show), product name and description, file version from the stamped `config/version` (`0.1.1.0`), no console wrapper. `scripts/build_widget.sh` picks the preset by the system it runs on (`TARGET=windows|linux` for the other), and under Git Bash finds the templates in `%APPDATA%\Godot\export_templates\4.7.2.stable\` (only `windows_release_x86_64.exe` and `version.txt` are needed), strips CRLF from the staged `project.godot` before stamping, and writes `dist/strawberry-widget-<version>-windows-x86_64.exe` and its `.sha256`, about 110 MB. Nothing but the .exe is written: no ANGLE or D3D12 libraries (the renderer is `gl_compatibility` on OpenGL). `release.yml` builds it on `windows-latest`, runs `check_phase1.sh` on it there, and attaches both to the release.

**On Windows** the window is Godot's Windows driver with the same four properties and no driver argument: a `WS_POPUP` window without a caption, `WS_EX_TOPMOST`, per-pixel alpha composited by DWM, in the taskbar as "Strawberry" with the berry (the window's `WM_GETICON` icons come from `config/icon`; in developer mode the class icon is Godot's, in the export the .exe's). Checked 2026-09-23 on Windows 11 in both forms, against a throwaway daemon: the desktop showed through everywhere but her, `on_top` from the tray cleared and set `WS_EX_TOPMOST`, and `WindowFromPoint` found her window on her body and the window below in the empty corners. The difference that matters: Godot makes `mouse_passthrough_polygon` the window's region (`SetWindowRgn`), which cuts what is **drawn** as well as what is clicked, where X11's input shape only cuts clicks. So on Windows the region follows her pose: `widget.gd::follow_pose` runs on `RenderingServer.frame_pre_draw` (after the clip, reactions, dance style and hat have moved her) and takes the padded hull of her posed parts (`body_points`: every mesh follows one bone rigidly, so each bone's box of vertices, blend shapes' extents included, is carried by that bone's pose; a skinned mesh's own AABB is its rest shape, which is why raised claws and peeking eyes were cut before), the whole bubble line's block (`bubble.gd::outline`, the fitted font's height above the anchor), the badge and the type box. Each frame's hull is kept for `REGION_HOLD_S` (1.5 s) and the region, their hull plus `REGION_SLACK` (8 px), is set only when she reaches outside it, or at most once a `REGION_HOLD_S` when that frees 3 % of it: about 0.26 ms a frame, about 50 region updates a minute while she dances and reacts, and back to her rest shape afterwards. `update_passthrough` on Windows is `follow_pose(true)`, and while she is hidden the 1-pixel region stays. The open menu and its submenus join the region (`open_menus`, `with_menus`, checked each frame before it is drawn) instead of the polygon being emptied as on Linux: an empty polygon removes the region, and Windows repaints the whole window rectangle before the next frame, a visible flash. The whole-window `mouse_passthrough` flag is no substitute there: Godot 4.7 answers `HTTRANSPARENT` for it, which other processes' windows never see (`WINDOWS.md`, the widget). The idle helper for sleep runs on `STRAWBERRY_PYTHON`, which `strawberry widget` and the tray set to their own interpreter on Windows (there is no system Python); `desktop_idle.py` reads `GetLastInputInfo` there, and without the variable automatic sleep stays off. `strawberry widget` in developer mode runs Godot tied to itself by a kill-on-close job (`winproc.call_tied`), so ending it ends Godot. A second display was not available for the checks.

**X11 pin.** `strawberry widget` launches the widget (binary or Godot) with `--display-driver x11`. On the current X11/GNOME session that is native. On a Wayland session it runs under XWayland, where Mutter still acts as the X window manager and honours the keep-above and position hints. Native Wayland clients on GNOME get neither (no layer-shell, no client positioning), so we don't take that path. Always-on-top and remembered position are treated as **best effort**: if a compositor ignores them she degrades to a normal frameless window, nothing breaks.

**Interaction.** Drag her body to move the window; the position is remembered in `~/.config/strawberry/widget.cfg` along with the skin, and clamped to the screen's usable area on restore. `Q` quits, `C` cycles skins. Cel shading and the ink outline are always on in the widget (the v2 preview's `O` toggle was a review aid, not a feature). The daemon can also send `{"command": "quit"}` or `{"command": "skin", "value": "mint"}`.

**Right-click menu (`widget/menu.gd`).** A `PopupMenu` drawn inside her transparent window: *Mute her voice*, *Quiet for an hour* (shows minutes left, click again to cancel), *Voice volume* (25–100 %), *Skin*, *Always on top*, then *Settings file…* (opens `$XDG_CONFIG_HOME/strawberry/config.toml` in the desktop's text editor; a missing one is first written from the template by `strawberry config --init`), *Apply settings* (`strawberry restart`: under the tray unit that restarts the tray and everything under it, otherwise the daemon; the widget reconnects), *Voices folder…* (`$XDG_DATA_HOME/strawberry/voices`), *Reset position*, *Quit*. Mute and quiet keep the bubble and drop only the sound, widget-side, so the daemon still voices lines and a second widget would still hear them. While the menu is open the passthrough polygon is cleared so the whole window takes clicks; it is recomputed on close. (On Windows the region takes in the open menus instead; see above.) Every item has an explicit id: items added without one get their index as id, and a submenu row then steals a real row's check mark. Both CLI calls go to `$STRAWBERRY_CLI` (the tray and `strawberry widget` set it to the absolute path of the `strawberry` that launched her), else `strawberry` on PATH; if neither can be run she says so in her bubble. Nothing in the widget points into a checkout.

**Preferences** persist in `$XDG_CONFIG_HOME/strawberry/widget.cfg` (`[appearance] skin, top_hat`, `[audio] muted, quiet_until, volume`, `[window] always_on_top, x, y`, `[sleep] after_minutes`), written by the widget and read back by the tray (§14); `widget/paths.gd` and `src/strawberry_crab/paths.py` apply the same XDG rules. Until PACKAGING.md step 4 they lived in Godot's `user://widget.cfg` (`~/.local/share/godot/app_userdata/Strawberry/widget.cfg`): the first start with a display copies that file to the new place when the new one does not exist yet, and leaves the old one where it is. Headless runs (every acceptance check) read and write `user://widget_headless.cfg` instead, or a per-validator file, and never the real preferences; `validate_prefs.gd` checks the path, the one-time copy against scratch files, and the `config --init` call.

**Type box (`widget/type_box.gd`).** *Chat with Strawberry…* in the menu, or the T key while she has focus, opens a plain field under her: a square-cornered `LineEdit` styled as smoked glass (a 40 % dark tint with a hairline rim; the desktop is not in Godot's viewport, so there is nothing to blur) with only the caret in the skin's claw colour; the rim stays glass, and focus only brightens it. Enter sends the line over the websocket as `{"type": "heard", "text": …}`; the daemon turns it into `Event(source="voice", title=text)` and runs `handle_event` as a background task, so it takes the same funnel as speech and `strawberry talk` (gate, reflexes, Qwen, ledger) while the socket keeps answering pings. Escape closes it. The field greys out while she is listening or thinking ("She's on it…") or the daemon is away ("Not connected to strawberryd"). While open, the field's corners join the passthrough hull so it takes clicks; closed, they fall through again. `--typing` opens it for a capture; step 17 of `validate_widget.gd` opens it headless, submits a line and sees the answer and the ledger entry.

**Step chip (`widget/step_chip.gd`, §18).** While a run is busy, a small chip under her bubble says what she is doing in plain words: "thinking…", the tool's label from the daemon ("Spotify: next", "searching the web…"), "stopping…". It is the type box's smoked glass (40 % dark tint, hairline rim, cream text, square corners), centred under the bubble's anchor and as wide as its words. Its ✕ is a 40 px square (a fingertip on a touch screen) and sends `{"type": "run.cancel", "run_id": …}`; it shows only when the daemon's `welcome` said this body may cancel. The chip waits 0.35 s before it shows, so a reflex (~0.25 s) never flashes one, and goes with the run's terminal event (or after 60 s without one). While it shows, its corners join the click-through hull, as the type box's do (`chip_points`, in `update_passthrough` and `follow_pose`). It is fed by the protocol v2 run events (PROTOCOL.md Part 1b), which the widget asks for in its hello (`ws_client.gd` `hello`); it never sees an argument or a result. `--capture=… --chip` captures her with the chip of a run under way. `validate_widget.gd` §18 checks the v2 hello and welcome, the chip staying hidden for a quick run, the step text, the 40 px ✕, the corners in the hull, the round trip of a `run.cancel` the daemon declines, and the run's end hiding it.

**Pupils follow the cursor (`widget/gaze.gd`, `widget/strawberry_pupil.gdshader`).** No pupil bones: the pupil is a small ellipsoid baked into each eye mesh's ink surface, and the eye mesh is bound rigidly (weight 1) to its eyestalk bone. So each frame the widget passes the bone's pose to a variant of the cel shader as `to_rest`/`from_rest` (authored rest space ⇄ posed skeleton space); vertices that land within 0.034 of the authored pupil centre are rotated about the authored eyeball centre by a shared yaw/pitch, then sent back through the pose. Exact under any eyestalk bend or stretch, and the blend shapes (lids) are untouched. The gaze itself: the cursor's screen position (readable without focus on X11) is mapped to the crab's plane from the project window size and the orthographic camera (not `Camera3D.project_position`, which needs a real viewport), the direction from the midpoint between the eyes to that point, with the cursor imagined `LOOK_DEPTH` = 2.5 units in front of her, gives the angles, clamped to ±0.7 rad and eased at 14/s. Both pupils share one gaze. After 12 s of a still cursor she looks straight ahead. `--look=x,y` pins the cursor for captures and the headless check (step 13). `CelStyle.apply()` replaces surface materials on a skin change, so the widget re-applies the pupil material after it.

**Start on login.** One unit now: `strawberry install` writes `strawberry-tray.service` (`Type=simple`, `After=`/`WantedBy=graphical-session.target`, `Restart=on-failure`) with `ExecStart=<the installed strawberry> tray --port <port>`, where the first word is the absolute path of the `strawberry` executable that ran `install` (`~/.local/bin/strawberry` after `uv tool install`, `<checkout>/.venv/bin/strawberry` through `bin/strawberry`), else `<python> -m strawberry_crab`. It runs `systemctl --user import-environment DISPLAY XAUTHORITY WAYLAND_DISPLAY XDG_SESSION_TYPE DBUS_SESSION_BUS_ADDRESS`, enables it, and drops `~/.config/autostart/strawberry.desktop` as a fallback for a session that never reaches that target — the entry runs `strawberry tray-autostart`, which does nothing when the unit is enabled, so two trays can never start. It also writes `~/.local/share/applications/strawberry.desktop` (`NoDisplay=true`, `StartupWMClass=Strawberry`, `Icon=strawberry-crab`) and the berry at 48, 64 and 128 px into the user's hicolor theme as `strawberry-crab`: not a launcher, but what GNOME's app switcher and dock match the widget's window to (Godot sets its X11 class to the project name), so they show her name and the berry instead of a generic icon. The window itself carries the same berry (`config/icon`), and its title is set through the display server as well, because a debug build (developer mode) makes `Window` add " (DEBUG)". Then it `daemon-reload`s and **restarts** the unit (not just `enable --now`), so a tray still running from an older ExecStart comes back on the new one; that is how the move from `strawberryd/.venv/bin/strawberryd --tray` to the package is made. The old per-doorway units and `strawberryd.service` are removed by the same command. Everything else is the tray's to start (§14). Once installed, `daemon/stop/restart/status` drive the tray unit instead of pidfiles; logs move to `journalctl --user -u strawberry-tray`. `strawberry uninstall` reverses it.

**Start on login on Windows** (the port, `WINDOWS.md` step 4) is a shortcut, not a service: `strawberry install` writes `Strawberry.lnk` into the user's Startup folder (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`), with the berry as its icon (`%LOCALAPPDATA%\strawberry\strawberry.ico`, made from the PNGs), stops what ran before (a by-hand daemon and doorways, an older tray) and starts the tray as the shortcut will. The shortcut runs `strawberry-tray.exe --port <port>` beside the `strawberry.exe` that ran install: a `[project.gui-scripts]` entry point, which uv and pip make as a windowless launcher (`pythonw.exe`); without it `pythonw.exe -m strawberry_crab tray`. The .lnk is written by `WScript.Shell` through Windows PowerShell (`startup.py`), so there is no extra dependency. No scheduled task and no registry entry: the Startup folder runs it as the user, Task Manager's Startup apps page lists it, and deleting the file undoes it. Once installed, `strawberry daemon` starts the tray instead of a daemon of its own, `status` prints `starts at login: <path>` and where the tray logs (`%LOCALAPPDATA%\strawberry\state\tray.log`, with one log per child beside it), `stop` stops the tray through its stop event (§2), `restart` asks a running tray to restart its children (its restart event, `Local\strawberry-restart-<pid>`: her menu's "Apply settings" runs it from inside the tray, where stopping the tray would end the caller as well), and `tray` will not start a second tray. `strawberry uninstall` removes the shortcut and the .ico and stops the tray. No app switcher entry is written on Windows; the window's own icon and title serve.

**Layout.** 380×560 window, orthographic camera (`KEEP_WIDTH`, size 1.25) centred at y 0.78, so the view spans y −0.14…1.70: the crab sits low and a bubble anchored at y 0.98 has room for five lines above her. The bubble lays each line out with `TextParagraph` (same font, width and wrapping as the `Label3D`) and shrinks the font in steps of 2 (44 → no smaller than 28) until the block fits the height between its anchor and the top of the view, so nothing is ever cut off; the badge sits at (0.5, 0.86), between the eyes and the bubble's bottom line, out of the text's way. `run/max_fps=60`, `gl_compatibility` renderer (same as the v2 preview).

**Reaction recipes (`widget/reactions.gd`).** A node at process priority 150 layers short procedural poses over whatever clip is playing, after the AnimationPlayer has written the frame, the same way the blink and claw controllers layer their morphs. Each recipe is an envelope (ease in, hold, ease out) on a few bone offsets and morphs; nothing is baked into the GLB, so a recipe is a dozen tunable lines. Bone axes from the rig: claw lift = local +X (`alert_snap` uses 37°), eyestalk sway = Z, body pitch = X, roll = Z. Every bone touched has a track in all seven clips, so the mixer resets it each frame and offsets never compound. (`SkeletonModifier3D` renders the same result, but its changes are invisible to `get_bone_global_pose()`, which the acceptance check uses to prove the lift; the plain node's are measurable: 48.5° during a wave, 0.7° after.)

| recipe | length | what moves |
|---|---|---|
| `wave` | 1.5 s | left claw arm lifts 48°, claw opens and closes twice, eyes wide |
| `peek` | 1.4 s | eyestalks stretch 35%, body leans 7° toward the viewer, eyes half wide |
| `shiver` | 0.9 s | body rolls ±3° at 18 Hz, shell squash jitters, squint |
| `double_hop` | 1.6 s | `notify_perk` played twice back to back, eyes wide throughout |
| `nod` | 1.1 s | body pitches ±10° twice, happy eyes |

Eye morphs go through `blink_controller` (`extra_wide` / `extra_happy` / `extra_squint`, combined with the clip-driven values) so the blink logic keeps ownership; claw and squash morphs are written directly at a later process priority and cleared when the recipe ends.

**Badge (`widget/badge.gd`).** A billboarded `Sprite3D` at the bubble's upper left showing the `icon` image (PNG/JPG, or SVG rasterised at load), visible while she talks, hidden with the bubble. **Window hop.** `hop: true` tweens the X11 window 26 px up and back with a small second bounce (0.47 s total); skipped while dragging or headless.

---

## 14. The tray — `strawberry/tray.py`

`strawberry tray` (the same as `strawberryd --tray`) puts a 🍓 in the top bar and is **the one process that starts at login**. It launches the daemon, the doorways and the widget as children, restarts a child that exits, and stops them all on Quit. `--tray --no-children` runs just the icon against a daemon that is already up (development, and `scripts/check_tray.sh`).

Three modules: `traymenu.py` is the menu as data and what its rows do (`menu_items`, the check marks read back, Message bodies, the health poll: `TrayCore`), `supervisor.py` the children, and the icon in front of them is `tray.py` on Linux (below) or `wintray.py` on Windows (at the end of this section). `strawberryd --tray` picks the icon by system; `tray.py` re-exports the shared names.

**The icon is a StatusNotifierItem, written by hand on jeepney.** The tray takes a bus name of its own (`org.kde.StatusNotifierItem-<pid>-1`), exports `org.kde.StatusNotifierItem` on `/StatusNotifierItem` and a `com.canonical.dbusmenu` menu on `/MenuBar`, and calls `RegisterStatusNotifierItem` on `org.kde.StatusNotifierWatcher`. Incoming method calls are answered from one dispatch table (`Properties.Get/GetAll`, `Introspectable.Introspect`, `Peer.Ping`, `Activate`/`SecondaryActivate`/`ContextMenu`/`Scroll`, and the dbusmenu's `GetLayout`/`GetGroupProperties`/`GetProperty`/`Event`/`EventGroup`/`AboutToShow(Group)`), and changes go out as `LayoutUpdated`, `ItemsPropertiesUpdated` and `NewToolTip`. When the watcher is missing (GNOME without the AppIndicator extension) nothing breaks: the tray says so once, watches `NameOwnerChanged` for it, and registers the moment it appears — a GNOME Shell restart re-registers by itself.

**The icon pixels.** `IconPixmap` is `a(iiay)`: width, height and the pixels as **ARGB32 in network byte order**, which is not what a PNG stores. `scripts/render_icons.py` renders 🍓 from Noto Color Emoji (Apache-2.0, a CBDT bitmap font: Pillow loads it only at its one 109 ppem strike) to `src/strawberry_crab/assets/icons/strawberry-{16,22,24,32,48,64}.png`, checked in and shipped in the wheel as package data (`paths.icons_dir()` finds them through `importlib.resources`); `strawberry/icons.py` reads them at runtime with `zlib` alone and reorders RGBA to ARGB, so Pillow stays a dev dependency. **`IconName` is left empty unless the icon really is in an icon theme**: GNOME's AppIndicator extension prefers the name over the pixmaps and draws a placeholder for one it cannot resolve — with `IconName = "strawberry"` the panel showed "…" where the berry should be, and with it empty the pixmaps came through (measured 2026-09-22).

**The menu is her right-click menu, in the top bar** (§13): a disabled status row (`Idle` / `Listening` / `Thinking…` / `Talking` / `Dancing`, or "strawberryd is not running"), Show her / Hide her, Chat with Strawberry…, then Mute her voice, Quiet for an hour (counting down), Voice volume, Skin, Sleep after inactivity, Sleep now, Top hat, Always on top, Message bodies (below), Router: roll back to previous (only while the learning loop has a head to go back to: `/health.learning.rollback`; it runs `Learning.rollback` in the tray's own process, and the daemon follows `current` on its own, §8d), then Settings file…, Voices folder…, Reset position, Restart (which is her menu's "apply settings": the children come back with the new config) and Quit. The choices are the same lists as `widget/menu.gd` and a test compares the two files, because that is the seam that would drift.

**How the tray knows anything.** `GET /health` every two seconds gives the status row (`state`: the last state performed, with the transients expiring after 6 s since the widget leaves them on its own). The check marks are read back from the widget's own settings file (`~/.config/strawberry/widget.cfg`, `paths.widget_prefs_file()`; the old `~/.local/share/godot/app_userdata/Strawberry/widget.cfg` until the widget has copied it over, §13), so the tray and her right-click menu agree whichever one you used last.

**Clicks go through the daemon, never straight at the widget**: `POST /command {"command": ..., "value": ...}` broadcasts to every open widget socket, and `widget.gd`'s `run_command` acts on it (`show`, `hide`, `chat`, `mute`, `quiet`, `volume`, `skin`, `on_top`, `hat`, `sleep_after`, `sleep_now`, `reset_position`, `quit`). The daemon validates the name and the value type, so a typo is a 400 rather than a `push_warning` nobody reads. Settings file… and Voices folder… are the tray's own business (`xdg-open`, writing the commented config template first if it is missing; on Windows the file's associated app, and Notepad for a `config.toml` nothing is associated with).

**Message bodies ▸ Off / React / Glance** (2026-09-22) is the one row that is not the widget's: it is `[notifications] body` in config.toml (§4), so it lives in the tray only, not in her right-click menu (which holds just the widget's own preferences; adding it there would mean a new CLI write path from GDScript, for a setting the tray already covers). The three are dbusmenu `radio` rows (ids 51–53 under 50), the checked one read from config.toml with `tomllib` whenever the file's mtime or size changes; a file that does not parse leaves the check where it was. A choice:

1. writes `body = "<mode>"` through `configedit.set_value` (the editor `strawberry setup` uses, moved out of `setupcmd.py`): only that line changes, a trailing comment on it stays, a missing file starts as the commented template, the result must load through `config.load` before anything is written, and the old file is copied to `config.toml.bak-<stamp>` first. A `body` it cannot set on one line (a multi-line string, `[notifications]` as an inline table) is refused, not half-written;
2. `POST /command {"command": "reload_notifications"}`: the daemon's own command, never sent to a widget. It re-reads `[notifications]` from its config file into `Daemon.config.notifications` (react against glance, `body_apps`, the filters); a file that does not load is a 409 and the old settings stay. Nothing else is reloaded;
3. restarts the `notify_watch` child alone (`toast_watch` on Windows; `Children.restart_child`: at once, no backoff, not counted as a restart). The watcher reads the mode once, at start, and logs `bodies <mode>`; the tray passes it `--config` when the tray has one, so both read the file that was written.

The daemon goes first, so turning bodies on never has the new watcher forwarding a body that the daemon would still read the old way, and turning them off drops bodies at the daemon while the old watcher finishes. `body_apps` is never touched; when it has entries, a disabled "Per-app overrides in config" row shows under the three (it and its separator are always in the layout and only hidden otherwise, so no id moves). With `--no-children` step 3 is a warning to restart the watcher by hand. The tray logs the mode name and nothing else from the file, and she says nothing about it.

**The children.** `daemon` (`python -m strawberry_crab.strawberryd`), `mpris_watch`, `notify_watch`, `beat_watch` (`python -m strawberry_crab.doorways.<name>`), all on the tray's own interpreter so an installed tray never reaches into a checkout, and the widget, chosen by `widgetbin.resolve()` (§13): the installed binary itself (`strawberry-widget --display-driver x11 -- --ws=ws://127.0.0.1:<port>/ws`), or in developer mode `python -m strawberry_crab widget` (which imports the project if needed and execs Godot; `STRAWBERRY_TRAY=1` keeps it from starting a second daemon or a second set of doorways). The widget child's environment adds `STRAWBERRYD_PORT` (the tray's port) and `STRAWBERRY_CLI` (the absolute path of this `strawberry`, for her menu). With neither a binary nor a checkout the tray logs how to get one (`strawberry widget --fetch`) and runs the rest. A child that exits is restarted after a backoff that doubles from 1 s to 30 s and resets once the child has stayed up for 30 s; Quit terminates them all, killing what does not go in 5 s. Stopping the unit does the same after systemd has already sent SIGTERM to every process in its cgroup (the default `KillMode=control-group`), so each child is signalled twice; the daemon treats that as one stop (§2). The tray writes `~/.local/state/strawberry/tray.json` (its own pid, each child's pid, command line and restart count) and `strawberry status` prints it.

**On Windows** (`WINDOWS.md` step 4) the children are the same and are started differently: no console window (`CREATE_NO_WINDOW`), a process group of their own, output to `%LOCALAPPDATA%\strawberry\state\<name>.log` (`strawberryd.log` for the daemon; moved to `.1` past 5 MB) since there is no journal, and a kill-on-close job object, so a tray that is killed takes them with it, as the unit's cgroup does. Stopping one sets its stop event (§2), keyed `<tray pid>-<name>-<n>` and passed in `STRAWBERRY_STOP_EVENT`; the widget binary has none and gets TerminateProcess. The 5 s grace and the kill after it are the same.

**The Windows icon** (`wintray.py`) is the Win32 API through ctypes, as the Linux one is jeepney: no tray library, no Pillow. A hidden top-level window on a thread of its own owns a `Shell_NotifyIconW` icon (NOTIFYICON_VERSION_4) and runs its message loop; asyncio, `TrayCore` and the supervisor stay on the main thread. A right click refreshes from `/health` (up to 0.5 s, as AboutToShow), builds a popup menu from `menu_items()` (`MFS_CHECKED` check marks, `MFT_RADIOCHECK` radio rows, the four submenus, the greyed status row, separators; rows hidden on Linux are left out) and shows it with `TrackPopupMenuEx`; the chosen id goes to `TrayCore.clicked`. "Hide her"/"Show her" is the default row, in bold, and a left click on the icon runs it (Activate on Linux). The tooltip is "Strawberry: <status>". The icon is a 32-bit HICON with alpha made at runtime from the packaged PNGs at the notification area's size. `TaskbarCreated` (Explorer restarted) adds it again, an add that fails at login is retried every 2 s, and `WM_ENDSESSION` stops the children before logoff. The tray logs to `<state>\tray.log`, with dates. The same window holds the listen hotkey (§7): `RegisterHotKey` with `MOD_NOREPEAT` for `[voice] hotkey` (default Ctrl+Alt+Space), and WM_HOTKEY becomes `POST /listen` on a bare socket, what `strawberry listen` sends; the tray reads the setting again whenever config.toml changes (the same mtime check as Message bodies) and re-registers it. A combination another program or Windows holds is one warning (`hotkey <combo> not registered: another program or Windows already uses it; choose another with: strawberry hotkey COMBO`) and the tray runs on without one. Acceptance on Windows is `tests/test_wintray.py`, which builds real menus and icons and reads them back with `GetMenuItemInfoW` (conftest blocks `Shell_NotifyIconW`, so nothing is shown), and the live check in `WINDOWS.md`.

Acceptance: `scripts/check_tray.sh` registers the item on the real session bus and reads it back with `busctl --user` (introspect the item, `GetLayout`, the watcher's registered list). Unit tests build every reply as a jeepney message, serialise it and parse it back, which is how a hand-written D-Bus server catches a signature that does not match its data.

## 15. Settings

One file, `~/.config/strawberry/config.toml` (`$XDG_CONFIG_HOME` respected). **Every path comes from `strawberry/paths.py`**: config `$XDG_CONFIG_HOME/strawberry/`, data `$XDG_DATA_HOME/strawberry/` (`voices/`, `models/` (the gate's ONNX model), `gate/heads/` (the gate's trained heads and their manifests, §8a, §8d), `gate/learned.json` and `gate/learning.json` (the learning loop, §8d), the widget binary), state `$XDG_STATE_HOME/strawberry/` (`tray.json`, the pidfiles and logs of by-hand runs); an unset, empty or relative variable means the default under `~`. On Windows (the port, `WINDOWS.md`) they are `%APPDATA%\strawberry\` for the config and `%LOCALAPPDATA%\strawberry\` for data, with state in its `state\` subdir. The config and the widget's preferences are read and written as UTF-8 on every system. Defaults live in code (`strawberry/config.py`), so the file only needs the lines you change. Read by the daemon at start and by the media watcher; `GET /config` shows the effective result. Unknown keys warn; wrong types refuse to start with the key named.

```toml
[daemon]
port = 8770
warm_on_wake = true              # reload the gate and reaction models after a suspend (§4)
log_sentences = false            # true: the user's sentences go into the journal as they are (below)

[brain]
enabled = true
reaction_model = "gemma3:1b"     # the reflex (one line per event, resident)
action_model = "qwen3.8:27b"     # the thinker (voice + tools, later)
ollama_url = "http://127.0.0.1:11434"
timeout_s = 1.5
temperature = 0.8
max_words = 15
keep_alive = -1                  # seconds; -1 = stay in VRAM, or "10m" to unload when idle
# persona = """..."""           # her system prompt

[[brain.examples]]               # replaces the built-in five; the real lever on her voice
event = "source: git\napp: post-commit\ntitle: lighthouse\nbody: Fix flaky login test"
line = "A fix! Did that wobbly login test finally stop wiggling?"
emotion = "happy"

[media]
only = []                        # e.g. ["spotify"]
ignore = []                      # e.g. ["firefox"]

[notifications]
ignore_apps = ["Spotify"]
only_apps = []
min_urgency = "low"
body = "off"                     # off | react | glance: what she does with a message body (§4)
# body_apps = { Slack = "glance" } # per app, case-insensitive
max_body_chars = 1000
coalesce_s = 2.0

[speech]
enabled = false                  # true: Piper reads every line aloud (CPU)
voice = "en_GB-alba-medium"      # a name in voices_dir, or a path to an .onnx
speed = 1.0                      # 1.25 = a quarter faster
volume = 1.0
quiet_hours = ""                 # "22:00-08:00": bubble only, no sound

[learning]
log_outcomes = false             # true: keep routed sentences and their outcomes in <state>/outcomes.jsonl (§8c)
max_days = 30
max_records = 5000
undo_s = 10.0                    # the windows in which the next sentence says something about this one
rephrase_s = 10.0
silence_s = 30.0
rephrase_similarity = 0.8
idle_train = true                # §8d: a candidate head on its own when idle…
idle_minutes = 20.0              # …this long without a sentence…
min_new_labels = 10              # …and this many new labelled sentences
auto_switch = false              # true: a candidate that passes the held-out check is put in use at once
weekly_line = false              # true: once a week she says what she learned (never in quiet hours)
max_share = 0.25                 # the user's labels weigh at most this share of the data set's, per option

[runs]
events = true                    # each run's steps go to the bodies that ask (the widget's chip) and the Brain UI (§18)
supersede = true                 # a new sentence stops the one she is on (never a yes or no to her question); false: it waits
keep = 50                        # finished runs kept in memory for the Brain UI

[approvals]
change_s = 10.0                  # how long she waits for a yes to a `change` (the old [actions] confirm_s) (§19)
sends_s = 30.0                   # …to a call that sends something to someone
destructive_s = 30.0             # …to one that deletes or cannot be undone
grace_s = 10.0                   # while the user is still answering, at most this much longer
hold = ["sends", "destructive"]  # on her card, a yes to these tiers is a press-and-hold
# risk = { "spotify.remove_saved_tracks" = "destructive", "notes" = "read" }   # a tool's or a server's tier
```

`[thinker] stream = true` (the default) reads Ollama's reply as it is written, only to count tokens for the run's `token_rate` (§19); `false` asks for one reply at the end, as before.

`strawberry config` creates the file from a commented template (`strawberryd --init-config`) and opens it in `$EDITOR`; `strawberry restart` applies it; `strawberry config --init` only writes the template if missing and prints the path (the widget's *Settings file…* runs that). `STRAWBERRYD_PORT` still overrides the port for scripts. The widget's own preferences (skin, window position, …) are `~/.config/strawberry/widget.cfg` (§13).

**The user's sentences in the journal** (`[daemon] log_sentences`, 2026-09-25; `strawberry_crab/logtext.py`). A sentence the user says or types used to be logged verbatim in six places: the server (`widget typed: …`, at INFO), whisper (`voice: heard …`), the daemon's event line, the gate (`gate: 'skip this' -> …`), the reflexes and the thinker, and the tool call it filled in (`spotify.search({"query": "daft punk"})`). Every one of them now goes through `logtext.sentence()` (a tool call's arguments through `logtext.arguments()`): off, the default, the line carries `<sentence, 23 chars>` (and `"query": "<9 chars>"`; numbers and flags in the arguments stay); on, the sentence as it was, for tuning the gate from the journal. Her own lines are still logged, minus the sentence she is answering: the canned fallback for a voice event is "You said: …" and a model may quote the user, so while a sentence is handled (`Daemon.handle_voice`, a context variable) `logtext.line()` replaces it in her line with the same placeholder. The outcome logger never logged a sentence (§8c). Tool results are still logged (their first 160 characters), as are `/health`'s ledger, `voice.last_transcript` and the last action and thinker call, which are not logs (and `/health` refuses browsers, §2). `tests/test_log_sentences.py` sends a canary sentence typed, posted and heard by whisper, through the real gate, a reflex, the thinker and a tool call, with every logger at DEBUG: off, it is in no record; on, it is.

**The first-run privacy note** (`strawberry_crab/firstrun.py`). While `$XDG_STATE_HOME/strawberry/privacy-notice-shown` is missing, the daemon logs one `privacy:` line at start: what she reads from notifications under the current `body` / `body_apps`, the three modes, and the config file to change it in, then whether the sentences she hears are kept (`[learning] log_outcomes`, §8c) and in which file. The first widget whose hello is served (not refused for its version) gets a short version in her bubble (`talking`, happy, a wave) and the marker is written before it is sent, so it is said once per user. Deleting the marker brings it back. Tests start with the marker present (tests/conftest.py gives every test throwaway XDG dirs), and `scripts/check_phase1.sh` gives its daemon a state dir that has it, so the validators see only the performances they ask for.

## 16. Repository map

```
WIRING.md                this document
PROTOCOL.md              the bus protocol between the brain and its bodies (v1 as built, v2's runs as built, the rest of v2 proposed)
PACKAGING.md             the plan from a developer checkout to `uv tool install strawberry-crab`
ADAPTERS.md              adding an MCP server, and writing an adapter for one
README.md                for users: install, setup, configuration, privacy
CHANGELOG.md             what changed per version (by hand; the release notes are its section)
LICENSE, THIRD_PARTY.md  MIT; what we use from others, and the models and voices the user downloads
.github/workflows/       ci.yml (tests + build on push/PR), release.yml (v* tag -> GitHub release + PyPI, PACKAGING.md)
pyproject.toml, uv.lock  the one Python package, `strawberry` (hatchling; `uv build`, `uv tool install .`); groups dev (pytest, Pillow) and gpu (cuBLAS/cuDNN, also the `gpu` extra)
src/strawberry_crab/          the package: contract, events/reactor, brain, speech (Piper), voice (whisper), systemone (the gate), embedder (the gate's model in-process, ONNX; `--fetch`), gatehead (the gate's trained head, §8a), gateeval + gatecmd (`strawberry gate train|eval|use`), outcomes + labels + learnfit + learning + learncmd (the learning loop, `strawberry learning ...`, §8c, §8d), data/ (the gate's data set, held-out set and shipped head), tools (MCP client), actions (reflexes), thinker (Qwen tool loop), confirm (a spoken yes before a removal), adapters/ (what is particular to one server: spotify, web; ADAPTERS.md), ledger (short memory), outcomes (the learning loop's log, §8c), hub, server
                         + cli.py (`strawberry`), strawberryd.py (`strawberryd`), paths.py (XDG dirs, the checkout), firstrun.py (the one-time privacy note, §15), widgetbin.py (which widget runs; `widget --fetch`), bus.py (jeepney plumbing), wake.py (logind resume -> model warm-up, §4), winwake.py (the same on Windows: the suspend and resume notification), client.py (HTTP to the daemon), icons.py (PNG -> ARGB32, BGRA, .ico), tray.py (the StatusNotifierItem, §14), traymenu.py (the tray's menu and its rows' actions), supervisor.py (the tray's children), wintray.py (the Windows notification-area icon), wasapi.py (Windows audio through ctypes: process loopback, the audio sessions), winproc.py (Windows stop and restart events, the kill-on-close job, §2), winmic.py (the Windows microphone, §7), hotkey.py (the listen hotkey's syntax and its Windows setting, §7), startup.py (the Windows Startup shortcut, §13), media.py (which media controls this system has), mpris.py, smtc.py (Windows), adapters/,
                         setupcmd.py (`strawberry setup [--no-download]`: tiers by VRAM, config merge, pulls, the gate's model, voice, widget), configedit.py (config.toml edited as text: setup and the tray's Message bodies), doctor.py (`strawberry doctor [--talk]`; on Windows its own checks: notification access, media sessions, microphone, process loopback, the Startup shortcut, the tray)
src/strawberry_crab/doorways/ notify_watch.py, mpris_watch.py, beat_watch.py + beat_track.py with its captures beat_pipewire.py (Linux) and beat_loopback.py (Windows), smtc_watch.py and toast_watch.py (Windows), notifications.py (what both notification doorways share) (`strawberry-doorway <name>`, or python -m strawberry_crab.doorways.<name>; `for_system()` says which run where)
src/strawberry_crab/assets/icons/  the tray icon PNGs (package data), rendered by scripts/render_icons.py
src/strawberry_crab/runs.py  runs (§18): Run, RunBook, the event whitelist, `is_stop`; hub.py keeps a Body per socket (protocol v2)
src/strawberry_crab/approvals.py  approvals (§19): ApprovalBook, the digest, `needed`; confirm.py words the question and keeps the call
src/strawberry_crab/ui/  the Brain UI's page: index.html, app.js, style.css, icon.svg (package data, no build step; §17)
tests/                   the package's tests (`.venv/bin/python -m pytest -q`)
widget/                  Godot 4.7 desktop widget: widget.gd, ws_client.gd, bubble.gd, speech_player.gd, reactions.gd, dance_style.gd, gaze.gd, menu.gd, type_box.gd, step_chip.gd (§18), paths.gd (XDG, the CLI, the version), validate_*.gd
                         + strawberry_v2.glb, the shaders/controllers, export_presets.cfg (the Linux binary)
bin/strawberry           the checkout's shim: runs the CLI from .venv (created with `uv sync --inexact --group gpu` on first use)
scripts/check_phase1.sh  Phase 1 acceptance: unit tests + headless widget against a real daemon (WIDGET=<binary> for the export)
scripts/build_widget.sh  export the widget: dist/strawberry-widget-<version>-linux-x86_64 + .sha256 (§13)
scripts/check_reconnect.sh  restart (or SIGNAL=KILL) the daemon under a headless widget; it must reconnect
scripts/check_tray.sh    register the tray on the real session bus and read it back with busctl (§14)
scripts/gate_check.py    the gate over scripts/gate_phrases.json on the configured embedder (ONNX or Ollama); add sentences she misreads
scripts/sensitive_check.py  the notification-body filter over scripts/sensitive_phrases.json on the configured embedder (§4)
scripts/gate_heldout_check.py  the gate on its held-out set, nearest examples and head side by side (§8a); a measurement, not a contract
scripts/gate_data.py     regenerates the gate's data set and held-out set with the local Qwen; scripts/gate_data/ keeps the raw files and the hand review
scripts/beat_eval.py     the beat tracker offline: generate the synthetic set, score tempo/octave/lock/jitter/phase/CPU (§4c)
model/                   the asset: GLB, editable Blender scene, procedural build script
```

**Run it:** `bin/strawberry` in a checkout, or `strawberry` once installed. Then, from anywhere:

```bash
curl -s -H 'Content-Type: application/json' localhost:8770/perform \
  -d '{"state":"talking","anim":"alert_snap","text":"Did someone say my name?","emotion":"alert"}'
curl -s -H 'Content-Type: application/json' localhost:8770/event \
  -d '{"source":"git","title":"strawberry","body":"Add websocket"}'
```

Or press play in any media player: the MPRIS watcher (§4b) does the rest.

---

## 17. The Brain UI — `strawberry/brainui.py`, `strawberry/ui/`

A local web page, served by the daemon, that shows what the router decides and what the learning loop learns (§8a–8d), and lets the user act on it. `strawberry ui` opens it. It calls `learning.py` and reads the daemon's parts; it decides nothing itself.

**What it shows**, in six sections:

1. *Learning*: the head in use, the candidate, the labelled examples, the last run and the trainer, with **Train now** (`IdleTrainer.train_now`: the idle run's own path, the gate's embedder, stopped when the user speaks, the child process, without waiting for quiet) and **Roll back**. The candidate against the head it was compared with: fields, strict sentences, Finnish fields, privacy readings, wrong reflexes and AUROC on the held-out set, the per-field scores, how many sentences it learned and how many are new to it, with **Accept** and **Reject**. The learned examples with their labels, avoids, conflicts, signals and review state, each with **Approve** and **Reject** (`Learning.review`; reject deletes the sentence for good). The outcome log's last 60 records and its counts per signal. Every head with its status, held-out score, the score of the head it beat and **Use**, and the history of switches. The week's report.
2. *Router live*: each sentence as it is handled: kind and topic with confidence, the decision, the tool and its confidence, what handled it (the reflex, the thinker and its tool calls, no_catalogue, chat), the gate's ms and the whole sentence's ms. `routefeed.py` keeps the last 100 in memory (`Daemon.feed`, filled in `handle_voice` beside the outcome log) and the page gets them over a stream.
3. *Data and scores*: the training and held-out set sizes, the learned examples, what review and the privacy check removed, what the last run trained on and left out, and the held-out score of every head the loop built, as an SVG chart drawn by the page (no library) with the shipped head as a line.
4. *Settings and privacy*: outcome logging, `log_sentences` and the notification body modes, each with what it means now and the line in `config.toml` that changes it; the learning settings; the whole effective config (env values and anything named like a secret shown as `(set)`). Read-only in this version. **Forget** (`Learning.forget`, optionally with every head) is here, behind a second click.
5. *Runs* (§18): each run, newest first, with its steps as a timeline (routing and its path, thinking and the model, each tool started and completed with its duration and code, speaking, the end and why), the tools by name, the outcome and the duration, and **Cancel** on a run that is going on (the widget's ✕ by another way: `RunBook.cancel(…, "stopped")`). The stream's `run` events fill it as they happen (a `token_rate` shows beside the run's state, not as a step); `GET /ui/api/runs` gives the last `[runs] keep`. Names and timings only: what the page gets is what `runs.emit` let through. Above it, while she waits for a yes (§19), **Waiting for a yes**: the card's line, the tier, the tool, the time left when loaded, and **Yes** and **No** (the fallback for her card and her question); below it, **Approvals**: each one asked about, with its id, run, tool, tier, outcome, who answered and how long it waited, never the call's arguments.
6. *System*: the version and uptime, where the gate embeds (ONNX or the Ollama fallback, and why), the scorer and the head in use with its thresholds, the reaction model, the thinker, speech, voice, the MCP servers and the trainer. `/health` has no VRAM or CPU figures, so neither does the page.

**Routes.**

| route | what | who |
|---|---|---|
| `POST /ui-token` | a one-time login token: `{"token", "expires_s", "path"}` | `strawberry ui` (not a browser route: an `Origin` is refused, a non-loopback client is refused) |
| `GET /ui/login?token=…` | swaps the token for a session cookie, then 303 to `/ui` | the browser, once |
| `GET /ui` | the page, with the session's CSRF token in `<meta name="csrf">` | the browser |
| `GET /ui/app.js`, `style.css`, `icon.svg` | the page's files (no data in them; no session needed, so the sign-in page is styled) | the browser |
| `GET /ui/api/learning`, `routes`, `runs`, `data`, `settings`, `system` | JSON for each section | the page |
| `GET /ui/api/events` | server-sent events: `route` (one entry, with its `run_id`), `run` (one run event, §18, with the run's `source`), `learning` (a file of the loop changed; the page asks again), a keep-alive comment every 15 s | the page |
| `POST /ui/api/cancel` (`{run_id}`) | stops that run if it is going on (`{"cancelled": id}`), else 409 | the page |
| `POST /ui/api/approval` (`{approval_id, answer}`) | a yes or no to the open approval (`{"answered": id, "answer": …}`); 409 with `reason` (`not_open`, `resolved`) when it is not open; `GET /ui/api/runs` carries `approvals` (`open`, with its line, and `history`) | the page |
| `POST /ui/api/accept`, `reject` (`{version?}`), `rollback`, `use` (`{version}`), `review` (`{key, verdict}`), `train`, `forget` (`{confirm: "forget", everything}`) | `Learning`'s own calls; a refusal from it ("no candidate is waiting", "a training run is already going") is a 409 with the reason | the page |

**The security model.** Every other route refuses any request with an `Origin` header (§2), so no web page the user visits can make her talk, read `/health` or open `/ws`. `/ui` is the one place a browser is let in, on these terms (`brainui.checked`, one decorator on every UI handler; `server.local_only` lets a handler through only when it carries the decorator's mark, so a new route cannot open itself by accident, and an unmatched path such as `/ui/../health` still meets the Origin rule):

- *This machine only*: the client must be loopback (as for `/probe`), and the `Host` header must be `127.0.0.1:<port>` or `localhost:<port>`, the port the request actually arrived on. A DNS-rebinding page has its own name in `Host` and is refused, on every `/ui` route.
- *A one-time token*: 256 bits (`secrets.token_urlsafe(32)`), single-use, good for 60 s, at most 8 waiting. `strawberry ui` gets it with a plain local POST and opens `http://127.0.0.1:<port>/ui/login?token=…` (`xdg-open` on Linux with a display; Python's `webbrowser` on Windows and macOS; otherwise, or with `--no-browser`, it prints the link). The login swaps it for a session and redirects to `/ui`, so the token leaves the address bar; a used or expired token gets a page saying to run `strawberry ui` again.
- *The session*: another 256 bits in a cookie, `HttpOnly; SameSite=Strict; Path=/ui`, kept only in the daemon's memory. It ends after 12 hours without a request, after 24 hours whatever happens, and when the daemon restarts. Tokens and session ids are compared in constant time (`hmac.compare_digest`).
- *The API*: a session, and the session's CSRF token in `X-Strawberry-CSRF`. A page of another origin cannot add that header without a CORS preflight, which nobody answers. A POST must also carry `Origin` equal to the daemon's own origin (`http://<the Host above>`) and `Content-Type: application/json`. Browsers send no `Origin` on a same-origin GET, so a GET needs `Origin` absent or exact and `Sec-Fetch-Site`, when sent, `same-origin`. SameSite=Strict alone is not enough: a page on another port of `127.0.0.1` is the same *site*, and its requests carry the cookie; the Origin check and the header stop it.
- *Headers on every `/ui` response*: `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Cross-Origin-Opener-Policy` and `Cross-Origin-Resource-Policy: same-origin`, and `Cache-Control: no-store` on the API and the page (it holds the CSRF token).
- *Live updates without the widget's socket*: `/ws` stays closed to browsers; the page reads `/ui/api/events` with `fetch()` (EventSource cannot send the CSRF header), under the same checks, read-only, at most 8 streams; a stream ends when its session does or the daemon stops.
- *Offline*: the page loads nothing from anywhere else (no CDN, no font, no library); the CSP would refuse it anyway.

**Privacy.** Sentences reach the page only while `[learning] log_outcomes` is on, and only through the session's API: the examples (`Example.summary(text=True)`), the outcome records and the live feed carry their text then, and none of them do while it is off (the routes still show, without words). The live feed keeps a sentence only where the outcome log would (`outcomes.private`: not private, not `kind=other`), and the outcome records shown are checked against `privacy.pattern` again. The page puts every string it gets into the document as text (`textContent`, `Node.append`); nothing is parsed as markup, so a sentence like `<img src=x onerror=alert(1)>` shows as those characters (and the CSP would block the handler regardless). The access log leaves out the query string on `/ui` (the login token) and does not log the page's successful API reads; the UI's own log lines say what was done (`ui: candidate accepted`, `ui: an example rejected`), never a sentence, key, token or cookie. Nothing leaves the machine.

**What it does not protect against.** Any process the user (or another user on this machine) runs could already call the daemon's non-browser routes, `/health` and `/ui-token` included: the token keeps browsers out, not local programs. That is the model the daemon has always had (§2).

**Measured end to end** (2026-10-08, a throwaway daemon on port 8796 with temp dirs, the real ONNX model, outcome logging on, 11 synthetic outcome records and ten typed sentences, headless Chrome over the DevTools protocol): `strawberry ui --no-browser` printed the link; the login left `/ui#learning` in the address bar and an empty `document.cookie`; the canary sentence showed as text with no `img` element and no dialog; **Train now** built a candidate in the daemon's child process, which the held-out gate rejected (wrong reflexes 4 > 3); two pause examples rejected in the page and a second run gave a passing candidate (95.0% against 94.9%); **Accept** moved `/health.learning.head` and the gate's head to it at once (`follow_pointer` after the switch, not at the next 5 s tick) and **Roll back** moved both back to the shipped head; a typed sentence appeared in Router live within a second; after a daemon restart the old session got the sign-in page; **Forget** left 0 examples. The daemon's log had no sentence, token or cookie in it.

Tests: `tests/test_brainui.py` (the token's single use and expiry, the session's idle and total limits and its end with the daemon, no session, a wrong Host, Origin or CSRF header, JSON only, every other route refusing any Origin including the UI's own, the headers, the script's text-only rule, no sentence with logging off and the sentences with it on, the live feed's privacy, nothing in a log line, each action reaching `Learning`, a real accept and rollback through the API, the event stream, `strawberry ui`).

---

## 18. Runs — `strawberry/runs.py` (brain step 6, stage 1)

Every input she handles is a **run**: a sentence the user says or types, or a notification she
reacts to. A run has an id (`r-<n>`), a `source` (`voice`, `typed`, `notification`; `job` is
reserved for scheduled work), a sequence of events and one end. It goes

```
routing → thinking ⇄ tool → (awaiting_approval, stage 2) → speaking → completed | failed | cancelled
```

and each step is an event (PROTOCOL.md Part 1b has every field). The bodies that ask for them get
them over `/ws` (the widget's step chip, §13); the Brain UI shows them (§17).

**Where the events come from.** `Daemon.handle_voice` starts the run; `_routing` sends `routing`
once the path is known (a reflex as it starts, `escalate` before the thinker, `fixed` for the
add-on line, `chat` when the thinker is off). `Actor.act` tells its `on_call` about the reflex's
call (`tool.started` / `tool.completed`, named by `Actor.label`). `Thinker.run(run=…)` sends
`thinking`, and `Thinker._call` puts `tool.started` / `tool.completed` around each call; a refused
call is one `tool.completed` with `error: "refused"`. `Daemon.perform` sends `speaking` for the
line that answers the run going on in its task (`runs.active`, a context variable set in the run's
own task), and first waits up to 0.5 s for the run's earlier events to go out, so a body sees them
in order with the line. `RunBook.finish` sends the terminal event, once, from the `finally` of
`handle_voice` (and of `handle_notification`), or, for a run that waits for a yes, from the task
that waits (§19).

**What may go out.** `RunBook.emit` keeps, per event type, only the fields PROTOCOL §11 lists, as
plain numbers, booleans and short strings, and codes only from their lists. A tool's arguments and
result, the user's sentence and her line cannot reach the bus, the Brain UI or the run's log line
through it (`tests/test_runs.py` puts canaries in all four with every logger at DEBUG). A made-up
tool name goes out as `unknown`. The labels the chip shows are written by code: an adapter's
`labels` (the web adapter's "searching the web…"), else its `title` (or the server's name) and the
tool's name. Sinks are queues fed with `put_nowait`: a slow body or page misses events rather
than slowing her down; a run carries at most 200.

**One foreground run.** A typed or spoken sentence is the foreground run, and there is one at a
time: a new sentence waits for the run before it to end. Before it waits, it stops that run
(`superseded`) when `[runs] supersede` is on, except when it is a yes or no to her question
(confirm.py), which is the answer that run was after. A whole sentence of "stop", "cancel that",
"never mind" (`runs.is_stop`, word lists like `confirm.answer`) while a run is busy stops every
foreground run going on (`stopped`), never reaches the gate and is answered "Okay, stopped.". The
same "stop" with nothing going on is an ordinary sentence (the pause reflex, usually). A
notification's run is never the foreground one: a sentence does not stop it.

**Cancel.** From the widget's ✕ (`run.cancel`, only from a v2 body that declared it, only for the
foreground run going on), the Brain UI's Cancel, "stop", supersede, and the daemon's shutdown
(`Daemon.close`: `shutdown`). `RunBook.cancel` cancels the run's own task (`Daemon._in_run` runs
the sentence in a task of its own, so the request handler or the typed sentence's background task
is not the one cancelled). A call already in flight decides what happens next:

- one that only reads (a web search or page, a Spotify search; `Toolbox.reads`: an adapter that
  only looks things up, its `reads`, or MCP's `readOnlyHint` on the tool) is dropped at once. The
  server finishes it on its own; its queue is serial, so the next call to that server waits for it;
- one that may change something is shielded (`asyncio.shield`, in `Thinker._call` and
  `Actor.act`): it finishes, bounded by its timeout, no further round is asked of the model, and
  she says "Stopped, but <label> had already gone through." before `run.cancelled`.

Otherwise a cancelled run goes back to her resting pose with nothing said (a superseded one leaves
that to the sentence after it). The cover lines ("On it.", "Still on it.") are not said once a stop
is under way. `CancelledError` is caught nowhere on the way: the thinker catches only timeouts and
its own errors, and the tool client only `ToolError`.

**Built in stage 2 (§19):** approvals bound to the exact call, the `listening` and `token_rate`
gauges, the `didnt_catch` end. **Not built yet (later stages):** several foreground runs, scheduled
jobs.

Measured end to end (2026-10-09, a throwaway daemon on port 8797 with temp XDG dirs, the gate on
Ollama's embeddinggemma, the real thinker, the fake Spotify (`python -m tests.fake_spotify`) and
the echo server over stdio, a v2 ws client): "next song" was `routing (reflex, skip) →
tool.started spotify.skip → tool.completed → speaking → run.completed` in 0.74 s; "play some jazz"
was `routing (escalate) → thinking → spotify.search → spotify.play → speaking → run.completed` in
6.4 s; a slow read cancelled from the ws client ended with `run.cancelled` 1 s after the cancel; a
4 s change call cancelled a second in finished first and she said "Stopped, but Echo slow change
had already gone through."; the Brain UI in headless Chrome cancelled a slow run with its button
(`run.cancelled`, reason `stopped`); the widget under Xvfb showed the chip with "Echo: slow" and
its ✕, hid it when the run ended, and stopped a second run with its own ✕.

Tests: `tests/test_runs.py` (the order of events for a reflex, a thinker run with tools, a refused
call, a failure, a timeout and a notification; one terminal event per run and `seq` without gaps;
cancel mid-thought, a read dropped at once, a change finishing first and her line about it for the
thinker and a reflex; "stop"; supersede and supersede off; an answer never superseding; shutdown;
the privacy canaries; the v1 golden bytes; welcome and the clock in pong; who may cancel; the Brain
UI's list, stream and Cancel), `tests/test_tools.py` (`readOnlyHint`, labels),
`widget/validate_widget.gd` §18.


---

## 19. Approvals — `strawberry/approvals.py` (brain step 6, stage 2)

Some calls wait for the user's yes, bound to that exact call. confirm.py (§8b) words the question
and keeps the call; `approvals.py` keeps the one open question, who may answer it and how; the run
that asked waits for it (§18).

**What waits for a yes** (`approvals.needed`, asked in `Thinker._run` through
`Toolbox.needs_approval`): a tool on its server's `confirm` list, and every call of the `sends` or
`destructive` tier. Stage 3 adds every call that is not `read` once text from strangers is in the
conversation: the hook is `needed(..., foreign=True)`, and nothing passes it yet. A reflex is a
fixed call written in an adapter, never a `sends` or `destructive` one, and never waits.

**The tiers** (`Server.risk`): `read`, `change`, `sends` (something reaches other people or leaves
for someone: a message, an email), `destructive` (deletes, or cannot be undone). `[approvals] risk`
decides first, by `"server.tool"` or a whole `"server"`, taken as written; else the adapter's
(`Adapter.risk`: its `risks`, and `read` for its `reads` and every tool of a look-up-only server);
else `change`. A tool the server marks `destructiveHint` is then raised to `destructive`. An
annotation never lowers a tier: `readOnlyHint` makes no tool `read` here (a server cannot talk its
way out of a yes), though a cancel still drops such a call at once (§18). MCP's default of
`destructiveHint: true` for any tool that is not read-only is not applied: only a server that says
it raises a tier. Spotify's two removals are `change` (a track can be added back): asked about
through `confirm`, with the short wait.

**The flow.** The thinker stops at the call and returns her question (`confirm.hold` gives the
`Held` call its tier and the card's line, `Adapter.describe`, by default the question without "Say
yes."). `Daemon._handle_voice` says the question (`speaking`), then `Daemon.hold` opens the
approval on the run (`ApprovalBook.request`: `approval.request`, the run `awaiting_approval`) and
hands the run to a task of its own (`Daemon._await_answer`), so the sentence's handler returns at
once (the voice session ends, the hotkey is free for the answer). That task becomes `run.task`, the
one `RunBook.cancel` stops, and it ends the run:

| outcome | what happens |
|---|---|
| `yes` | a last look first: if the run was stopped, superseded or ended after the yes, nothing starts (below). Then the stored call is copied afresh and the copy checked against the digest (`confirm.run`; a mismatch is never made: "That changed while I waited, so I've left it.", `run.failed`), made between `tool.started` and `tool.completed`, shielded like any change (§18), and the adapter's `done` writes the fact with the quip after it (`Daemon.confirmed`); no model is asked again |
| `no` | "Okay, I've left it." |
| `timeout` | "No answer, so I've left it.", also in the ledger as `(no answer)` |
| `superseded` | nothing; the newer sentence's line starts "I've left that, then." (`run.cancelled`, `superseded`) |
| `cancelled` | the ✕, the Brain UI's Cancel or the daemon stopping: nothing is made (`run.cancelled`, `stopped` or `shutdown`) |

**A stop between the yes and the call.** A ✕, a "stop" or a newer sentence can land after the yes
and before the call starts. Then nothing is made: the approval keeps `yes` (it was the user's
answer) with `made: false` in the Brain UI's history; on the bus the `yes` is followed by no
`tool.started` and ends `run.cancelled`; she says "I stopped before doing it, so nothing changed."
(for a newer sentence, ahead of its own line, and a "stop" says nothing over it), and the ledger
keeps that too. Once the call has started it is let finish, as any change (§18).

A late yes or no (within a minute of a no or a timeout) still gets the fixed line, not the thinker.

**The call is bound.** `ApprovalBook.request` stores a deep copy of the call and its digest
(sha256 of the server, the tool and the arguments, canonical JSON). Just before the call that copy
is copied again, the new copy checked against the digest, and only it goes to the server and to the
adapter's `done` (`confirm.run`), so neither a server nor an adapter can change the stored call. One
approval is open at a time; a newer one supersedes it. Ids are `a-<boot>-<n>`, `<boot>` 6 random hex
digits per start, so a card left over from before a restart cannot answer a new question. There is
no "always allow".

**Timeouts.** No answer is a no: `[approvals] change_s` (10 s; also for a listed `read`), `sends_s`
and `destructive_s` (30 s). The clock starts once she has asked. If the user is speaking at the
deadline (a voice capture or its transcription: the answer on its way), it waits once more, for at
most `[approvals] grace_s` (10 s), then it is `timeout` whatever the microphone does: a stuck
listener or a noisy room cannot hold a question open.

**Who answers** (`ApprovalBook.answer`; the first answer wins, later ones are refused with
`resolved`):

- the user's own sentence, said or typed (`Daemon.handle_voice`): a yes or no read by
  `confirm.answer` while an approval is open is **no run of its own**: it answers the run that asked,
  which goes on, and the sentence returns that run's last line. Any other sentence supersedes the
  question, whatever `[runs] supersede` says (nothing is under way), and is handled as usual. Any
  tier; today's wording throughout;
- a v2 body (her card in the widget): `approval.answer {approval_id, answer, hold}` (PROTOCOL §13b),
  only from a body whose hello declared `approvals: true` and `sends.approval`, only for the open id;
  for a tier in `[approvals] hold` (`sends`, `destructive`) a yes must say `hold: true` (the body
  times the ~1 s press; the brain checks the flag). Anything else is `input.refused` (`not_declared`,
  `not_open`, `resolved`, `bad_answer`, `hold_required`); a v1 body's is ignored. A body can only
  answer: no message asks for a call;
- the Brain UI (§17): `POST /ui/api/approval`, with the session and the CSRF header.

Notifications, media and git events, tool results and web pages never answer: they have no path to
any of the three.

**What goes out.** `approval.request` (id, tier, the card's line, `timeout_s`, `expires_t`, `hold`)
and `approval.resolved` (id, outcome, and `by` for a yes or no) are run events, whitelisted like the
others (`runs.FIELDS`; the line is the one free-text field, cut to 160 characters, control
characters removed). Only bodies with `capabilities.approvals` get them, and one that says hello
while an approval is open gets it right after `welcome`. The call's arguments as they came, its
result and the user's sentence never reach the bus, the Brain UI's approvals or a log line of
`approvals.py` (it names the tool and the tier). The card's line is the exception by design: like
her spoken question, which goes out as her line anyway, it can carry names an adapter took from the
call (a song, a playlist, a recipient's display name). For `sends` and `destructive` tools it must
not carry free text from the arguments, such as a message body (ADAPTERS.md, `Adapter.describe`).
`/health.approvals` has the open id, its tier and counts per outcome; `/health.confirm` the held
tool and the wait.

**What this protects against, and what it does not.** Approvals stop the model's mistakes and
misheard speech: a call the user did not mean is never made without their yes, and only the call
asked about is made. They do not stop local code running with the user's privileges. Such a process
can already ask for a call (a typed `heard` on the websocket, or `POST /event`) and then answer it
with a typed "yes", and any body can claim `hold: true`; the bus has no secret today (§2: the Origin
rule keeps browsers out, not local programs). A per-install secret for the bus is planned for step
6, stage 3: a 0600 file the widget reads, required for the `approvals` and `sends` capabilities, for
`heard`, `/event` and `/ui-token`. It is not built yet.

**Open items** (known, not fixed in stage 2):

- MCP tools whose adapter has no `log_result` still have their result's first 160 characters, and
  their arguments as `logtext.arguments` shows them, in the log (stage 3 closes this);
- reflexes do not consult the approval tiers (they are fixed calls in adapters; a config that raises
  a reflex's tool to `sends` or `destructive` does not make the reflex ask);
- a whole-server `[approvals] risk` entry (`"notes" = "change"`) is taken as written and so lowers a
  tool the server marks `destructiveHint`; only the per-tool entry should be able to.

**The gauges and `didnt_catch`.** `listening` (a voice capture `started` and `ended`, with how long it
recorded and whether it heard speech; no run, no audio, no words) and `token_rate` (tokens a second
while the thinker writes, at most 4 a second per run, and once per reply with Ollama's own count;
`[thinker] stream` reads the reply as it is written for it, the text going nowhere else) are sent
without `seq` and are not kept on the run (`RunBook.signal`, `RunBook.rate`; PROTOCOL §11d). A voice
capture with nothing understood in it is a run of its own that says "Sorry, I didn't catch that." and
ends `run.cancelled` with the reason `didnt_catch` (`Daemon.didnt_catch`); it is never the foreground
run, so it stops nothing and leaves an open question waiting.

Measured end to end (2026-10-09, a throwaway daemon on port 8797 with temp XDG dirs, the gate on
Ollama's embeddinggemma, the real thinker (`qwen3.8:27b`), the fake Spotify (`python -m
tests.fake_spotify`, which now lists `find_playlist` and `remove_from_playlist`) and the echo server
(`shred`, marked `destructiveHint`), a scripted v2 body that declared `approvals` and
`sends.approval`, `[approvals] change_s = 8`): "take this off my running playlist" asked "Remove
'Feeling Good' from Running? Say yes." after 3.9 s, `approval.request` (`change`, 8 s, no hold)
followed the question's `speaking`, and a typed "yes, go ahead" made the stored call (`tool.started`
/ `tool.completed`, then "Removed Feeling Good by Nina Simone from Running.", `run.completed`); the
same answered on the card a second later did the same with `by: body`, and a second answer to it
got `input.refused` (`resolved`); left alone, it resolved `timeout` after 8.0 s and she said "No
answer, so I've left it."; "shred my note called groceries" asked with `destructive`, 30 s and
`hold: true`, a tap got `hold_required` and a held yes made the call. Each Qwen round sent one
`token_rate` (36-37 tokens a second); a spoken reply streamed four, about a quarter of a second
apart. In headless Chrome the Brain UI's Runs section showed "Waiting for a yes" with the card's
line, its Yes answered it (`by: ui`) and the Approvals history listed it. No argument was in the
daemon's log.

Tests: `tests/test_approvals.py` (the digest and the stored copy; the tiers from the config, the
adapter and the annotations, which only raise; the timeouts per tier and the clock while the user
speaks; first answer wins; the hold flag; refusals from undeclared and v1 bodies, wrong and stale
ids; the ✕, supersede (also with it off), "stop" as a no, shutdown, a stop after the yes; the card
after a reconnect; no argument on the bus, in the Brain UI or the log; the Brain UI's Yes and No; the
gauges, the stream and `didnt_catch`), `tests/test_confirm.py` (the spoken flow, unchanged),
`tests/test_runs.py` (an answer never superseding), `tests/test_tools.py` (`destructiveHint`).
