"""`strawberry doctor`: check what she needs and say how to fix what is missing (PACKAGING.md step 5).

One line per check: ✓ fine, ! worth knowing (nothing breaks), ✗ broken, with the fix under it.
The exit code is 1 when any check is ✗, so every bug report can start with this output.
`--talk` then runs the daemon's scripted lines through each slot (POST /probe) and prints the
latency of the gate, the desktop voice, the brain, Piper and whisper.

Each check is a small function over injectable probes (which, run, http), so the tests can say
what the machine looks like without having one.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

from . import paths

OK, WARN, FAIL = "✓", "!", "✗"
PIPEWIRE_HINT = ("install PipeWire's tools: sudo apt install pipewire-bin (Debian/Ubuntu), "
                 "sudo dnf install pipewire-utils (Fedora), sudo pacman -S pipewire (Arch)")
APPINDICATOR_HINT = ("on GNOME enable the AppIndicator extension (sudo apt install gnome-shell-extension-appindicator, "
                     "then log out and in); without it her right-click menu has everything the 🍓 has")


@dataclass
class Check:
    status: str
    label: str
    detail: str = ""
    fix: str = ""

    def lines(self) -> list[str]:
        out = [f"{self.status} {self.label}" + (f" — {self.detail}" if self.detail else "")]
        if self.fix and self.status != OK:
            out.append(f"    {'fix' if self.status == FAIL else 'hint'}: {self.fix}")
        return out


class Probes:
    """The outside world, one method each; tests replace them."""

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def run(self, argv: list[str], timeout: float = 10.0, **_ignored: Any) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(argv, 127, "", str(exc))

    def http(self, url: str, body: dict | None = None, timeout: float = 3.0) -> Any:
        """The JSON a URL answers with, or None when it does not answer with a 2xx."""
        import urllib.error
        import urllib.request

        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def bus_has_owner(self, name: str) -> bool | None:
        """Whether `name` has an owner on the session bus; None when there is no session bus."""
        try:
            from jeepney.bus_messages import message_bus
            from jeepney.io.blocking import open_dbus_connection
        except ImportError:
            return None
        try:
            with open_dbus_connection("SESSION") as conn:
                reply = conn.send_and_get_reply(message_bus.NameHasOwner(name), timeout=3)
        except Exception:   # noqa: BLE001 - no bus address, a refused socket, a timeout
            return None
        return bool(reply.body[0])

    def find_module(self, name: str) -> bool:
        import importlib.util

        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            return False

    def cuda_devices(self) -> int | None:
        """CUDA devices ctranslate2 (whisper's engine) sees after the pip CUDA libraries are
        preloaded, or None when ctranslate2 cannot say."""
        try:
            from .voice import preload_cuda_libraries

            preload_cuda_libraries()
            import ctranslate2

            return int(ctranslate2.get_cuda_device_count())
        except Exception:   # noqa: BLE001 - an import or driver error is the answer here
            return None


# --- the checks ---------------------------------------------------------------------

def check_config(probes: Probes) -> tuple[list[Check], Any]:
    """The file parses and validates; the keys it leaves to their defaults. Returns the config
    (the defaults when the file is broken, so the other checks can still run)."""
    from .config import Config, ConfigError, load
    from .setupcmd import missing_keys

    path = paths.config_file()
    if not path.exists():
        return [Check(WARN, "config", f"no {path}; every setting is its default",
                      "strawberry setup (or strawberry config) writes one")], Config()
    try:
        config = load(path)
    except ConfigError as exc:
        return [Check(FAIL, "config", str(exc), f"edit {path} (strawberry config)")], Config()
    checks = [Check(OK, "config", str(path))]
    missing = missing_keys(path.read_text())
    if missing:
        names = ", ".join(k for k, _ in missing[:8]) + (f" and {len(missing) - 8} more" if len(missing) > 8 else "")
        checks.append(Check(WARN, "config keys at their defaults", f"{len(missing)} not in your file: {names}",
                            "strawberry setup offers to append them, commented, so you can see and edit them"))
    return checks, config


def configured_models(config) -> list[tuple[str, str]]:
    """(slot, Ollama model) for every slot that is switched on."""
    models = []
    if config.gate.enabled:
        models.append(("gate", config.gate.model))
    if config.brain.enabled:
        models.append(("voice", config.brain.reaction_model))
    if config.thinker.enabled:
        models.append(("brain", config.thinker.model or config.brain.action_model))
    return models


def check_ollama(config, probes: Probes) -> list[Check]:
    from .setupcmd import has_model

    url = config.brain.ollama_url.rstrip("/")
    tags = probes.http(url + "/api/tags")
    models = configured_models(config)
    if tags is None:
        fix = ("install it: curl -fsSL https://ollama.com/install.sh | sh" if probes.which("ollama") is None
               else "start it: systemctl start ollama (or: ollama serve)")
        return [Check(FAIL if models else WARN, "ollama", f"no answer at {url}", fix)]
    version = probes.http(url + "/api/version") or {}
    available = [m.get("name", "") for m in tags.get("models", []) if isinstance(m, dict)]
    checks = [Check(OK, "ollama", f"{url}" + (f", version {version['version']}" if version.get("version") else ""))]
    for slot, model in models:
        if has_model(available, model):
            checks.append(Check(OK, f"model {model}", slot))
        else:
            checks.append(Check(FAIL, f"model {model}", f"{slot}: not pulled", f"ollama pull {model} (or strawberry setup)"))
    return checks


def check_gpu(config, probes: Probes) -> list[Check]:
    from .setupcmd import DEFAULT_TIER, detect_gpu

    gpu = detect_gpu(probes.run) if probes.which("nvidia-smi") else None
    wants_cuda = config.voice.enabled and config.voice.device == "cuda"
    if gpu is None:
        if wants_cuda:
            return [Check(FAIL, "GPU", "none found (nvidia-smi), but [voice] device = \"cuda\"",
                          'set [voice] device = "cpu" and compute_type = "int8", or install the NVIDIA driver')]
        return [Check(WARN, "GPU", "no NVIDIA GPU found (nvidia-smi); the models run on the CPU, slowly",
                      "strawberry setup proposes models that fit")]
    detail = f"{gpu.name}, {gpu.total_mb} MiB, {gpu.free_mb} MiB free"
    brain = config.thinker.model or config.brain.action_model
    if config.thinker.enabled and brain == DEFAULT_TIER.brain and gpu.total_mb < DEFAULT_TIER.min_vram_mb:
        return [Check(WARN, "GPU", f"{detail}; {brain} wants a ~24 GB card",
                      "strawberry setup proposes a smaller brain for this card")]
    return [Check(OK, "GPU", detail)]


def check_whisper(config, probes: Probes) -> list[Check]:
    if not config.voice.enabled:
        return [Check(OK, "whisper", "voice off in the config")]
    if not probes.find_module("faster_whisper"):
        return [Check(FAIL, "whisper", "faster-whisper is not importable", "reinstall strawberry-crab (it is a dependency)")]
    detail = f"faster-whisper, {config.voice.model} on {config.voice.device}/{config.voice.compute_type}"
    if config.voice.device == "cuda":
        devices = probes.cuda_devices()
        if not devices:
            return [Check(FAIL, "whisper", f"{detail}, but CUDA is not usable ({'no device' if devices == 0 else 'ctranslate2 failed'})",
                          "uv tool install 'strawberry-crab[gpu]' (checkout: uv sync --inexact --group gpu), "
                          'or set [voice] device = "cpu"')]
        detail += f", {devices} CUDA device(s)"
    return [Check(OK, "whisper", detail)]


def check_voice(config, probes: Probes) -> list[Check]:
    from pathlib import Path

    from .speech import resolve_voice

    if not config.speech.enabled:
        return [Check(OK, "Piper voice", "speech off in the config (bubble only)")]
    directory = Path(config.speech.voices_dir).expanduser() if config.speech.voices_dir else paths.voices_dir()
    model = resolve_voice(config.speech.voice, directory)
    if model.is_file() and model.with_name(model.name + ".json").is_file():
        return [Check(OK, "Piper voice", str(model))]
    return [Check(FAIL, "Piper voice", f"{config.speech.voice} is not in {directory}",
                  f"strawberry voices {config.speech.voice} (or strawberry setup)")]


def check_widget(probes: Probes) -> list[Check]:
    from . import __version__, widgetbin

    binary = paths.widget_binary()
    installed = widgetbin.installed_version()
    if widgetbin.is_runnable(binary):
        if installed == __version__:
            return [Check(OK, "widget", f"{binary}, version {installed}")]
        return [Check(FAIL, "widget", f"{binary} is version {installed or 'unknown'}, the package is {__version__}",
                      "strawberry widget --fetch")]
    if paths.widget_project() is not None and probes.which("godot"):
        return [Check(WARN, "widget", "no binary; this checkout runs the Godot project (developer mode)",
                      "strawberry widget --fetch installs the release binary")]
    return [Check(FAIL, "widget", f"no binary at {binary}", "strawberry widget --fetch (or strawberry setup)")]


def check_tools(probes: Probes) -> list[Check]:
    checks = []
    missing = [name for name in ("pw-record", "pw-dump") if probes.which(name) is None]
    if missing:
        checks.append(Check(FAIL, "PipeWire tools", f"{', '.join(missing)} not found (voice and beat need them)",
                            PIPEWIRE_HINT))
    else:
        checks.append(Check(OK, "PipeWire tools", "pw-record, pw-dump"))
    if probes.which("git") is None:
        checks.append(Check(WARN, "git", "not found; the git doorway has nothing to watch",
                            "sudo apt install git (or your distro's package)"))
    else:
        checks.append(Check(OK, "git", probes.which("git") or ""))
    return checks


def check_tray_host(probes: Probes) -> list[Check]:
    from .tray import WATCHER_NAME

    owned = probes.bus_has_owner(WATCHER_NAME)
    if owned is None:
        return [Check(WARN, "tray host", "no session bus reachable (DBUS_SESSION_BUS_ADDRESS)",
                      "run doctor inside the desktop session")]
    if not owned:
        return [Check(WARN, "tray host", f"no {WATCHER_NAME} on the session bus: the 🍓 will not show",
                      APPINDICATOR_HINT)]
    return [Check(OK, "tray host", WATCHER_NAME)]


def daemon_url(config) -> str:
    from .cli import config_port

    return f"http://127.0.0.1:{config_port()}"


def check_daemon(config, probes: Probes) -> tuple[list[Check], dict | None]:
    from . import __version__

    base = daemon_url(config)
    health = probes.http(base + "/health")
    if health is None:
        return [Check(WARN, "daemon", f"not running at {base}", "start her with: strawberry")], None
    detail = f"{base}, version {health.get('version')}, {health.get('widgets', 0)} widget(s)"
    checks = [Check(OK if health.get("version") == __version__ else WARN, "daemon", detail,
                    f"the running daemon is not this package ({__version__}); strawberry restart")]
    others = [v for v in health.get("widget_versions") or [] if v not in (__version__, "dev")]
    if others:
        checks.append(Check(WARN, "connected widget", f"version {', '.join(others)}, daemon {health.get('version')}",
                            "strawberry widget --fetch"))
    for slot, key in (("gate", "gate"), ("speech", "speech"), ("voice", "voice")):
        stats = health.get(key) or {}
        reason = stats.get("disabled_reason") or stats.get("reason")
        switched_on = stats.get("enabled") if "enabled" in stats else stats.get("model") is not None
        if switched_on and stats.get("ready") is False and reason:
            checks.append(Check(WARN, f"daemon {slot}", f"not ready: {reason}", "see the journal, then strawberry restart"))
    return checks, health


def run_checks(probes: Probes | None = None) -> tuple[list[Check], Any]:
    probes = probes or Probes()
    checks, config = check_config(probes)
    checks += check_ollama(config, probes)
    checks += check_gpu(config, probes)
    checks += check_whisper(config, probes)
    checks += check_voice(config, probes)
    checks += check_widget(probes)
    checks += check_tools(probes)
    checks += check_tray_host(probes)
    daemon, _health = check_daemon(config, probes)
    checks += daemon
    return checks, config


# --- --talk -------------------------------------------------------------------------

SLOTS = (("gate", "gate"), ("voice", "voice (Gemma)"), ("brain", "brain (Qwen, no tools)"), ("tts", "Piper"),
         ("whisper", "whisper"))


def talk(config, probes: Probes, say: Callable[[str], None]) -> bool:
    """The daemon's scripted lines through each slot; True when every slot that ran answered."""
    base = daemon_url(config)
    if probes.http(base + "/health") is None:
        say(f"{FAIL} talk — the daemon is not running at {base}")
        say("    fix: start her with: strawberry, then doctor --talk again")
        return False
    result = probes.http(base + "/probe", body={}, timeout=180.0)
    if result is None:
        say(f"{FAIL} talk — POST /probe did not answer (an older daemon? strawberry restart)")
        return False
    say("")
    say("latency per slot (ms; the first line may include a cold load)")
    say("  line  " + "".join(f"{label:>24s}" for _, label in SLOTS))
    ok = True
    for n, row in enumerate(result.get("lines", []), 1):
        cells = []
        for slot, _ in SLOTS:
            cell = row.get(slot)
            if cell is None:
                cells.append("-")
            elif cell.get("ok") and "ms" in cell:
                cells.append(f"{cell['ms']:.0f}")
            else:
                ok = False
                cells.append(f"failed {cell.get('error', '')}".strip() if "ms" not in cell else f"{cell['ms']:.0f} failed")
        say(f"  {n:<4d}  " + "".join(f"{c:>24s}" for c in cells))
    for slot, reason in (result.get("skipped") or {}).items():
        say(f"  {WARN} {slot} skipped: {reason}")
    say(f"{OK if ok else FAIL} talk")
    return ok


def main(talk_too: bool = False, probes: Probes | None = None, say: Callable[[str], None] = print) -> int:
    probes = probes or Probes()
    checks, config = run_checks(probes)
    for check in checks:
        for line in check.lines():
            say(line)
    failed = [c for c in checks if c.status == FAIL]
    warned = [c for c in checks if c.status == WARN]
    say("")
    say(f"{len(checks) - len(failed) - len(warned)} ok, {len(warned)} to know, {len(failed)} to fix")
    talked = talk(config, probes, say) if talk_too else True
    return 1 if failed or not talked else 0
