"""The listen hotkey as a setting (WIRING.md §7): parsing a key combination, and where Windows
keeps it.

On Linux the hotkey is a GNOME custom shortcut (`strawberry hotkey`, cli.py) and GNOME holds the
binding. On Windows no desktop setting runs a command on a key, so the tray registers it
(`RegisterHotKey`, wintray.py) and it lives in config.toml as `[voice] hotkey`; `strawberry
hotkey` writes it there. The syntax is GNOME's on both systems, `<Control><Alt>space`, and
`Ctrl+Alt+Space` is read too.

The default differs. Linux binds `<Super><Shift>space`; on Windows every Windows-key
combination is the system's to take (RegisterHotKey's documentation says so) and that one is
already taken: Win+Shift+Space switches back through the input languages, and registering it
fails with ERROR_HOTKEY_ALREADY_REGISTERED. So Windows binds `<Control><Alt>space`.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

LINUX_DEFAULT = "<Super><Shift>space"
WINDOWS_DEFAULT = "<Control><Alt>space"
OFF = "off"                          # `[voice] hotkey = "off"`: the tray registers nothing

# RegisterHotKey's modifier flags.
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8
MODIFIERS = {
    "control": MOD_CONTROL, "ctrl": MOD_CONTROL, "primary": MOD_CONTROL,
    "alt": MOD_ALT, "mod1": MOD_ALT,
    "shift": MOD_SHIFT,
    "super": MOD_WIN, "win": MOD_WIN, "windows": MOD_WIN,
}
MODIFIER_ORDER = ((MOD_CONTROL, "Control"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift"), (MOD_WIN, "Super"))

# Keys by their GNOME (X keysym) names, as virtual-key codes. Punctuation is left out: its key
# moves with the keyboard layout.
KEYS: dict[str, tuple[str, int]] = {
    "space": ("space", 0x20), "return": ("Return", 0x0D), "enter": ("Return", 0x0D), "tab": ("Tab", 0x09),
    "escape": ("Escape", 0x1B), "esc": ("Escape", 0x1B), "insert": ("Insert", 0x2D), "delete": ("Delete", 0x2E),
    "home": ("Home", 0x24), "end": ("End", 0x23), "page_up": ("Page_Up", 0x21), "prior": ("Page_Up", 0x21),
    "page_down": ("Page_Down", 0x22), "next": ("Page_Down", 0x22), "left": ("Left", 0x25), "up": ("Up", 0x26),
    "right": ("Right", 0x27), "down": ("Down", 0x28), "pause": ("Pause", 0x13), "print": ("Print", 0x2C),
    "scroll_lock": ("Scroll_Lock", 0x91),
}
KEYS.update({chr(c).lower(): (chr(c).lower(), c) for c in range(ord("A"), ord("Z") + 1)})
KEYS.update({str(d): (str(d), 0x30 + d) for d in range(10)})
KEYS.update({f"f{n}": (f"F{n}", 0x6F + n) for n in range(1, 25)})


@dataclass(frozen=True)
class Hotkey:
    modifiers: int          # MOD_* flags
    vk: int                 # the virtual-key code
    key: str                # the key's name, as written back

    @property
    def text(self) -> str:
        """The combination in GNOME's syntax: `<Control><Alt>space`."""
        return "".join(f"<{name}>" for flag, name in MODIFIER_ORDER if self.modifiers & flag) + self.key


def parse(combo: str) -> Hotkey:
    """`<Control><Alt>space` or `Ctrl+Alt+Space` as a Hotkey; ValueError says what is wrong.
    At least one modifier: a bare key would be taken from every program."""
    text = combo.strip()
    if "<" in text:
        names = re.findall(r"<([^<>]*)>", text)
        key = re.sub(r"<[^<>]*>", "", text).strip()
    else:
        *names, key = [part.strip() for part in text.split("+")] if text else [""]
    modifiers = 0
    for name in names:
        flag = MODIFIERS.get(name.strip().lower())
        if flag is None:
            raise ValueError(f"{name!r} is not a modifier (Control, Alt, Shift, Super)")
        modifiers |= flag
    found = KEYS.get(key.lower())
    if found is None:
        raise ValueError(f"{key!r} is not a key this knows (a letter, a digit, F1-F24, space, Return, ...)")
    if not modifiers:
        raise ValueError("a hotkey needs a modifier (Control, Alt, Shift or Super)")
    return Hotkey(modifiers, found[1], found[0])


def windows_hotkey(value: str) -> Hotkey | None:
    """What the Windows tray registers for a `[voice] hotkey` value: "" is the default, "off" is
    nothing. Raises ValueError for a combination that does not parse."""
    value = value.strip()
    if value.lower() == OFF:
        return None
    return parse(value or WINDOWS_DEFAULT)


def read_setting(path: Path) -> str:
    """`[voice] hotkey` from config.toml; "" (the default) when the file or the key is missing.
    tomllib alone, as the tray's other reads: this runs on every change of the file."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    voice = data.get("voice")
    value = voice.get("hotkey", "") if isinstance(voice, dict) else ""
    return value if isinstance(value, str) else ""
