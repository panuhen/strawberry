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

from . import paths, persona

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
    persona: str = persona.PERSONA
    examples: list[dict[str, str]] = field(default_factory=lambda: [dict(e) for e in persona.EXAMPLES])


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


@dataclass
class BeatConfig:
    enabled: bool = True          # listen to the player's audio stream for the beat (doorways/beat_watch.py)
    target: str = ""              # PipeWire node or application name; empty = the running player, auto
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
    quiet_hours: str = ""                                   # e.g. "22:00-08:00": bubble only, no sound
    max_chars: int = 400                                    # longer lines are cut before synthesis
    keep_files: int = 3                                     # recent wavs kept so a playing one is not deleted


@dataclass
class GateConfig:
    """The System One gate on spoken sentences (WIRING.md §8a)."""

    enabled: bool = True
    model: str = "embeddinggemma"  # Ollama embedding model; the examples are the classifier
    query_prefix: str = "task: classification | query: "   # embeddinggemma's prompt conventions;
    document_prefix: str = "title: none | text: "           # empty both for a model without them
    neighbours: int = 2            # an option scores the mean of its N nearest examples
    temperature: float = 0.05      # softmax over those scores; lower = more decisive
    act: float = 0.6               # kind confidence at which a plain command may fire a reflex
    offer: float = 0.3             # below act: a label in the journal; the sentence goes to the thinker anyway
    topic_min: float = 0.2         # below this the topic is "other" and no tools are loaded
    timeout_s: float = 2.0         # one embedding call is ~165 ms on a GPU
    retry_timeout_s: float = 15.0  # a notification body's check that timed out waits this long for the
                                   # model to load and asks once more (a cold load is ~10 s); voice never waits
    # Extra phrases per option, keyed "kind.request", "topic.music", ...; a misread sentence
    # goes here and is fixed.
    examples: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class ToolsConfig:
    """MCP servers she can act through (WIRING.md §8b). The servers you list are the servers."""

    enabled: bool = True
    preconnect: bool = True        # connect at start (in the background) so the first request is quick
    result_chars: int = 2000       # a tool result is cut here before any model reads it
    connect_timeout_s: float = 20.0
    call_timeout_s: float = 20.0
    # name -> {topic, command, args, env, cwd, careful, adapter}; topic is one of the gate's (music, calendar,
    # notes, system); careful lists tools with consequences, offered to the thinker only when you ask for such a
    # change; adapter names one of strawberry/adapters/ when the server's own name does not (ADAPTERS.md).
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
    ledger_turns: int = 6          # her memory: this many recent exchanges…
    ledger_age_s: float = 600.0    # …no older than this, given to both models


@dataclass
class ThinkerConfig:
    """The big model with the tools, in her voice, for everything but a bare reflex (WIRING.md §8b)."""

    enabled: bool = True           # false: Gemma answers spoken sentences as chat, as she used to
    model: str = ""                # "" = brain.action_model
    think: bool | str = False      # Ollama: false | "low" | "medium" | true (= xhigh); off: the gate routed already
    keep_alive: int | str = "30m"  # stays loaded this long after a request; a cold load is 7-17 s
    num_ctx: int = 8192
    num_predict: int = 300
    max_tools: int = 30            # more schemas than this and the list is cut: the gate's topic first,
                                   # then each adapter's common tools (25 Spotify tools are ~2400 tokens)
    max_rounds: int = 6            # tool rounds before she has to answer honestly with what she has
    timeout_s: float = 45.0        # the whole request, cold load included
    ack_after_s: float = 2.5       # silent thinking pose first; a spoken ack only if the reply takes longer
    still_on_it_s: float = 8.0     # she says so once if it takes longer than this
    acks: list[str] = field(default_factory=lambda: ["On it.", "Let me see.", "One moment.", "Right, hang on."])


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
    if config.speech.keep_files < 1:
        raise ConfigError("speech.keep_files must be >= 1")
    if config.voice.max_seconds <= 0 or config.voice.silence_s <= 0:
        raise ConfigError("voice.max_seconds and voice.silence_s must be positive")
    if config.voice.device not in ("cpu", "cuda", "auto"):
        raise ConfigError("voice.device must be cpu, cuda, or auto")
    if not all(isinstance(w, str) for w in config.voice.vocabulary):
        raise ConfigError("voice.vocabulary must be a list of strings")
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
        if not isinstance(server, dict) or not isinstance(server.get("command"), str) or not server["command"]:
            raise ConfigError(f"tools.servers.{name} needs a command")
        if not isinstance(server.get("topic", "other"), str):
            raise ConfigError(f"tools.servers.{name}.topic must be a string")
        if not all(isinstance(a, str) for a in server.get("args", [])):
            raise ConfigError(f"tools.servers.{name}.args must be a list of strings")
        env = server.get("env", {})
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ConfigError(f"tools.servers.{name}.env must be a table of strings")
        if not all(isinstance(t, str) for t in server.get("careful", [])):
            raise ConfigError(f"tools.servers.{name}.careful must be a list of tool names")
        if "adapter" in server and not (isinstance(server["adapter"], str) and server["adapter"].strip()):
            raise ConfigError(f"tools.servers.{name}.adapter must be an adapter name, e.g. \"spotify\" (ADAPTERS.md)")
        unknown = set(server) - {"topic", "command", "args", "env", "cwd", "careful", "adapter"}
        if unknown:
            raise ConfigError(f"tools.servers.{name}: unknown keys {sorted(unknown)}")
    if config.tools.result_chars < 100:
        raise ConfigError("tools.result_chars must be >= 100")
    if not (0.0 <= config.actions.reflex <= 1.0 and 0.0 <= config.actions.argument <= 1.0):
        raise ConfigError("actions.reflex and actions.argument must be between 0 and 1")
    if config.actions.ledger_turns < 1 or config.actions.ledger_age_s <= 0:
        raise ConfigError("actions.ledger_turns >= 1 and actions.ledger_age_s > 0 are required")
    if config.thinker.max_rounds < 1 or config.thinker.timeout_s <= 0 or config.thinker.num_ctx < 1024:
        raise ConfigError("thinker.max_rounds >= 1, timeout_s > 0 and num_ctx >= 1024 are required")
    if config.thinker.max_tools < 1:
        raise ConfigError("thinker.max_tools must be >= 1 (it caps the tool schemas in the prompt)")
    if not config.thinker.acks or not all(isinstance(a, str) and a for a in config.thinker.acks):
        raise ConfigError("thinker.acks must be a non-empty list of strings")
    from .speech import parse_quiet_hours  # local: speech imports SpeechConfig from here

    try:
        parse_quiet_hours(config.speech.quiet_hours)
    except ValueError as exc:
        raise ConfigError(f"speech.{exc}") from exc


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


def load(path: Path | None = None, env: dict[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    path = path or default_path()
    config = Config(path=path if path.exists() else None)
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
        for section_name, values in data.items():
            if section_name not in _SECTIONS:
                log.warning("config: unknown section [%s] ignored", section_name)
                continue
            if not isinstance(values, dict):
                raise ConfigError(f"[{section_name}] must be a table")
            if section_name == "notifications":
                values = _migrate_notifications(values)
            _apply(section_name, getattr(config, section_name), values)
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
    lines = [
        "# Strawberry settings. Every key is optional; these are the defaults.",
        "# Restart the daemon after editing: bin/strawberry stop && bin/strawberry daemon",
        "",
        "[daemon]",
        f'host = "{d.host}"',
        f"port = {d.port}",
        f'log_level = "{d.log_level}"',
        f"warm_on_wake = {str(d.warm_on_wake).lower()}            # reload the gate and reaction models after a suspend",
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
        "# Her voice. The examples matter more than the description for a small model.",
        "# persona = \"\"\"...\"\"\"",
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
        f'quiet_hours = "{s.quiet_hours}"           # e.g. "22:00-08:00": bubble only, no sound',
        f"max_chars = {s.max_chars}",
        "",
        "[beat]",
        "enabled = true                 # listen to the player's own audio stream and dance to its beat",
        'target = ""                    # PipeWire node/app name; empty = whichever player is running',
        "interval_s = 2.0",
        "",
        "[voice]",
        "enabled = true                 # hotkey (strawberry hotkey) -> she listens -> faster-whisper -> answers",
        'model = "small"                # tiny | base | small | medium | large-v3; small is ~460 MB, ~1 s on CPU',
        'language = ""                  # "" detects; "en" is faster and steadier',
        'device = "cpu"                 # "cuda" if the GPU has room next to Ollama',
        'source = ""                    # microphone name fragment (pactl list sources short); "" = auto',
        "bluetooth = true               # auto prefers a Bluetooth headset mic (its profile is switched while she listens)",
        "max_seconds = 15.0",
        "silence_s = 1.1                # quiet after speech that ends the recording",
        'vocabulary = []                # names she should recognise, e.g. ["Lighthouse", "Alex"]; artists come from Spotify',
        "",
        "[gate]",
        "enabled = true                 # sorts what you said: chat, or a request/question for the action path",
        'model = "embeddinggemma"       # Ollama embedding model (ollama pull embeddinggemma)',
        "act = 0.6                      # confidence at which a plain command may fire a reflex",
        "offer = 0.3                    # below act: a label in the journal; the sentence goes to the thinker",
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
        "# careful = [\"save_tracks\", \"remove_saved_tracks\", \"add_to_playlist\", \"favorite_current\",",
        '#            "remove_favorite", "clear_favorites"]   # offered only when you ask for such a change',
        "",
        "[actions]",
        "enabled = true                 # the reflexes: skip, pause, what's playing… (needs [gate])",
        "mpris = true                   # do the bare music commands over MPRIS (SMTC on Windows) when no server covers them",
        "reflex = 0.6                   # how sure the gate must be to fire a plain command straight away",
        "ledger_turns = 6               # her memory: this many recent exchanges, given to whoever answers",
        "",
        "[thinker]",
        "enabled = true                 # the big model with the tools; she answers you herself through it",
        'model = ""                     # empty = brain.action_model',
        'think = false                  # false | "low" | "medium" | true; off is fine, the gate already routed',
        'keep_alive = "30m"             # a cold load is 7-17 s; she says an acknowledgement while it happens',
        "max_tools = 30                 # more tool schemas than this and the least likely are cut (§8b)",
        "",
        "# Example exchanges she imitates. Uncomment and edit to change her register.",
    ]
    for example in b.examples:
        event = example["event"].replace("\n", "\\n")
        lines += [
            "# [[brain.examples]]",
            f'# event = "{event}"',
            f'# line = "{example["line"]}"',
            f'# emotion = "{example["emotion"]}"',
        ]
    return "\n".join(lines) + "\n"
