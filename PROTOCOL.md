# Strawberry — Bus Protocol

**What this is:** the wire protocol between the **brain** (`strawberryd`, the daemon) and its
**bodies** (avatars that connect to it and perform what it says). Today there is one body, the
Godot crab widget (`widget/`). A second body can be written from this document alone, in any
language, without importing any brain code.

**Three parts.** Part 1 is protocol v1 as the code does it; every statement there is taken from
the code and cites it as `file:line`. Part 1b is the part of protocol v2 that is built (runs and
their events, stopping a run, the clock in ping and pong); it cites functions rather than lines.
Part 2 is the rest of v2, **PROPOSED**: designs for new message families, not code. Each of its
sections says in its heading whether it is proposed or partly built.

File paths are relative to the repo root; `server.py` means `src/strawberry_crab/server.py`, and
so on for the other Python modules.

---

# Part 1 — Protocol v1, as built

## 1. Transport

```
doorways, tray, curl ──HTTP POST──▶  strawberryd  ◀──websocket /ws──▶  body (the crab widget)
```

| | |
|---|---|
| Host | `[daemon] host`, default `127.0.0.1` (`config.py:34`); env `STRAWBERRYD_HOST` (`config.py:408`) |
| Port | `[daemon] port`, default `8770` (`config.py:35`); env `STRAWBERRYD_PORT` (`config.py:410`) |
| One port | HTTP and the websocket share it (`server.py:85-97`) |
| Websocket URL | `ws://127.0.0.1:8770/ws`. The widget's default is the same (`ws_client.gd:21`, `widget.gd:47`); `-- --ws=ws://host:port/ws` overrides it (`widget.gd:119-120`), and the tray passes it (`widgetbin.py:81`) |
| Framing | one JSON object per websocket **text** frame, UTF-8, `ensure_ascii=False` (`hub.py:59`). The widget drops binary frames (`ws_client.gd:81-82`) and anything that is not a JSON object (`ws_client.gd:84-89`) |
| Who is server | the brain. Bodies connect out and reconnect on their own |

### 1.1 HTTP routes

| Route | Body | Answer | Who calls it | Code |
|---|---|---|---|---|
| `POST /event` | `{source, app, title, body, urgency, category, icon}` | `{"sent": n, "performance": {…}}` | the doorways, git hooks | `server.py:340-347`, `events.py:35-58` |
| `POST /perform` | a performance (§3) | `{"sent": n, "performance": {…}}` | the media doorways, `strawberry say`, curl, tests | `server.py:184-191` |
| `POST /tempo` | a beat estimate (§4) | `{"sent": n}` | `doorways/beat_watch.py` | `server.py:330-337` |
| `POST /command` | `{command, value}` (§5) | `{"sent": n, "command": {…}}` | the tray | `server.py:287-304` |
| `POST /listen` | none | `{"listening": true}`, or `false` with `loading`/`error`/`busy`/`stopped`/`debounced`; 503 when `error` | the listen hotkey | `server.py:307-310`, `daemon.py:186-213` |
| `POST /probe` | none | per-slot timings | `strawberry doctor --talk`; loopback clients only (403 otherwise) | `server.py:316-327` |
| `GET /health` | – | status object (below) | the tray (every 2 s), `strawberry doctor` | `server.py:151-176` |
| `GET /config` | – | the effective settings | inspection (curl) | `server.py:180-182` |
| `GET /ws` | – | websocket upgrade | bodies | `server.py:350-371` |
| `POST /ui-token`, `/ui/...` | – | the Brain UI: a one-time login token, then a page and its own API under a session (WIRING.md §17) | `strawberry ui`, the user's browser | `brainui.py` |

`sent` is the number of open body sockets the message went to (`hub.py:57-71`). A 400 answer is
`{"error": "<reason>"}` (`server.py:121-122`).

`/health` fields: `ok`, `widgets` (open sockets), `widget_versions` (one per socket that said
hello), `version` (the brain's), `performed`, `uptime_s`, `brain`, `speech`, `voice`, `gate`,
`wake`, `tools`, `actions`, `thinker`, `ledger`, `state` (now; a transient older than 6 s reads as
the resting state, `daemon.py:245-255`), `rest_state`, `tempo` (the fresh estimate or `null`),
`tempo_age_s` (`server.py:153-175`). A body does not need `/health`; it is for the tray and
`doctor`.

### 1.2 Origin and content type

- A request with an `Origin` header is refused with 403 `{"error": "browser origins are not
  accepted"}` on every route, `/ws` and `/health` included, by one middleware
  (`server.py:125-135`, `:80`). Browsers always send `Origin`; Godot, curl and the doorways do
  not. **A body must not send `Origin`** on the upgrade request. The one exception is the Brain UI under
  `/ui` (WIRING.md §17), which makes its own stricter checks (this host, its own origin, a
  session and a CSRF header) and is not part of the bus: its live view is a server-sent-events
  stream at `/ui/api/events`, not `/ws`, and a body never uses it.
- POSTs with a body must be `Content-Type: application/json`, else 415
  (`server.py:141-144`); a body that is not JSON is 400 (`server.py:147-148`).

### 1.3 Liveness and reconnect

| Side | Behaviour | Code |
|---|---|---|
| Body → brain | `{"type": "ping"}` every 5 s | `ws_client.gd:18`, `:92-94` |
| Brain → body | `{"type": "pong"}` for each ping | `server.py:387-388` |
| Body | drops the socket and reconnects after 12 s with no frame received (any frame counts) | `ws_client.gd:19`, `:79`, `:95-96` |
| Body | a connect attempt that stalls 12 s is dropped too | `ws_client.gd:97-100` |
| Brain | websocket-level ping frames every 20 s (`heartbeat=20`); the socket is closed if no pong frame comes back. Godot answers them on its own; any standard websocket library does | `server.py:352` |
| Body | reconnect backoff 1 s, doubling to 8 s, reset to 1 s on a successful open | `ws_client.gd:14`, `:44-47`, `:72` |
| Brain shutdown | every socket is closed with `1001 going away`, reason `strawberryd shutting down`, and the brain exits within 2 s | `hub.py:42-55`, `server.py:115-118`, `:463` |
| Refused version | close code `4001`; the body backs off to 60 s between tries | `ws_client.gd:15-16`, `:105-110` |

The widget swallows `pong` before handing messages on (`ws_client.gd:85-86`).

## 2. The session

In order, on one socket:

1. The body opens `GET /ws` with no `Origin`.
2. **The brain sends catch-up messages at once, before any hello arrives** (`server.py:356-361`):
   - `{"state": "<rest_state>"}` if the resting state is not `idle` (music is playing);
   - `{"tempo": {…}}` if a beat estimate arrived in the last 6 s (`daemon.py:257-262`).
3. The body sends its hello (§2.1).
4. From then on the brain broadcasts performances, tempo and commands to every open socket, and
   the body may send `ping`, `heard` and `poked` (§6).

A socket that never says hello is still served: it gets every broadcast and may send `heard` and `poked`
(`server.py:374-398`, `hub.py:57-71`).

### 2.1 `hello` (body → brain)

```json
{"type": "hello", "client": "strawberry-widget", "version": "0.2.0", "godot": "4.7.2-stable (official)"}
```

| Field | Type | Meaning |
|---|---|---|
| `type` | `"hello"` | |
| `client` | string | the body's name. Logged only (`server.py:384`) |
| `version` | string | the body's release version, `MAJOR.MINOR[.PATCH]` with an optional `v`, or `"dev"` for a source run (`paths.gd:140-142`) |
| `godot` | string | engine version. Logged only |

Sent by `ws_client.gd:75-76` on every open. The brain compares `version` with its own package
version (`server.py:52-62`, `:416-430`):

| verdict | when | what the brain does |
|---|---|---|
| `dev` | `version == "dev"` | serves it |
| `same` | same major, minor, patch | serves it |
| `minor` | same major, another minor or patch | serves it, logs a warning |
| `unknown` | missing or unreadable | serves it, logs a warning |
| `major` | another major | logs an error and closes with code **4001**, reason `widget <theirs>, daemon <ours>` |

On `4001` the widget shows `My daemon and I don't match (<reason>). Update one of us.` once
(`widget.gd:629-633`) and retries every 60 s.

After a hello that is not refused, the brain may send the one-time privacy note as a performance
(`server.py:385-386`, `:401-413`).

## 3. Performances (brain → body)

One JSON object per performance. **It has no `type` field**: a body recognises it by having a
`state` key (`widget.gd:656-659`). Built by `Performance.to_dict` (`contract.py:53-68`) and sent by
`Daemon.perform` (`daemon.py:219-240`).

```json
{"state": "talking", "emotion": "happy", "anim": "notify_perk",
 "text": "Sam's written to you.", "audio": "/tmp/strawberry-speech-k2x9/line-000041.wav",
 "reaction": "wave", "icon": "/usr/share/icons/hicolor/48x48/apps/slack.png", "hop": true}
```

| Field | On the wire | Type | Allowed values | Meaning |
|---|---|---|---|---|
| `state` | always | string | `idle` `listening` `thinking` `talking` `dancing` (`contract.py:15-21`) | the looping state. `idle` and `dancing` are **resting** states the body returns to; the others are transients |
| `emotion` | always from `to_dict`; absent in the catch-up message | string | `neutral` `happy` `alert` `angry` (`contract.py:25`) | the widget uses it only to tint the bubble (`widget.gd:669`, `bubble.gd:8-13`, `:50`). The brain has already picked `anim` from it (`contract.py:28-33`) |
| `anim` | optional | string | `alert_snap` `notify_perk` (`contract.py:22`) | a one-shot clip over the state; the state's loop resumes when it ends (`widget.gd:672-677`, `:787-794`) |
| `text` | optional | string, trimmed, never empty | – | the speech-bubble line. Omitted: no bubble (`contract.py:58-59`) |
| `audio` | optional | string | an absolute path to a wav on the brain's disk | play it and time the bubble to it (§3.2). Omitted: silent |
| `reaction` | optional | string | `wave` `peek` `shiver` `double_hop` `nod` (`contract.py:24`) | a procedural move layered over the clip (`widget/reactions.gd`). `double_hop` also plays `notify_perk` twice (`widget.gd:679-684`) |
| `icon` | optional | string | an absolute path to a PNG, JPG or SVG | the notifying app's icon beside the bubble; shown only when `text` is present (`widget.gd:701-705`, `badge.gd:15`) |
| `hop` | optional, only `true` | boolean | `true` | the window itself bounces (`widget.gd:685-686`, `:596-606`) |

Optional fields are omitted, never `null`. `POST /perform` rejects unknown fields, unknown values,
and `audio`/`icon` paths that are not files, with 400 (`contract.py:71-118`). The brain does not
re-validate its own performances; the widget copes with bad values: an unknown `state` becomes
`idle` (`widget.gd:666-668`), an unknown `anim` is ignored (`widget.gd:677`), unknown fields are
never read.

### 3.1 What the widget does on receipt

From `widget.gd:661-716`:

1. If she is asleep, the performance is held (only the newest one is kept), a resting `state` is
   remembered, and she wakes; it is performed when the wake clip ends (`sleep_controller.gd:111-121`,
   `:127-135`). Tempo and commands are never held.
2. Apply `state` (`widget.gd:769-776`): crossfade to its loop; `idle`/`dancing` become the resting
   state.
3. Fire `anim`, `reaction`, `hop`.
4. Audio and bubble (§3.2).
5. When the bubble finishes and the state is still `talking`, return to the resting state
   (`widget.gd:796-801`). **The brain never sends a follow-up** to end a line.

### 3.2 How `audio` is delivered and how the bubble is timed

- The brain runs Piper for any performance that has `text` and no `audio` (`daemon.py:225-228`,
  `speech.py:182-210`). It writes a wav (16-bit PCM, mono, the voice's sample rate) into a per-daemon
  temp directory, `strawberry-speech-*` (`speech.py:151`), and puts the **absolute path** in `audio`.
  No audio bytes cross the socket: **the body must share the brain's filesystem.**
- The brain keeps only the last `[speech] keep_files` wavs (default 3) and deletes older ones
  (`speech.py:205-206`, `config.py:133`). A body must open the file soon after receipt.
- No `audio` means silent mode: speech off, quiet hours, no voice installed, or a failed
  synthesis (`speech.py:182-196`). The bubble still shows.
- The widget plays the wav through its own analysed audio bus and drives the claws from its
  loudness (`speech_player.gd:50-60`, `:62-75`). It does not play it when the user has muted her
  or set "quiet for a while" (`widget.gd:693`, `:419-420`).
- **Timing:** `play_file` returns the wav's length in seconds (`speech_player.gd:50-60`). The
  bubble reveals the line character by character over exactly that length, holds 0.9 s, fades over
  0.35 s, then reports finished (`bubble.gd:44-60`). With no audio (or a wav that fails to load,
  which returns 0), the reveal takes `clamp(0.6 + 0.055 × characters, 1.6, 9.0)` seconds
  (`bubble.gd:39-41`).
- Audio with no text: the talking pose is held for the wav's length + 0.3 s (`widget.gd:709-712`).
  `talking` with neither: 1 s (`widget.gd:713-716`).
- A wav that is still playing is stopped by a performance with a new line (`text` or `audio`) and
  by `listening` (her voice would go into the microphone); a performance with neither, such as the
  media doorway's `{"state": "dancing"}`, leaves it playing. Its `state` is taken at once: she
  dances to the end of the sentence (`widget.gd:692-699`).

### 3.3 Where performances come from

| Sender | Example | Code |
|---|---|---|
| Every desktop event and reply | `{"state":"talking","text":…,"emotion":…,…}` | `daemon.py:300-318` and the handlers below it |
| Voice session start | `{"state":"listening","emotion":"neutral"}` | `voice.py:577` |
| Recording done, transcribing | `{"state":"thinking","emotion":"neutral"}` | `voice.py:598` |
| Thinker started | `{"state":"thinking","emotion":"neutral"}`, then after `ack_after_s` `{"state":"thinking","text":"On it.",…}`, then `"Still on it."` | `daemon.py:451-457` |
| Whisper still loading | `{"state":"talking","text":"<ears loading line>",…}` | `daemon.py:195` |
| Media doorways | `POST /perform {"state":"dancing"}` / `{"state":"idle"}` | `doorways/mpris_watch.py:189`, `doorways/smtc_watch.py:186` |
| Catch-up on connect | `{"state":"dancing"}` | `server.py:356-358` |
| First-run privacy note | `{"state":"talking","reaction":"wave","anim":"notify_perk","emotion":"happy","text":…}` | `server.py:411-413` |

## 4. `tempo` (brain → body)

The beat watcher posts an estimate to `POST /tempo` every 2 s; the brain validates it
(`server.py:194-234`), keeps it, and forwards it to every body wrapped in a `tempo` key
(`daemon.py:264-268`). A body that connects while an estimate is fresh (6 s) gets it at once
(`server.py:359-361`).

```json
{"tempo": {"bpm": 128.4, "period_s": 0.467, "confidence": 0.71, "next_beat": 1789935826.592,
           "evenness": 0.62, "low_ratio": 0.55, "density": 4.1, "loudness_db": -18.0, "steady": true}}
```

or `{"tempo": {"silent": true}}` when nothing plays.

| Field | Type | Range (`server.py:194-203`) | Meaning |
|---|---|---|---|
| `bpm` | number | 30–300 | tempo |
| `period_s` | number | 0.2–2.0 | seconds per beat |
| `confidence` | number | 0–1 | how sure the tracker is of the tempo |
| `next_beat` | number | 0–1e11 | **wall-clock** Unix time (s) of the next beat; the watcher uses `time.time()` less a fixed 0.05 s capture latency (`doorways/beat_watch.py:47`, `:111`) |
| `evenness` | number | 0–1 | autocorrelation at 1, 2, 4 periods; four-on-the-floor is high |
| `low_ratio` | number | 0–1 | share of energy below 150 Hz |
| `density` | number | 0–60 | onsets per second |
| `loudness_db` | number | −120–10 | the player's stream level |
| `steady` | boolean, optional | – | the tempo has held; missing means `true` (`server.py:208`, `dance_style.gd:92`) |
| `silent` | `true` | – | alone: nothing plays; all other fields absent |

The body computes the beat phase itself every frame from its own wall clock:
`phase = fposmod((now − next_beat) / period_s, 1)` (`dance_style.gd:111-112`). It treats an
estimate older than 6 s by its own receipt time as gone (`dance_style.gd:18`, `:60`, `:103`).
All fields are required except `steady`; unknown fields are refused at `/tempo`.

## 5. Commands (brain → body)

The tray posts `POST /command {"command": …, "value": …}`; the brain checks the name and the
value's type (`server.py:239-284`) and broadcasts `{"command": name[, "value": v]}`
(`server.py:302`). **No `type` field**: a body recognises it by the `command` key, which is checked
first (`widget.gd:650-652`).

```json
{"command": "volume", "value": 0.6}
```

| `command` | `value` | Widget behaviour (`widget.gd:718-767`) |
|---|---|---|
| `quit` | – | quit |
| `show` | – | show the window |
| `hide` | – | hide it; clicks fall through |
| `chat` | – | show the window and open the type box |
| `sleep_now` | – | doze off now |
| `reset_position` | – | move to the corner of the current screen |
| `skin` | string (a skin id) | change skin; unknown ids ignored |
| `mute` | boolean | mute her voice |
| `on_top` | boolean | always on top |
| `hat` | boolean | the top hat |
| `quiet` | number ≥ 0 | seconds of "quiet for a while" from now; 0 clears |
| `volume` | number 0–1 | voice volume |
| `sleep_after` | number ≥ 0 | minutes of quiet before sleep; 0 = never |

`reload_notifications` (no value) is the brain's own and is never sent to a body
(`server.py:256-258`, `:294-301`). An unknown command reaching the widget is logged and ignored
(`widget.gd:766-767`).

## 6. Messages a body sends

| Message | Fields | Brain's handling | Code |
|---|---|---|---|
| `{"type":"hello",…}` | §2.1 | version check, privacy note | `server.py:383-386` |
| `{"type":"ping"}` | – | answers `{"type":"pong"}` | `server.py:387-388` |
| `{"type":"heard","text":"…"}` | `text`: string, the sentence the user typed | trimmed; empty ignored; becomes `Event(source="voice", title=text)` and runs the same funnel as a spoken sentence, in the background. It is a foreground run (Part 1b): one at a time, and it stops the one before it unless it answers her question | `server.py:390-397`, sent by `widget.gd:411-417` |
| `{"type":"run.cancel","run_id":"…"}` | v2 only | §11c; from a v1 body it is ignored | `server.py` `_cancel_from_body` |
| `{"type":"poked","zone":"…","level":1}` | `zone`: `shell` `belly` `eye` `claw` `near`; `level`: 1 (curious) or 2 (annoyed) | from any body. The widget sends it now and then only with its *Talk when poked* setting on (off by default; WIRING.md §13, Touch); it has already reacted itself. The brain may answer with one short line written in `pokes.py` (no model, nothing in the ledger) as an ordinary `talking` performance without `anim` or `reaction`, unless a run is going on, its state is not `idle` or `dancing`, or it said one in the last 20 s. Anything else in the fields: logged at DEBUG, ignored. Not the proposed v2 `touch` (§14), which maps to bindings and never speaks | `server.py` `_poked`, sent by `touch.gd` `maybe_talk` |
| anything else | – | logged at DEBUG, ignored | `server.py:398-399` |
| not JSON | – | logged at DEBUG, ignored | `server.py:379-381` |

`heard` text is cut to 2000 characters by `Event.from_dict` (`events.py:17`, `:45-51`). The answer
to it comes back as ordinary performances; there is no reply addressed to the sender.

## 7. How a body tells the messages apart (v1)

In this order (`widget.gd` `_on_message`, `ws_client.gd` `_process`):

1. `type == "pong"` → liveness only.
2. has `type` (any other) → a v2 message (Part 1b): the widget hands it to `on_typed`. A v1 body
   never receives one.
3. has `command` → a command.
4. has `tempo` (an object) → a beat estimate.
5. has `state` → a performance.
6. anything else → ignore.

## 8. Things found in the code that a body author should know

These are v1 as it stands; some are gaps, noted for fixing. The numbers stay when one is fixed.

1. *Fixed 2026-09-25.* `/health` did not check `Origin`, though it holds the ledger (the user's
   recent sentences and her replies). Every route checks it now, in one middleware (§1.2), and
   `tests/test_server.py` asks each route with an `Origin`.
2. `/listen` and `/probe` do not require the JSON content type (`server.py:307-327`). They take no
   body, so this is harmless; WIRING §2 now says the rule is for POSTs with a body.
3. **No message from the brain has a `type` except `pong`.** Bodies dispatch on which key is
   present (§7). Any new message that carries a top-level `state`, `command` or `tempo` key would be
   misread by today's widget.
4. **Catch-up messages go out before the hello** (`server.py:356-361`), so the brain cannot tailor
   them to the body; and every socket gets every broadcast whether it said hello or not.
5. `hello.client` and `hello.godot` are logged and never used (`server.py:384`).
6. `emotion` is always on the wire (`contract.py:55`), though WIRING §1 lists it as optional; the
   catch-up `{"state": …}` has none. WIRING §1 says emotion "tints bubble / picks `anim`"; the widget
   only tints; the brain picks `anim` (`contract.py:28-33`, `reactions.py:68-72`).
7. `audio` and `icon` are paths on the brain's disk. A body in a sandbox, a container or on another
   machine cannot open them, and a slow body can find the wav already deleted (§3.2).
8. With two bodies connected, each gets the same `audio` and both would play it (`hub.py:57-71`):
   the voice would be doubled.
9. `next_beat` is wall-clock time, which steps when the system clock is corrected; the only latency
   handled is a fixed 0.05 s capture latency in the watcher (`doorways/beat_watch.py:47`). The sound
   card's output latency is not reported.
10. *Fixed 2026-09-25.* Any performance without `audio` stopped the wav that was playing, so a
    `{"state": "dancing"}` from the media doorway arriving mid-line cut her voice off while the
    bubble carried on. Only a new line or `listening` stops it now (§3.2);
    `widget/validate_widget.gd` checks it against a real daemon.
11. *Fixed 2026-09-25.* The typed `heard` text was written to the journal in full at INFO, and the
    gate, the reflexes, the thinker and whisper logged the sentence too. With `[daemon]
    log_sentences = false`, the default, they log only its length (`<sentence, 23 chars>`); on,
    the sentence as before (WIRING §15).


---

# Part 1b — Protocol v2, as built (runs)

Built in brain step 6, stage 1. A body that says `protocol: 2` in its hello gets what it asks for
from this part; a body that does not is a v1 body and gets exactly the Part 1 traffic, byte for
byte (`tests/test_runs.py` `test_v1_bodies_get_exactly_the_bytes_they_always_did`, recorded from the
code before runs existed). The crab widget speaks v2 from this stage on (`ws_client.gd` `hello`).
What stays proposed is in Part 2.

## 9b. The v2 hello and `welcome`

```json
{"type": "hello", "client": "strawberry-widget", "version": "0.2.0", "godot": "4.7.2-stable (official)",
 "protocol": 2, "body": {"id": "crab", "name": "Strawberry"},
 "capabilities": {"phases": ["routing", "thinking", "tool", "speaking", "run"],
                  "sends": {"heard": true, "cancel": true}}}
```

The v1 fields and the version check of §2.1 stay as they are. Of the v2 fields the brain reads
(`hub.py` `body_from_hello`):

| Field | Meaning |
|---|---|
| `protocol` | 2, or absent / below 2 for v1. A higher number is served as 2 |
| `body.id`, `body.name` | strings, at most 64 printable characters; for logs, `/health` and `welcome` |
| `capabilities.phases` | the run event families it wants: `routing`, `thinking`, `tool` (both tool events), `speaking`, `run` (the terminal events). Others (`listening`, `subagent`, `token_rate`) are not produced yet and are not accepted |
| `capabilities.sends.cancel` | `true`: it may send `run.cancel` (§11b) |

Every other capability of §10 is accepted and ignored for now. The brain answers a v2 hello that is
not refused (§2.1) with `welcome` (`server.py` `welcome`), on that socket only:

```json
{"type": "welcome", "protocol": 2, "brain": "0.2.0", "t": 81234.512, "rest_state": "idle",
 "accepted": {"phases": ["routing", "run", "speaking", "thinking", "tool"], "cancel": true}, "body_id": "crab"}
```

`t` is brain monotonic time; `accepted` is what this body will get and may send. The v1 catch-up
(§2 step 2) still goes out at connect, before the hello. `/health` lists each open socket under
`bodies` (`protocol`, and for v2 `id`, `phases`, `cancel`) and the run book under `runs`.

## 10b. Runs

Every input the brain handles is a **run** (`runs.py`): a sentence the user says or types
(`source` `voice` or `typed`; one at a time, the *foreground* run) or a notification it reacts to
(`notification`, never foreground, never stopped). `job` is reserved. A run goes through

```
routing → thinking ⇄ tool → (awaiting_approval, stage 2) → speaking → completed | failed | cancelled
```

and each step is one event. `run_id` is `r-<n>`, counting from 1 at each daemon start.

## 11b. Run events (brain → body)

Every event has `type`, `run_id`, `seq` (1, 2, … within the run, no gaps) and `t` (brain monotonic
seconds), and only the fields below (`runs.py` `FIELDS`, checked in `RunBook.emit`): anything else
is dropped before it reaches a sink, numbers are rounded to 3 decimals, strings are cut to 64
characters, and the fixed codes accept nothing outside their list. A v2 body gets the families it
accepted; a v1 body gets none.

| `type` | Fields | When (code) |
|---|---|---|
| `routing` | `kind`, `topic`, `decision`, `path` (`reflex` `escalate` `fixed` `chat`), `tool` (only with `reflex`: the gate's option, e.g. `skip`), `confidence`, `ms` | once the path is known: a reflex as it starts, the thinker before `thinking`, a fixed line, chat (`daemon.py` `_routing`). Not sent when the gate is off or did not answer, nor for a yes or no to her question, nor for "stop" |
| `thinking` | `backend` (`builtin`), `model` | the thinker starts (`Thinker.run`) |
| `tool.started` | `call_id` (`c1`, `c2`, … within the run), `tool` (`server.tool`; a reflex's is `server.option`, e.g. `spotify.skip`), `label`, `careful` | before each call, reflex (`Actor.act` `on_call`) or thinker (`Thinker._call`) |
| `tool.completed` | `call_id`, `tool`, `duration`, `ok`, `error` (only when not ok: `timeout` `refused` `failed` `unavailable`) | after it. A call the thinker refuses (not offered, over a limit, a guard) is one `tool.completed` with `error: "refused"` and no `tool.started`; a made-up tool name is sent as `unknown` |
| `speaking` | `duration` (the wav's length, or the bubble's reading pace when silent), `emotion` | the performance that answers the run goes out (`Daemon.perform`), after the run's earlier events |
| `run.completed` | `duration`, `outcome` (`spoken` `nothing`) | terminal |
| `run.failed` | `duration`, `error` (`timeout` `backend` `tools` `other`) | terminal: the thinker timed out or could not be reached, a reflex or a confirmed call failed, or an error. She has usually said so |
| `run.cancelled` | `duration`, `reason` (`superseded` `stopped` `shutdown`) | terminal: a newer sentence replaced it, the user stopped it, or the daemon is stopping |

`label` is how the widget's chip names the step: the adapter's own words for the tool
(`searching the web…`), else the server's title and the tool's name (`Spotify: next`,
`Player: pause` for MPRIS). It is written by code, never from an argument.

**Exactly one terminal event ends each run** (`RunBook.finish`, idempotent, called in a `finally`).
A run carries at most 200 events; past that only its terminal event goes out. A body that has
seen nothing of a run for 60 s treats it as over.

**Never on the bus** (§14.1, enforced by the whitelist): tool arguments, tool results, the user's
sentence, her line, prompts, model output, server error messages. Her line still goes out as the
performance's `text`, as in v1.

The performance that answers a run carries `run_id` as an extra field, for v2 bodies only.

Examples (a reflex, then a thinker run):

```json
{"type": "routing", "run_id": "r-1", "seq": 1, "t": 1496.23, "kind": "request", "topic": "music", "decision": "act", "path": "reflex", "tool": "skip", "confidence": 0.989, "ms": 119.324}
{"type": "tool.started", "run_id": "r-1", "seq": 2, "t": 1496.23, "call_id": "c1", "tool": "spotify.skip", "label": "Spotify: skip", "careful": false}
{"type": "tool.completed", "run_id": "r-1", "seq": 3, "t": 1496.852, "call_id": "c1", "tool": "spotify.skip", "duration": 0.623, "ok": true}
{"type": "speaking", "run_id": "r-1", "seq": 4, "t": 1496.853, "duration": 2.69, "emotion": "happy"}
{"type": "run.completed", "run_id": "r-1", "seq": 5, "t": 1496.853, "duration": 0.743, "outcome": "spoken"}

{"type": "routing", "run_id": "r-2", "seq": 1, "t": 1442.728, "kind": "request", "topic": "music", "decision": "act", "path": "escalate", "confidence": 0.991, "ms": 120.694}
{"type": "thinking", "run_id": "r-2", "seq": 2, "t": 1442.73, "backend": "builtin", "model": "qwen3.8:27b"}
{"type": "tool.started", "run_id": "r-2", "seq": 3, "t": 1445.925, "call_id": "c1", "tool": "spotify.search", "label": "Spotify: search", "careful": false}
{"type": "tool.completed", "run_id": "r-2", "seq": 4, "t": 1445.928, "call_id": "c1", "tool": "spotify.search", "duration": 0.003, "ok": true}
{"type": "tool.started", "run_id": "r-2", "seq": 5, "t": 1447.889, "call_id": "c2", "tool": "spotify.play", "label": "Spotify: play", "careful": false}
{"type": "tool.completed", "run_id": "r-2", "seq": 6, "t": 1447.892, "call_id": "c2", "tool": "spotify.play", "duration": 0.003, "ok": true}
{"type": "speaking", "run_id": "r-2", "seq": 7, "t": 1448.959, "duration": 4.835, "emotion": "happy"}
{"type": "run.completed", "run_id": "r-2", "seq": 8, "t": 1448.959, "duration": 6.352, "outcome": "spoken"}
```

A notification is `speaking → run.completed`.

## 11c. Stopping a run (`run.cancel`, body → brain)

```json
{"type": "run.cancel", "run_id": "r-2"}
```

Taken only from a v2 body whose welcome said `cancel: true`, and only for the foreground run going
on now (`server.py` `_cancel_from_body`). Anything else is ignored and logged; a v2 body that may
cancel but named another run gets

```json
{"type": "input.refused", "ref": "r-2", "reason": "not_current"}
```

A cancel does nothing but stop that run: no line, no new run. The run ends with
`run.cancelled` (`reason: "stopped"`) and she goes back to her resting state. A call that only
reads (a search, a web page, a tool the server marks `readOnlyHint`, or one its adapter lists in
`reads`) is dropped at once. A call that may change something is let finish first
(`asyncio.shield` in `Thinker._call` and `Actor.act`): its `tool.completed` comes, she says
"Stopped, but <label> had already gone through." (`speaking`), and then `run.cancelled`.

The same stop comes from the Brain UI's Cancel (WIRING §17), from a whole-sentence "stop",
"cancel that" or "never mind" while a run is busy (`runs.is_stop`; answered "Okay, stopped." as a
run of its own), from a newer sentence (`superseded`, `[runs] supersede`), and from the daemon
stopping (`shutdown`). A yes or no to her question never stops anything: it is the answer.

## 12b. The clock in ping and pong

A v2 body may put its own monotonic time in `ping.t`; the brain echoes it and adds its own
(`server.py` `_on_widget_message`):

```json
{"type": "ping", "t": 1532.004}
{"type": "pong", "t": 1532.004, "brain_t": 81250.117}
```

A ping without a number in `t` gets the v1 `{"type": "pong"}`. The mapping is §12.1's.

---

# Part 2 — PROPOSED: protocol v2

The rest of protocol v2 is a design, not code; what is built is in Part 1b, and the sections below
say where they overlap. A v1 body keeps working unchanged: the brain sends it exactly the v1
traffic of Part 1 and nothing else (§15).

## 9. PROPOSED (v2): conventions

- **Every v2 message has a `type`.** Names are lowercase; families use a dot (`tool.started`,
  `approval.request`).
- **No v2 message has a top-level `state`, `command` or `tempo` key**, so a v1 body that receives
  one by mistake ignores it (§7).
- `t` fields are **brain monotonic time** in seconds (Python `time.monotonic()`), mapped to the
  body's clock by §12.1. Wall-clock time is used only where v1 already uses it.
- `run_id` is a short opaque string, one per handled input (a spoken or typed sentence, a
  notification, a gesture). Every phase message of that handling carries it, and so does the
  performance that answers it (as an extra field, v2 bodies only).
- Durations are seconds as numbers (`duration`, matching Hermes), levels and confidences are 0–1.
- Optional fields are omitted, never `null`, as in v1.

## 10. PROPOSED (v2), partly built: the hello with capabilities, and `welcome`

*Built:* `protocol`, `body`, `capabilities.phases`, `capabilities.sends.cancel` and `welcome`
(§9b). The rest of this section is proposed.

A v2 body adds `protocol`, `body` and `capabilities` to the v1 hello. Old fields stay, so the
version check of §2.1 still applies.

```json
{"type": "hello", "client": "orbs", "version": "0.1.0", "protocol": 2,
 "body": {"id": "orbs-7c1e", "name": "Orbs"},
 "capabilities": {
   "performs": {"states": ["idle", "listening", "thinking", "talking", "dancing"],
                "anims": [], "reactions": ["nod"], "emotions": ["neutral", "happy", "alert", "angry"],
                "icon": false, "hop": false},
   "speech": {"bubble": true, "audio": "file", "primary": false},
   "phases": ["listening", "routing", "thinking", "tool", "subagent", "token_rate", "speaking", "run"],
   "beat": true,
   "entities": [
     {"id": "music", "kind": "orb", "label": "the music orb", "topic": "music"},
     {"id": "calendar", "kind": "orb", "label": "the calendar orb", "topic": "calendar"}
   ],
   "displays": [{"id": "edge", "primary": false}],
   "commands": ["show", "hide", "mute", "volume", "quit"],
   "sends": {"heard": true, "touch": ["tap", "double_tap", "hold"],
             "gestures": ["point", "flick", "circle", "nod", "shake"],
             "gesture_targets": true, "point": true,
             "approval": ["click", "gesture"]},
   "approvals": true
 }}
```

| Field | Type | Meaning |
|---|---|---|
| `protocol` | integer | highest protocol version the body speaks. Absent = 1 |
| `body.id` | string | stable per install (not per connection); used in `by` fields and logs. Must not contain user data |
| `body.name` | string | for logs and `/health` |
| `capabilities.performs` | object | which performance values it can show. Lists name the values; `icon`/`hop` booleans. Absent = it performs nothing (an input-only body, a light strip) |
| `capabilities.speech.bubble` | boolean | shows `text` |
| `capabilities.speech.audio` | `"file"` \| `"url"` \| `"none"` | how it can play speech: a path on the shared disk (v1), a URL on the brain's port (§10.2), or not at all |
| `capabilities.speech.primary` | boolean | asks to be the one body that plays the voice (§10.3) |
| `capabilities.phases` | list | which phase families of §11 it wants: `listening`, `routing`, `thinking`, `tool` (both tool events), `subagent`, `token_rate`, `speaking`, `run` (the terminal events) |
| `capabilities.beat` | boolean | wants the `beat` stream of §12 instead of v1 `tempo` |
| `capabilities.entities` | list | the separate things it draws. `id` (unique within the body), `kind` (free text: `orb`, `crab`), `label` (how the brain may name it aloud), `topic` (optional: one of the gate's topics `music` `calendar` `notes` `system` `other`, which ties the entity to what it stands for) |
| `capabilities.displays` | list | the screens it draws on: `id`, `primary` |
| `capabilities.commands` | list | which §5 commands it acts on |
| `capabilities.sends` | object | the input it can produce (§14): `heard`, the `touch` actions, the `gestures`, whether a gesture can name a target entity (`gesture_targets`), `point`, and which ways it can answer an approval (`click`, `gesture`) |
| `capabilities.approvals` | boolean | shows approval requests (§13) |

The brain answers every v2 hello with `welcome`, then the catch-up for that body:

```json
{"type": "welcome", "protocol": 2, "brain": "0.3.0", "body_id": "orbs-7c1e",
 "t": 81234.512, "rest_state": "dancing", "voice": "orbs-7c1e",
 "accepted": {"phases": ["listening", "routing", "thinking", "tool", "run"], "beat": true}}
```

| Field | Meaning |
|---|---|
| `protocol` | the version both will speak: `min(body, brain)` |
| `brain` | the brain's package version |
| `t` | brain monotonic time now (a first clock sample, §12.1) |
| `rest_state` | `idle` \| `dancing` |
| `voice` | the `body.id` that plays the voice now (§10.3), or `""` for none |
| `accepted` | the capabilities the brain will serve (a subset of what was asked: a brain without a gate never sends `routing`) |

Then, for that body only: the current `beat` if fresh, and every approval still open (§13). The v1
pre-hello catch-up (§2 step 2) is still sent at connect to every socket, because a v1 body
depends on it; a v2 body ignores anything before `welcome` except `pong`.

### 10.1 What the brain does with capabilities

The brain keeps the union of the capabilities of the bodies connected now and consults it before
it offers anything the user would have to see or do on a body:

- The thinker's situation line lists what is on screen: "On screen: the crab; the music orb and
  the calendar orb (can be pointed at)." Only declared entities with a `label` are named, so the
  brain never says "point at the music orb" when no body has `gesture_targets` and an entity with
  `topic = "music"`.
- An approval prompt says "say yes, or tap it" only if some body declared `sends.approval`
  containing `click`; otherwise "say yes or no".
- A performance field is sent to a body only if it declared the value: a `reaction` the body did
  not list is dropped for that body, `anim` likewise; `state` is always sent (every body must
  accept all five, even if it draws some the same).
- Phase messages go only to bodies that asked for them.

### 10.2 Audio by URL

A body with `speech.audio = "url"` gets `audio_url` instead of `audio`:

```json
{"state": "talking", "emotion": "neutral", "text": "Skipped.", "audio_url": "/audio/line-000042.wav",
 "duration": 0.84, "run_id": "r-19"}
```

`GET /audio/<name>.wav` on the brain's port serves the wav (same Origin rule, loopback only, only
files in the speech directory, only the last `keep_files`). `duration` (seconds) is added to every
v2 performance with speech, audio or not: the wav's length, or the text-length heuristic of §3.2
when silent, so every body times its bubble the same way.

### 10.3 One voice

With several bodies, exactly one plays the wav: the most recent body that asked for
`speech.primary`, else the oldest body with `speech.audio` other than `none`. The others get the
performance without `audio`/`audio_url` but with `duration`, and must not stop anything because of
it. This fixes point 8 of §8 for v2 bodies (point 10 is fixed for v1 already). When the voice
body disconnects the next one takes over; `welcome.voice` and a
`{"type": "voice", "body_id": "…"}` message say who has it.

### 10.4 Entities and displays in performances

A v2 performance may carry `entity` (an id the body declared) and `display` (a display id). The
body that owns that entity performs it; other bodies apply only `state`. Absent means the body's
main entity. The brain chooses the entity from the event's topic: a track change goes to the entity
with `topic = "music"` if there is one.

## 11. PROPOSED (v2), partly built: agent phases

*Built:* `routing`, `thinking`, `tool.started`, `tool.completed`, `speaking` and the three terminal
events, as §11b says (with `label` on `tool.started`, `shutdown` as a cancel reason and `fixed`
paths). Proposed still: `listening`, `subagent.*`, `token_rate`, `outcome: "silent"` and the
`didnt_catch` reason.

What the brain is doing, as it happens, so a body can show listening, deciding, thinking and tool
use without waiting for the reply. These do not replace performances: the crab still gets its
`listening`/`thinking` states as today. Names follow the Hermes Agent Runs API SSE stream where
the two overlap (`tool.started`, `tool.completed`, `subagent.start`, `subagent.complete`,
`run.completed`, `run.failed`, `run.cancelled`), so a run on a Hermes backend and a run on the
builtin thinker look the same to a body. Hermes fields that can carry private data (`preview` of
arguments and results, `summary`, message text) are dropped by the brain before anything reaches
the bus (§14.1).

Every phase message has `type`, `run_id`, `seq` (1, 2, … within the run) and `t`.

A typical spoken request:

```
listening (level…) → routing → thinking → tool.started → tool.completed → speaking → run.completed
```

A reflex ("skip this"): `listening → routing (path reflex) → tool.started → tool.completed →
speaking → run.completed`. A notification: `speaking → run.completed`.

| `type` | Fields | When |
|---|---|---|
| `listening` | `level` 0–1, `speech` boolean (the level is above the speech threshold) | at the start of a voice session with `level` 0, then once per 0.1 s chunk while recording (the recorder's chunk, `voice.py:201-232`), so at most 10 per second. The level is the chunk's dB above the noise floor, 0 at the floor and 1 at floor + 36 dB. No audio, no transcript |
| `routing` | `kind` (`request` `question` `chat` `other`), `topic` (`music` `calendar` `notes` `system` `other`), `decision` (`chat` `offer` `act`), `path` (`reflex` `escalate` `fixed` `chat`), `tool` (the reflex option, e.g. `skip`; only with `path = "reflex"`), `confidence` 0–1 (of `kind`), `ms` | once, when the gate (System One) has read the sentence. `escalate` = handed to System Two (the thinker); `fixed` = a canned line such as "needs a music add-on"; `chat` = the small model answers. Not sent when the gate is off |
| `thinking` | `backend` (`builtin` \| `hermes` \| a configured name), `model` (optional, the model name as configured) | when System Two starts |
| `tool.started` | `call_id`, `tool` (`server.tool`, e.g. `spotify.next`), `careful` boolean (a tool from the server's `careful` list) | before each tool call, reflex or thinker |
| `tool.completed` | `call_id`, `tool`, `duration`, `ok` boolean, `error` (only when not ok: `timeout` \| `refused` \| `failed` \| `unavailable`) | after it. `error` is a fixed code, never the tool's message |
| `subagent.start` | `subagent_id` (Hermes `delegation_id`, or the brain's own id) | a backend delegates |
| `subagent.complete` | `subagent_id`, `status` (`ok` `failed` `cancelled`), `duration` | it returns |
| `token_rate` | `tokens_per_s`, `tokens` (so far in this run) | optional; at most 4 per second while System Two writes. Derived from Hermes `message.delta` or Ollama's stream by counting; the text is never forwarded |
| `speaking` | `duration`, `emotion`, `entity` (optional) | as the answering performance goes out. For bodies that show the brain's activity but not its words |
| `run.completed` | `duration` (whole run), `outcome` (`spoken` `silent` `nothing`) | terminal, success |
| `run.failed` | `duration`, `error` (`timeout` `backend` `tools` `other`) | terminal, the run ended in an error (she may still have said so) |
| `run.cancelled` | `duration`, `reason` (`superseded` `stopped` `didnt_catch`) | terminal: a newer input replaced it, the user stopped it, or nothing was heard |

Exactly one terminal event ends each run. A body that has not seen one 60 s after the run's last
message treats the run as over.

Examples:

```json
{"type": "listening", "run_id": "r-19", "seq": 3, "t": 81240.10, "level": 0.62, "speech": true}
{"type": "routing", "run_id": "r-19", "seq": 9, "t": 81241.73, "kind": "request", "topic": "music",
 "decision": "act", "path": "escalate", "confidence": 0.91, "ms": 142.0}
{"type": "thinking", "run_id": "r-19", "seq": 10, "t": 81241.74, "backend": "builtin", "model": "qwen3.8:27b"}
{"type": "tool.started", "run_id": "r-19", "seq": 11, "t": 81243.02, "call_id": "c1", "tool": "spotify.search", "careful": false}
{"type": "tool.completed", "run_id": "r-19", "seq": 12, "t": 81243.31, "call_id": "c1", "tool": "spotify.search", "duration": 0.29, "ok": true}
{"type": "token_rate", "run_id": "r-19", "seq": 15, "t": 81244.40, "tokens_per_s": 31.5, "tokens": 12}
{"type": "speaking", "run_id": "r-19", "seq": 16, "t": 81244.90, "duration": 2.1, "emotion": "happy"}
{"type": "run.completed", "run_id": "r-19", "seq": 17, "t": 81244.91, "duration": 4.8, "outcome": "spoken"}
```

## 12. PROPOSED (v2): the beat phase stream

v1 bodies keep getting `tempo` (§4). A body with `capabilities.beat` gets `beat` instead:

```json
{"type": "beat", "seq": 412, "t": 81250.004,
 "bpm": 128.4, "period_s": 0.4673, "phase": 0.31, "confidence": 0.71, "steady": true,
 "next_beat_t": 81250.326, "next_beat": 1789935826.592, "output_latency_s": 0.042,
 "features": {"evenness": 0.62, "low_ratio": 0.55, "density": 4.1, "loudness_db": -18.0}}
```

| Field | Type | Meaning |
|---|---|---|
| `seq` | integer | increases per estimate |
| `t` | number | brain monotonic time at which `phase` holds |
| `bpm`, `period_s` | number | as v1 |
| `phase` | 0–1 | where in the beat the **music as captured** is at `t`; 0 = on the beat |
| `confidence` | 0–1 | as v1 |
| `steady` | boolean | as v1, but always present |
| `next_beat_t` | number | brain monotonic time of the next beat as captured (`t + (1 − phase) × period_s`) |
| `next_beat` | number | the same in wall-clock time, as in v1, for a body that has not synced clocks yet |
| `output_latency_s` | number ≥ 0 | how long after capture the sound comes out of the speakers: the output sink's latency, read by the capture (PipeWire: the sink node's reported latency; WASAPI: the render endpoint's stream latency). 0 when unknown |
| `features` | object | `evenness`, `low_ratio`, `density`, `loudness_db`, as v1 |
| `silent` | `true` | alone with `type`, `seq`, `t`: nothing plays |

Sent every 2 s while music plays (the watcher's rate), and once on `welcome` if fresh.

**Hook for build-ups and drops.** Reserved, not produced yet (WIRING §4c: the detector is not
built):

```json
{"type": "beat.event", "seq": 413, "t": 81262.1, "kind": "drop", "at_t": 81264.0, "confidence": 0.6}
```

`kind`: `buildup` (energy rising over bars; `at_t` is when it started), `drop` (`at_t` is the
predicted or detected downbeat), `breakdown`, `section`. Bodies ignore kinds they do not know.
A later `bar` object on `beat` (`beats_per_bar`, `beat_in_bar`) is reserved the same way.

### 12.1 Clock mapping (the ping and pong half is built, §12b)

Brain and body clocks differ. v2 extends ping/pong, which v1 already sends every 5 s:

```json
{"type": "ping", "t": 1532.004}
{"type": "pong", "t": 1532.004, "brain_t": 81250.117}
```

The body puts its own monotonic time in `t`; the brain echoes it and adds `brain_t`. When the pong
arrives at body time `r`: `rtt = r − t`, `offset = brain_t − (t + r) / 2`. Keep the offset of the
sample with the smallest `rtt` among the last 8 (on localhost `rtt` is well under a millisecond).
Brain time now = body time + `offset`. A v1 ping without `t` gets the v1 pong; adding `brain_t` to
every pong is harmless to v1, which drops pongs unread (`ws_client.gd:85-86`). `welcome.t` is a
first rough sample.

### 12.2 Predicting beats on the body

1. On each `beat`: `b0 = next_beat_t − offset` (the next captured beat, in body time).
2. Every frame at body time `now`: `phase = fposmod((now − b0) / period_s, 1)`; beat `k` falls at
   `b0 + k × period_s`. Nothing is needed from the brain between estimates.
3. When a new estimate moves the grid, do not jump: slew the displayed phase towards the new one
   over half a beat, unless the error is over 0.25 beat (then snap on the next beat).
4. `steady = false` or `confidence < 0.3`: move freely, do not lock to the beat (the crab sways).
5. No `beat` for 6 s, or `silent`: stop beat-locked motion.

### 12.3 Latency

Three delays sit between the music and the picture, and each side handles its own:

| Delay | Who knows it | Handling |
|---|---|---|
| capture (the watcher's buffer) | the brain | folded into `phase`, `next_beat_t` before sending (v1 does this with a fixed 0.05 s) |
| output (sink to speaker, Bluetooth can be 150–250 ms) | the brain, from the sink | reported as `output_latency_s` |
| display (render, compositor, monitor) | the body | the body's own estimate: at least one frame interval |

The body shows beat `k` at `b0 + k × period_s + output_latency_s − display_latency_s`, so the
visual lands when the sound is heard. Speech the body plays itself is the body's own business
(Godot: `AudioServer.get_output_latency()`).

## 13. PROPOSED (v2): confirmations

For an action that needs a yes or no before it happens ("Send these?"). Hermes calls the same
thing `approval.request`, so the name is shared. The brain asks by voice as usual (a performance
with the question) **and** broadcasts the request to bodies with `capabilities.approvals`:

```json
{"type": "approval.request", "approval_id": "a-7", "run_id": "r-22", "t": 81300.0,
 "prompt": "Send these 3 messages?", "risk": "sends", "timeout_s": 30, "expires_t": 81330.0,
 "accepts": ["voice", "click", "gesture"]}
```

| Field | Meaning |
|---|---|
| `approval_id` | unique, opaque |
| `prompt` | display text: what is being approved, written by the brain for showing. It follows §14.1 like any `text`: no tool arguments pasted in, no message bodies |
| `risk` | `low` (reversible, stays on this machine: "save this song"), `sends` (something leaves the machine or reaches other people), `destructive` (deletes or cannot be undone) |
| `timeout_s`, `expires_t` | how long it stays open; brain monotonic expiry |
| `accepts` | the ways an answer is taken for this request |

A body answers:

```json
{"type": "approval.answer", "approval_id": "a-7", "answer": "yes", "via": "click", "entity": "music"}
```

`answer`: `yes` \| `no`; `via`: `click` \| `touch` \| `gesture`; `entity` optional. Voice answers
never come from a body: the brain listens after asking, and its own gate reads yes or no.

The brain decides and tells every approvals body, the answering one included:

```json
{"type": "approval.resolved", "approval_id": "a-7", "answer": "yes", "via": "click", "by": "orbs-7c1e"}
```

`answer`: `yes` \| `no` \| `timeout` \| `cancelled`; `by`: a body id, `voice`, or `brain`.

Rules:

1. **First answer wins.** Any later answer to a resolved id is ignored; the body already has
   `approval.resolved` and must hide the request on it.
2. **Timeout is no.** Nothing happens; `answer: "timeout"`.
3. **`destructive` refuses gesture yes.** For `risk = "destructive"`, `accepts` never contains
   `gesture`, and a `yes` with `via: "gesture"` is refused with `{"type": "input.refused",
   "ref": "a-7", "reason": "gesture_not_accepted"}` and the request stays open. A gesture **no** is
   always accepted, at any tier: declining is safe.
4. `sends` and `destructive` are never auto-approved, and there is no "always allow".
5. A body that connects while a request is open gets it after `welcome`.
6. An input that supersedes the run (the user says something else) cancels its open approvals.

## 14. PROPOSED (v2): input from bodies

(`run.cancel` is built: §11c.)

`heard` stays as in v1. New, each only from a body that declared it in `sends`:

```json
{"type": "touch", "entity": "music", "action": "tap", "display": "edge"}
{"type": "gesture", "name": "flick", "entity": "music", "confidence": 0.8}
{"type": "point", "entity": "calendar"}
{"type": "point", "entity": null}
```

| `type` | Fields | Brain's handling |
|---|---|---|
| `heard` | `text` | as v1 (§6) |
| `touch` | `entity` (declared id, or absent for the body as a whole), `action` (`tap` `double_tap` `hold` `release`), `display` optional | a discrete event. The brain maps it through `[bodies.bindings]` in the config (e.g. `music.double_tap = "skip"` runs the MPRIS reflex); unbound touches only reset her sleep timer. A touch never reaches a model |
| `gesture` | `name` (one the body declared), `entity` (target, only with `gesture_targets`), `confidence` 0–1 optional | the same bindings; a gesture on an open approval's entity is not an answer unless sent as `approval.answer` |
| `point` | `entity` or `null` | sets what the user is pointing at. For the next 10 s (or until another `point`), the thinker's situation line gets "The user is pointing at the calendar orb." (the entity's `label`), so "what's this?" and "turn this down" mean something. `null` clears it |
| `approval.answer` | §13 | §13 |

Coordinates are not sent: a body resolves what was touched into an entity itself. The brain drops
input from a body above 20 messages per second, and any `entity` the body did not declare.

## 14.1 PROPOSED (v2): privacy rules of the bus

The bus is localhost only, and any local process that does not send `Origin` can open `/ws` and
read everything on it. So the rule is: **only what a body needs to perform goes on the bus.**

Never on the bus, in any message, v1 or v2:

- **Notification bodies.** The watcher does not send a body in `off` mode and the brain drops one
  that arrives (WIRING §4). In `react` and `glance` modes a line derived from the body (her
  reaction, or the gist) reaches `text`, because the user chose that mode; the body text itself
  never does, and a body flagged sensitive yields only "`<app>` sent something private." A v2
  `prompt` in an approval follows the same modes.
- **Tool arguments and tool results.** Tool events carry the tool's name, timing and a fixed
  error code only. Hermes `preview` fields are dropped.
- **Prompts**: the system prompt, the situation line, the ledger context, the gate's inputs, and
  the user's transcript. The typed `heard` line goes body → brain only and is never echoed to other
  bodies.
- **Model output before it is her line**: reasoning, `message.delta` text, subagent goals and
  summaries, tokens and costs.
- **Paths outside the brain's speech directory and the app icons**, and anything from MCP server
  errors.

What is on it: states, her spoken lines (`text`), wav paths or URLs, app icons, beat features,
phase names with gate categories and timings, approval prompts written for display, and the
names of entities and tools.

## 15. PROPOSED (v2): versioning and compatibility

- **Protocol version** is an integer, separate from the package version. v1 = Part 1. A hello
  without `protocol` is v1. The brain speaks `min(body, brain)`. A protocol mismatch is never a
  refusal; the package-major refusal (`4001`, §2.1) stays as it is.
- **A body without capabilities is today's crab.** The brain treats it as v1 with this profile:
  performs all five states, both anims, all five reactions, all four emotions, `icon`, `hop`;
  bubble and `audio: "file"`; all §5 commands; sends `heard`; no phases, no `beat` (gets `tempo`),
  no entities, no approvals UI, no touch or gestures. It gets exactly the Part 1 traffic, byte for
  byte, including the pre-hello catch-up.
- **The brain sends v2 messages only to v2 bodies**, per socket, and only the families the body
  accepted in `welcome`. The hub stops being a plain broadcast: each socket has its protocol and
  capabilities, and each message is filtered for it.
- **Bodies ignore what they do not know**: unknown `type`s, unknown fields, unknown enum values in
  families they know (an unknown `state` falls back to `idle` as v1 does, an unknown `reaction` or
  `anim` is skipped). A body must never close the socket over an unknown message.
- **The brain ignores what it does not know**: unknown body message types are logged at DEBUG and
  dropped, as v1 does (`server.py:398-399`); unknown fields in known types are ignored.
- **Adding** an optional field or an enum value is not a version change; bodies must already
  ignore it. **Adding** a message family is announced in capabilities, not by a version bump.
  **Changing or removing** a field's meaning, or anything a v1 body relies on (the keys of §7, the
  catch-up, the wav-path `audio`), is a protocol version bump, and the brain keeps serving the old
  version to bodies that declare it.
- The v2 hello keeps `version` so the package-major check works for every body. A body that is not
  released with the brain (the orbs, in another repo) says its own version and should say `dev`
  until the brain learns to key the check by `client`, or it will be refused whenever the majors
  differ.
