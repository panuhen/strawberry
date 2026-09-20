# Strawberry — Harness & Wiring Spec

**Target:** the glue that makes the finished crab react. OS notifications, git events, and voice all flow through one backend into a Godot desktop widget, which speaks (TTS), listens (STT), and shows a speech bubble the local model writes.

**For:** an agent (Claude Code / Codex) and the human running it.

**Assumes done:** `v2/strawberry_v2.glb` — rig, seven clips, morphs, cel shader, verified in Godot 4.7.2 (see `v2/README.md`). This spec references those names in §9; they must match.

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

Optional fields are omitted on the wire, never sent as `null`. Unknown fields, unknown states, and missing audio files are rejected by the daemon with a 400 and a reason.

**Godot's behaviour on receipt:** apply `state` (crossfade to its loop); if `anim` present, fire it as a one-shot and resume the state's loop when it ends; if `text`, show the bubble; if `audio`, play it through the analysed bus (§6). When speech/bubble finishes and the state was `talking`, **auto-return to her resting state**: `dancing` if she was dancing before the line, otherwise `idle`. The daemon doesn't send a follow-up. `listening`/`thinking` are pipeline transients and are not remembered; `dancing` persists until the next `idle`.

**Transport:** the **daemon is the websocket server**, Godot is a **client** that connects out and auto-reconnects with backoff (1 s doubling to 8 s). localhost only. One port carries both HTTP and the websocket (`/ws`).

**Liveness, both ends.** On shutdown the daemon closes every widget socket with `1001 going away` and exits within 2 s, so a restart is noticed immediately (aiohttp would otherwise hold the handler for up to 60 s and the widget would sit on a dead socket). The widget also sends `{"type":"ping"}` every 5 s, gets `{"type":"pong"}`, and drops and reconnects after 12 s of silence, covering a half-open socket after sleep/wake. `scripts/check_reconnect.sh` (and `SIGNAL=KILL …`) proves both paths: drop noticed at 0.0 s, reconnected in 2.6 s.

Source of truth: `strawberryd/strawberryd/contract.py` and the constants at the top of `widget/widget.gd`.

---

## 2. Backend daemon — `strawberryd`

Long-running Python (asyncio, aiohttp). One port, default **8770**:

- `POST /event` — accepts `{source, app, title, body, urgency}`. This is what the hooks hit. `source` ∈ `notification|git|voice|manual`; `urgency` is normalised from dunst's `LOW|NORMAL|CRITICAL`.
- `POST /perform` — accepts a raw contract blob. For `curl`, tests, and callers that already know what they want.
- `GET /health` — `{ok, widgets, performed, uptime_s, brain: {model, loaded, calls, fallbacks, last_latency_s}}`.
- `GET /config` — the effective settings (§15).
- `GET /ws` — the widget connects here.
- **Core function:** `Daemon.perform(performance)` builds the blob, (Phase 4) runs TTS, and sends to Godot. Everything routes through it.

Emotion → animation is **owned by the daemon**, so the model never has to name a clip:

| emotion | anim |
|---|---|
| alert | `alert_snap` |
| angry | `alert_snap` |
| happy | `notify_perk` |
| neutral | `notify_perk` |

**Local tools only.** Requests carrying a browser `Origin` header are refused (403) on every route including `/ws`, and POSTs must be `Content-Type: application/json` (415 otherwise). Godot, curl, and the doorways never send `Origin`; browsers always do. Without this a web page could make her talk, or open `/ws` and read notification text as it goes past.

Run: `strawberryd/.venv/bin/strawberryd [--port 8770]` (after `uv sync`), or `bin/strawberry daemon`.

---

## 3. The brain — two paths, don't conflate them

- **Reaction path (no tools):** a notification or commit needs a fast one-liner, not tool use. `OllamaReactor` (`strawberryd/brain.py`) calls `POST /api/chat` on a **small, always-resident model** with the persona as system prompt, five example exchanges, and the output **forced to a JSON schema** `{"line": str, "emotion": neutral|happy|alert|angry}`. Thinking mode off, 1.5 s timeout, then the canned line instead. The model is loaded at daemon start with `keep_alive = -1` so the first event never waits for a cold load.
- **Action path (tools):** a voice command like "skip the track" or "log this to re:call" must call an MCP. This path runs the tool loop against your MCP servers (§8) on the big model, loaded on demand.

Both paths end by calling `perform(...)`. Route notifications/git/media → reaction path; voice → action path.

**Model choice (bake-off, `scripts/reactor_bakeoff.py`, results in `scripts/bakeoff_*.json`).** The reflex needs speed and residency, not intelligence: the 27B would cold-load for 10–20 s after a quiet half hour and hog the GPU. Among ~1B models on the Ollama library, **`gemma3:1b`** won: 815 MB, ~0.5 s warm, 0/32 schema misses, short lines (median 7 words), emotions right, and it does not invent details. `qwen3.5:0.8b` was as fast and livelier but hallucinated specifics ("Dinner Friday at 7 PM" for "dinner on Sunday?"), leaked the examples, and ran long. For a mascot reading your real notifications, saying less beats saying wrong. The decisive lever was **few-shot examples**: with the description alone Gemma answered "Interesting…" to everything; with five example exchanges it became a crab. The examples live in config (§15), so her voice is tunable without code.

The interface is `Reactor` (`strawberryd/events.py`): `async react(event) -> Performance`. `CannedReactor` (fixed line per source, no model) remains as the fallback and as `brain.enabled = false`.

---

## 4. Doorway: notifications

**On this machine the notification daemon is GNOME Shell, not dunst**, and dunst cannot run beside it (both claim `org.freedesktop.Notifications`). So the dunst `script =` rule from the original plan is out. Same doorway, different plumbing: a small listener **eavesdrops on the session bus** for `org.freedesktop.Notifications.Notify` method calls (a `GDBusConnection` monitor, or `busctl --user monitor` piped to a script), pulls app name, summary, body, and urgency hint out of each, and POSTs them to `/event` with `source=notification`. GNOME still shows the notification normally; the listener is additive.

If a machine does run dunst, the `[strawberry] script = …` rule in `dunstrc` is the simpler equivalent and posts the same event.

This is also how WhatsApp / Messenger reach Strawberry: you react to the **desktop notification**, never their APIs.

## 4b. Doorway: media (MPRIS) — `doorways/mpris_watch.py`

Any player that speaks MPRIS (Spotify, VLC, Rhythmbox, browser tabs with media) registers as `org.mpris.MediaPlayer2.<name>` on the session bus. The watcher follows **all of them at once** with PyGObject/Gio, no extra tools, and reacts to the union:

| Change | Daemon call |
|---|---|
| any player → Playing | `POST /perform {"state":"dancing"}` |
| nothing playing / player quits | `POST /perform {"state":"idle"}` |
| a playing player changes track | `POST /event {"source":"media","app":"<player Identity>","title":"Artist — Title"}` |

Changes are debounced 400 ms (players fire several property updates per track), a track is announced once per player, and un-pausing the same song is not an announcement. `--only spotify,vlc` or `--ignore firefox` narrow it. `bin/strawberry` starts the watcher next to the daemon. This is why the widget returns to `dancing` rather than `idle` after a line (§1): the music context outlives the reaction.

---

## 5. Doorway: git — `doorways/git/`

Global by construction: hooks run inside the `git` binary when the commit or push happens, so a terminal, Claude Code, Codex, opencode, or an IDE all fire the same hook. One line makes them apply to every repo on the machine, present and future:

```bash
doorways/git/install.sh        # symlinks the hooks into ~/.config/git/hooks and sets core.hooksPath
doorways/git/install.sh --remove
```

| Hook | Event posted |
|---|---|
| `post-commit` | `{source:"git", app:"post-commit", title:"<repo>", body:"<commit subject>"}` |
| `pre-push` | `{source:"git", app:"pre-push", title:"<repo>", body:"pushing N commits on <branch> to <remote>"}` (skipped for branch deletions) |

`app` carries the hook name so the reactor can shade the reaction. Both go through `strawberry-git-event`, which builds the JSON with `jq`, detaches the `curl` with `setsid`, gives up after one second, and always exits 0: a hook must never slow or break git, and a stopped daemon just means she doesn't react.

Two things the hooks take care of:

- **A global `core.hooksPath` replaces per-repo `.git/hooks`**, so each hook ends by handing over to the repo's own hook of the same name if one exists (pre-push replays its stdin to it). Repos that set `core.hooksPath` locally (husky-style) win over the global one; add a call to `strawberry-git-event` in their hook if you want her there too.
- **Only this machine's git fires.** Commits elsewhere, GitHub web merges, and CI results reach her through the notification doorway (§4). `post-merge`/`post-rewrite` are deliberately not installed for now; they'd make her chatty.

---

## 6. TTS + speech bubble (Godot side)

The reply text goes **two places at once**: to Piper (wav) and into the blob's `text`.

- **Piper:** daemon shells out to Piper → wav in `/tmp`, path goes in `audio`. CPU-only, keeps the GPU for Qwen.
- **Bubble:** a `Label3D` billboarded above the crab (`widget/bubble.gd`). Reveals characters over time, timed to the **audio duration** if `audio` present, or a text-length heuristic if silent (≈18 chars/s, clamped 1.6–9 s). Holds, fades, auto-hides, and signals `finished`. Tinted by `emotion`.
- **Audio-reactive claws:** play the wav on an `AudioStreamPlayer` whose bus carries an `AudioEffectSpectrumAnalyzer`. Each frame, read the magnitude, normalise 0–1, write it to `claw_open_L` and `claw_open_R` blend shapes. Zero them when not talking. **The wav must play through Godot**; that's the only way the analyser sees it. Don't also send it to the system mixer.

**Silent mode falls out for free:** omit `audio` and you get a talking crab with a bubble and no sound. The model still writes the line. This is "quiet hours," and it's the mode built **first** (§10).

---

## 7. Doorway: voice (STT)

- Hotkey → capture mic → **faster-whisper** (kept resident, model loaded once, CPU) → transcript.
- Transcript goes to the **action path** (§8) so speech can do things, not just chat.
- While capturing, send `{state:"listening"}`; while transcribing + thinking, `{state:"thinking"}`.
- The hotkey is bound as a **GNOME custom keyboard shortcut** that runs a one-line script POSTing to the daemon. GNOME owns the key, so it is identical on X11 and Wayland and the daemon never grabs keys itself.

---

## 8. Action path — MCP (last, and a real fork)

MCPHost is a good **interactive** pane for typing at the model, but the daemon needs to drive tool-calls **programmatically**. Two options:

- **Recommended:** run the tool loop inside the daemon with the **Python MCP SDK**. The daemon is already Python, and this keeps the whole action path in one process. MCPHost stays as the separate human terminal.
- Alternative: shell out to MCPHost if/when it exposes a non-interactive mode.

Either way, the servers are the existing MCPs unchanged (Spotify today, re:call/arxiv later). The loop: transcript → Ollama with tools → if tool call, run the MCP, feed result back → final text → `perform(...)`.

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
3. **Notifications doorway.** dunst script → same intake. Anything that notifies (including WhatsApp/Messenger) makes her react.
4. **TTS.** Add Piper → `audio` field → Godot plays it through the analysed bus → claws clack in time.
5. **Voice in.** Hotkey → faster-whisper → transcript. Route to plain Ollama first to prove capture.
6. **MCP actions.** Wire the Python MCP client; voice commands start *doing* things (skip track, log to re:call).

Stop after any phase and you still have something that works.

---

## 11. Acceptance criteria

- [x] `curl` posting a contract blob to the daemon makes Godot change state / play a one-shot. (`scripts/check_phase1.sh`)
- [x] Bubble reveal is timed to text length when silent, and auto-hides. (Audio timing lands with Phase 4.)
- [x] Speech ends → crab returns to `idle` with no follow-up message from the daemon.
- [x] A `/event` round-trips through the reactor to the widget. (Canned reactor; model in Phase 2.)
- [x] Play/pause in any MPRIS media player makes her dance/idle; a track change shows a "Now playing" bubble and she returns to dancing. (`doorways/mpris_watch.py`)
- [x] Browser origins are refused on HTTP and `/ws`; POSTs need the JSON content type.
- [x] A git commit makes Strawberry show a model-written bubble (silent). (`gemma3:1b` via `OllamaReactor`)
- [ ] A desktop notification (any app) triggers a reaction.
- [ ] With TTS on, the wav plays through Godot and the claws clack to the audio; `claw_open_*` is 0 when idle.
- [ ] A voice command routed through MCP successfully calls one real tool (e.g. Spotify skip).

---

## 12. Notes for the human (Panu)

- **Two hops, not one.** Hooks hit the daemon over HTTP; only the daemon talks to Godot. Don't let a git hook open a websocket.
- **Godot is the ws client**, daemon is the server. Simpler, and it reconnects cleanly if you restart either side while iterating.
- **Silent-first** is the whole de-risking trick: you can prove the entire loop before touching audio.
- **One audio rule:** Piper's wav plays *through Godot*, or the analyser is blind and the clack dies.
- **CPU/GPU split:** whisper + Piper on CPU, Qwen keeps the 3090.
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

**X11 pin.** `bin/strawberry` launches Godot with `--display-driver x11`. On the current X11/GNOME session that is native. On a Wayland session it runs under XWayland, where Mutter still acts as the X window manager and honours the keep-above and position hints. Native Wayland clients on GNOME get neither (no layer-shell, no client positioning), so we don't take that path. Always-on-top and remembered position are treated as **best effort**: if a compositor ignores them she degrades to a normal frameless window, nothing breaks.

**Interaction.** Drag her body to move the window; the position is remembered in `user://widget.cfg` along with the skin, and clamped to the screen's usable area on restore. `Q` quits, `C` cycles skins. Cel shading and the ink outline are always on in the widget (the v2 preview's `O` toggle was a review aid, not a feature). The daemon can also send `{"command": "quit"}` or `{"command": "skin", "value": "mint"}`.

**Layout.** 380×460 window, orthographic camera (`KEEP_WIDTH`, size 1.25) centred at y 0.55 so the crab sits low and the bubble has room above at y 0.98. `run/max_fps=60`, `gl_compatibility` renderer (same as the v2 preview).

---

## 15. Settings

One file, `~/.config/strawberry/config.toml` (`$XDG_CONFIG_HOME` respected). Defaults live in code (`strawberryd/config.py`), so the file only needs the lines you change. Read by the daemon at start and by the media watcher; `GET /config` shows the effective result. Unknown keys warn; wrong types refuse to start with the key named.

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
event = "source: git\napp: post-commit\ntitle: kaelon\nbody: Fix flaky login test"
line = "A fix! Did that wobbly login test finally stop wiggling?"
emotion = "happy"

[media]
only = []                        # e.g. ["spotify"]
ignore = []                      # e.g. ["firefox"]
```

`bin/strawberry config` creates the file from a commented template (`strawberryd --init-config`) and opens it in `$EDITOR`; `bin/strawberry restart` applies it. `STRAWBERRYD_PORT` still overrides the port for scripts. The widget's own preferences (skin, window position) stay in Godot's `user://widget.cfg` for now.

## 14. Repository map

```
WIRING.md                this document
strawberryd/             Python daemon (uv project): contract, events/reactor, hub, server, tests
widget/                  Godot 4.7 desktop widget: widget.gd, ws_client.gd, bubble.gd, validate_widget.gd
                         + strawberry_v2.glb and the v2 shaders/controllers (copied from v2/godot_check)
doorways/                short-lived event producers: mpris_watch.py (any MPRIS media player), git/ (global post-commit + pre-push hooks); notification listener to come
bin/strawberry           launcher: daemon + media watcher up, then widget on the X11 backend
scripts/check_phase1.sh  Phase 1 acceptance: unit tests + headless widget against a real daemon
scripts/check_reconnect.sh  restart (or SIGNAL=KILL) the daemon under a headless widget; it must reconnect
v2/                      the asset: Blender build scripts, GLB, evidence, preview project
v1/ (top level)          the earlier deliverable
```

**Run it:** `bin/strawberry`. Then, from anywhere:

```bash
curl -s -H 'Content-Type: application/json' localhost:8770/perform \
  -d '{"state":"talking","anim":"alert_snap","text":"Did someone say my name?","emotion":"alert"}'
curl -s -H 'Content-Type: application/json' localhost:8770/event \
  -d '{"source":"git","title":"strawberry","body":"Add websocket"}'
```

Or press play in any media player: the MPRIS watcher (§4b) does the rest.
