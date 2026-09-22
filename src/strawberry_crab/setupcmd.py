"""`strawberry setup`: the models, a voice and the widget, fetched from where they are published
(PACKAGING.md step 5).

    1. what fits: VRAM from nvidia-smi picks a tier (the tested setup needs a 24 GB card); every
       slot can be overridden; the choices go into config.toml, backed up first
    2. Ollama and the models: one licence line per model, then `ollama pull`
    3. the Piper voice, into the voices dir
    4. the widget binary, unless the installed one is this package's version
    5. settings the file does not have yet, appended with their defaults
    6. `strawberry install`, if asked

Idempotent: a model that is there is not pulled again, a voice that is there is not downloaded,
and a key already in the user's file is kept unless the user types a new value for it. Nothing
here hosts or redistributes a model: each is downloaded by the user from its publisher, under
its own terms, and setup names them before it downloads anything.

`--yes` takes every default without asking and does not run `install` unless `--install` is given.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Callable

from . import paths

OLLAMA_INSTALL = "curl -fsSL https://ollama.com/install.sh | sh"
GEMMA_TERMS = "Gemma Terms of Use, https://ai.google.dev/gemma/terms"
APACHE = "Apache-2.0, https://www.apache.org/licenses/LICENSE-2.0"


@dataclass(frozen=True)
class Tier:
    """One row of "what fits": a model per slot and the VRAM it wants (total, not free: the
    user's Ollama may already hold these very models)."""

    name: str
    min_vram_mb: int
    summary: str
    gate: str = "embeddinggemma"
    voice: str = "gemma3:1b"
    brain: str = "qwen3.8:27b"
    thinker: bool = True
    whisper: str = "medium"
    whisper_device: str = "cuda"
    whisper_compute: str = "int8_float16"
    piper: str = "en_GB-alba-medium"


# The first is the tested setup and the default; the others trade the brain (and whisper's device)
# for VRAM. Below ~8B parameters tool calling gets unreliable, so the smallest GPU tier is honest
# about it, and without a usable GPU the thinker is off: reflexes, chat and the desktop voice work.
TIERS = (
    Tier("24gb", 22000, "the tested setup: Qwen 27B brain, whisper medium on CUDA"),
    Tier("16gb", 15000, "Qwen3 14B brain, whisper medium on CUDA", brain="qwen3:14b"),
    Tier("10gb", 9500, "Qwen3 8B brain, whisper small on CUDA", brain="qwen3:8b", whisper="small"),
    Tier("6gb", 5800, "Qwen3 4B brain (tool use gets shaky), whisper small on the CPU", brain="qwen3:4b",
         whisper="small", whisper_device="cpu", whisper_compute="int8"),
    Tier("cpu", 0, "no brain (thinker off): reflexes, chat and the desktop voice; whisper small on the CPU",
         thinker=False, whisper="small", whisper_device="cpu", whisper_compute="int8"),
)
DEFAULT_TIER = TIERS[0]

# slot -> the config key its value goes to, and the question asked for it
SLOTS = (
    ("gate", "gate.model", "gate (Ollama embedding model)"),
    ("voice", "brain.reaction_model", "desktop voice (small Ollama model)"),
    ("brain", "brain.action_model", "brain (Ollama model with tool calling)"),
    ("whisper", "voice.model", "whisper size (tiny|base|small|medium|large-v3)"),
    ("whisper_device", "voice.device", "whisper device (cpu|cuda)"),
    ("whisper_compute", "voice.compute_type", "whisper compute type"),
    ("piper", "speech.voice", "Piper voice"),
)

# Config keys `missing_keys` does not offer to append: long or structured values that read badly
# as one line (her persona, the example lists, the server tables). `strawberry config` shows them.
LONG_KEYS = {"brain.persona", "brain.examples", "gate.examples", "tools.servers", "thinker.acks"}


# --- what fits -------------------------------------------------------------------

@dataclass(frozen=True)
class Gpu:
    name: str
    total_mb: int
    free_mb: int


def detect_gpu(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> Gpu | None:
    """The largest NVIDIA card nvidia-smi reports, or None (no nvidia-smi, no card, an error)."""
    if shutil.which("nvidia-smi") is None and run is subprocess.run:
        return None
    try:
        result = run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
                     capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    gpus = []
    for line in result.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            gpus.append(Gpu(parts[0], int(parts[1]), int(parts[2])))
    return max(gpus, key=lambda g: g.total_mb) if gpus else None


def pick_tier(gpu: Gpu | None) -> Tier:
    total = gpu.total_mb if gpu else 0
    for tier in TIERS:
        if total >= tier.min_vram_mb:
            return tier
    return TIERS[-1]


def tier_named(name: str) -> Tier:
    for tier in TIERS:
        if tier.name == name:
            return tier
    raise ValueError(f"no tier {name!r}; one of {', '.join(t.name for t in TIERS)}")


def tier_values(tier: Tier) -> dict[str, Any]:
    """The config keys a tier sets, as "section.key" -> value."""
    values: dict[str, Any] = {key: getattr(tier, slot) for slot, key, _ in SLOTS}
    values["thinker.enabled"] = tier.thinker
    values["speech.enabled"] = True     # the tested setup speaks; she stays silent without a voice anyway
    return values


# --- the config file, edited as text ------------------------------------------------
# tomllib reads but does not write, and rewriting the file from a dict would lose the user's
# comments. So keys are set line by line inside their [section], and the result is parsed (and
# validated by config.load) before anything is written.

_HEADER = re.compile(r"^\s*\[\[?\s*([A-Za-z0-9_.\-\"' ]+?)\s*\]\]?\s*(#.*)?$")


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)      # a JSON string is a TOML basic string
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(f"{_toml_key(k)} = {toml_value(v)}" for k, v in value.items())
        return "{ " + inner + " }" if inner else "{}"
    raise TypeError(f"no TOML for {type(value).__name__}")


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key)


def _section_span(lines: list[str], section: str) -> tuple[int, int] | None:
    """(header index, end index) of `[section]`: its keys are lines header+1 .. end-1."""
    start = None
    for i, line in enumerate(lines):
        match = _HEADER.match(line)
        if not match:
            continue
        if start is not None:
            return start, i
        if not line.lstrip().startswith("[[") and match.group(1).strip() == section:
            start = i
    return (start, len(lines)) if start is not None else None


def set_key(text: str, dotted: str, value: Any, overwrite: bool, note: str = "") -> tuple[str, bool]:
    """`text` with `section.key = value`, and whether it changed. An existing key is replaced
    only when `overwrite`; a new one goes after the section's last key line (or in a new section
    at the end). `note` becomes a trailing comment on a line this writes."""
    section, key = dotted.rsplit(".", 1)
    lines = text.splitlines()
    line = f"{key} = {toml_value(value)}" + (f"   # {note}" if note else "")
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    span = _section_span(lines, section)
    if span is None:
        tail = [] if not lines or not lines[-1].strip() else [""]
        lines += [*tail, f"[{section}]", line]
        return "\n".join(lines) + "\n", True
    start, end = span
    for i in range(start + 1, end):
        if key_re.match(lines[i]):
            old_value, comment = _split_comment(lines[i])
            if not overwrite or old_value is None or old_value == value:   # None: a multi-line value, left alone
                return text, False
            lines[i] = f"{key} = {toml_value(value)}{comment}"      # the user's comment stays
            return "\n".join(lines) + "\n", True
    last = start
    for i in range(start + 1, end):
        if lines[i].strip() and not lines[i].lstrip().startswith("#"):
            last = i
    lines.insert(last + 1, line)
    return "\n".join(lines) + "\n", True


def _split_comment(line: str) -> tuple[Any, str]:
    """A one-line `key = value   # comment` as (value, "   # comment"); (None, "") when the value
    does not parse on its own line (a multi-line array or string starts there)."""
    for match in re.finditer(r"\s+#|$", line):
        try:
            data = tomllib.loads(line[: match.start()])
        except tomllib.TOMLDecodeError:
            continue
        return next(iter(data.values()), None), line[match.start():]
    return None, ""


def file_keys(text: str) -> dict[str, Any]:
    """The "section.key" -> value pairs a config file sets, for the sections config.py knows."""
    from .config import _SECTIONS

    data = tomllib.loads(text) if text.strip() else {}
    out = {}
    for section, values in data.items():
        if section in _SECTIONS and isinstance(values, dict):
            for key, value in values.items():
                out[f"{section}.{key}"] = value
    return out


def missing_keys(text: str) -> list[tuple[str, Any]]:
    """Every config.py default the file does not set, in the order config.py lists them; the long
    values (LONG_KEYS) are left out. For `setup` to append and `doctor` to list."""
    from .config import _SECTIONS

    present = file_keys(text)
    missing = []
    for section, cls in _SECTIONS.items():
        defaults = cls()
        for f in fields(cls):
            dotted = f"{section}.{f.name}"
            if dotted not in present and dotted not in LONG_KEYS:
                missing.append((dotted, getattr(defaults, f.name)))
    return missing


def append_missing(text: str, missing: list[tuple[str, Any]], when: str) -> str:
    """Each missing key into its section with its default and a comment saying where it came from.
    The defaults are what the daemon uses already, so this changes nothing she does."""
    for dotted, value in missing:
        text, _ = set_key(text, dotted, value, overwrite=False, note=f"default, added by strawberry setup {when}")
    return text


def backup(path: Path, clock: Callable[[], float] = time.time) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))
    target = path.with_name(f"{path.name}.bak-{stamp}")
    n = 1
    while target.exists():
        target = path.with_name(f"{path.name}.bak-{stamp}-{n}")
        n += 1
    shutil.copy2(path, target)
    return target


def check_config_text(text: str) -> None:
    """Raise ConfigError when `text` would not load; nothing is written until this passes."""
    import tempfile

    from .config import ConfigError, load

    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"the edited config would not parse: {exc}") from exc
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as handle:
        handle.write(text)
    try:
        load(Path(handle.name), env={})
    finally:
        Path(handle.name).unlink(missing_ok=True)


def write_config(path: Path, text: str, say: Callable[[str], None]) -> Path | None:
    """Back up the file if it exists, then write `text`. Returns the backup's path."""
    check_config_text(text)
    saved = None
    if path.exists():
        if path.read_text() == text:
            return None
        saved = backup(path)
        say(f"  backed up {path} -> {saved.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return saved


# --- licences ---------------------------------------------------------------------

def model_licence(model: str) -> str:
    """One line naming the terms a model comes under, as far as its family is known."""
    family = model.split(":")[0].split("/")[-1].lower()
    if family.startswith(("gemma", "embeddinggemma", "codegemma")):
        return GEMMA_TERMS
    if family.startswith(("qwen", "mistral", "nomic-embed")):
        return APACHE
    if family.startswith("llama"):
        return "Llama Community License, https://www.llama.com/llama-downloads/ (per version)"
    return f"see its page, https://ollama.com/library/{family}"


def voice_licence(voice: str) -> str:
    if voice == "en_GB-alba-medium":
        return "CC BY 4.0 (dataset: https://datashare.ed.ac.uk/handle/10283/3270)"
    return "see its MODEL_CARD, https://huggingface.co/rhasspy/piper-voices"


WHISPER_LICENCE = "MIT (OpenAI Whisper weights, converted by Systran), fetched by faster-whisper on first use"


# --- Ollama -----------------------------------------------------------------------

def ollama_models(url: str, timeout: float = 3.0) -> list[str] | None:
    """The model names Ollama has, or None when its API does not answer."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=timeout) as response:
            data = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return [m.get("name", "") for m in data.get("models", []) if isinstance(m, dict)]


def normalise(model: str) -> str:
    """Ollama's own spelling: a name without a tag is `:latest`."""
    return model if ":" in model else f"{model}:latest"


def has_model(available: list[str], model: str) -> bool:
    return normalise(model) in {normalise(m) for m in available}


def pull(model: str, run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> bool:
    """`ollama pull MODEL` with its progress on this terminal."""
    try:
        return run(["ollama", "pull", model]).returncode == 0
    except OSError:
        return False


# --- the run ----------------------------------------------------------------------

class Setup:
    """One `strawberry setup`. `ask(question, default)` returns the answer (the default under
    --yes); `run` is subprocess.run; both are injected by the tests."""

    def __init__(self, yes: bool = False, install: bool = False, tier: str | None = None,
                 ask: Callable[[str, str], str] | None = None,
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 say: Callable[[str], None] = lambda line: print(line, flush=True)) -> None:
        self.yes = yes
        self.install = install
        self.tier_name = tier
        self.ask = (lambda question, default: default) if yes else (ask or _ask_tty)
        self.run = run
        self.say = say
        self.path = paths.config_file()
        self.failures: list[str] = []

    def confirm(self, question: str, default: bool = True) -> bool:
        answer = self.ask(f"{question} [{'Y/n' if default else 'y/N'}]", "y" if default else "n").strip().lower()
        return default if not answer else answer.startswith("y")

    def main(self) -> int:
        self.say("Strawberry setup. Models and voices are not part of this package: each is downloaded")
        self.say("from its publisher, under its own licence, which is named before it is fetched.\n")
        values = self.step_models()
        self.step_ollama(values)
        self.step_voice(values)
        self.step_widget()
        self.step_missing()
        self.step_install()
        if self.failures:
            self.say(f"\nsetup finished with {len(self.failures)} problem(s): {', '.join(self.failures)}")
            self.say("run it again when they are fixed (it skips what is done); `strawberry doctor` checks everything")
            return 1
        self.say("\nsetup done. Start her with: strawberry   (check with: strawberry doctor)")
        return 0

    # 1 ---------------------------------------------------------------------------
    def step_models(self) -> dict[str, Any]:
        """Choose a tier and per-slot overrides; write them. Returns the effective slot values."""
        self.say("1. What fits")
        gpu = detect_gpu(self.run)
        if gpu:
            self.say(f"  GPU: {gpu.name}, {gpu.total_mb} MiB ({gpu.free_mb} MiB free now)")
        else:
            self.say("  no NVIDIA GPU found (nvidia-smi); Ollama and whisper would run on the CPU")
        proposed = tier_named(self.tier_name) if self.tier_name else pick_tier(gpu)
        for i, tier in enumerate(TIERS, 1):
            mark = "*" if tier == proposed else " "
            self.say(f"  {mark}{i}. {tier.name:5s} {tier.summary}")
        if proposed != DEFAULT_TIER:
            self.say(f"  the default ({DEFAULT_TIER.brain}) needs a ~24 GB card; proposing {proposed.name}")
        answer = self.ask(f"  tier [{TIERS.index(proposed) + 1}]", str(TIERS.index(proposed) + 1)).strip()
        tier = proposed
        if answer:
            try:
                tier = TIERS[int(answer) - 1] if answer.isdigit() else tier_named(answer)
            except (IndexError, ValueError):
                self.say(f"  no tier {answer!r}; keeping {proposed.name}")
        values = tier_values(tier)

        text = self.path.read_text() if self.path.exists() else ""
        fresh = not text.strip()
        if fresh:
            from .config import default_toml

            text = default_toml()
        present = {} if fresh else file_keys(text)
        # A non-empty thinker.model overrides brain.action_model, so that is the brain's key then.
        brain_key = "thinker.model" if present.get("thinker.model") else "brain.action_model"
        proposal = {(brain_key if slot == "brain" else key): values[key] for slot, key, _ in SLOTS}
        proposal |= {"thinker.enabled": values["thinker.enabled"], "speech.enabled": values["speech.enabled"]}
        if not tier.thinker:
            proposal.pop(brain_key)     # no brain in this tier: the thinker goes off instead
        # The user's file wins over a tier they merely accepted; a tier they asked for (--tier, or
        # another number typed) and a value typed for a slot win over the file.
        tier_chosen = bool(self.tier_name) or tier != proposed
        chosen: dict[str, Any] = {}
        explicit: set[str] = set()
        for slot, key, question in SLOTS:
            key = brain_key if slot == "brain" else key
            if key not in proposal:
                continue
            current = proposal[key] if tier_chosen else present.get(key, proposal[key])
            typed = self.ask(f"  {question} [{current}]", "").strip()
            chosen[key] = typed or current
            if chosen[key] != present.get(key, chosen[key]):
                explicit.add(key)
        for key in ("thinker.enabled", "speech.enabled"):
            chosen[key] = proposal[key] if tier_chosen and key == "thinker.enabled" else present.get(key, proposal[key])
            if chosen[key] != present.get(key, chosen[key]):
                explicit.add(key)

        for key in chosen:
            if key in present and key not in explicit and present[key] != proposal[key]:
                self.say(f"  kept {key} = {toml_value(present[key])} (your file; type a value to change it)")
        changed = []
        for key, value in chosen.items():
            text, did = set_key(text, key, value, overwrite=fresh or key in explicit,
                                note="" if fresh else "strawberry setup")
            if did:
                changed.append(f"{key} = {toml_value(value)}")
        try:
            write_config(self.path, text, self.say)
        except ValueError as exc:
            self.say(f"  ✗ not written: {exc}")
            self.failures.append("config")
            return self._effective(chosen, present, brain_key)
        if fresh:
            self.say(f"  wrote {self.path}")
        for line in changed:
            self.say(f"  set {line}")
        return self._effective(chosen, present, brain_key)

    @staticmethod
    def _effective(chosen: dict[str, Any], present: dict[str, Any], brain_key: str) -> dict[str, Any]:
        """slot -> the value she will run with, after the merge."""
        from .config import Config

        default = Config().to_dict()

        def value(key: str) -> Any:
            section, name = key.split(".")
            return chosen.get(key, present.get(key, default[section][name]))

        out = {slot: value(brain_key if slot == "brain" else key) for slot, key, _ in SLOTS}
        out["thinker"] = bool(value("thinker.enabled"))
        out["speech"] = bool(value("speech.enabled"))
        return out

    # 2 ---------------------------------------------------------------------------
    def step_ollama(self, values: dict[str, Any]) -> None:
        self.say("\n2. Ollama and the models")
        from .config import ConfigError, load

        try:
            url = load(self.path).brain.ollama_url
        except ConfigError:
            url = "http://127.0.0.1:11434"
        available = ollama_models(url)
        if available is None:
            if shutil.which("ollama") is None:
                self.say(f"  Ollama is not installed. Its official installer: {OLLAMA_INSTALL}")
                if not self.yes and self.confirm("  run it now (it asks for sudo)?", default=False):
                    self.run(["sh", "-c", OLLAMA_INSTALL])
                available = ollama_models(url)
            else:
                self.say(f"  ollama is installed but its API at {url} does not answer; "
                         f"start it (systemctl start ollama, or: ollama serve)")
        if available is None:
            self.say("  ✗ no Ollama: the models are not pulled")
            self.failures.append("ollama")
            return
        models = [values["gate"], values["voice"]] + ([values["brain"]] if values["thinker"] else [])
        for model in models:
            self.say(f"  {model} — {model_licence(model)}")
        self.say(f"  whisper {values['whisper']} — {WHISPER_LICENCE}")
        todo = [m for m in models if not has_model(available, m)]
        for model in models:
            if model not in todo:
                self.say(f"  ✓ {model} is there")
        if todo and not self.confirm(f"  pull {', '.join(todo)} now?"):
            self.say("  skipped; pull them later with: " + "; ".join(f"ollama pull {m}" for m in todo))
            self.failures.append("models")
            return
        for model in todo:
            self.say(f"  ollama pull {model}")
            if pull(model, self.run):
                self.say(f"  ✓ {model}")
            else:
                self.say(f"  ✗ ollama pull {model} failed")
                self.failures.append(model)

    # 3 ---------------------------------------------------------------------------
    def step_voice(self, values: dict[str, Any]) -> None:
        self.say("\n3. Her voice (Piper)")
        from .config import ConfigError, load
        from .speech import resolve_voice

        voice = values["piper"]
        try:
            configured = load(self.path).speech.voices_dir
        except ConfigError:
            configured = ""
        directory = Path(configured).expanduser() if configured else paths.voices_dir()
        self.say(f"  {voice} — {voice_licence(voice)}")
        if resolve_voice(voice, directory).is_file():
            self.say(f"  ✓ {voice} is in {directory}")
            return
        if not self.confirm(f"  download {voice} into {directory}?"):
            self.say(f"  skipped; later: strawberry voices {voice}")
            return
        directory.mkdir(parents=True, exist_ok=True)
        code = self.run([sys.executable, "-m", "piper.download_voices", "--download-dir", str(directory), voice]).returncode
        if code == 0 and resolve_voice(voice, directory).is_file():
            self.say(f"  ✓ {voice}  (compare voices: strawberry audition)")
        else:
            self.say(f"  ✗ could not download {voice}")
            self.failures.append("voice")

    # 4 ---------------------------------------------------------------------------
    def step_widget(self) -> None:
        self.say("\n4. The widget")
        from . import __version__, widgetbin

        installed = widgetbin.installed_version()
        if not needs_widget(installed, __version__):
            self.say(f"  ✓ strawberry-widget {installed} is installed")
            return
        self.say(f"  installed: {installed or 'none'}; this package: {__version__}")
        try:
            widgetbin.fetch(__version__, say=lambda line: self.say("  " + line))
        except widgetbin.FetchError as exc:
            if paths.widget_project() is not None and shutil.which("godot"):
                self.say(f"  ! fetch failed ({exc}); this checkout runs the Godot project instead (developer mode)")
            else:
                self.say(f"  ✗ widget fetch failed: {exc}")
                self.failures.append("widget")

    # 5 ---------------------------------------------------------------------------
    def step_missing(self) -> None:
        self.say("\n5. Settings your file does not have")
        if not self.path.exists():
            return
        text = self.path.read_text()
        try:
            missing = missing_keys(text)
        except tomllib.TOMLDecodeError as exc:
            self.say(f"  ✗ {self.path} does not parse: {exc}")
            self.failures.append("config")
            return
        if not missing:
            self.say("  ✓ none")
            return
        self.say(f"  {len(missing)} key(s) use their built-in default: " + ", ".join(k for k, _ in missing))
        if not self.confirm("  append them with their defaults (commented as such) so you can see and edit them?",
                            default=not self.yes):
            if self.yes:
                self.say("  left as they are (run setup without --yes to append them)")
            return
        try:
            write_config(self.path, append_missing(text, missing, time.strftime("%Y-%m-%d")), self.say)
        except ValueError as exc:
            self.say(f"  ✗ not written: {exc}")
            self.failures.append("config")
            return
        self.say(f"  ✓ appended {len(missing)} key(s)")

    # 6 ---------------------------------------------------------------------------
    def step_install(self) -> None:
        self.say("\n6. Start on login")
        from . import cli

        if cli.tray_managed():
            self.say(f"  ✓ {cli.TRAY_UNIT} is installed")
            return
        if self.yes and not self.install:
            self.say("  not installed (strawberry install, or setup --yes --install)")
            return
        if self.install or self.confirm("  install the tray unit now (strawberry install)?", default=False):
            cli.cmd_install(cli.Here())
        else:
            self.say("  later: strawberry install")


def needs_widget(installed: str | None, version: str) -> bool:
    return installed != version


def _ask_tty(question: str, default: str) -> str:
    try:
        return input(question + " ")
    except EOFError:
        return default


def main(yes: bool = False, install: bool = False, tier: str | None = None) -> int:
    if tier:
        tier_named(tier)          # an unknown name fails before anything is asked or written
    return Setup(yes=yes, install=install, tier=tier).main()
