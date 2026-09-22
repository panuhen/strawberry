"""beat_watch picks the player's RUNNING stream and notices when PipeWire fed it the sink monitor instead."""

from __future__ import annotations

from strawberry_crab.doorways import beat_watch


def node(id_, name, media_class, app="", state="running", serial=None):
    return {"id": id_, "type": "PipeWire:Interface:Node",
            "info": {"state": state, "props": {"node.name": name, "media.class": media_class, "application.name": app,
                                               "object.serial": serial or id_}}}


def link(out_id, in_id):
    return {"id": 900 + out_id * 10 + in_id, "type": "PipeWire:Interface:Link", "info": {"output-node-id": out_id, "input-node-id": in_id}}


# Spotify as seen live: one running stream and one idle one, plus her own voice and a sink.
DUMP = [
    node(53, "bluez_output.50_C2", "Audio/Sink"),
    node(62, "Strawberry", "Stream/Output/Audio", app="Strawberry"),
    node(68, "spotify", "Stream/Output/Audio", app="Spotify", state="suspended", serial=521),
    node(70, "spotify", "Stream/Output/Audio", app="Spotify", state="running", serial=522),
    node(67, "strawberry-beat", "Stream/Input/Audio", app="pw-record"),
]


def test_only_a_running_player_stream_is_a_target():
    streams = beat_watch.pipewire_streams(DUMP)
    picked = beat_watch.pick_target(streams)
    assert picked["id"] == 70 and picked["serial"] == "522"
    idle_only = [s for s in streams if s["id"] != 70]
    assert beat_watch.pick_target(idle_only) is None          # wait, rather than capture the idle node
    assert beat_watch.pick_target(streams, "spotify")["id"] == 70
    assert beat_watch.pick_target(idle_only, "spotify") is None


def test_sink_monitor_substitution_is_detected():
    target = {"id": 70}
    fed_by_sink = beat_watch.capture_sources(DUMP + [link(53, 67)])
    assert fed_by_sink == [{"id": 53, "name": "bluez_output.50_C2", "class": "Audio/Sink"}]
    assert beat_watch.linked_to_player(fed_by_sink, target) is False
    fed_by_player = beat_watch.capture_sources(DUMP + [link(70, 67)])
    assert beat_watch.linked_to_player(fed_by_player, target) is True
    assert beat_watch.linked_to_player(beat_watch.capture_sources(DUMP), target) is None   # not linked yet
    assert beat_watch.capture_sources(DUMP, node_name="nobody") == []
