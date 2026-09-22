import pytest

from strawberry_crab.contract import EMOTION_ANIM, ONE_SHOTS, STATE_CLIPS, ContractError, Performance, anim_for
from strawberry_crab.events import Event


def test_minimal_blob_round_trips():
    perf = Performance.from_dict({"state": "idle"})
    assert perf == Performance(state="idle")
    assert perf.to_dict() == {"state": "idle", "emotion": "neutral"}


def test_full_blob_round_trips(tmp_path):
    wav = tmp_path / "line.wav"
    wav.write_bytes(b"RIFF")
    data = {"state": "talking", "anim": "alert_snap", "text": "Hi", "audio": str(wav), "emotion": "alert"}
    perf = Performance.from_dict(data)
    assert perf.to_dict() == data


def test_optional_fields_are_omitted_not_null():
    assert "anim" not in Performance(state="idle").to_dict()
    assert "text" not in Performance(state="idle", text="   ").to_dict()


@pytest.mark.parametrize("state", list(STATE_CLIPS))
def test_every_state_is_accepted(state):
    assert Performance.from_dict({"state": state}).state == state


@pytest.mark.parametrize(
    "blob, fragment",
    [
        ({}, "state must be one of"),
        ({"state": "zoomies"}, "state must be one of"),
        ({"state": "idle", "anim": "idle_loop"}, "anim must be one of"),
        ({"state": "idle", "emotion": "bored"}, "emotion must be one of"),
        ({"state": "idle", "text": 42}, "text must be a string"),
        ({"state": "idle", "audio": "/nope/never.wav"}, "audio file not found"),
        ({"state": "idle", "clip": "x"}, "unknown field"),
        ([], "must be a JSON object"),
    ],
)
def test_bad_blobs_are_rejected_with_a_reason(blob, fragment):
    with pytest.raises(ContractError, match=fragment):
        Performance.from_dict(blob)


def test_reaction_icon_and_hop_round_trip(tmp_path):
    icon = tmp_path / "app.png"
    icon.write_bytes(b"\x89PNG")
    data = {"state": "talking", "text": "Hi", "emotion": "happy", "reaction": "wave", "icon": str(icon), "hop": True}
    assert Performance.from_dict(data).to_dict() == data
    assert "hop" not in Performance(state="idle").to_dict()


@pytest.mark.parametrize(
    "blob, fragment",
    [
        ({"state": "idle", "reaction": "backflip"}, "reaction must be one of"),
        ({"state": "idle", "icon": "/nope.png"}, "icon file not found"),
        ({"state": "idle", "icon": 3}, "icon must be a file path"),
        ({"state": "idle", "hop": "yes"}, "hop must be true or false"),
    ],
)
def test_bad_reaction_fields_are_rejected(blob, fragment):
    with pytest.raises(ContractError, match=fragment):
        Performance.from_dict(blob)


def test_blank_text_becomes_none():
    assert Performance.from_dict({"state": "idle", "text": "  "}).text is None


def test_emotion_to_anim_mapping_only_uses_real_one_shots():
    for emotion, anim in EMOTION_ANIM.items():
        assert anim in ONE_SHOTS, emotion
    assert anim_for("alert") == "alert_snap"
    assert anim_for("made-up") == "notify_perk"


def test_event_requires_a_known_source():
    with pytest.raises(ContractError, match="source is required"):
        Event.from_dict({"title": "x"})
    with pytest.raises(ContractError, match="source must be one of"):
        Event.from_dict({"source": "carrier-pigeon"})


def test_event_normalises_dunst_urgency():
    assert Event.from_dict({"source": "notification", "urgency": "CRITICAL"}).urgency == "critical"
    assert Event.from_dict({"source": "notification", "urgency": "weird"}).urgency == "normal"
    assert Event.from_dict({"source": "notification"}).urgency == "normal"


def test_event_truncates_long_bodies():
    event = Event.from_dict({"source": "git", "body": "x" * 5000})
    assert len(event.body) == 2000
