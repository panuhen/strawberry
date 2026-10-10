"""Settings: one TOML file, defaults in code, env vars for the odd override (WIRING.md §15).

    ~/.config/strawberry/config.toml   (or $XDG_CONFIG_HOME/strawberry/config.toml)

Only the keys you change need to be in the file. Unknown keys are warned about, not fatal;
wrong types are fatal with the key named, because a silently ignored setting is worse.
"""

from __future__ import annotations

import json
import logging
import os
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from . import paths

log = logging.getLogger("strawberryd.config")


class ConfigError(ValueError):
    pass


def default_path() -> Path:
    return paths.config_file()


@dataclass
class DaemonConfig:
    host: str = "127.0.0.1"
    port: int = 8770
    log_level: str = "INFO"
    warm_on_wake: bool = True      # on resume from suspend (logind), load the gate and reaction models again
    # The user's own sentences (said or typed) in the journal: false logs "<sentence, 23 chars>" in their
    # place (the gate, the reflexes, the thinker, whisper, tool arguments); true logs them as they were.
    log_sentences: bool = False


@dataclass
class BrainConfig:
    enabled: bool = True                      # false -> canned lines, no model at all
    reaction_model: str = "gemma3:1b"         # the reflex: one line per event, always resident
    action_model: str = "qwen3.8:27b"         # the thinker: voice commands with tools (Phase 6)
    ollama_url: str = "http://127.0.0.1:11434"
    timeout_s: float = 1.5                    # slower than this and she says the canned line instead
    temperature: float = 0.8
    max_words: int = 15
    keep_alive: int | str = -1                # seconds; -1 pins the model in VRAM, "10m" lets it unload
    # Deprecated: her persona and examples are persona.md now (persona.py). Set here, they still replace the
    # reaction model's whole system prompt and its examples, with a warning at start.
    persona: str = ""
    examples: list[dict[str, str]] = field(default_factory=list)


@dataclass
class MediaConfig:
    only: list[str] = field(default_factory=list)    # follow just these players (MPRIS names; smtc.app_key on Windows)
    ignore: list[str] = field(default_factory=list)  # skip these, e.g. ["firefox"]


@dataclass
class VoiceConfig:
    enabled: bool = True          # hotkey -> listen -> faster-whisper -> she answers (Phase 5)
    model: str = "small"          # faster-whisper model: tiny | base | small | medium | large-v3 (or a path)
    language: str = ""            # "" = detect; "en" pins English and is faster
    device: str = "cpu"           # keep the GPU for Ollama; "cuda" works if you have room
    compute_type: str = "int8"
    fallback_model: str = ""      # CUDA out of memory (Qwen filled the card): whisper moves to the CPU (int8)
                                  # with this size until a restart; "" = the same model ("small" is quicker there)
    source: str = ""              # microphone (pactl source name fragment; Windows: a fragment of the device's
                                  # name); "" = auto (see bluetooth; Windows: the default recording device)
    bluetooth: bool = True        # auto: prefer a connected Bluetooth headset's mic, switching it to its
                                  # headset profile while she listens (music drops to phone quality for those seconds)
    max_seconds: float = 15.0
    silence_s: float = 1.1        # this much quiet after speech ends the recording
    min_speech_s: float = 0.4
    level_db: float = -50.0       # speech must be louder than this (and than the room + 12 dB); webcam mics are quiet
    beam_size: int = 1
    # Names the recogniser should know. Yours here; artists and playlists come from the music
    # server on their own (hotwords), refreshed now and then. "Daft Punk" is not a common word.
    vocabulary: list[str] = field(default_factory=list)
    hotwords: bool = True
    max_hotwords: int = 60
    vocabulary_refresh_s: float = 600.0
    # Windows: the tray's hotkey for listen (hotkey.py); "" = <Control><Alt>space, "off" = none.
    # Linux keeps it in a GNOME shortcut instead (strawberry hotkey).
    hotkey: str = ""


@dataclass
class BeatConfig:
    enabled: bool = True          # listen to the player's audio stream for the beat (doorways/beat_watch.py)
    target: str = ""              # PipeWire node or app name (Windows: spotify, chrome); empty = the running player
    interval_s: float = 2.0       # how often the estimate goes to the widget


@dataclass
class NotificationsConfig:
    ignore_apps: list[str] = field(default_factory=lambda: ["Spotify"])  # MPRIS already covers music
    only_apps: list[str] = field(default_factory=list)      # non-empty: forward these apps only
    min_urgency: str = "low"                                # low | normal | critical
    # What she does with a message body (WIRING.md §4): "off" = it never leaves the watcher, she knows
    # the app and the sender; "react" = Gemma reads it and reacts without quoting it; "glance" = a
    # neutral one-line gist first, then her quip. A sensitive body (codes, sign-ins, bank alerts) is
    # dropped whatever the mode.
    body: str = "off"
    body_apps: dict[str, str] = field(default_factory=dict)  # app name -> mode, case-insensitive
    max_body_chars: int = 1000                              # cut at a word boundary, with an ellipsis
    ignore_replacements: bool = True                        # updates to an existing notification (progress bars)
    coalesce_s: float = 2.0                                 # several within this window become one event

    def mode_for(self, *names: str) -> str:
        """The body mode for an app: its body_apps entry (app name or desktop entry), else `body`."""
        overrides = {k.lower(): v for k, v in self.body_apps.items()}
        for name in names:
            if name and name.lower() in overrides:
                return overrides[name.lower()]
        return self.body


BODY_MODES = ("off", "react", "glance")


@dataclass
class SpeechConfig:
    enabled: bool = False                                   # true: Piper reads every line aloud (Phase 4)
    voice: str = "en_GB-alba-medium"                        # Piper voice name in voices_dir, or a path to an .onnx
    voices_dir: str = ""                                    # default ~/.local/share/strawberry/voices
    speed: float = 1.0                                      # 1.25 = a quarter faster
    volume: float = 1.0
    # How lively the voice is (Piper's own variation; 0 = the voice's default, 0.667 and 0.8 for most).
    # Higher noise_scale varies pitch and tone more, higher noise_w the rhythm; past ~1.0 / ~1.3 it slurs.
    noise_scale: float = 0.0
    noise_w: float = 0.0
    quiet_hours: str = ""                                   # e.g. "22:00-08:00": bubble only, no sound
    max_chars: int = 400                                    # longer lines are cut before synthesis
    keep_files: int = 3                                     # recent wavs kept so a playing one is not deleted


@dataclass
class GateConfig:
    """The System One gate on spoken sentences (WIRING.md §8a)."""

    enabled: bool = True
    # Where embeddinggemma runs. "onnx": in the daemon's process on the CPU (embedder.py, ~20 ms a
    # sentence); "ollama": `model` through Ollama (~170 ms). Without the ONNX files the gate falls
    # back to Ollama and says so in /health.
    embedder: str = "onnx"
    onnx_dir: str = ""             # default ~/.local/share/strawberry/models/embeddinggemma-300m-onnx
    onnx_threads: int = 4          # ONNX Runtime's threads for one call; all the cores gave a worse p95
    model: str = "embeddinggemma"  # Ollama embedding model (embedder = "ollama", and the fallback)
    query_prefix: str = "task: classification | query: "   # embeddinggemma's prompt conventions;
    document_prefix: str = "title: none | text: "           # empty both for a model without them
    # What turns the sentence's embedding into answers. "head": the trained head (gatehead.py), which
    # carries its own act/offer; "nearest": the mean similarity of each option's nearest examples. A
    # head that is missing or does not fit the embedder or the questions falls back to "nearest".
    scorer: str = "head"
    head: str = ""                 # a head file; "" = the data dir's current head, else the shipped one
    neighbours: int = 2            # nearest: an option scores the mean of its N nearest examples
    temperature: float = 0.05      # nearest: softmax over those scores; lower = more decisive
    act: float = 0.6               # nearest: kind confidence at which a plain command may fire a reflex
    offer: float = 0.3             # nearest: below act, a label in the journal; the sentence goes to the thinker anyway
    topic_min: float = 0.2         # below this the topic is "other" and no tools are loaded
    timeout_s: float = 2.0         # one embedding call is ~165 ms on a GPU
    retry_timeout_s: float = 15.0  # a notification body's check that timed out waits this long for the
                                   # model to load and asks once more (a cold load is ~10 s); voice never waits
    # Extra phrases per option, keyed "kind.request", "topic.music", ...; a misread sentence
    # goes here and is fixed.
    examples: dict[str, list[str]] = field(default_factory=dict)


EMBEDDERS = ("onnx", "ollama")
SCORERS = ("head", "nearest")


@dataclass
class ToolsConfig:
    """MCP servers she can act through (WIRING.md §8b). The servers you list are the servers."""

    enabled: bool = True
    preconnect: bool = True        # connect at start (in the background) so the first request is quick
    result_chars: int = 2000       # a tool result is cut here before any model reads it
    connect_timeout_s: float = 20.0
    call_timeout_s: float = 20.0
    # name -> {topic, command, args, env, cwd, careful, confirm, adapter}; topic is one of the gate's (music,
    # calendar, notes, system); careful lists tools with consequences, offered to the thinker only when you ask
    # for such a change; confirm lists tools she asks about out loud first and runs only after a spoken yes
    # (missing: the adapter's own list, Spotify's two removals; [] asks about none, confirm.py); adapter names
    # one of strawberry/adapters/ when the server's own name does not (ADAPTERS.md); flags says what the
    # server is for the trust model (private, foreign, egress; trust.py): a server with no adapter and no
    # flags is all three, and flags only add to an adapter's; offer is when the thinker gets its tools:
    # always (the default), topic (a sentence of its topic) or asked (a sentence that asks for it).
    # A server reached over streamable HTTP has a `url` (https, or http on this machine) instead of a command,
    # and logs in with `strawberry tools login <name>` (remote.py); recall's adapter reads `workspaces`.
    # Empty by default: no server ships configured, and music control works over MPRIS without one.
    servers: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class ActionsConfig:
    """Acting on what you said (WIRING.md §8b): the reflex tier, and the thinker behind it."""

    enabled: bool = True
    mpris: bool = True             # skip/pause/resume/volume/what's playing over MPRIS for any desktop
                                   # player, when no configured server has a reflex for it (mpris.py;
                                   # on Windows the System Media Transport Controls, smtc.py)
    reflex: float = 0.6            # tool confidence at which a plain command fires the tool directly
    argument: float = 0.5          # p(has_argument) above this needs the thinker (Qwen) to fill it in
    timeout_s: float = 25.0        # the whole action, tools included; then she says it failed
                                   # (how long she waits for a yes is [approvals] change_s now; an old
                                   # `confirm_s` here is read as it, _migrate_actions)
    # (ledger_turns and ledger_age_s are [ledger] turns and window_minutes now; read as them, _migrate_ledger)


@dataclass
class ThinkerConfig:
    """The big model with the tools, in her voice, for everything but a bare reflex (WIRING.md §8b)."""

    enabled: bool = True           # false: Gemma answers spoken sentences as chat, as she used to
    model: str = ""                # "" = brain.action_model
    think: bool | str = False      # Ollama: false | "low" | "medium" | true (= xhigh); off: the gate routed already
    keep_alive: int | str = "30m"  # stays loaded this long after a request; a cold load is 7-17 s
    stream: bool = True            # read Ollama's reply as it is written, to count the run's tokens per
                                   # second (token_rate, numbers only); false: one reply at the end
    num_ctx: int = 8192
    num_predict: int = 300
    max_tools: int = 30            # more schemas than this and the list is cut: the gate's topic first,
                                   # then each adapter's common tools (25 Spotify tools are ~2400 tokens)
    tool_tokens: int = 4000        # …and at most this many prompt tokens of them, by the thinker's own
                                   # estimate (3 characters a token; Thinker.fit); 0: no budget
    max_rounds: int = 6            # tool rounds before she has to answer honestly with what she has
    timeout_s: float = 45.0        # the whole request, cold load included
    ack_after_s: float = 2.5       # silent thinking pose first; a spoken ack only if the reply takes longer
    still_on_it_s: float = 8.0     # she says so once if it takes longer than this
    # Deprecated: the cover lines are persona.md's `cover.ack` now. Set here, they still replace them.
    acks: list[str] = field(default_factory=list)


@dataclass
class LedgerConfig:
    """Her short memory (ledger.py, WIRING.md §23): one timeline of the user's turns and what she reacted to
    on her own (a commit, a notification, a track), given to the thinker with each entry's age."""

    turns: int = 8                 # the user's last this many exchanges…
    notices: int = 8               # …and this many of the things she reacted to on her own (0: none)…
    window_minutes: float = 60.0   # …none older than this
    foreign_minutes: float = 10.0  # a notice with strangers' text (a sender, a track) makes a run ask before
                                   # changes for this long, and is then left out of the thinker's lines


@dataclass
class MessagesConfig:
    """Her inbox (inbox.py, WIRING.md §24): the notifications she got, for "any new messages?", in memory
    only. A body is kept only as far as `[notifications] body` lets it reach a model; off, she knows who
    wrote and where."""

    enabled: bool = True           # keep an inbox and give the big model its read-only tools
    keep: int = 100                # at most this many notifications…
    max_age_hours: float = 24.0    # …none older than this


@dataclass
class LearningConfig:
    """The router's learning loop (WIRING.md §8c, §8d): what came of each routed sentence, and the
    labels, the candidate heads and the switch built on it. Off by default: on, the sentences you
    say or type are kept in a local file (outcomes.py), and so are the examples learned from them."""

    log_outcomes: bool = False     # keep routed sentences and their outcomes in <state>/outcomes.jsonl
    max_days: int = 30             # records older than this are pruned…
    max_records: int = 5000        # …and at most this many are kept, the newest
    undo_s: float = 10.0           # the opposite reflex this soon after one (skip, then previous) is an undo
    rephrase_s: float = 10.0       # a close sentence, or a correction ("no, I meant…"), this soon after is about it
    silence_s: float = 30.0        # nothing said for this long after a sentence: the weak "that was right"
    rephrase_similarity: float = 0.8   # cosine of the gate's two embeddings from which a sentence is a rephrase
    # The second half (learning.py, §8d): a candidate head from the labels, held to the held-out set.
    idle_train: bool = True        # the daemon trains a candidate on its own when idle (strawberry learning train by hand)
    idle_minutes: float = 20.0     # …once nobody has spoken for this long…
    min_new_labels: int = 10       # …and at least this many labelled sentences are new since the last run
    auto_switch: bool = False      # true: a candidate that passes the held-out check is put in use at once;
                                   # false: it waits for `strawberry learning accept`
    weekly_line: bool = False      # once a week she says what she learned, outside quiet hours
    max_share: float = 0.25        # the user's labels weigh at most this share of the data set's, per option


@dataclass
class RunsConfig:
    """Runs (WIRING.md §18): every sentence she handles, its steps on the bus and a way to stop it."""

    events: bool = True            # each run's steps go to the bodies that ask (the widget's chip) and the Brain UI
    supersede: bool = True         # a new sentence stops the one she is still on (not an answer to her question);
                                   # false: it waits its turn
    keep: int = 50                 # finished runs kept in memory for the Brain UI


# The approval tiers, lowest first (approvals.py, WIRING §19, §20). `playback` is a change to what plays and
# how, local to the user's player and undone in a second (play, pause, skip, volume, the queue): unlike
# `change`, it does not wait for a yes once strangers' text is in the conversation.
RISKS = ("read", "playback", "change", "sends", "destructive")
TRUST_FLAGS = ("private", "foreign", "egress")     # = trust.FLAGS
GESTURE_WATCH = ("always", "armed")
# = gestures.DEFAULT_MAP (a test keeps the two the same): what a gesture does once [gestures] is on
DEFAULT_GESTURE_MAP = {"thumb_up": "like", "swipe_left": "previous", "swipe_right": "skip", "palm_hold": "pause",
                       "point_hold": "listen"}
OFFERS = ("always", "topic", "asked")


@dataclass
class ApprovalsConfig:
    """Approvals (WIRING.md §19): a call she asks about first waits for a yes, bound to that exact call.
    A yes is needed for a server's `confirm` list and for every call of the `sends` or `destructive`
    tier; no answer in time is a no."""

    change_s: float = 10.0         # how long she waits for a yes to a `change` call (or a listed `read`)…
    sends_s: float = 30.0          # …to one that sends something to someone (a message, an email)…
    destructive_s: float = 30.0    # …and to one that deletes or cannot be undone. Not counted while she
                                   # is listening to the answer…
    grace_s: float = 10.0          # …for at most this much longer past the wait (a stuck mic cannot hold it open)
    hold: list[str] = field(default_factory=lambda: ["sends", "destructive"])
                                   # tiers whose yes on a body's card must be a press-and-hold
    # A tool's tier, by "server.tool" or a whole "server": read | playback | change | sends | destructive, taken as
    # written. Without one: the adapter's own tier, else `change`, raised to `destructive` when the server
    # marks the tool destructiveHint (an annotation only ever raises a tier).
    risk: dict[str, str] = field(default_factory=dict)


@dataclass
class TouchConfig:
    """Input from bodies (WIRING.md §25, PROTOCOL Part 1c): what a touch on an entity does, and how long what
    the user points at stays in her situation line."""

    target_s: float = 8.0          # a target holds this long after the body's last `target`
    cooldown_s: float = 1.0        # at least this long between two touch actions (a drag repeats fast)
    # entity id -> touch kind -> a reflex (bodylink.TOUCH_ACTIONS): written `music.flick = "next"` in [touch].
    # Empty by default: a touch is shown to the other bodies and never acted on. Only `read` and `playback`
    # reflexes can be mapped, since a touch cannot answer a question (_validate_touch).
    actions: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class GesturesConfig:
    """The camera doorway (doorways/gesture_watch.py, WIRING.md §26): hand gestures run the reflexes voice has.
    Off by default; MediaPipe on the CPU; only gesture names and a hand's landmarks leave the watcher."""

    enabled: bool = False          # opt-in: the camera is never opened while this is false
    camera: str = ""               # "" = the first camera; an index ("1") or a device ("/dev/video2")
    watch: str = "always"          # always: while enabled the camera is open, scanning at idle_fps for a raised
                                   # hand and at fps while one is up; armed: closed until `strawberry gestures arm`
                                   # (or an approval she shows), then open for armed_s after the last hand seen
    fps: float = 15.0              # frames a second while a hand is up (swipes need 10 or more)
    idle_fps: float = 4.0          # …and while looking for one
    hold_ms: float = 400.0         # a shape must be held this long to count; the bodies show it filling
    arming: bool = False           # true: a raised open palm, held, turns command mode on for armed_s first
    armed_s: float = 8.0
    approvals: bool = True         # a held thumbs up or down answers the approval she shows (not a hold tier)
    zone: float = 0.8              # a hand counts only with its wrist above this line (0 top, 1 bottom)…
    min_size: float = 0.12         # …and at least this big (of the frame's height): raised toward the screen
    hand_hz: float = 15.0          # the hand's position to the bodies that ask for it, at most this often
    map: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_GESTURE_MAP))


@dataclass
class Config:
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    speech: SpeechConfig = field(default_factory=SpeechConfig)
    beat: BeatConfig = field(default_factory=BeatConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    actions: ActionsConfig = field(default_factory=ActionsConfig)
    thinker: ThinkerConfig = field(default_factory=ThinkerConfig)
    ledger: LedgerConfig = field(default_factory=LedgerConfig)
    messages: MessagesConfig = field(default_factory=MessagesConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    runs: RunsConfig = field(default_factory=RunsConfig)
    approvals: ApprovalsConfig = field(default_factory=ApprovalsConfig)
    touch: TouchConfig = field(default_factory=TouchConfig)
    gestures: GesturesConfig = field(default_factory=GesturesConfig)
    path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "daemon": asdict(self.daemon),
            "brain": asdict(self.brain),
            "media": asdict(self.media),
            "notifications": asdict(self.notifications),
            "speech": asdict(self.speech),
            "beat": asdict(self.beat),
            "voice": asdict(self.voice),
            "gate": asdict(self.gate),
            "tools": asdict(self.tools),
            "actions": asdict(self.actions),
            "thinker": asdict(self.thinker),
            "ledger": asdict(self.ledger),
            "messages": asdict(self.messages),
            "learning": asdict(self.learning),
            "runs": asdict(self.runs),
            "approvals": asdict(self.approvals),
            "touch": asdict(self.touch),
            "gestures": asdict(self.gestures),
        }
        out["path"] = str(self.path) if self.path else None
        return out


_SECTIONS = {
    "daemon": DaemonConfig,
    "brain": BrainConfig,
    "media": MediaConfig,
    "notifications": NotificationsConfig,
    "speech": SpeechConfig,
    "beat": BeatConfig,
    "voice": VoiceConfig,
    "gate": GateConfig,
    "tools": ToolsConfig,
    "actions": ActionsConfig,
    "thinker": ThinkerConfig,
    "ledger": LedgerConfig,
    "messages": MessagesConfig,
    "learning": LearningConfig,
    "runs": RunsConfig,
    "approvals": ApprovalsConfig,
    "touch": TouchConfig,
    "gestures": GesturesConfig,
}


def _apply(section_name: str, target: Any, values: dict[str, Any]) -> None:
    known = {f.name: f for f in fields(target)}
    for key, value in values.items():
        if key not in known:
            log.warning("config: unknown key %s.%s ignored", section_name, key)
            continue
        current = getattr(target, key)
        expected = type(current)
        # Ollama's keep_alive is seconds as a number or a duration string ("10m"); both are fine.
        if key == "keep_alive":
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise ConfigError(f"{section_name}.{key} must be an int (seconds, -1 = forever) or a duration string like \"10m\"")
            setattr(target, key, value)
            continue
        if key == "think":
            if not (isinstance(value, bool) or value in ("low", "medium")):
                raise ConfigError(f"{section_name}.{key} must be true, false, \"low\" or \"medium\"")
            setattr(target, key, value)
            continue
        # TOML integers are fine where we hold a float; nothing else is coerced.
        if expected is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            raise ConfigError(f"{section_name}.{key} must be {expected.__name__}, got {type(value).__name__}")
        setattr(target, key, value)


def _validate(config: Config) -> None:
    for example in config.brain.examples:
        missing = {"event", "line", "emotion"} - set(example)
        if missing:
            raise ConfigError(f"brain.examples entries need event, line, emotion; missing {sorted(missing)}")
        if example["emotion"] not in ("neutral", "happy", "alert", "angry"):
            raise ConfigError(f"brain.examples emotion {example['emotion']!r} is not one of neutral/happy/alert/angry")
    if config.brain.timeout_s <= 0:
        raise ConfigError("brain.timeout_s must be positive")
    if config.notifications.min_urgency not in ("low", "normal", "critical"):
        raise ConfigError("notifications.min_urgency must be low, normal, or critical")
    if config.notifications.body not in BODY_MODES:
        raise ConfigError("notifications.body must be off, react, or glance")
    for app, mode in config.notifications.body_apps.items():
        if mode not in BODY_MODES:
            raise ConfigError(f"notifications.body_apps.{app} must be off, react, or glance")
    if config.notifications.max_body_chars < 20:
        raise ConfigError("notifications.max_body_chars must be >= 20")
    if config.notifications.coalesce_s < 0:
        raise ConfigError("notifications.coalesce_s must be >= 0")
    if not (1 <= config.daemon.port <= 65535):
        raise ConfigError("daemon.port must be 1-65535")
    if not (0.25 <= config.speech.speed <= 4.0):
        raise ConfigError("speech.speed must be between 0.25 and 4")
    if config.speech.volume < 0:
        raise ConfigError("speech.volume must be >= 0")
    for knob in ("noise_scale", "noise_w"):
        if not (0.0 <= getattr(config.speech, knob) <= 2.0):
            raise ConfigError(f"speech.{knob} must be between 0 (the voice's own) and 2")
    if config.speech.keep_files < 1:
        raise ConfigError("speech.keep_files must be >= 1")
    if config.voice.max_seconds <= 0 or config.voice.silence_s <= 0:
        raise ConfigError("voice.max_seconds and voice.silence_s must be positive")
    if config.voice.device not in ("cpu", "cuda", "auto"):
        raise ConfigError("voice.device must be cpu, cuda, or auto")
    if not all(isinstance(w, str) for w in config.voice.vocabulary):
        raise ConfigError("voice.vocabulary must be a list of strings")
    try:
        from .hotkey import windows_hotkey

        windows_hotkey(config.voice.hotkey)
    except ValueError as exc:
        raise ConfigError(f"voice.hotkey: {exc}") from None
    if config.gate.embedder not in EMBEDDERS:
        raise ConfigError("gate.embedder must be onnx or ollama")
    if config.gate.scorer not in SCORERS:
        raise ConfigError("gate.scorer must be head or nearest")
    if not (1 <= config.gate.onnx_threads <= 64):
        raise ConfigError("gate.onnx_threads must be 1-64")
    if config.gate.temperature <= 0:
        raise ConfigError("gate.temperature must be positive")
    if config.gate.timeout_s <= 0 or config.gate.retry_timeout_s < 0:
        raise ConfigError("gate.timeout_s must be positive and gate.retry_timeout_s not negative")
    if config.gate.neighbours < 1:
        raise ConfigError("gate.neighbours must be >= 1")
    if not (0.0 <= config.gate.offer <= config.gate.act <= 1.0):
        raise ConfigError("gate thresholds need 0 <= offer <= act <= 1")
    for key, phrases in config.gate.examples.items():
        if not isinstance(phrases, list) or not all(isinstance(p, str) for p in phrases):
            raise ConfigError(f"gate.examples.{key} must be a list of strings")
        if key.split(".")[0] not in ("kind", "topic"):
            raise ConfigError(f"gate.examples key {key!r} must start with kind. or topic.")
    for name, server in config.tools.servers.items():
        if not isinstance(server, dict):
            raise ConfigError(f"tools.servers.{name} must be a table")
        command, url = server.get("command"), server.get("url")
        if (command is None) == (url is None):
            raise ConfigError(f"tools.servers.{name} needs a command or a url (one of them)")
        if command is not None and not (isinstance(command, str) and command):
            raise ConfigError(f"tools.servers.{name} needs a command")
        if url is not None:
            from .remote import url_problem   # local: it brings the SDK's HTTP client

            why = url_problem(url)
            if why:
                raise ConfigError(f"tools.servers.{name}.url: {why}")
        workspaces = server.get("workspaces", [])
        if not isinstance(workspaces, list) or not all(isinstance(w, str) for w in workspaces):
            raise ConfigError(f"tools.servers.{name}.workspaces must be a list of workspace names or ids")
        if not isinstance(server.get("topic", "other"), str):
            raise ConfigError(f"tools.servers.{name}.topic must be a string")
        if not all(isinstance(a, str) for a in server.get("args", [])):
            raise ConfigError(f"tools.servers.{name}.args must be a list of strings")
        env = server.get("env", {})
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ConfigError(f"tools.servers.{name}.env must be a table of strings")
        for key in ("careful", "confirm"):
            listed = server.get(key, [])
            if not isinstance(listed, list) or not all(isinstance(t, str) for t in listed):
                raise ConfigError(f"tools.servers.{name}.{key} must be a list of tool names")
        if "adapter" in server and not (isinstance(server["adapter"], str) and server["adapter"].strip()):
            raise ConfigError(f"tools.servers.{name}.adapter must be an adapter name, e.g. \"spotify\" (ADAPTERS.md)")
        flags = server.get("flags", [])
        if not isinstance(flags, list) or not all(f in TRUST_FLAGS for f in flags):
            raise ConfigError(f"tools.servers.{name}.flags must list some of {', '.join(TRUST_FLAGS)} (trust.py)")
        if server.get("offer", "always") not in OFFERS:
            raise ConfigError(f"tools.servers.{name}.offer must be one of {', '.join(OFFERS)}")
        unknown = set(server) - {"topic", "command", "url", "args", "env", "cwd", "careful", "confirm", "adapter",
                                 "flags", "offer", "workspaces"}
        if unknown:
            raise ConfigError(f"tools.servers.{name}: unknown keys {sorted(unknown)}")
    if config.tools.result_chars < 100:
        raise ConfigError("tools.result_chars must be >= 100")
    if not (0.0 <= config.actions.reflex <= 1.0 and 0.0 <= config.actions.argument <= 1.0):
        raise ConfigError("actions.reflex and actions.argument must be between 0 and 1")
    ledger = config.ledger
    if ledger.turns < 1 or ledger.notices < 0 or ledger.window_minutes <= 0 or ledger.foreign_minutes < 0:
        raise ConfigError("ledger.turns >= 1, ledger.notices >= 0, ledger.window_minutes > 0 and "
                          "ledger.foreign_minutes >= 0 are required")
    if config.messages.keep < 1 or config.messages.max_age_hours <= 0:
        raise ConfigError("messages.keep >= 1 and messages.max_age_hours > 0 are required")
    approvals = config.approvals
    if min(approvals.change_s, approvals.sends_s, approvals.destructive_s) <= 0:
        raise ConfigError("approvals.change_s, sends_s and destructive_s must be positive (how long she waits for a yes)")
    if approvals.grace_s < 0:
        raise ConfigError("approvals.grace_s must not be negative")
    if not all(tier in RISKS for tier in approvals.hold):
        raise ConfigError(f"approvals.hold must list tiers of {', '.join(RISKS)}")
    # `spotify.remove_saved_tracks = "…"` unquoted in a [approvals.risk] table is a nested table in TOML.
    flat: dict[str, Any] = {}
    for key, tier in approvals.risk.items():
        if isinstance(tier, dict):
            flat |= {f"{key}.{tool}": inner for tool, inner in tier.items()}
        else:
            flat[key] = tier
    approvals.risk = flat
    for key, tier in approvals.risk.items():
        if not key.strip() or tier not in RISKS:
            raise ConfigError(f"approvals.risk.{key} must be one of {', '.join(RISKS)} "
                              "(a key is \"server.tool\" or \"server\")")
    if config.thinker.max_rounds < 1 or config.thinker.timeout_s <= 0 or config.thinker.num_ctx < 1024:
        raise ConfigError("thinker.max_rounds >= 1, timeout_s > 0 and num_ctx >= 1024 are required")
    if not (1 <= config.thinker.num_predict <= config.thinker.num_ctx // 2):
        raise ConfigError("thinker.num_predict must be between 1 and half of num_ctx (the prompt needs the rest)")
    if config.thinker.max_tools < 1:
        raise ConfigError("thinker.max_tools must be >= 1 (it caps the tool schemas in the prompt)")
    if not 0 <= config.thinker.tool_tokens < config.thinker.num_ctx:
        raise ConfigError("thinker.tool_tokens must be between 0 (no budget) and num_ctx")
    if not all(isinstance(a, str) and a for a in config.thinker.acks):
        raise ConfigError("thinker.acks must be a list of strings (and is deprecated: persona.md's cover.ack)")
    learning = config.learning
    if learning.max_days < 1 or learning.max_records < 1:
        raise ConfigError("learning.max_days and learning.max_records must be >= 1")
    if learning.undo_s <= 0 or learning.rephrase_s <= 0 or learning.silence_s < max(learning.undo_s, learning.rephrase_s):
        raise ConfigError("learning.undo_s and learning.rephrase_s must be positive, and learning.silence_s at least both")
    if not (0.0 < learning.rephrase_similarity <= 1.0):
        raise ConfigError("learning.rephrase_similarity must be above 0 and at most 1")
    if learning.idle_minutes <= 0 or learning.min_new_labels < 1:
        raise ConfigError("learning.idle_minutes must be positive and learning.min_new_labels >= 1")
    if not (0.0 < learning.max_share <= 1.0):
        raise ConfigError("learning.max_share must be above 0 and at most 1")
    if not (1 <= config.runs.keep <= 1000):
        raise ConfigError("runs.keep must be between 1 and 1000")
    _validate_touch(config)
    _validate_gestures(config.gestures)
    from .speech import parse_quiet_hours  # local: speech imports SpeechConfig from here

    try:
        parse_quiet_hours(config.speech.quiet_hours)
    except ValueError as exc:
        raise ConfigError(f"speech.{exc}") from exc


def _touch_table(values: dict[str, Any]) -> dict[str, Any]:
    """[touch] as written: its settings, and a table per entity (`music.flick = "next"` is {"music": {"flick":
    "next"}} in TOML), which become `actions`."""
    out = {key: value for key, value in values.items() if not isinstance(value, dict)}
    tables = {key: value for key, value in values.items() if isinstance(value, dict)}
    if tables:
        out["actions"] = tables
    return out


def _validate_touch(config: Config) -> None:
    """[touch]: the times, then each mapping: an entity id, a touch kind and a reflex of `read` or `playback`.
    A touch cannot answer a question, so a mapping whose calls would wait for a yes is refused here: a reflex
    above `playback`, or one whose server's tools `[approvals] risk` (or the server's `confirm` list) puts
    there. The daemon checks the tiers again before each touch action (Actor.asks_first)."""
    from .bodylink import ID, TOUCH_ACTIONS, TOUCH_KINDS, action_name   # local: bodylink is the protocol side

    touch = config.touch
    if not (0 < touch.target_s <= 60) or touch.cooldown_s < 0:
        raise ConfigError("touch.target_s must be above 0 and at most 60, and touch.cooldown_s not negative")
    if not touch.actions:
        return
    from .adapters import adapter_for   # local: the adapters import actions, which imports this module

    mapped: dict[str, dict[str, str]] = {}
    for entity, kinds in touch.actions.items():
        if not isinstance(entity, str) or ID.fullmatch(entity) is None or not isinstance(kinds, dict):
            raise ConfigError(f"touch.{entity}: an entity id is a lowercase word of at most 24 (a letter, then a-z, 0-9, _ and -), and "
                              f"its touches are written {entity}.flick = \"next\"")
        for kind, value in kinds.items():
            key = f"touch.{entity}.{kind}"
            if kind not in TOUCH_KINDS:
                raise ConfigError(f"{key}: the touch kinds are {', '.join(TOUCH_KINDS)}")
            action = action_name(value) if isinstance(value, str) else ""
            if not action:
                raise ConfigError(f"{key} = {value!r}: not a reflex a touch can do; one of "
                                  f"{', '.join(sorted(TOUCH_ACTIONS))} (\"next\" is skip)")
            tier = _touch_tier(config, action, TOUCH_ACTIONS[action], adapter_for)
            if RISKS.index(tier) > RISKS.index("playback"):
                raise ConfigError(f"{key} = {value!r}: {action} would be a {tier} call here, which waits for the "
                                  "user's yes; a touch cannot answer a question, so only read and playback "
                                  "reflexes can be mapped (approvals by gesture come later)")
            mapped.setdefault(entity, {})[kind] = action
    touch.actions = mapped


def _touch_tier(config: Config, action: str, tier: str, adapter_for: Any) -> str:
    """The highest tier `action` can reach with this config: its own, raised by an `[approvals] risk` entry for a
    tool it calls on a configured server ("server.tool", or the whole server), and `change` for a tool on that
    server's `confirm` list (its question needs a yes)."""
    highest = RISKS.index(tier)
    for name, table in config.tools.servers.items():
        table = table if isinstance(table, dict) else {}
        adapter = adapter_for(name, table)
        tools = (getattr(adapter, "reflex_tools", None) or {}).get(action, ()) if adapter is not None else ()
        for tool in tools:
            for key in (f"{name}.{tool}", name):
                given = config.approvals.risk.get(key)
                if given in RISKS:
                    highest = max(highest, RISKS.index(given))
            if tool in (table.get("confirm") or ()):
                highest = max(highest, RISKS.index("change"))
    return RISKS[highest]


def _validate_gestures(gestures: GesturesConfig) -> None:
    """[gestures] (WIRING.md §26): the map may name only read and playback actions (gestures.check_map)."""
    from .gestures import GestureError, check_map   # local: gestures.py reads paths, and tests import it alone

    if gestures.watch not in GESTURE_WATCH:
        raise ConfigError(f"gestures.watch must be one of {', '.join(GESTURE_WATCH)}")
    if not (1.0 <= gestures.fps <= 60.0) or not (0.5 <= gestures.idle_fps <= gestures.fps):
        raise ConfigError("gestures.fps must be 1-60 and gestures.idle_fps 0.5 up to fps")
    if not (100.0 <= gestures.hold_ms <= 3000.0):
        raise ConfigError("gestures.hold_ms must be between 100 and 3000")
    if not (1.0 <= gestures.armed_s <= 120.0):
        raise ConfigError("gestures.armed_s must be between 1 and 120")
    if not (0.1 <= gestures.zone <= 1.0) or not (0.0 <= gestures.min_size < 1.0):
        raise ConfigError("gestures.zone must be 0.1-1 and gestures.min_size 0 up to 1")
    if not (1.0 <= gestures.hand_hz <= 30.0):
        raise ConfigError("gestures.hand_hz must be between 1 and 30")
    try:
        gestures.map = check_map(gestures.map)
    except GestureError as exc:
        raise ConfigError(str(exc)) from None


def _migrate_notifications(values: dict[str, Any]) -> dict[str, Any]:
    """`include_body = true|false` (until 2026-09-22) reads as `body = "react"|"off"`, with a warning."""
    if "include_body" not in values:
        return values
    values = dict(values)
    old = values.pop("include_body")
    if not isinstance(old, bool):
        raise ConfigError('notifications.include_body must be bool (and is deprecated: use body = "off" | "react" | "glance")')
    if "body" in values:
        log.warning("config: notifications.include_body is deprecated and ignored; body = %r is set", values["body"])
    else:
        values["body"] = "react" if old else "off"
        log.warning('config: notifications.include_body is deprecated; read as body = %r (use body = "off" | "react" '
                    '| "glance", WIRING.md §4)', values["body"])
    return values


def _migrate_actions(data: dict[str, Any]) -> dict[str, Any]:
    """`[actions] confirm_s` (until brain step 6, stage 2) reads as `[approvals] change_s`, with a warning."""
    actions = data.get("actions")
    if not isinstance(actions, dict) or "confirm_s" not in actions:
        return data
    data, actions = dict(data), dict(actions)
    old = actions.pop("confirm_s")
    data["actions"] = actions
    approvals = data.get("approvals") if isinstance(data.get("approvals"), dict) else {}
    if "change_s" in approvals:
        log.warning("config: actions.confirm_s is deprecated and ignored; approvals.change_s = %r is set",
                    approvals["change_s"])
    else:
        data["approvals"] = approvals | {"change_s": old}
        log.warning("config: actions.confirm_s is deprecated; read as approvals.change_s = %r (WIRING.md §19)", old)
    return data


def _deprecated_persona(config: Config) -> None:
    """`[brain] persona`, `[brain] examples` and `[thinker] acks` (until the persona stage) still replace what
    persona.md says, with a warning: her persona, her examples and her cover lines live there now."""
    for key, value in (("brain.persona", config.brain.persona), ("brain.examples", config.brain.examples),
                       ("thinker.acks", config.thinker.acks)):
        if value:
            log.warning("config: %s is deprecated and still used, over persona.md; move it to %s (persona.md, "
                        "WIRING.md §21) and delete it here", key, paths.persona_file())


def _migrate_ledger(data: dict[str, Any]) -> dict[str, Any]:
    """`[actions] ledger_turns` and `ledger_age_s` (until the persona stage) read as `[ledger] turns` and
    `window_minutes`, with a warning; a value [ledger] sets itself wins."""
    actions = data.get("actions")
    if not isinstance(actions, dict) or not ({"ledger_turns", "ledger_age_s"} & set(actions)):
        return data
    data, actions = dict(data), dict(actions)
    ledger = dict(data["ledger"]) if isinstance(data.get("ledger"), dict) else {}
    for old, new, convert in (("ledger_turns", "turns", lambda v: v),
                              ("ledger_age_s", "window_minutes", lambda v: v / 60.0 if isinstance(v, (int, float))
                               and not isinstance(v, bool) else v)):
        if old not in actions:
            continue
        value = actions.pop(old)
        if new in ledger:
            log.warning("config: actions.%s is deprecated and ignored; ledger.%s = %r is set", old, new, ledger[new])
        else:
            ledger[new] = convert(value)
            log.warning("config: actions.%s is deprecated; read as ledger.%s = %r (WIRING.md §23)", old, new,
                        ledger[new])
    data["actions"], data["ledger"] = actions, ledger
    return data


def load(path: Path | None = None, env: dict[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    path = path or default_path()
    config = Config(path=path if path.exists() else None)
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
        data = _migrate_actions(data)
        data = _migrate_ledger(data)
        for section_name, values in data.items():
            if section_name not in _SECTIONS:
                log.warning("config: unknown section [%s] ignored", section_name)
                continue
            if not isinstance(values, dict):
                raise ConfigError(f"[{section_name}] must be a table")
            if section_name == "notifications":
                values = _migrate_notifications(values)
            if section_name == "touch":
                values = _touch_table(values)
            _apply(section_name, getattr(config, section_name), values)
    _deprecated_persona(config)
    if "STRAWBERRYD_HOST" in env:
        config.daemon.host = env["STRAWBERRYD_HOST"]
    if "STRAWBERRYD_PORT" in env:
        config.daemon.port = int(env["STRAWBERRYD_PORT"])
    if "STRAWBERRYD_LOG" in env:
        config.daemon.log_level = env["STRAWBERRYD_LOG"]
    _validate(config)
    return config


def default_toml() -> str:
    """A commented template with every default, for `strawberryd --init-config`."""
    b = BrainConfig()
    d = DaemonConfig()
    n = NotificationsConfig()
    s = SpeechConfig()
    g = GateConfig()
    lr = LearningConfig()
    ru = RunsConfig()
    ap = ApprovalsConfig()
    th = ThinkerConfig()
    lines = [
        "# Strawberry settings. Every key is optional; these are the defaults.",
        "# Restart the daemon after editing: bin/strawberry stop && bin/strawberry daemon",
        "",
        "[daemon]",
        f'host = "{d.host}"',
        f"port = {d.port}",
        f'log_level = "{d.log_level}"',
        f"warm_on_wake = {str(d.warm_on_wake).lower()}            # reload the gate and reaction models after a suspend",
        f"log_sentences = {str(d.log_sentences).lower()}          # true: what you say or type is written to the log as it is;",
        "                               # false: only its length (\"<sentence, 23 chars>\")",
        "",
        "[brain]",
        f"enabled = {str(b.enabled).lower()}          # false: canned one-liners, no model",
        f'reaction_model = "{b.reaction_model}"   # the reflex, one line per event (bake-off: scripts/bakeoff_*.json)',
        f'action_model = "{b.action_model}"    # the thinker, voice commands with tools (later)',
        f'ollama_url = "{b.ollama_url}"',
        f"timeout_s = {b.timeout_s}                # slower than this and she uses the canned line",
        f"temperature = {b.temperature}",
        f"max_words = {b.max_words}",
        f'keep_alive = {b.keep_alive}               # seconds; -1 keeps the model in VRAM, "10m" lets it unload',
        "",
        "# Her persona, her example exchanges and her fixed lines are in persona.md beside this file",
        "# (the Brain UI's Persona tab edits it; without one she uses the shipped persona).",
        "",
        "[media]",
        "only = []      # e.g. [\"spotify\"] to follow one player; empty = every player (MPRIS; SMTC on Windows)",
        "ignore = []    # e.g. [\"firefox\"]",
        "",
        "[notifications]",
        f"ignore_apps = {json.dumps(n.ignore_apps)}   # music is covered by the media doorway",
        "only_apps = []              # non-empty: forward only these apps",
        f'min_urgency = "{n.min_urgency}"         # low | normal | critical',
        "# Message bodies. off: the body never leaves the watcher; she knows the app and the sender.",
        "# react: Gemma reads it and reacts in her own words, never quoting it.",
        "# glance: a plain one-line gist first (\"Alex asks about lunch at noon.\"), then her quip.",
        "# Codes, sign-ins and bank alerts are dropped in every mode (\"Slack sent something private.\").",
        "# react and glance need [gate] (embeddinggemma): with the gate down every body counts as private.",
        f'body = "{n.body}"',
        '# body_apps = { Slack = "glance", Signal = "off" }   # per app, case-insensitive; overrides body',
        f"max_body_chars = {n.max_body_chars}         # cut at a word boundary",
        f"ignore_replacements = {str(n.ignore_replacements).lower()}  # progress-bar style updates to an existing notification",
        f"coalesce_s = {n.coalesce_s}            # several within this window become one \"N notifications\" event",
        "",
        "[speech]",
        f"enabled = {str(s.enabled).lower()}             # true: she reads every line aloud (Piper, CPU)",
        f'voice = "{s.voice}"   # a name in voices_dir, or a path to an .onnx',
        '# voices_dir = "~/.local/share/strawberry/voices"',
        "# Install a voice:  strawberry voices en_US-amy-medium    (list at rhasspy.github.io/piper-samples)",
        f"speed = {s.speed}                 # 1.25 = a quarter faster",
        f"volume = {s.volume}",
        f"noise_scale = {s.noise_scale}           # liveliness of pitch and tone: 0 = the voice's own (0.667); try 0.8-0.9",
        f"noise_w = {s.noise_w}               # liveliness of rhythm: 0 = the voice's own (0.8); try 1.0-1.15",
        f'quiet_hours = "{s.quiet_hours}"           # e.g. "22:00-08:00": bubble only, no sound',
        f"max_chars = {s.max_chars}",
        "",
        "[beat]",
        "enabled = true                 # listen to the player's own audio stream and dance to its beat",
        'target = ""                    # PipeWire node/app name (Windows: spotify, chrome); empty = whichever plays',
        "interval_s = 2.0",
        "",
        "[voice]",
        "enabled = true                 # hotkey (strawberry hotkey) -> she listens -> faster-whisper -> answers",
        'model = "small"                # tiny | base | small | medium | large-v3; small is ~460 MB, ~1 s on CPU',
        'language = ""                  # "" detects; "en" is faster and steadier',
        'device = "cpu"                 # "cuda" if the GPU has room next to Ollama',
        'fallback_model = ""            # CUDA out of memory: the CPU with this size until a restart; "" = model',
        'source = ""                    # microphone name fragment (pactl list sources short); "" = auto',
        "bluetooth = true               # auto prefers a Bluetooth headset mic (its profile is switched while she listens)",
        "max_seconds = 15.0",
        "silence_s = 1.1                # quiet after speech that ends the recording",
        'vocabulary = []                # names she should recognise, e.g. ["Lighthouse", "Alex"]; artists come from Spotify',
        "# hotkey = \"<Control><Alt>space\"   # Windows: the tray's key for listen (\"off\" = none); strawberry hotkey sets it",
        "",
        "[gate]",
        "enabled = true                 # sorts what you said: chat, or a request/question for the action path",
        'embedder = "onnx"              # onnx: embeddinggemma in this process, CPU (strawberry setup fetches it);',
        '                               # ollama: through Ollama, and the fallback when the ONNX files are missing',
        '# onnx_dir = "~/.local/share/strawberry/models/embeddinggemma-300m-onnx"',
        "onnx_threads = 4               # CPU threads for one embedding",
        'model = "embeddinggemma"       # Ollama embedding model (ollama pull embeddinggemma)',
        f'scorer = "{g.scorer}"                # head: the trained head, with its own thresholds (strawberry gate eval);',
        "                               # nearest: each option's nearest examples; a head that does not fit falls back",
        '# head = ""                    # a head file; empty: the data dir\'s current head, else the shipped one',
        "act = 0.6                      # nearest: confidence at which a plain command may fire a reflex",
        "offer = 0.3                    # nearest: below act, a label in the journal; the sentence goes to the thinker",
        "retry_timeout_s = 15.0         # a notification check that timed out waits this long for the model, once",
        "# [gate.examples]              # a sentence she misreads goes under the option it belongs to",
        '# \"kind.request\" = ["put the kettle on"]',
        '# \"topic.music\" = ["what year is this from"]',
        "",
        "[tools]",
        "enabled = true                 # MCP servers she acts through; the servers you list are the servers",
        "result_chars = 2000            # tool results are cut here before a model reads them",
        "",
        "# No server is configured by default: skip, pause, resume, volume and \"what's playing\" already",
        "# work for any desktop player over MPRIS. Add a server to give her more, one table each.",
        "# A server whose name (or `adapter`) matches one of strawberry/adapters/ also gets that adapter:",
        "# its reflexes, its situation line, names for the recogniser, its error wording. See ADAPTERS.md.",
        "#",
        "# [tools.servers.spotify]                # the name matches the shipped Spotify adapter",
        '# topic = "music"                        # one of the gate\'s topics: music, calendar, notes, system',
        '# command = "spotify-mcp"                # or a full path, e.g. ~/spotify-mcp/.venv/bin/spotify-mcp',
        "# args = []",
        "# env = {}",
        '# adapter = "spotify"                    # only when the server\'s own name does not say so',
        "# careful = [\"save_tracks\", \"remove_saved_tracks\", \"add_to_playlist\", \"remove_from_playlist\",",
        '#            "create_playlist"]',
        "#                                        # offered only when you ask for such a change; like_current,",
        "#                                        # add_current_to_playlist and play_liked are easy to undo and stay offered",
        "# confirm = [\"remove_from_playlist\", \"remove_saved_tracks\"]",
        "#                                        # she asks first (\"Remove 'Teardrop' from Gym? Say yes.\") and runs",
        "#                                        # it only after a spoken yes; this is the default, [] asks about none",
        "# offer = \"always\"                     # always | topic (sentences of its topic) | asked (when asked for)",
        "#",
        "# A server with no adapter is treated as private, foreign and egress until you say otherwise:",
        "# flags = [\"private\"]                  # its results are yours; foreign: they carry others' text;",
        "#                                        # egress: a call sends something off this machine (ADAPTERS.md)",
        "#",
        "# [tools.servers.web]                    # web search through your own SearXNG (README: Web search);",
        "# topic = \"other\"                        # your search queries go to the engines SearXNG asks",
        '# command = "/full/path/to/npx"',
        '# args = ["-y", "mcp-searxng@2.5.1"]',
        '# env = { SEARXNG_URL = "http://127.0.0.1:8888", NODE_OPTIONS = "--dns-result-order=ipv4first" }',
        "#",
        "# [tools.servers.recall]                 # your recall notes, read-only (README: Your notes);",
        "# topic = \"notes\"                        # then run: strawberry tools login recall",
        '# url = "https://recall.example.com/mcp" # a remote MCP server: https (or http on this machine)',
        '# workspaces = ["My project"]            # the only workspaces she may read, by name or id; [] is none',
        "",
        "[actions]",
        "enabled = true                 # the reflexes: skip, pause, what's playing… (needs [gate])",
        "mpris = true                   # do the bare music commands over MPRIS (SMTC on Windows) when no server covers them",
        "reflex = 0.6                   # how sure the gate must be to fire a plain command straight away",
        "",
        "[thinker]",
        "enabled = true                 # the big model with the tools; she answers you herself through it",
        'model = ""                     # empty = brain.action_model',
        'think = false                  # false | "low" | "medium" | true; off is fine, the gate already routed',
        'keep_alive = "30m"             # a cold load is 7-17 s; she says an acknowledgement while it happens',
        "stream = true                  # read the reply as it is written (tokens per second for the widget)",
        "max_tools = 30                 # more tool schemas than this and the least likely are cut (§8b)",
        f"tool_tokens = {th.tool_tokens}             # …and at most this many prompt tokens of them; 0 = no budget",
        "",
        "[ledger]",
        "# Her short memory, in memory only: your last exchanges and what she reacted to on her own (a commit,",
        "# a notification's app and sender, a track; never a message's text), given to the big model with ages.",
        f"turns = {LedgerConfig().turns}                      # your last this many exchanges",
        f"notices = {LedgerConfig().notices}                    # and this many things she reacted to (0: none)",
        f"window_minutes = {LedgerConfig().window_minutes}         # none older than this",
        f"foreign_minutes = {LedgerConfig().foreign_minutes}        # a sender's or a track's name makes changes ask first for this long",
        "",
        "[messages]",
        "# Her inbox, in memory only: the notifications she got, so you can ask \"any new messages?\" or \"what",
        "# did Alex say?\". Read-only. A message's text only as [notifications] body allows (off: who and where).",
        f"enabled = {str(MessagesConfig().enabled).lower()}",
        f"keep = {MessagesConfig().keep}                     # at most this many",
        f"max_age_hours = {MessagesConfig().max_age_hours}         # none older than this",
        "",
        "[learning]",
        "# The router's learning loop, data only for now: each sentence you say or type, how the gate read",
        "# it and what came of it (an undo, a correction, a rephrase, silence), kept on this machine in",
        "# outcomes.jsonl in the state dir. Nothing leaves it; `strawberry outcomes` shows the file and where",
        "# it is, `--clear` deletes it. Sensitive sentences and other voices (the TV) are never kept.",
        f"log_outcomes = {str(lr.log_outcomes).lower()}",
        f"max_days = {lr.max_days}                  # older records are pruned",
        f"max_records = {lr.max_records}             # and only the newest this many are kept",
        f"undo_s = {lr.undo_s}                  # the opposite reflex this soon after one is an undo (skip, then previous)",
        f"rephrase_s = {lr.rephrase_s}              # a close sentence or a \"no, I meant\" this soon after is about it",
        f"silence_s = {lr.silence_s}               # nothing said for this long afterwards counts as a weak \"right\"",
        "# What she learns from it (README: Learning): the outcomes become labelled sentences in the data",
        "# dir, and a candidate head is trained on them and the shipped data set. It must score at least",
        "# as well as the head in use on the held-out set; `strawberry learning accept` puts it in use and",
        "# `strawberry learning rollback` goes back. `strawberry learning forget` deletes what was learned.",
        f"idle_train = {str(lr.idle_train).lower()}             # train a candidate on its own when nobody has spoken for a while",
        f"idle_minutes = {lr.idle_minutes}           # that long",
        f"min_new_labels = {lr.min_new_labels}            # and only with at least this many new labelled sentences",
        f"auto_switch = {str(lr.auto_switch).lower()}            # true: a candidate that passes is put in use without asking",
        f"weekly_line = {str(lr.weekly_line).lower()}            # true: once a week she says what she learned (never in quiet hours)",
        f"max_share = {lr.max_share}              # your labels weigh at most this share of the data set's, per option",
        "",
        "[runs]",
        "# Each sentence she handles is a run: its steps (deciding, thinking, a tool) show as a chip under her",
        "# bubble with a stop button, and in the Brain UI. Tool names and timings only, never what was said.",
        f"events = {str(ru.events).lower()}                  # send the steps to the widget and the Brain UI",
        f"supersede = {str(ru.supersede).lower()}               # a new sentence stops the one she is on (not a yes or no to her question)",
        f"keep = {ru.keep}                      # finished runs the Brain UI can show",
        "",
        "[approvals]",
        "# Some calls wait for your yes first, bound to that exact call: a server's confirm list, and every",
        "# call that sends something to someone (sends) or deletes or cannot be undone (destructive). Say or",
        "# type yes or no, answer the card in her bubble, or use the Brain UI. No answer in time is a no.",
        f"change_s = {ap.change_s}                # how long she waits for a yes to a change (Spotify's removals)",
        f"sends_s = {ap.sends_s}                 # …to a call that sends something",
        f"destructive_s = {ap.destructive_s}           # …to one that deletes or cannot be undone",
        f"grace_s = {ap.grace_s}                # while you are still answering, at most this much longer",
        f"hold = {json.dumps(ap.hold)}   # on her card, a yes to these tiers is a press-and-hold",
        '# risk = { "spotify.remove_saved_tracks" = "destructive", "notes" = "read" }',
        "#                              # a tool's (or a whole server's) tier: read | playback | change | sends | destructive;",
        "#                              # a whole server's never lowers a tool its adapter or server marks higher",
        "",
        "[touch]",
        "# Input from bodies (the orbs on a touch screen, the crab): what the user touches or points at. A target",
        "# goes into what the thinker is told (\"the user is pointing at the music orb\"), so \"this\" means it.",
        f"target_s = {TouchConfig().target_s}                # a target holds this long after the body last reported it",
        f"cooldown_s = {TouchConfig().cooldown_s}              # at least this long between two touch actions",
        "# A touch on an entity can run a music reflex: now_playing, skip (or next), previous, pause, resume (or",
        "# play), volume_up, volume_down. A touch cannot answer a question, so nothing that would ask can be",
        "# mapped. None by default: a touch is shown to the other bodies and does nothing else.",
        '# music.flick = "next"',
        '# music.grab = "pause"',
        *_gestures_toml(),
    ]
    return "\n".join(lines) + "\n"


def _gestures_toml() -> list[str]:
    """[gestures] in the commented template (WIRING.md §26)."""
    ge = GesturesConfig()
    lines = [
        "",
        "[gestures]",
        "# Hand gestures through the webcam: MediaPipe on the CPU, in a process of its own. Only gesture names",
        "# and a hand's position leave it; frames are never stored, logged or sent. Needs the gestures extra",
        "# (`strawberry gestures fetch` gets the model). The tray's Gestures row turns it on and off.",
        f"enabled = {str(ge.enabled).lower()}",
        f'camera = "{ge.camera}"                    # "" = the first camera; "1" or "/dev/video2" for another',
        f'watch = "{ge.watch}"               # always: the camera is on while enabled (its light too), looking',
        "#                              # for a raised hand a few times a second; armed: off until",
        "#                              # `strawberry gestures arm` or an approval she shows, then on for armed_s",
        f"fps = {ge.fps}                     # while a hand is up",
        f"idle_fps = {ge.idle_fps}                 # while looking for one",
        f"hold_ms = {ge.hold_ms}               # how long a shape is held before it counts",
        f"arming = {str(ge.arming).lower()}                 # true: a held open palm turns command mode on first, for armed_s",
        f"armed_s = {ge.armed_s}",
        f"approvals = {str(ge.approvals).lower()}               # a held thumbs up/down answers her card (never a hold tier)",
        f"zone = {ge.zone}                     # the wrist must be above this line (0 top, 1 bottom)",
        f"min_size = {ge.min_size}               # and the hand this big: raised toward the screen, not on the desk",
        f"hand_hz = {ge.hand_hz}                # the hand's position to the bodies that ask, at most this often",
        "",
        "[gestures.map]",
        "# thumb_up thumb_down palm_hold fist_hold point_hold victory_hold pinch_hold swipe_left swipe_right",
        "# -> now_playing pause resume skip previous volume_up volume_down like play_liked listen",
        "# Only these: a gesture runs what plays and how, never a change that needs a yes.",
    ]
    lines += [f'{name} = "{action}"' for name, action in ge.map.items()]
    return lines
