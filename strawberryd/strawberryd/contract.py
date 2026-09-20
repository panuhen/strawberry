"""The message contract between strawberryd and the Godot widget (WIRING.md §1, §9).

Both sides implement this. The widget applies `state`, fires `anim` as a one-shot,
shows `text` in the bubble, and plays `audio` through its analysed bus. Names here
must match the GLB exactly; do not rename.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# state -> looping clip in strawberry_v2.glb
STATE_CLIPS = {
    "idle": "idle_loop",
    "listening": "listen_loop",
    "thinking": "think_loop",
    "talking": "talk_base",
    "dancing": "dance_loop",
}
ONE_SHOTS = ("alert_snap", "notify_perk")
# Procedural recipes the widget layers over the playing clip (widget/reactions.gd, WIRING.md §13).
REACTIONS = ("wave", "peek", "shiver", "double_hop", "nod")
EMOTIONS = ("neutral", "happy", "alert", "angry")

# Emotion -> animation is owned by the daemon so the model never names a clip (§2).
EMOTION_ANIM = {
    "alert": "alert_snap",
    "angry": "alert_snap",
    "happy": "notify_perk",
    "neutral": "notify_perk",
}

FIELDS = frozenset({"state", "anim", "text", "audio", "emotion", "reaction", "icon", "hop"})


class ContractError(ValueError):
    """A blob that the widget would not know what to do with."""


@dataclass(frozen=True, slots=True)
class Performance:
    state: str
    anim: str | None = None
    text: str | None = None
    audio: str | None = None
    emotion: str = "neutral"
    reaction: str | None = None   # procedural recipe layered over the clip
    icon: str | None = None       # image file shown as a badge beside the bubble (the app's icon)
    hop: bool = False             # the window itself bounces

    def to_dict(self) -> dict[str, Any]:
        """Wire form: optional fields are omitted rather than sent as null."""
        out: dict[str, Any] = {"state": self.state, "emotion": self.emotion}
        if self.anim:
            out["anim"] = self.anim
        if self.text and self.text.strip():
            out["text"] = self.text
        if self.audio:
            out["audio"] = self.audio
        if self.reaction:
            out["reaction"] = self.reaction
        if self.icon:
            out["icon"] = self.icon
        if self.hop:
            out["hop"] = True
        return out

    @classmethod
    def from_dict(cls, data: Any) -> Performance:
        if not isinstance(data, dict):
            raise ContractError("performance must be a JSON object")
        unknown = set(data) - FIELDS
        if unknown:
            raise ContractError(f"unknown field(s): {', '.join(sorted(unknown))}")

        state = data.get("state")
        if state not in STATE_CLIPS:
            raise ContractError(f"state must be one of {sorted(STATE_CLIPS)}, got {state!r}")

        anim = data.get("anim")
        if anim is not None and anim not in ONE_SHOTS:
            raise ContractError(f"anim must be one of {list(ONE_SHOTS)}, got {anim!r}")

        emotion = data.get("emotion", "neutral")
        if emotion not in EMOTIONS:
            raise ContractError(f"emotion must be one of {list(EMOTIONS)}, got {emotion!r}")

        text = data.get("text")
        if text is not None:
            if not isinstance(text, str):
                raise ContractError("text must be a string")
            text = text.strip() or None

        audio = data.get("audio")
        if audio is not None:
            if not isinstance(audio, str):
                raise ContractError("audio must be a file path string")
            if not os.path.isfile(audio):
                raise ContractError("audio file not found")

        reaction = data.get("reaction")
        if reaction is not None and reaction not in REACTIONS:
            raise ContractError(f"reaction must be one of {list(REACTIONS)}, got {reaction!r}")

        icon = data.get("icon")
        if icon is not None:
            if not isinstance(icon, str):
                raise ContractError("icon must be a file path string")
            if not os.path.isfile(icon):
                raise ContractError("icon file not found")

        hop = data.get("hop", False)
        if not isinstance(hop, bool):
            raise ContractError("hop must be true or false")

        return cls(state=state, anim=anim, text=text, audio=audio, emotion=emotion, reaction=reaction, icon=icon, hop=hop)


def anim_for(emotion: str) -> str:
    return EMOTION_ANIM.get(emotion, "notify_perk")
