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

**Godot's behaviour on receipt:** apply `state` (crossfade to its loop); if `anim` present, fire it as a one-shot and resume the state's loop when it ends; if `text`, show the bubble; if `audio`, play it through the analysed bus (§6). When speech/bubble finishes and the state was `talking`, **auto-return to her resting state**: `dancing` if she was dancing before the line, otherwise `idle`. The daemon doesn't send a follow-up. `listening`/`thinking` are pipeline transients and are not remembered; `dancing` persists until the next `idle`.

**Transport:** the **daemon is the websocket server**, Godot is a **client** that connects out and auto-reconnects with backoff (1 s doubling to 8 s). localhost only. One port carries both HTTP and the websocket (`/ws`).

**Liveness, both ends.** On shutdown the daemon closes every widget socket with `1001 going away` and exits within 2 s, so a restart is noticed immediately (aiohttp would otherwise hold the handler for up to 60 s and the widget would sit on a dead socket). The widget also sends `{"type":"ping"}` every 5 s, gets `{"type":"pong"}`, and drops and reconnects after 12 s of silence, covering a half-open socket after sleep/wake. `scripts/check_reconnect.sh` (and `SIGNAL=KILL …`) proves both paths: drop noticed at 0.0 s, reconnected in 2.6 s.

**Hello and the version handshake.** On connecting the widget sends `{"type": "hello", "client": "strawberry-widget", "version": …, "godot": …}`. `version` is `application/config/version`, which `scripts/build_widget.sh` stamps into the exported binary from `pyproject.toml`; a source run has none and says `dev`. The daemon compares it with its own package version (`server.version_verdict`): `dev` is always served; the same major with another minor or patch is served with a warning in the log; another major is **refused**: an error line (`refusing widget 1.0.0: its major version differs from strawberryd 0.1.0 …`) and the socket closed with code `4001` and the reason `widget X, daemon Y`. The widget shows that reason once in her bubble and retries every 60 s instead of 1–8 s, in case the daemon is upgraded underneath it. A hello without a readable version is served with a warning. `/health` lists `widget_versions` (one per open socket that said hello) and `version` (the daemon's).

Source of truth: `src/strawberry_crab/contract.py` and the constants at the top of `widget/widget.gd`.

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

**Local tools only.** Requests carrying a browser `Origin` header are refused (403) on every route including `/ws`, and POSTs must be `Content-Type: application/json` (415 otherwise). Godot, curl, and the doorways never send `Origin`; browsers always do. Without this a web page could make her talk, or open `/ws` and read notification text as it goes past.

**Shutdown.** SIGTERM runs aiohttp's cleanup: widgets are closed with GOING_AWAY, then `Daemon.close()` closes every part (each one even if another fails). Stopping the tray unit signals its whole cgroup and the tray also terminates its children, so the daemon sees SIGTERM twice; the first shutdown hook swaps in a handler that ignores the second, which would otherwise cancel the cleanup halfway and leave the Ollama sessions to the garbage collector ("Unclosed client session" in the journal). A SIGTERM while the models are still loading closes what was opened, too.

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

`off` is enforced twice: the watcher does not put the body in the POST (it never crosses even localhost HTTP), and the daemon drops a body that arrives anyway (an old watcher, a `curl`). A burst's body is the senders' titles, not message text, so it stays in every mode. Glance is `Daemon.report()`'s shape: the fact first, then the quip, and the quip's prompt never contains the body. The gist is refused (and she reacts instead) if it carries any digit, link or address; a `react` line that carries a link, a number from the body, or four body words in a row is replaced by the canned `App: Sender`. The old `include_body = true|false` still reads, as `react|off`, with one deprecation warning.

**The sensitive filter, fail closed** (`strawberry/privacy.py`). Before any body reaches Gemma, in `react` or `glance`, the daemon (it owns the gate) runs two checks; either one says yes and the body is dropped and she says only `"<app> sent something private."`, whatever the mode:

1. **Patterns**, plain code, on the title and the body: a 4–8 digit run (or `G-123456`, `123-456`) within 60 characters of *code, OTP, PIN, passcode, verification, one-time, 2FA, MFA, security/login/sign-in/confirmation code*; a body that is only a code; reset and magic-login wording or links (`token=`, `/reset`, `verify` in a URL); "do not share this code", "code expires". PR numbers (`#4821`), times, decimals and a number with no code word near it pass. The title is checked in `off` too: some apps put the code in the summary.
2. **`IS_SENSITIVE`**, a Noul on the gate's embedder (§8a; `systemone.py`): yes-examples are verification codes, password changed/reset, new sign-in from a device, confirm this login, bank and card transactions, account locked or security notices; no-examples are chat, work messages, CI results, calendar reminders, updates, music, battery. It reads `"<title>: <body>"`; p(yes) ≥ 0.5 drops. A separate question on the same embedder, embedded at start with the routing examples; it does not touch the voice questions (`gate_check.py` still 71/71).

**Fail closed**: a gate that is disabled, still warming, or erroring counts as a yes. So `react` and `glance` need `[gate]`; without it every body is "something private". Cost, measured 2026-09-22: one embedding call per body, **151 ms median, 182 ms max** (`scripts/sensitive_check.py`, warm); on the live daemon 141–174 ms. A glance is one more Gemma call (~0.6 s for the gist); end to end ~0.8 s for `react`, ~1.5 s for `glance`, 0 ms extra for `off`. Once, the first two calls after the gate's start hit the 2 s timeout (3.8 s; the GPU was busy): those bodies were dropped as private, as designed, and the gate reloaded in the background.

`scripts/sensitive_check.py` is the contract, like `gate_check.py`: it builds the gate the way the daemon does and runs the whole filter over `scripts/sensitive_phrases.json` (38 notifications, 20 sensitive, 18 not, including "meeting at 1400", "PR #4821 merged", "3 tests failed"); it prints the gate-alone tally too and exits 1 on a miss. 38/38, the gate alone 38/38. A notification she misjudges goes in the file, and the fix goes in `IS_SENSITIVE` or the patterns.

**No message text in logs, anywhere.** `client.py` logs an event's source, app, keys and `body_len`; the watcher logs app, title and `body_len`; the daemon logs the privacy decision (`private, pattern: one-time code`, `body read, mode glance, clear 0.02 (149 ms)`) and lengths; the gate logs p(yes) at DEBUG, never the text; the prompt is never logged. Her own generated line is logged, as for every event. `tests/test_privacy.py` runs a canary body through watcher → HTTP → daemon → gate → Gemma (fake) with every logger at DEBUG and fails if the canary is in any record.

## 4b. Doorway: media (MPRIS) — `strawberry/doorways/mpris_watch.py`

Any player that speaks MPRIS (Spotify, VLC, Rhythmbox, browser tabs with media) registers as `org.mpris.MediaPlayer2.<name>` on the session bus. The watcher follows **all of them at once** with jeepney, no extra tools, and reacts to the union:

| Change | Daemon call |
|---|---|
| any player → Playing | `POST /perform {"state":"dancing"}` |
| nothing playing / player quits | `POST /perform {"state":"idle"}` |
| a playing player changes track | `POST /event {"source":"media","app":"<player Identity>","title":"Artist — Title"}` |

Two subscriptions carry it: `PropertiesChanged` on `org.mpris.MediaPlayer2.Player` (matched by path, `/org/mpris/MediaPlayer2`) for what a known player is doing, and `NameOwnerChanged` with `arg0namespace='org.mpris.MediaPlayer2'` for players appearing and disappearing — a player is tracked by its **unique owner name** (`:1.494`), which is what a signal's sender field carries. Every value arrives as a variant, `Metadata` as a variant holding a whole `a{sv}`, so `bus.plain()` unwraps them before anything reads `xesam:title`.

Changes are debounced 400 ms (players fire several property updates per track), a track is announced once per player, and un-pausing the same song is not an announcement. A dance/idle post the daemon could not take (it restarts at the same moment as the watcher under the tray) is retried every 2 s until it lands; track announcements are not. The daemon in turn remembers her resting state (`idle`|`dancing`, shown in `/health`) and sends it to any widget the moment it connects, so a relaunched widget does not stand still while the music plays. `--only spotify,vlc` or `--ignore firefox` narrow it. `strawberry daemon` (or the tray) starts the watcher next to the daemon. This is why the widget returns to `dancing` rather than `idle` after a line (§1): the music context outlives the reaction.

---

## 4c. Doorway: the beat — `strawberry/doorways/beat_watch.py` + `beat_track.py`

MPRIS says *that* music plays; this says *how it goes*. Like the other two it is a package module on the same interpreter (`strawberry-doorway beat_watch`; numpy is a normal dependency). The watcher captures the player's own PipeWire output stream (`pw-record --target <node>`: never the microphone, never the whole mixer, so her voice, calls and system sounds stay out) and feeds it to a pure-numpy beat tracker: spectral-flux onset envelope at ~43 frames/s, autocorrelation over the last 8 s for the period (60–190 BPM, mild prior around 120), a comb filter over the last 4 s for the phase, with kick-band onsets leading the phase search so she lands on the kick rather than the hi-hats. Every 2 s it posts to `POST /tempo`:

```json
{"bpm": 128.4, "period_s": 0.467, "confidence": 0.71, "next_beat": 1789935826.592,
 "evenness": 0.62, "low_ratio": 0.55, "density": 4.1, "loudness_db": -18.0}
```

or `{"silent": true}`. `next_beat` is wall-clock, so the widget computes the beat phase itself every frame. `evenness` (autocorrelation at 1, 2 and 4 periods: four-on-the-floor scores high), `low_ratio` (energy below 150 Hz), `density` (onsets/s) and loudness are the style features. The daemon validates ranges, forwards `{"tempo": {...}}` to the widgets, shows the fresh estimate in `/health`, and hands it to a widget that connects while it is fresh (6 s). Stream discovery is automatic (a running `Stream/Output/Audio` node from a known player, else any running stream that is not ours) or pinned with `[beat].target`. Only a *running* stream is ever targeted, and after the first second of data the watcher reads the graph (`pw-dump` links into its own `strawberry-beat` node) to confirm the bytes come from that stream: Spotify keeps a second, idle stream node, and when the watcher once targeted it at a track change PipeWire quietly linked the capture to the default sink's monitor instead. From then on the beat followed the headset, went dead across an A2DP→handsfree profile switch, and came back as 8 kHz telephone audio (2026-09-22). A capture fed by an `Audio/Sink` node is dropped and the next strategy tried; the explicit `sink-monitor` strategy is the last resort and says so in the log.

**Dance styles (`widget/dance_style.gd`).** While she is dancing with a fresh estimate the node runs `dance_loop` at the music's tempo (one leg lift per beat: the clip's natural rate is 119 BPM, halved or doubled to stay within 0.65–1.6×) and layers beat-locked moves over it, the same bone-offset-after-the-AnimationPlayer technique as the reactions. The rule table, in order:

| style | when | moves |
|---|---|---|
| `sway` | confidence < 0.3, or < 76 BPM, or quieter than −38 dB | slow roll, clip at 0.75×, happy eyes; no beat lock |
| `rave` | ≥ 118 BPM, evenness ≥ 0.45, low_ratio ≥ 0.25 (techno, house, trance, hardstyle) | stomp squash on every beat, arms pump alternately, eyes wide |
| `headbang` | ≥ 132 BPM, density ≥ 4 (rock, metal) | forward nod on the beat, claws half up, squint |
| `groove` | ≤ 108 BPM, low_ratio ≥ 0.2 (hip hop, funk) | two-beat roll, claw pumps on alternate beats |
| `bounce` | everything else with a beat | squash and a small nod on the beat |

A new style must win two estimates in a row (4 s) before she switches, so borderline songs do not flicker. Silence or a stale estimate resets speed and layers. The thresholds are constants at the top of the script and the watcher logs the same features per song, so tuning is: play the song, read the journal, adjust. First live reading: Schrotthagen at 161 BPM, evenness 0.5, low 0.77 → rave. Not built yet: a build-up/drop detector for rave (energy rising over bars, then a crouch and a drop back in) and a genre hint from the brain.

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

- **Piper (`strawberry/speech.py`):** `Daemon.perform()` hands any line without `audio` to `Speaker.say()`, which runs the `piper-tts` Python package (onnxruntime, CPU) in a worker thread and writes a wav into a per-daemon temp dir; the path goes in `audio`. A medium voice loads in ~0.9 s at startup and voices a line in 60–150 ms, so the bubble lands a tenth of a second later than silent mode. The last three wavs are kept so a widget still playing one is not cut off. Silence is never an error: speech off, quiet hours, no voice installed, or a synthesis failure all mean "send the blob without `audio`", and `/health` says why under `speech.reason`. A caller that supplies its own `audio` keeps it. Emoji and dashes are stripped before synthesis; the bubble still shows them.
- **Voices** are Piper `.onnx` files in `~/.local/share/strawberry/voices/`. `strawberry voices` lists them, `strawberry voices en_US-amy-medium` downloads one (catalogue: rhasspy.github.io/piper-samples), `strawberry audition` plays a sample line in each, `strawberryd --say TEXT [--voice V]` writes a wav for one. Installed: `en_GB-alba-medium` (default; a Scottish voice that reads British English lines well), `en_GB-jenny_dioco-medium` (RP), `en_US-amy-medium` (US), `en_GB-alan-medium` (Scottish male). The persona is British English with a dry wit and no dialect words; the accent comes from the voice alone. The persona speaks of "the user's desktop", never a name, so the same defaults ship to everyone.
- **Bubble:** a `Label3D` billboarded above the crab (`widget/bubble.gd`). Reveals characters over time, timed to the **audio duration** if `audio` present, or a text-length heuristic if silent (≈18 chars/s, clamped 1.6–9 s). Holds, fades, auto-hides, and signals `finished`. Tinted by `emotion`.
- **Audio-reactive claws (`widget/speech_player.gd`):** an `AudioStreamPlayer` on its own `Speech` bus carrying an `AudioEffectSpectrumAnalyzer`. Each frame it reads the 90–4000 Hz magnitude, maps −52…−16 dB to 0…1 through an envelope follower (fast attack, slower release), and writes it to the `claw_open_L/R` blend shapes at process priority 160, after the reaction recipes. When playback ends it writes 0 once and stops writing, so the claw controller's dance/think gestures own the morphs again. **The wav must play through Godot**; that's the only way the analyser sees it. Don't also send it to the system mixer. Godot's dummy audio driver still mixes, so the headless acceptance check measures the clack (0.78 mid-tone, 0 after).

**Silent mode falls out for free:** omit `audio` and you get a talking crab with a bubble and no sound. The model still writes the line. This is "quiet hours" (`speech.quiet_hours = "22:00-08:00"` in config, or `speech.enabled = false`), and it's the mode built **first** (§10).

---

## 7. Doorway: voice (STT) — `strawberry/voice.py`

Hotkey → she listens → **faster-whisper** → the transcript is a `source: voice` event → the brain answers in her voice. Voice lives *inside the daemon* (not a fourth watcher) so the whisper model loads once at start (~1.6 s for `small`, int8, CPU) and the hotkey is a bare `POST /listen`.

One session (`Listener.session`):

1. `{state: "listening"}` → `pw-record` from the microphone at 16 kHz mono. The mic is `[voice].source` (a `pactl` source-name fragment) or the first non-monitor input; the machine's *default* source is the speaker monitor, which would make her hear the music instead of you, so the default is never used blindly.
2. Recording ends after `silence_s` (1.1 s) of quiet following at least `min_speech_s` of speech, on a second poke (`/listen` again = stop early), or at `max_seconds` (15). Speech is any 0.1 s chunk louder than the room's quietest chunk + 12 dB and than `level_db` (−40 dBFS).
3. `{state: "thinking"}` → whisper in a worker thread (`vad_filter`, `beam_size` 1, language detect or pinned with `language = "en"`).
4. Empty transcript → `{state: "talking", text: "Sorry, I didn't catch that."}`. Otherwise `Event(source="voice", title=<transcript>)` goes through `handle_event`, so Gemma writes the reply and Piper speaks it. `describe()` renders voice events as "the user is talking to you; reply to them", and the persona carries two voice examples, so she answers rather than narrates.

`/health` shows `voice` (model, ready, phase, sessions, empty, last_transcript, last_ms). If whisper cannot load (no model, no network for the first download), voice is disabled with the reason and the rest of the daemon is unaffected. `scripts/check_config.toml` disables voice for the acceptance run; the flow is unit-tested with fake recorder and transcriber (`tests/test_voice.py`).

**Names.** Whisper `small` hears "Daft Punk" as Dothpunk, Duff Punk, Dove Punk (2026-09-21, three tries; the thinker then played a real artist called Dovepunk). faster-whisper's `hotwords` biases decoding toward given names, so the daemon keeps a vocabulary: `[voice] vocabulary` from the config (your own names) plus whatever a configured server's adapter can offer (none ships configured; with the optional Spotify server, in order of likelihood: the artist playing now, favourites, the last 50 saved tracks' artists, playlist names — `adapters/spotify.py`, refreshed every `vocabulary_refresh_s` = 10 min, first 2 s after start). The first `max_hotwords` (60) go to every transcription. Without such a server the list is just your own `vocabulary`. An artist you have never saved gets no help from this; `[voice] model = "medium"` is the next lever. Measured on Piper-synthesised phrases (8 artist sentences): small 3/8 plain, 7/8 with hotwords, 1.5 s; medium 5/8 plain, 7/8 with hotwords, 3.2 s per sentence on the CPU. A real microphone and a non-native accent are harder than Piper (`small` heard "Kashmir by Led Zeppelin" as "Cosmere Pie, Let's Cheppelin'"), so `medium` is the recommendation. Whisper on CUDA (`[voice] device = "cuda"`, `compute_type = "int8_float16"`; `uv sync --inexact --group gpu` installs the cuBLAS and cuDNN wheels in a checkout, `uv tool install 'strawberry[gpu]'` for an installed one, and `voice.preload_cuda_libraries` loads them by path) (the scripts sync with `--inexact --group gpu` so `bin/strawberry` or the acceptance run does not prune that group again; a bare `uv sync` does, and whisper then logs `No module named 'nvidia'` and falls back to the CPU) is 0.13 s per sentence for `medium` and takes 1.2 GB of VRAM; measured next to Qwen and both Gemmas it fits with ~3.7 GB spare and nothing evicted. `/health.voice.hotwords` is the count.

**Hotkey.** `strawberry hotkey [COMBO]` writes a GNOME custom keyboard shortcut (`gsettings`, default `<Super><Shift>space`; `<Super><Alt>s` is GNOME's screen-reader toggle) that runs `strawberry listen` (the absolute path of the CLI that set it; a bare-socket `POST /listen`, ~40 ms). GNOME owns the key, so it is identical on X11 and Wayland and the daemon never grabs keyboard input. `--remove` undoes it. Phase 6 routes voice through the action model with tools; today it goes to the reaction path like everything else.

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

An option's score is the mean similarity of its two nearest examples (one odd example cannot carry it; one close example is enough), softmax at T = 0.05 → probabilities, confidence by the formula above. Score = expected level over the rubric, 1-based, with a legend. Noul = Choice between the `yes` and `no` examples, reported as p(yes). A single Ollama embedding call costs ~165 ms regardless of batch size, so a sentence is routed in ~170 ms (the bake-off's 16 ms was amortised over 16 texts). The examples are embedded once at start (warm-up gets the same 120 s allowance as the brain); if Ollama is down the gate reports `disabled_reason` in `/health.gate` and every sentence is chat.

Config `[gate]`: `enabled`, `model`, `query_prefix`/`document_prefix` (empty both for a model without conventions), `neighbours`, `temperature`, `act`/`offer`/`topic_min` thresholds, `timeout_s`, and `[gate.examples]` with `"kind.request" = [...]`-style extra phrases: a sentence she misreads goes under the option it belongs to. Tools: `strawberry route "…"` prints one sentence's full reading; `scripts/gate_check.py` runs the phrase set (add to it; it exits 1 on a miss). Unit tests run on a bag-of-words fake embedder (`tests/test_systemone.py`).

**The routing questions** (all asked at once, atomic, combined in code):

- `kind` Choice: `request` (asks her to do something), `question` (wants information looked up), `chat` (small talk, feelings, banter), `other`.
- `topic` Choice: `music`, `calendar`, `notes`, `system`, `other` — picks which MCP servers to load.
- `is_urgent` Noul; `is_about_her` Noul (she answers those herself, whatever the kind).

Then in code (`decide()`): `act` at confidence ≥ 0.6 on a `request`/`question`, `chat` below `offer` (0.3), the middle band in between. Since 2026-09-22 the reading is used for exactly two things (§8b): a clear `act` with a sure tool and nothing to fill in fires a **reflex**; `wants_library_change` ≥ 0.5 decides whether Qwen is offered the careful tools. Everything else the user says goes to Qwen whatever the decision says, so the three labels are a journal entry now, not three code paths. Every decision is still logged with its probabilities so the thresholds can be tuned on real sentences.

### 8b. The tool loop, and the one brain behind it

**Built so far (2026-09-21): the MCP client, `strawberry/tools.py`.** `[tools.servers.<name>]` in the config lists the servers with a `topic` (one of the gate's) and a launch `command`; the servers you list are the servers, and none is listed by default (ADAPTERS.md). Each runs as a child process over stdio through the official `mcp` SDK (2.x); the SDK's transport has to be opened and closed from one task, so every server gets a task that owns the connection and serves calls from a queue. Servers connect in the background at start (`preconnect`) or lazily, are skipped with a warning when they fail, and are retried on the next use; a hung call drops the connection instead of blocking the ones behind it. `Toolbox.tools_for(topic)` returns `ToolSpec`s with an Ollama-ready `for_ollama()` (names prefixed with the server on a collision); results are text cut to `result_chars` (2000) and `ok` reads both the MCP error flag and the `{"error": …}` body some servers return instead. Measured with the Spotify server: connect 0.45 s, 25 tools, a call 0.1–0.3 s. `strawberry tools [TOPIC]` lists, `strawberry tool spotify next` calls; `/health.tools` shows each server's state. Tests: a fake session for the logic and a real stdio round trip against `tests/mcp_echo_server.py`.

**Built (2026-09-21): the reflex tier, `strawberry/actions.py`.** The gate asks two more questions on the same embedding (§8a, chained): the topic's *tool* Choice (`music_tool`: skip, previous, pause, resume, volume_down, volume_up, now_playing, other) and a *has_argument* Noul (a specific song, artist, amount or time the embedding cannot extract). When the tool is sure (`[actions] reflex`, 0.6) and there is nothing to fill in (`argument`, 0.5), the reflex runs directly: no model in the loop.

**Who does it (2026-09-22): MPRIS, or a server's adapter.** `Actor.reflex_for` asks two questions in order. Does a configured server for the sentence's topic have a reflex for this tool? Then it, because a server usually knows more than the desktop does — Spotify's Web API can name the *next* track before the player's metadata catches up, and its volume is the active device's rather than the app's. Otherwise the bare music commands go to **MPRIS** (`strawberry/mpris.py`), which every desktop player answers on the session bus, so skip, previous, pause, resume, volume and "what's playing" work with nothing configured at all — no server, no account, no tokens. `[actions] mpris = false` turns that off; the thresholds and `carries_argument` are the same either way, so "play daft punk" still goes to Qwen rather than resuming whatever was paused.

MPRIS in detail (jeepney, asyncio, one connection reopened after a failure): `ListNames` for `org.mpris.MediaPlayer2.*`, one `GetAll` per player, and the active one is a player that is `Playing`, else the one she acted on last, else the first — so "pause" and "play" keep talking to the same player. Volume is a double, stepped by ±0.15 and clamped; `Next`/`Previous` are followed by a 0.4 s settle and a re-read, because a player reports the old track for a moment. A player that does not implement something says so in the sentence ("I tried to skip, but Mozilla Firefox won't skip from here.", "…won't say where the volume is.") instead of failing silently. Measured live 2026-09-22 against Spotify's own MPRIS: `now_playing` 9 ms, `pause` 7 ms, `skip` 403 ms (the settle); the same sentences through the Spotify server are 463 ms, 425 ms and 1201 ms.

**Adapters (2026-09-22, ADAPTERS.md).** Everything that was Spotify-specific now lives in `strawberry/adapters/spotify.py`: its reflexes, its situation line, the recogniser's vocabulary, the rewording of its 403s and 404s, its gate phrases and its `common_tools`. An adapter is loaded only when a configured server matches it, by the name in `[tools.servers.<name>]` or by an explicit `adapter = "…"` there. `ToolsConfig.servers` is empty by default, `tools.clarify_error` is a hook that asks the matching adapter (and leaves a server without one exactly as it answered), and the generic music phrases stay in `systemone.py` because music control is core now — only the library ones ("save this song", "play my running playlist") come with the Spotify adapter. Those two are the only sentences in `scripts/gate_phrases.json` that need it: 71/71 with a Spotify server configured, 69/71 without, and both misreadings are harmless (one is a name the thinker handles, the other is chat, which also goes to Qwen).

What she says is split by reliability: **code writes the fact** in one plain sentence ("Skipped. Now Blue Monday by New Order.", "Volume down to 65.", "I tried to skip, but Spotify said: no active device.") and the reaction path adds **one short quip** in her voice after it (an `action` event: `brain.describe` quotes the fact and asks for at most 8 words; `Daemon.report` joins them, forces `alert` on a failure). Measured first: a 1B model given the structured result composed lines like "Let's go. A bit more fun, perhaps?" for a skip and a cheerful line for a failure, so the fact never depends on it. The MPRIS doorway's reaction to a track change she caused is swallowed for 8 s (`quiet_media_until`, set before the action runs because the watcher is faster than the confirmation). `/health.actions` keeps the last action with its tool calls. Anything the reflex tier does not cover (an argument, `other`, a topic without reflexes) is counted as `deferred` in the journal and handed to the thinker.

**Built (2026-09-21), rebuilt (2026-09-22): the thinker, `strawberry/thinker.py`.** Everything the user says that is not a bare reflex is now ONE Qwen call (`brain.action_model`, `qwen3.8:27b`): small talk, questions, and requests with something to fill in. It gets the sentence, the *situation* (the date, "Now playing on Spotify: … (album: …)" so "this song" means something — from the server's adapter, or from MPRIS when no server can say — the library names, the ledger) and **the configured servers' tools** as Ollama specs from `Toolbox.tools_for` — the careful ones (save, remove, add to a playlist) only when the gate's `wants_library_change` is ≥ 0.5.

**How many tools (2026-09-22).** Measured on the live `qwen3.8:27b` with Ollama's `prompt_eval_count`: her system prompt and one sentence are 326 tokens, and the Spotify server's 25 tool schemas add **2382** on top — 29 % of `num_ctx` 8192, not the ~360 this section claimed before anyone counted (that number was the prompt *without* the tools). Its ten `common_tools` cost 1304. So `[thinker] max_tools` (30) caps the schemas: `Thinker.tools` orders the topics with the gate's first, each server's adapter `common_tools` first inside it, cuts the tail and logs what was left out. One Spotify server is under the cap; two or three servers are not.

It calls tools until it answers; each call goes through the same client with truncated results; after `max_rounds` (6) the tools are withdrawn and it must say honestly what it did. `think = false` (Ollama accepts false / "low" / "medium" / true = xhigh, not "high"), `keep_alive = "30m"` so she is rarely cold, `num_ctx = 8192`, temperature 0.2, one `timeout_s` (45 s) over the whole request. Every call is a fresh conversation; nothing accumulates but the ledger.

**The reply is hers.** Qwen's system prompt is her voice (`thinker.VOICE`: small, dry, warm, British, ≤ 15 words for small talk, two or three sentences when there are facts, never the user's name) plus the tool-use rules (`TOOLS_GUIDE`: be decisive, a misheard name is probably a library name, 'play' means play, report only what a tool did) — or, with no servers answering, `NO_TOOLS` (answer from memory, no internet, say so when the question needs today's news). The line is performed as it stands: no Gemma quip on top of a Qwen answer. It is asked to start with its mood in square brackets, `[happy] Skipped. Blue Monday next.`, which `split_emotion` parses off into the performance's `emotion`; a missing or unknown tag is `neutral`, and a tool that failed forces `alert`. `tidy_sentence` takes the first paragraph, strips markdown and caps it at 380 chars (`speech.max_chars` is 400).

Cover for the wait scales with it: `Daemon.think` puts her in the `thinking` pose at once and says nothing (a warm round is ~2 s, and "On it." before the answer to "what's up?" read odd), speaks a random acknowledgement from `[thinker] acks` only if the reply has not come after `ack_after_s` (2.5 s), says "Still on it." once after `still_on_it_s` (8 s), then her line. Measured: cold load 7–17 s (the first request of a session), a warm round ~2 s, prompt 326 tokens plus 2382 for 25 Spotify tools. Loading Qwen can evict Gemma and the embedding model from VRAM; both re-warm themselves in the background after a timeout (see §3). `strawberry think "…"` runs it by hand and prints the calls; `/health.thinker` keeps the last one. Tests on a scripted fake Ollama and the fake Spotify (`tests/test_thinker.py`).

**Why one brain (2026-09-22).** The tiers between the reflex and chat were where every live failure came from: a wrong reflex on a sentence that carried a name, "it's already playing" when she had misheard, and offers nobody had asked for. Gemma keeps what it is good at — the desktop events (§3, §4) and the quip after a reflex — and the user gets one voice for everything else. `[thinker] enabled = false` (the unit tests, `scripts/check_config.toml`, a machine without Qwen) falls back to the old Gemma chat path, so voice still works with a 1B model and no MCP servers.

**The ledger (built 2026-09-21).** `strawberry/ledger.py` is her only memory across turns: the last `[actions] ledger_turns` (6) exchanges no older than `ledger_age_s` (10 min), each "the user said …; you did … and said …". Qwen gets all of them in the situation, Gemma the last three when it is the fallback, so "the other one" and "skip this one too" resolve; nothing else accumulates. `/health` shows it. **Typed input:** `strawberry talk` posts each terminal line as a voice event, so a typed sentence and a spoken one take the same path and she answers in both places; the prompt prints the gate's reading and the reflex or the thinker's tool calls with its mood.

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
- [x] The beat of the playing music picks a dance style and the clip runs at its tempo; synthetic 128/92/168 BPM drums are tracked to within 2 BPM with the phase on the kick. (`strawberry/doorways/beat_watch.py`, `check_phase1.sh` step 14, `tests/test_beat_track.py`)
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

**The export.** `widget/export_presets.cfg` has one preset, `Linux`: x86_64, the PCK embedded in the executable, `export_filter = all_resources` (every scene, script, shader and the GLB, and the `validate_*.gd` scripts, so the binary can run its own acceptance) plus `desktop_idle.py` by `include_filter`. `scripts/build_widget.sh` copies `widget/` to a staging directory, stamps `config/version` from `pyproject.toml` there (the checkout's `project.godot` stays unversioned, so a source run is `dev`), exports headless with the official 4.7.2 export templates (`~/.local/share/godot/export_templates/4.7.2.stable/`) and writes `dist/strawberry-widget-<version>-linux-x86_64` and its `.sha256` (`sha256sum` format). About 75 MB, nearly all of it the Godot runtime. What changes once exported, and how the widget copes: `res://` is inside the executable, so a file an outside program needs (the idle helper run by `/usr/bin/python3`, the evidence icon whose path goes to the daemon) is copied out to `$XDG_CACHE_HOME/strawberry/widget/` (`paths.gd::on_disk`); `res://` is read-only, so `validate_widget.gd` takes `--report=<path>`; and release templates have no `--script`, so the binary runs a validator with `strawberry-widget --headless -- --acceptance=res://validate_widget.gd …`: `widget.gd` gives the SceneTree the validator's script and steps aside before building anything. The window properties below all come through the export unchanged (checked 2026-09-22 on X11: 32-bit ARGB visual, `_NET_WM_STATE_ABOVE`, Motif hints with no decorations, a 117-rectangle input shape covering a third of the window, sound through PulseAudio on the analysed bus, and a capture whose background pixels have alpha 0).

**X11 pin.** `strawberry widget` launches the widget (binary or Godot) with `--display-driver x11`. On the current X11/GNOME session that is native. On a Wayland session it runs under XWayland, where Mutter still acts as the X window manager and honours the keep-above and position hints. Native Wayland clients on GNOME get neither (no layer-shell, no client positioning), so we don't take that path. Always-on-top and remembered position are treated as **best effort**: if a compositor ignores them she degrades to a normal frameless window, nothing breaks.

**Interaction.** Drag her body to move the window; the position is remembered in `~/.config/strawberry/widget.cfg` along with the skin, and clamped to the screen's usable area on restore. `Q` quits, `C` cycles skins. Cel shading and the ink outline are always on in the widget (the v2 preview's `O` toggle was a review aid, not a feature). The daemon can also send `{"command": "quit"}` or `{"command": "skin", "value": "mint"}`.

**Right-click menu (`widget/menu.gd`).** A `PopupMenu` drawn inside her transparent window: *Mute her voice*, *Quiet for an hour* (shows minutes left, click again to cancel), *Voice volume* (25–100 %), *Skin*, *Always on top*, then *Settings file…* (opens `$XDG_CONFIG_HOME/strawberry/config.toml` in the desktop's text editor; a missing one is first written from the template by `strawberry config --init`), *Apply settings* (`strawberry restart`: under the tray unit that restarts the tray and everything under it, otherwise the daemon; the widget reconnects), *Voices folder…* (`$XDG_DATA_HOME/strawberry/voices`), *Reset position*, *Quit*. Mute and quiet keep the bubble and drop only the sound, widget-side, so the daemon still voices lines and a second widget would still hear them. While the menu is open the passthrough polygon is cleared so the whole window takes clicks; it is recomputed on close. Every item has an explicit id: items added without one get their index as id, and a submenu row then steals a real row's check mark. Both CLI calls go to `$STRAWBERRY_CLI` (the tray and `strawberry widget` set it to the absolute path of the `strawberry` that launched her), else `strawberry` on PATH; if neither can be run she says so in her bubble. Nothing in the widget points into a checkout.

**Preferences** persist in `$XDG_CONFIG_HOME/strawberry/widget.cfg` (`[appearance] skin, top_hat`, `[audio] muted, quiet_until, volume`, `[window] always_on_top, x, y`, `[sleep] after_minutes`), written by the widget and read back by the tray (§14); `widget/paths.gd` and `src/strawberry_crab/paths.py` apply the same XDG rules. Until PACKAGING.md step 4 they lived in Godot's `user://widget.cfg` (`~/.local/share/godot/app_userdata/Strawberry/widget.cfg`): the first start with a display copies that file to the new place when the new one does not exist yet, and leaves the old one where it is. Headless runs (every acceptance check) read and write `user://widget_headless.cfg` instead, or a per-validator file, and never the real preferences; `validate_prefs.gd` checks the path, the one-time copy against scratch files, and the `config --init` call.

**Type box (`widget/type_box.gd`).** *Chat with Strawberry…* in the menu, or the T key while she has focus, opens a plain field under her: a square-cornered `LineEdit` styled as smoked glass (a 40 % dark tint with a hairline rim; the desktop is not in Godot's viewport, so there is nothing to blur) with only the caret in the skin's claw colour; the rim stays glass, and focus only brightens it. Enter sends the line over the websocket as `{"type": "heard", "text": …}`; the daemon turns it into `Event(source="voice", title=text)` and runs `handle_event` as a background task, so it takes the same funnel as speech and `strawberry talk` (gate, reflexes, Qwen, ledger) while the socket keeps answering pings. Escape closes it. The field greys out while she is listening or thinking ("She's on it…") or the daemon is away ("Not connected to strawberryd"). While open, the field's corners join the passthrough hull so it takes clicks; closed, they fall through again. `--typing` opens it for a capture; step 17 of `validate_widget.gd` opens it headless, submits a line and sees the answer and the ledger entry.

**Pupils follow the cursor (`widget/gaze.gd`, `widget/strawberry_pupil.gdshader`).** No pupil bones: the pupil is a small ellipsoid baked into each eye mesh's ink surface, and the eye mesh is bound rigidly (weight 1) to its eyestalk bone. So each frame the widget passes the bone's pose to a variant of the cel shader as `to_rest`/`from_rest` (authored rest space ⇄ posed skeleton space); vertices that land within 0.034 of the authored pupil centre are rotated about the authored eyeball centre by a shared yaw/pitch, then sent back through the pose. Exact under any eyestalk bend or stretch, and the blend shapes (lids) are untouched. The gaze itself: the cursor's screen position (readable without focus on X11) is mapped to the crab's plane from the project window size and the orthographic camera (not `Camera3D.project_position`, which needs a real viewport), the direction from the midpoint between the eyes to that point, with the cursor imagined `LOOK_DEPTH` = 2.5 units in front of her, gives the angles, clamped to ±0.7 rad and eased at 14/s. Both pupils share one gaze. After 12 s of a still cursor she looks straight ahead. `--look=x,y` pins the cursor for captures and the headless check (step 13). `CelStyle.apply()` replaces surface materials on a skin change, so the widget re-applies the pupil material after it.

**Start on login.** One unit now: `strawberry install` writes `strawberry-tray.service` (`Type=simple`, `After=`/`WantedBy=graphical-session.target`, `Restart=on-failure`) with `ExecStart=<the installed strawberry> tray --port <port>`, where the first word is the absolute path of the `strawberry` executable that ran `install` (`~/.local/bin/strawberry` after `uv tool install`, `<checkout>/.venv/bin/strawberry` through `bin/strawberry`), else `<python> -m strawberry_crab`. It runs `systemctl --user import-environment DISPLAY XAUTHORITY WAYLAND_DISPLAY XDG_SESSION_TYPE DBUS_SESSION_BUS_ADDRESS`, enables it, and drops `~/.config/autostart/strawberry.desktop` as a fallback for a session that never reaches that target — the entry runs `strawberry tray-autostart`, which does nothing when the unit is enabled, so two trays can never start. Then it `daemon-reload`s and **restarts** the unit (not just `enable --now`), so a tray still running from an older ExecStart comes back on the new one; that is how the move from `strawberryd/.venv/bin/strawberryd --tray` to the package is made. The old per-doorway units and `strawberryd.service` are removed by the same command. Everything else is the tray's to start (§14). Once installed, `daemon/stop/restart/status` drive the tray unit instead of pidfiles; logs move to `journalctl --user -u strawberry-tray`. `strawberry uninstall` reverses it.

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

**The icon is a StatusNotifierItem, written by hand on jeepney.** The tray takes a bus name of its own (`org.kde.StatusNotifierItem-<pid>-1`), exports `org.kde.StatusNotifierItem` on `/StatusNotifierItem` and a `com.canonical.dbusmenu` menu on `/MenuBar`, and calls `RegisterStatusNotifierItem` on `org.kde.StatusNotifierWatcher`. Incoming method calls are answered from one dispatch table (`Properties.Get/GetAll`, `Introspectable.Introspect`, `Peer.Ping`, `Activate`/`SecondaryActivate`/`ContextMenu`/`Scroll`, and the dbusmenu's `GetLayout`/`GetGroupProperties`/`GetProperty`/`Event`/`EventGroup`/`AboutToShow(Group)`), and changes go out as `LayoutUpdated`, `ItemsPropertiesUpdated` and `NewToolTip`. When the watcher is missing (GNOME without the AppIndicator extension) nothing breaks: the tray says so once, watches `NameOwnerChanged` for it, and registers the moment it appears — a GNOME Shell restart re-registers by itself.

**The icon pixels.** `IconPixmap` is `a(iiay)`: width, height and the pixels as **ARGB32 in network byte order**, which is not what a PNG stores. `scripts/render_icons.py` renders 🍓 from Noto Color Emoji (Apache-2.0, a CBDT bitmap font: Pillow loads it only at its one 109 ppem strike) to `src/strawberry_crab/assets/icons/strawberry-{16,22,24,32,48,64}.png`, checked in and shipped in the wheel as package data (`paths.icons_dir()` finds them through `importlib.resources`); `strawberry/icons.py` reads them at runtime with `zlib` alone and reorders RGBA to ARGB, so Pillow stays a dev dependency. **`IconName` is left empty unless the icon really is in an icon theme**: GNOME's AppIndicator extension prefers the name over the pixmaps and draws a placeholder for one it cannot resolve — with `IconName = "strawberry"` the panel showed "…" where the berry should be, and with it empty the pixmaps came through (measured 2026-09-22).

**The menu is her right-click menu, in the top bar** (§13): a disabled status row (`Idle` / `Listening` / `Thinking…` / `Talking` / `Dancing`, or "strawberryd is not running"), Show her / Hide her, Chat with Strawberry…, then Mute her voice, Quiet for an hour (counting down), Voice volume, Skin, Sleep after inactivity, Sleep now, Top hat, Always on top, then Settings file…, Voices folder…, Reset position, Restart (which is her menu's "apply settings": the children come back with the new config) and Quit. The choices are the same lists as `widget/menu.gd` and a test compares the two files, because that is the seam that would drift.

**How the tray knows anything.** `GET /health` every two seconds gives the status row (`state`: the last state performed, with the transients expiring after 6 s since the widget leaves them on its own). The check marks are read back from the widget's own settings file (`~/.config/strawberry/widget.cfg`, `paths.widget_prefs_file()`; the old `~/.local/share/godot/app_userdata/Strawberry/widget.cfg` until the widget has copied it over, §13), so the tray and her right-click menu agree whichever one you used last.

**Clicks go through the daemon, never straight at the widget**: `POST /command {"command": ..., "value": ...}` broadcasts to every open widget socket, and `widget.gd`'s `run_command` acts on it (`show`, `hide`, `chat`, `mute`, `quiet`, `volume`, `skin`, `on_top`, `hat`, `sleep_after`, `sleep_now`, `reset_position`, `quit`). The daemon validates the name and the value type, so a typo is a 400 rather than a `push_warning` nobody reads. Settings file… and Voices folder… are the tray's own business (`xdg-open`, writing the commented config template first if it is missing).

**The children.** `daemon` (`python -m strawberry_crab.strawberryd`), `mpris_watch`, `notify_watch`, `beat_watch` (`python -m strawberry_crab.doorways.<name>`), all on the tray's own interpreter so an installed tray never reaches into a checkout, and the widget, chosen by `widgetbin.resolve()` (§13): the installed binary itself (`strawberry-widget --display-driver x11 -- --ws=ws://127.0.0.1:<port>/ws`), or in developer mode `python -m strawberry_crab widget` (which imports the project if needed and execs Godot; `STRAWBERRY_TRAY=1` keeps it from starting a second daemon or a second set of doorways). The widget child's environment adds `STRAWBERRYD_PORT` (the tray's port) and `STRAWBERRY_CLI` (the absolute path of this `strawberry`, for her menu). With neither a binary nor a checkout the tray logs how to get one (`strawberry widget --fetch`) and runs the rest. A child that exits is restarted after a backoff that doubles from 1 s to 30 s and resets once the child has stayed up for 30 s; Quit terminates them all, killing what does not go in 5 s. The tray writes `~/.local/state/strawberry/tray.json` (its own pid, each child's pid, command line and restart count) and `strawberry status` prints it.

Acceptance: `scripts/check_tray.sh` registers the item on the real session bus and reads it back with `busctl --user` (introspect the item, `GetLayout`, the watcher's registered list). Unit tests build every reply as a jeepney message, serialise it and parse it back, which is how a hand-written D-Bus server catches a signature that does not match its data.

## 15. Settings

One file, `~/.config/strawberry/config.toml` (`$XDG_CONFIG_HOME` respected). **Every path comes from `strawberry/paths.py`**: config `$XDG_CONFIG_HOME/strawberry/`, data `$XDG_DATA_HOME/strawberry/` (`voices/`, later the widget binary), state `$XDG_STATE_HOME/strawberry/` (`tray.json`, the pidfiles and logs of by-hand runs); an unset, empty or relative variable means the default under `~`. Defaults live in code (`strawberry/config.py`), so the file only needs the lines you change. Read by the daemon at start and by the media watcher; `GET /config` shows the effective result. Unknown keys warn; wrong types refuse to start with the key named.

```toml
[daemon]
port = 8770

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
```

`strawberry config` creates the file from a commented template (`strawberryd --init-config`) and opens it in `$EDITOR`; `strawberry restart` applies it; `strawberry config --init` only writes the template if missing and prints the path (the widget's *Settings file…* runs that). `STRAWBERRYD_PORT` still overrides the port for scripts. The widget's own preferences (skin, window position, …) are `~/.config/strawberry/widget.cfg` (§13).

**The first-run privacy note** (`strawberry_crab/firstrun.py`). While `$XDG_STATE_HOME/strawberry/privacy-notice-shown` is missing, the daemon logs one `privacy:` line at start: what she reads from notifications under the current `body` / `body_apps`, the three modes, and the config file to change it in. The first widget whose hello is served (not refused for its version) gets a short version in her bubble (`talking`, happy, a wave) and the marker is written before it is sent, so it is said once per user. Deleting the marker brings it back. Tests start with the marker present (tests/conftest.py gives every test throwaway XDG dirs), and `scripts/check_phase1.sh` gives its daemon a state dir that has it, so the validators see only the performances they ask for.

## 16. Repository map

```
WIRING.md                this document
PACKAGING.md             the plan from a developer checkout to `uv tool install strawberry-crab`
ADAPTERS.md              adding an MCP server, and writing an adapter for one
README.md                for users: install, setup, configuration, privacy
CHANGELOG.md             what changed per version (by hand; the release notes are its section)
LICENSE, THIRD_PARTY.md  MIT; what we use from others, and the models and voices the user downloads
.github/workflows/       ci.yml (tests + build on push/PR), release.yml (v* tag -> GitHub release + PyPI, PACKAGING.md)
pyproject.toml, uv.lock  the one Python package, `strawberry` (hatchling; `uv build`, `uv tool install .`); groups dev (pytest, Pillow) and gpu (cuBLAS/cuDNN, also the `gpu` extra)
src/strawberry_crab/          the package: contract, events/reactor, brain, speech (Piper), voice (whisper), systemone (the gate), tools (MCP client), actions (reflexes), thinker (Qwen tool loop), ledger (short memory), hub, server
                         + cli.py (`strawberry`), strawberryd.py (`strawberryd`), paths.py (XDG dirs, the checkout), firstrun.py (the one-time privacy note, §15), widgetbin.py (which widget runs; `widget --fetch`), bus.py (jeepney plumbing), client.py (HTTP to the daemon), icons.py (PNG -> ARGB32), tray.py (the StatusNotifierItem and the process that owns her, §14), mpris.py, adapters/,
                         setupcmd.py (`strawberry setup`: tiers by VRAM, config merge, pulls, voice, widget), doctor.py (`strawberry doctor [--talk]`)
src/strawberry_crab/doorways/ notify_watch.py, mpris_watch.py, beat_watch.py + beat_track.py (`strawberry-doorway <name>`, or python -m strawberry_crab.doorways.<name>)
src/strawberry_crab/assets/icons/  the tray icon PNGs (package data), rendered by scripts/render_icons.py
tests/                   the package's tests (`.venv/bin/python -m pytest -q`)
widget/                  Godot 4.7 desktop widget: widget.gd, ws_client.gd, bubble.gd, speech_player.gd, reactions.gd, dance_style.gd, gaze.gd, menu.gd, type_box.gd, paths.gd (XDG, the CLI, the version), validate_*.gd
                         + strawberry_v2.glb, the shaders/controllers, export_presets.cfg (the Linux binary)
bin/strawberry           the checkout's shim: runs the CLI from .venv (created with `uv sync --inexact --group gpu` on first use)
scripts/check_phase1.sh  Phase 1 acceptance: unit tests + headless widget against a real daemon (WIDGET=<binary> for the export)
scripts/build_widget.sh  export the widget: dist/strawberry-widget-<version>-linux-x86_64 + .sha256 (§13)
scripts/check_reconnect.sh  restart (or SIGNAL=KILL) the daemon under a headless widget; it must reconnect
scripts/check_tray.sh    register the tray on the real session bus and read it back with busctl (§14)
scripts/gate_check.py    the gate over scripts/gate_phrases.json against live Ollama; add sentences she misreads
scripts/sensitive_check.py  the notification-body filter over scripts/sensitive_phrases.json against live Ollama (§4)
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
