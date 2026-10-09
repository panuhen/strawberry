# Changelog

All notable changes to Strawberry are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). One version covers the Python
package and the widget binary.

## [Unreleased]

### Added

- She turns. A slow, small sway while idle; a glance toward a notification as it arrives, toward
  the middle of the screen now and then, and toward the cursor when it comes to rest near her;
  a quick turn and back in the peek and the wave; a turn on the beat when she dances. Always a
  few degrees, always back to face you, and her eyes stay on you while she turns.
- *Turn toward the screen* in her right-click menu (on by default): her view follows where her
  window sits on its monitor. Near the top you see her a little from below, near the bottom a
  little from above, and near a side edge she turns a little toward the middle. Each monitor
  counts on its own, and it follows a drag smoothly. Her click-through area follows the turns.
- Touch her. A quick tap (mouse or finger) is a poke; pressing and moving still drags her. Pat
  her shell and she squints contentedly, tickle her belly and she giggles, poke an eye and she
  flinches, poke a claw and she pinches back or waves it, hold a finger on her and she leans into
  it. Poke her a few times in a row and she gets mildly annoyed. A poke wakes her gently. While
  she is talking or busy with something you asked, a poke only gets a blink, and the step chip's
  ✕ always wins. *Touch reactions* in her menu (on by default) turns it off; *Talk when poked*
  (off by default) lets her answer a poke now and then with a short line of her own (the widget
  sends `poked`, PROTOCOL.md §6; the lines are fixed, no model is asked).

- Runs you can watch and stop. Every sentence she handles, and every notification she reacts to,
  is a run with its steps: how the gate read it, thinking, each tool call and how long it took,
  what she said, and how it ended. While a slower one is going on, a chip under her bubble says
  what she is doing ("thinking…", "searching the web…", "Spotify: play"), in her smoked glass with
  square corners, and its ✕ stops it. "Stop", "cancel that" or "never mind" stops it too, and a new
  sentence takes over from the one she was on (`[runs] supersede`; a yes or no to her question
  never does). A call that only looks something up is dropped at once; one that changes something
  is let finish, and she says it had already gone through. One sentence is handled at a time now:
  two typed in a row no longer run side by side. The Brain UI has a Runs section with each run's
  timeline and a Cancel button. The steps carry tool names and timings only, never what you said,
  a tool's arguments or its results. New settings: `[runs] events`, `supersede`, `keep`.
- Approvals with risk tiers. A call that waits for your yes is now bound to that exact call: the
  question she asks keeps a copy of it, checked before it is made, and nothing else can be made
  with your yes. Besides Spotify's removals (asked about as before, in the same words), every call a
  server's tool makes that sends something to someone (`sends`) or deletes something for good
  (`destructive`) waits for a yes, for 30 seconds instead of 10. A tool's tier comes from the new
  `[approvals] risk` setting, else its adapter, else it is a plain change; a server that marks a
  tool destructive raises it, and nothing a server says lowers one. You can answer by voice or by
  typing as before, or in the Brain UI's Runs section (Yes and No, and a history of what was asked
  and how it was answered); the widget gets everything it needs to show the question as a card,
  where a yes to a `sends` or `destructive` call is a press-and-hold. Stopping the run, or saying
  something else, leaves the call undone. New settings: `[approvals] change_s` (the old `[actions]
  confirm_s`, still read), `sends_s`, `destructive_s`, `grace_s` (how much longer she waits while
  you are still answering, at most), `hold`, `risk`, and `[thinker] stream`.
- Answer her on screen. When she asks before a change ("Remove 'Feeling Good' from Running? Say
  yes."), a card shows under her line with the question, No and Yes, and a countdown. Tap Yes or No,
  or press Y, N or Esc while she has focus; for something that sends a message or cannot be undone,
  Yes is a press-and-hold of about a second that fills as you hold it (a tap or a key only says
  "hold to confirm"). Answering by voice, by typing or in the Brain UI closes the card too, and the
  step chip's ✕ still stops the whole thing. The card is the step chip's smoked glass with square
  corners; her line moves up above it while it shows, and she leans in a little while she waits.
  When the countdown runs out while you are still saying your answer, it says "waiting…".
- Protocol v2, second part (PROTOCOL.md §11d, §13b): `approval.request` and `approval.resolved` for a
  body that says `approvals`, `approval.answer` from one that also says `sends.approval` (refused,
  with the reason, for anything but the open question), the open question again after `welcome`
  for a body that reconnects, the `listening` gauge (a voice capture started and ended), the
  `token_rate` gauge (tokens a second while she thinks; numbers only), and a run that ends
  `didnt_catch` when she heard nothing she could understand. A v1 body still gets exactly the bytes
  it always did.
- Protocol v2, first part (PROTOCOL.md Part 1b): a body that says `protocol: 2` in its hello gets
  `welcome`, the run events it asks for, the run's id on the performance that answers it, the
  brain's clock in each pong, and may send `run.cancel` if it said so. A v1 body gets exactly the
  bytes it always did. The widget speaks v2. `/health` lists the bodies and the runs.

- The Brain UI (`strawberry ui`, README: *Brain UI*): a page in your browser, served by the
  daemon, that shows the router live (each sentence's reading, confidence, what handled it and
  how long it took) and what the learning loop learned: the candidate against the head in use
  on the held-out set, the learned sentences with approve and reject, the learning log, every
  head with its score over time, use and roll back, the week's report, train now and forget. It
  also shows the effective settings with how to change each, and what runs where. It opens with
  a one-time link and runs on a session cookie kept in memory; every other route still refuses
  browsers, the page checks the host, its own origin and a CSRF header, and loads nothing from
  the internet. Sentences appear only while outcome logging is on, and never in a log.
- The router learns from use (`strawberry learning`, README: *Learning*). With the learning log on,
  an undo, a correction, a rephrase, silence after a reflex, and the big model doing exactly what a
  reflex does become labelled sentences, kept locally and readable only by you. When she has been
  idle for a while and enough new labels wait, she trains a new head for the gate in the
  background at low priority (it stops when you speak); it must score at least as well as the one
  in use on the shipped held-out set, which your sentences never enter. A head that passes waits
  for `strawberry learning accept`, or goes in use at once with `[learning] auto_switch = true`.
  Every head is kept: `strawberry learning rollback`, or **Router: roll back to previous** in the
  tray, goes back one. A switch needs no restart. `strawberry learning status`, `versions`,
  `report` ("learned 14 new examples, 2 rejected, held-out 95.0% → 95.6%"), `train`, `examples`,
  `review` and `forget`; `/health.learning` shows the head in use, the candidate and the
  trainer, never a sentence. `[learning] weekly_line = true` has her say once a week what she got
  better at, never in quiet hours. New settings: `idle_train`, `idle_minutes`, `min_new_labels`,
  `auto_switch`, `weekly_line`, `max_share`.

- A spoken yes before a removal. When the big model calls a tool on a server's `confirm` list, she
  asks first ("Remove 'Teardrop' from Gym? Say yes.") and makes that exact call only if your next
  sentence is a yes (yes, sure, do it, go ahead). A no, any other sentence, or 10 seconds without
  an answer leaves it, and she says so; any other sentence is still done. Only your own spoken or
  typed sentence can answer, never a notification, a media change or a tool result. On by default
  for Spotify's `remove_from_playlist` and `remove_saved_tracks`; `confirm = [...]` in
  `[tools.servers.<name>]` sets the list and `confirm = []` turns it off. `[actions] confirm_s`
  is the wait. Saves and adds stay instant.
- Spotify likes and playlists by name, with the Spotify MCP server's new tools. "I like this"
  and "save this song" put the playing track in your Liked Songs at once, without the big model
  ("Liked: Blue Monday by New Order."). Your favourites are your Liked Songs: "add this to my
  favourites" likes the track the same way, and "play my favourites" plays your Liked Songs
  shuffled, also without the big model (with the server's `play_liked`). "Add this to my gym
  playlist", "play my running playlist" and "take this off my gym playlist" pass the name as heard
  and the server finds the playlist, misheard names included. When several playlists match she
  asks which one, naming at most three. Adding and liking are offered with every sentence (each is
  undone in a word, and duplicates are skipped); removing from a playlist and making one are
  careful tools, offered only when you ask for such a change. Add `remove_from_playlist` and
  `create_playlist` to the server's `careful` list (the config template has them). An older server
  without these tools works as before.
- Adapters can name reflexes by the sentence's own words, for commands the gate has no option
  for, and give their paragraph for the brain only to a server that lists the tools it names
  (ADAPTERS.md).
- Web search, with a SearXNG instance of your own and the `mcp-searxng` MCP server in
  `[tools.servers.web]` (README: *Web search*). She searches when you ask ("look up…", "search
  the web for…", "google…") and when a question depends on something current (weather, news,
  results, prices, opening hours, the latest of anything), and answers in a sentence or two,
  naming the site, never reading a link. Small talk, questions about her and music
  recommendations stay hers. This is the first feature that sends your words off the machine:
  the query goes to the engines SearXNG asks. The log says only that a search ran, the query's
  length, the result count and the time. Text from the web is treated as untrusted: while it is
  in her conversation your recent exchanges and what is playing are out of it, your other tools
  are refused, and a page can only be read from that search's own results. If SearXNG is down
  or `npx` is missing she says search isn't available. WIRING.md §8b, ADAPTERS.md.
- Adapters can now filter and shorten a server's tools, compact its results, keep its results
  out of the log, add a paragraph to the brain's rules or a line under the sentence, cap its calls
  per sentence, and mark its results as untrusted (ADAPTERS.md).

- PROTOCOL.md: the bus protocol between the daemon and its bodies, as built (v1), and proposed
  additions (v2) for bodies other than the crab.
- `[voice] fallback_model`: the whisper size to use on the CPU when CUDA runs out of memory (""
  keeps the configured model; `small` is quicker on the CPU).
- An outcome log for the router's learning loop, off by default (`[learning] log_outcomes =
  true` turns it on). Each sentence you say or type is kept in `outcomes.jsonl` in the state
  directory with the gate's full reading, whether a reflex or the thinker handled it (for the
  thinker, the tool names it called, never their results), and what came of it: a correction
  ("no, I meant…"), an undo (skip, then previous), a rephrase, a repeat, or silence. A future
  step will tune the routing on it; nothing reads it yet. Sentences that read as private and
  other voices in the room are never kept, no sentence goes into a log, and the file is
  readable only by you and pruned to 30 days and 5000 records (`max_days`, `max_records`).
  `strawberry outcomes` shows the counts and the last records, `--clear` deletes the file, and
  `/health` has a `learning` entry. WIRING.md §8c.
- `[daemon] log_sentences`, off by default. What you say or type to her is no longer written to
  the log: the server, whisper, the gate, the reflexes, the thinker and the arguments of the
  tool calls it fills in now log only the sentence's length (`<sentence, 23 chars>`), and her
  lines in the log leave out the sentence she is answering. `true` logs the sentences as before,
  for tuning the gate from the journal. `GET /config` shows it. WIRING.md §15.

- `strawberry gate train | eval | use`: fit a new head for the gate on its data set and compare it with
  the one in use on the held-out set, score the gate on the held-out set (or another labelled set),
  and switch heads (`use shipped` goes back to the one in the package). Heads are versioned files in
  `~/.local/share/strawberry/gate/heads/` with a `current` pointer. `scripts/gate_heldout_check.py`
  prints the same comparison from a checkout.

### Changed

- Her moves are eased. The baked clips use Bézier keys, `alert_snap` snaps her claws a little past
  the mark and settles, and the end of `notify_perk` lands softly. In the widget the wave's claw
  overshoots the same way, dance styles ease in and out and crossfade when the music changes
  style, and the rave lean no longer jumps on every beat. Every bone, clip and shape-key name is
  unchanged.
- `strawberry gate use` takes effect in a running daemon within a few seconds, without a restart,
  and `strawberry learning rollback` can undo it.
- The Spotify adapter no longer uses the server's local favourites list (`favorite_current`,
  `get_favorites`, `remove_favorite`, `play_favorites`, `clear_favorites`), which the server has
  dropped: "favourites" means Liked Songs. Take those three names out of the server's `careful`
  list if yours has them (`favorite_current`, `remove_favorite`, `clear_favorites`); left in, they
  do nothing. "Add this to my favourites" was sometimes answered "Saved" with no call; it is a
  reflex now.
- Spotify failures are worded by the server's error code: "I can't reach Spotify right now",
  "Spotify needs signing in again", and otherwise the server's own sentence without its hints for
  a terminal. Before, a network failure came out as "…but Spotify said: Could not reach Spotify.
  Check the internet connection and try again..". Errors from an older server read as before.
- The thinker offers the tools in the same order for every sentence while they fit
  `max_tools`, so Ollama reuses its cached prompt: putting the gate's topic first re-read the
  whole prompt whenever the topic changed (2.1-2.7 s instead of ~0.3 s). Over the cap the topic
  still decides what is cut.
- The thinker refuses a call to a tool that was not offered for that sentence (a careful tool
  offered for an earlier one, a made-up name), and makes at most six calls a reply.
- The gate reads what you say with a trained head instead of each option's nearest examples
  (`[gate] scorer = "head"`, the default; `"nearest"` keeps the old way). The head is a small
  logistic regression per question on the same embedding, trained on ~1800 sentences (the gate's
  own examples and sentences the local Qwen wrote, reviewed by hand; about an eighth Finnish),
  calibrated, and with its own thresholds (act 0.65, offer 0.05). On a held-out set of 199
  sentences and 42 notifications it never trained on: 95% of the fields right instead of 78%, 168
  of 199 sentences fully right instead of 123, the Finnish ones 26 of 35 instead of 18, and its
  confidence separates right from wrong far better (AUROC 0.93 against 0.71). It adds nothing you
  would notice to a route (~0.1 ms). Privacy still fails closed: a notification body counts as
  private when either the head or the nearest examples say so. Phrases under `[gate.examples]`
  still fix the sentences they are close to. A head that is missing or does not fit the embedder
  falls back to the nearest examples, says why in `/health` (`gate.scorer`) and in `strawberry
  doctor`. WIRING.md §8a.
- The gate's embeddinggemma runs in the daemon's own process, on the CPU, through ONNX Runtime
  (`onnx-community/embeddinggemma-300m-ONNX`, fp32): a spoken sentence is routed in ~20 ms
  instead of ~150 ms through Ollama, with the same readings (the gate's phrase set 87/87, the
  notification filter's 38/38). It takes ~0.8 GB of RAM and no VRAM. `[gate] embedder = "onnx"`
  is the default; `"ollama"` keeps the old path. `onnx_dir` and `onnx_threads` (4) go with it.
- `strawberry setup` downloads the model (~1.2 GB, from Hugging Face) into
  `~/.local/share/strawberry/models/` (`%LOCALAPPDATA%\strawberry\models\` on Windows) as a
  new step 3. Until it is there the gate uses Ollama's `embeddinggemma`, logs why and says so in
  `/health` (`gate.embedder`, with the backend and its load time); the daemon never fails to
  start over it. `strawberry doctor` says where the gate runs and warns when it fell back.
- The gate scores the sentence against every example in one numpy product (the same scores as
  before, to 1e-9).
- New dependencies: `onnxruntime` (the CPU build, Linux and Windows) and `tokenizers`; both
  were already installed through faster-whisper.
- The daemon's port opens at once (~0.3 s) instead of after the models load (~5 s with the gate
  in-process, more when Ollama is slow). The gate's examples, Piper's voice and the reaction
  model's warm-up load in the background. A sentence that comes before the gate is ready waits
  up to 2 s and is then answered as chat, a notification body waits for it as for a reload, and
  the first line waits for Piper rather than going silent. `/health` shows `gate.starting` and
  `speech.loading` meanwhile.

### Fixed

- The one-time privacy note now reaches a body that can show it. It went out on the first hello,
  so a body that shows no text (the orbs) saying hello first used it up and the user never saw
  it. It now goes only to a body that shows text (a v1 widget, or a v2 body whose hello says
  `capabilities.speech.bubble`, which the crab now does; PROTOCOL.md §9b), on that body's socket,
  and counts as shown only once it went out there.
- Web search no longer reads a page whose site name points at a private address. The page
  address was checked as written (no IP addresses, no internal names), but a public-looking name
  can resolve to the machine itself or the home network (`localtest.me` is 127.0.0.1). The name
  is now looked up first, with 1.5 seconds to answer, and the page is not read if any address is
  loopback, private, link-local, CGNAT, multicast or reserved, or if the name does not resolve.
  The search server's own address filter stays as a second layer, against DNS answers that change
  between the check and the fetch.
- A tool result with the MCP error flag set is read as a failure under either spelling of the
  flag: `is_error` (the `mcp` 2.x SDK) and `isError` (1.x, which the dependency range allows, and
  a result as a dict). Under 1.x only an `{"error": …}` body was caught, so a server that flagged
  an error with a plain sentence was taken as a success. The structured content is read under
  both spellings too.
- Whisper on CUDA no longer stops working when the GPU is full. Ollama cannot free VRAM for
  whisper, so when Qwen loads first a CUDA load or a transcription failed with "CUDA failed with
  error out of memory". Whisper now moves to the CPU (`int8`) and the failed sentence is
  transcribed again there, so it is not lost. It stays on the CPU until the daemon restarts;
  `/health.voice` shows the device in use and `fallback` says why, the journal logs it once, and
  `strawberry doctor` warns about it.
- The thinker no longer sends Qwen a prompt longer than `num_ctx`. Ollama drops the oldest tokens
  of such a prompt without an error, and those are the system prompt: her voice and the tool
  rules. Each round's prompt is now estimated first (3 characters a token, on the safe side) and
  trimmed to fit with `num_predict` left for the reply: the oldest ledger turns go first, then the
  tool results are shortened, keeping their start. The system prompt, the tool schemas and the
  sentence are never cut. When those alone do not fit, the request is not sent and she says it
  was too much to hold in her head. The journal line has counts only.
- `thinker.num_predict` is checked at start: between 1 and half of `num_ctx`.
- `GET /health` refuses browsers like every other route (403 on an `Origin` header). It holds
  the ledger, the user's recent sentences and her replies, and was the one route without the
  check. One middleware does it for every route now.
- A state change no longer cuts her voice. Any performance without audio stopped the line she
  was saying, so the music starting mid-sentence (the media doorway's `dancing`) silenced her
  while the bubble carried on. Now she dances to the end of the sentence; a new line, or the
  listen hotkey, still stops it.

## [0.2.0] - 2026-09-23

Strawberry runs on Windows 10 (2004 or later) and 11, with the same package and the same
commands as on Linux: `uv tool install strawberry-crab`, `strawberry setup`, `strawberry`,
`strawberry install`. README.md has the Windows install and its known limits; WINDOWS.md says how
each part works there.

### Added

- Windows is a supported system. The config lives in `%APPDATA%\strawberry`, the widget, voices,
  cache, logs and state in `%LOCALAPPDATA%\strawberry`. `STRAWBERRY_ALLOW_UNSUPPORTED=1` remains
  for trying her on a system that is not supported (macOS).
- The widget for Windows: `strawberry-widget-<version>-windows-x86_64.exe` on each GitHub release,
  with the berry as its icon. `strawberry widget --fetch` and `strawberry setup` download it and
  check its SHA-256, as on Linux. She is transparent, always on top and clicks go through her
  empty space, as on Linux.
- Media on Windows: play, pause, skip, previous and "what's playing" work with any player that
  shows in Windows' media controls (the System Media Transport Controls), and the `smtc_watch`
  doorway makes her dance while music plays and names each new track. There is no volume
  control there.
- Notifications on Windows: the `toast_watch` doorway reads other apps' toasts from the
  notification centre (the app's name and logo, the title and the body) with the same
  `[notifications]` settings, body modes and privacy checks as on Linux. It needs "Let apps access
  your notifications" on in Windows' privacy settings, and it checks for new toasts once a second.
- The beat on Windows: `beat_watch` listens to the playing player's own sound only (WASAPI process
  loopback), found through its media session, and she dances on the beat as on Linux. `[beat]
  target` takes the player's short name there (`spotify`, `chrome`).
- The tray and start on login on Windows: the berry in the notification area with the same menu
  as on Linux (a left click shows or hides her); it runs the daemon, the doorways and the widget
  and restarts one that stops. `strawberry install` puts a shortcut to it in the Startup folder
  and starts it, `strawberry uninstall` removes it. It runs without a console window and logs to
  `%LOCALAPPDATA%\strawberry\state`. A new command, `strawberry-tray`, is `strawberry tray`
  without a console window.
- The microphone and the listen hotkey on Windows: she records from Windows' default recording
  device (or the one `[voice] source` names) at the same 16 kHz mono. The tray holds the hotkey,
  Ctrl+Alt+Space by default (Windows keeps Win-key combinations for itself); `strawberry hotkey
  COMBO` changes it and `--remove` turns it off. It is kept as `[voice] hotkey`. Whisper on CUDA
  works with the `gpu` wheels alone, without a CUDA toolkit.
- Warm on wake on Windows: after a resume the daemon reloads the gate's model and the reaction
  model, as it does after logind's signal on Linux.
- `strawberry doctor` on Windows checks, in place of PipeWire, D-Bus, the tray host and systemd:
  whether Windows lets her read notifications, the media sessions, the microphone and its privacy
  switches, whether the build can capture a player's sound, the Startup shortcut and what it
  runs, and the tray. `strawberry setup` there names Ollama's Windows installer (or winget) and
  offers the Startup shortcut.
- `strawberry setup --no-download` writes the config but pulls no model and fetches no voice or
  widget; it names each one it would have fetched.
- On Windows the widget falls asleep after inactivity as on Linux: its idle helper reads
  `GetLastInputInfo` on the interpreter `strawberry widget` and the tray pass it.
- The tests run on Windows in CI, and the release builds and checks the Windows widget beside
  the Linux one.

### Changed

- `jeepney` is installed on Linux only; the `winrt-*` packages and `sounddevice` on Windows only.
- The config file and the widget's preferences are read and written as UTF-8 whatever the
  system's locale.
- `scripts/build_widget.sh` and `scripts/check_phase1.sh` also run under Git Bash on Windows.
- `strawberry setup` has a step for whisper (step 4, "Her ears"): it names the model with its size
  and licence and, when voice is on and the model is not in the Hugging Face cache, downloads it
  with a progress bar, so the first start is not the download. `--no-download` names it and
  leaves it to the first start. The widget, settings and install steps are now 5 to 7.

### Fixed

- The gate gave up at start when Ollama answered its first request with an error, and routed
  everything to chat (with every notification body private) until a restart or a resume. Seen
  with Ollama on Windows, where the first embed of a start sometimes went to a model runner that
  had just gone; an HTTP error at start is now tried once more.
- The widget's *Restart widget* passed `--display-driver X11` (or `Windows`) to the new process,
  which accepts only the lower-case names.
- On Windows her claws were cut off while she danced, and the tops of her eyes during a peek, a
  hop or a wave: the window's region (what Windows draws as well as what takes clicks) was her
  shape at rest. It now follows her pose and holds each pose for a moment, so the dance styles,
  the reactions and the top hat are drawn whole, and a raised claw takes clicks. Linux is
  unchanged.
- On Windows right-clicking her made the whole window's rectangle flash for a moment before the
  menu appeared: opening the menu removed the window's region, and Windows repainted all of it.
  The region now takes in the open menu and its submenus instead, so only she and the menu are
  drawn, the menu takes clicks and the desktop around it still does. Linux is unchanged.
- `scripts/gate_check.py` and `scripts/sensitive_check.py` read their phrase files in the
  locale's encoding; they are UTF-8.
- With no music server configured she refused to talk about music: "any good techno from
  <a country>?" got "Choosing music needs a music add-on" and "can you recommend some techno
  artists" got "I don't know much about techno artists". Recommending music and talking about
  artists, albums and genres are answered from her own knowledge again; only playing, queueing,
  opening or saving particular music gets the line that it needs a music add-on. "What's a good
  album to start with Radiohead" no longer reads as a request to play it.
- The first start with a whisper model that was not downloaded yet (after `strawberry setup
  --no-download` wrote `[voice] model = "medium"`, say) did not answer for minutes: the daemon
  loaded whisper before it opened its port, and faster-whisper downloaded the model (~1.5 GB)
  first. The widget said "not connected to strawberryd" and every watcher "strawberryd
  unreachable". Whisper now loads in the background: the daemon answers at once, `/health`'s
  `voice` says `phase: "loading"` and why (`first use: downloading ~1.5 GB`), the hotkey gets
  "I'm still getting my ears on." until it is done, and the tray's status row and `strawberry
  doctor` say it is loading. A stop during the download no longer waits for it to finish. The log
  line "listening on" now comes once the port really is open.

## [0.1.1] - 2026-09-23

### Fixed

- A notification that arrived just after a resume from suspend was dropped as "something
  private": Ollama had unloaded `embeddinggemma` over the suspend, and the privacy check's 2 s
  timeout treated a model that was loading as a gate that was down. A timed-out check now waits
  for one shared reload of the model, up to `[gate] retry_timeout_s` (15 s), and asks once more;
  it still fails closed if that retry fails, and a hard error (Ollama not running, an HTTP error)
  fails closed at once. Several notifications at once share the one wait. Spoken sentences never
  wait: a timeout there still reads the sentence as chat. The notification watcher now waits up to
  30 s for the daemon's reply instead of logging a slow one as "strawberryd unreachable".
- The daemon now reloads the gate's model and the reaction model (`gemma3:1b`) in the background
  when the machine wakes, from logind's `PrepareForSleep` signal on the system bus. Without a
  system bus or logind it logs one line and carries on. `[daemon] warm_on_wake = false` turns it
  off; `/health` shows it under `wake`, and the gate's `retries` and `warmups`.
- A gate whose examples could not be embedded at start (Ollama was down) now comes up at the next
  resume from suspend instead of staying off until a restart.
- The widget comes back on the display it was left on. Its saved position was kept inside the
  primary screen, so a widget on a second display reappeared at the bottom of the main one.
  *Reset position* now uses the corner of the screen she is on.
- The app switcher showed a generic gear and, in developer mode, "Strawberry (DEBUG)". The window
  now carries the berry icon and a plain title, and `strawberry install` adds a hidden
  `strawberry.desktop` entry and icon that GNOME matches her window to (run `install` again to
  get it).

## [0.1.0] - 2026-09-22

The first release on PyPI (`strawberry-crab`; 0.0.1 was a name placeholder) with the widget
binary on GitHub.

### Added

- The desktop widget: a transparent, always-on-top, click-through Godot 4.7 window with the
  cel-shaded crab. Her pupils follow the cursor; reactions (wave, peek, shiver, hop, nod), an app
  icon badge beside the speech bubble, a sleep mode, a top hat, skins, and a right-click menu.
  Shipped as one standalone Linux binary; `strawberry widget --fetch` downloads it from the
  GitHub release and checks its SHA-256.
- `strawberryd`, the daemon: HTTP intake and one websocket to the widget, the message contract,
  and a version handshake that refuses a widget on another major version.
- Doorways: desktop notifications (a D-Bus monitor with filters and coalescing), media players
  over MPRIS, git commits and pushes (global hooks), and the beat of the playing track from the
  player's own PipeWire stream, with five dance styles.
- Her voice: Piper TTS (default voice `en_GB-alba-medium`) with claws that move to the audio,
  quiet hours, and `strawberry voices` / `audition` / `say`.
- Listening: a GNOME hotkey (Super+Shift+Space) and faster-whisper, on the CPU or CUDA, with a
  Bluetooth headset microphone when there is one and a vocabulary of names to recognise.
- Typing to her: the *Chat with Strawberry…* box in the widget and `strawberry talk`.
- The gate: every sentence is sorted locally by `embeddinggemma` (kind, topic, tool, argument).
  Plain music commands (skip, previous, pause, resume, volume, what's playing) are reflexes over
  MPRIS for any player, answered in well under a second with no model in the loop.
- One brain for everything else: Qwen with the configured MCP servers' tools, the situation and
  her persona, a thinking pose, and a short memory of recent exchanges. `gemma3:1b` writes her
  one-line reactions.
- MCP servers in the config and adapters for them; Spotify is the first adapter (reflexes,
  situation text, recogniser names, error wording). No server ships configured.
- The 🍓 tray icon (StatusNotifierItem): the login process that starts and restarts the daemon,
  the watchers and the widget, with her whole menu and a status line.
- The `strawberry` CLI: `setup`, `doctor`, `install` / `uninstall` (one systemd user unit, with
  an autostart fallback), `status`, `stop`, `restart`, `config`, `hotkey`, `route`, `tools`,
  `tool`, `think`, `git-hooks`. Files follow the XDG base directories.
- Notification privacy: message bodies are off by default; `react` and `glance` modes per app;
  a sensitive filter that drops codes, sign-ins and bank alerts and fails closed; no message
  text in any log. A one-time first-run note in her bubble says so and where to change it.
- MIT licence, THIRD_PARTY.md, and release automation: on a `v*` tag GitHub Actions tests,
  builds the wheel and the widget, runs the widget's headless acceptance, creates the release and
  publishes to PyPI by trusted publishing.
- `strawberry doctor` checks whether the notification monitor is allowed, the MPRIS players on
  the bus, `strawberry-tray.service` (installed, enabled, active, and a stale ExecStart after a
  move or reinstall), the git hooks, and whether the beat watcher is posting and linked to the
  player's stream. `/health` has `tempo_age_s` for that last one.
- `strawberry setup` and `doctor` detect AMD cards (rocm-smi, or sysfs without ROCm). The models
  run on them through Ollama's ROCm support; whisper runs on the CPU. Intel graphics count as no
  usable GPU.
- A tray submenu, *Message bodies ▸ Off / React / Glance*: it writes `[notifications] body`
  into config.toml (comments kept, a backup first) and applies it without a restart.
- A steadier beat tracker: onsets in three bands with the kick leading the phase, octave
  checks, hysteresis and a median over the last estimates. On a synthetic set of 35 clips the
  right tempo went from 56 % to 84 % of estimates at the same CPU cost. Tempo events carry a
  `steady` flag, and the widget sways instead of stepping while it is false.
  `scripts/beat_eval.py` generates the set and scores a tracker.
- With no music server configured, asking her to play, queue or save particular music gets a
  plain answer that this needs a music add-on, instead of a wrong claim or a vague refusal.
  Skip, pause, resume and "what song is this" keep working over MPRIS.
- Supported platforms are checked at start: on anything but Linux the commands say so and exit.

### Fixed

- "Unclosed client session" errors when the tray service stopped: the daemon gets SIGTERM
  twice (systemd and the tray), and the second one cancelled aiohttp's cleanup. The daemon now
  runs its own shutdown and ignores repeat signals.
- The journal no longer gets a line every 2 s for the tray's `/health` poll and the beat
  watcher's tempo posts.

[Unreleased]: https://github.com/panuhen/strawberry/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/panuhen/strawberry/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/panuhen/strawberry/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/panuhen/strawberry/releases/tag/v0.1.0
