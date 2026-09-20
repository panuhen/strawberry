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

from . import persona

log = logging.getLogger("strawberryd.config")


class ConfigError(ValueError):
    pass


def default_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "strawberry" / "config.toml"


@dataclass
class DaemonConfig:
    host: str = "127.0.0.1"
    port: int = 8770
    log_level: str = "INFO"


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
    only: list[str] = field(default_factory=list)    # follow just these MPRIS players, e.g. ["spotify"]
    ignore: list[str] = field(default_factory=list)  # skip these, e.g. ["firefox"]


@dataclass
class NotificationsConfig:
    ignore_apps: list[str] = field(default_factory=lambda: ["Spotify"])  # MPRIS already covers music
    only_apps: list[str] = field(default_factory=list)      # non-empty: forward these apps only
    min_urgency: str = "low"                                # low | normal | critical
    include_body: bool = True                               # false: she knows who wrote, not what
    max_body_chars: int = 200
    ignore_replacements: bool = True                        # updates to an existing notification (progress bars)
    coalesce_s: float = 2.0                                 # several within this window become one event


@dataclass
class Config:
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "daemon": asdict(self.daemon),
            "brain": asdict(self.brain),
            "media": asdict(self.media),
            "notifications": asdict(self.notifications),
        }
        out["path"] = str(self.path) if self.path else None
        return out


_SECTIONS = {"daemon": DaemonConfig, "brain": BrainConfig, "media": MediaConfig, "notifications": NotificationsConfig}


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
    if config.notifications.coalesce_s < 0:
        raise ConfigError("notifications.coalesce_s must be >= 0")
    if not (1 <= config.daemon.port <= 65535):
        raise ConfigError("daemon.port must be 1-65535")


def load(path: Path | None = None, env: dict[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    path = path or default_path()
    config = Config(path=path if path.exists() else None)
    if path.exists():
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
        for section_name, values in data.items():
            if section_name not in _SECTIONS:
                log.warning("config: unknown section [%s] ignored", section_name)
                continue
            if not isinstance(values, dict):
                raise ConfigError(f"[{section_name}] must be a table")
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
    lines = [
        "# Strawberry settings. Every key is optional; these are the defaults.",
        "# Restart the daemon after editing: bin/strawberry stop && bin/strawberry daemon",
        "",
        "[daemon]",
        f'host = "{d.host}"',
        f"port = {d.port}",
        f'log_level = "{d.log_level}"',
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
        "only = []      # e.g. [\"spotify\"] to follow one player; empty = every MPRIS player",
        "ignore = []    # e.g. [\"firefox\"]",
        "",
        "[notifications]",
        f"ignore_apps = {json.dumps(n.ignore_apps)}   # music is covered by the media doorway",
        "only_apps = []              # non-empty: forward only these apps",
        f'min_urgency = "{n.min_urgency}"         # low | normal | critical',
        f"include_body = {str(n.include_body).lower()}         # false: she knows who wrote, not what they wrote",
        f"max_body_chars = {n.max_body_chars}",
        f"ignore_replacements = {str(n.ignore_replacements).lower()}  # progress-bar style updates to an existing notification",
        f"coalesce_s = {n.coalesce_s}            # several within this window become one \"N notifications\" event",
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
