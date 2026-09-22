import asyncio

import pytest

from strawberry.contract import ONE_SHOTS
from strawberry.daemon import Daemon
from strawberry.server import create_app


@pytest.fixture
def daemon():
    # Plumbing tests run against the canned reactor; the model path has its own tests.
    from strawberry.config import Config
    from strawberry.events import CannedReactor

    config = Config()
    config.brain.enabled = False
    config.speech.enabled = False
    # No whisper, Ollama or MCP servers in plumbing tests; each has its own test file.
    config.voice.enabled = False
    config.gate.enabled = False
    config.tools.enabled = False
    config.thinker.enabled = False
    return Daemon(reactor=CannedReactor(), config=config)


@pytest.fixture
async def client(aiohttp_client, daemon):
    return await aiohttp_client(create_app(daemon))


async def test_health_reports_no_widgets_at_start(client):
    response = await client.get("/health")
    assert response.status == 200
    body = await response.json()
    assert body["ok"] is True
    assert body["widgets"] == 0
    assert body["performed"] == 0
    assert body["brain"] == {"model": None, "canned": True}


async def test_config_endpoint_shows_effective_settings(client):
    response = await client.get("/config")
    assert response.status == 200
    body = await response.json()
    assert body["brain"]["reaction_model"] == "gemma3:1b"
    assert body["daemon"]["port"] == 8770
    assert (await client.get("/config", headers={"Origin": "https://evil.example"})).status == 403


async def test_perform_rejects_bad_blob_with_reason(client):
    response = await client.post("/perform", json={"state": "zoomies"})
    assert response.status == 400
    assert "state must be one of" in (await response.json())["error"]


async def test_perform_rejects_non_json(client):
    response = await client.post("/perform", data=b"not json", headers={"Content-Type": "application/json"})
    assert response.status == 400
    assert (await response.json())["error"] == "body must be JSON"


async def test_perform_requires_json_content_type(client):
    response = await client.post("/perform", data=b'{"state":"idle"}', headers={"Content-Type": "text/plain"})
    assert response.status == 415


async def test_browser_origins_are_refused_on_http(client):
    response = await client.post("/perform", json={"state": "idle"}, headers={"Origin": "https://evil.example"})
    assert response.status == 403


async def test_browser_origins_are_refused_on_websocket(client):
    response = await client.get("/ws", headers={"Origin": "https://evil.example", "Connection": "Upgrade", "Upgrade": "websocket"})
    assert response.status == 403
    assert (await (await client.get("/health")).json())["widgets"] == 0


async def test_perform_without_widget_is_accepted_but_reaches_nobody(client):
    response = await client.post("/perform", json={"state": "thinking"})
    assert response.status == 200
    assert (await response.json())["sent"] == 0


async def test_perform_reaches_connected_widget(client):
    ws = await client.ws_connect("/ws")
    blob = {"state": "talking", "anim": "alert_snap", "text": "Did someone say my name?", "emotion": "alert"}
    response = await client.post("/perform", json=blob)
    assert (await response.json())["sent"] == 1
    assert await ws.receive_json(timeout=2) == blob
    await ws.close()


async def test_widget_connecting_during_music_is_told_to_dance(client):
    await client.post("/perform", json={"state": "dancing"})            # music started, no widget yet
    await client.post("/perform", json={"state": "talking", "text": "Now playing: something"})
    assert (await (await client.get("/health")).json())["rest_state"] == "dancing"
    ws = await client.ws_connect("/ws")
    assert await ws.receive_json(timeout=2) == {"state": "dancing"}     # first thing it hears
    await client.post("/perform", json={"state": "idle"})               # paused
    assert (await ws.receive_json(timeout=2))["state"] == "idle"
    await ws.close()
    late = await client.ws_connect("/ws")                                # idle needs no catch-up
    await client.post("/perform", json={"state": "thinking"})
    assert (await late.receive_json(timeout=2))["state"] == "thinking"
    await late.close()


TEMPO = {"bpm": 128.0, "period_s": 0.469, "confidence": 0.8, "next_beat": 1_800_000_000.5,
         "evenness": 0.7, "low_ratio": 0.45, "density": 3.2, "loudness_db": -18.0}


async def test_tempo_is_forwarded_validated_and_remembered(client):
    ws = await client.ws_connect("/ws")
    response = await client.post("/tempo", json=TEMPO)
    assert response.status == 200 and (await response.json())["sent"] == 1
    assert await ws.receive_json(timeout=2) == {"tempo": TEMPO}
    assert (await (await client.get("/health")).json())["tempo"] == TEMPO
    late = await client.ws_connect("/ws")                       # a newcomer hears the fresh beat
    assert await late.receive_json(timeout=2) == {"tempo": TEMPO}
    assert (await client.post("/tempo", json={"silent": True})).status == 200
    assert await ws.receive_json(timeout=2) == {"tempo": {"silent": True}}
    for bad in ({"bpm": 128.0}, {**TEMPO, "bpm": 900}, {**TEMPO, "extra": 1}, {**TEMPO, "confidence": "high"}, [1]):
        assert (await client.post("/tempo", json=bad)).status == 400, bad
    await ws.close()
    await late.close()


async def test_health_counts_connected_widget(client):
    ws = await client.ws_connect("/ws")
    await ws.send_json({"type": "hello", "client": "test"})
    assert (await (await client.get("/health")).json())["widgets"] == 1
    await ws.close()


async def test_event_produces_canned_reaction_and_forwards_it(client):
    ws = await client.ws_connect("/ws")
    response = await client.post("/event", json={"source": "git", "title": "strawberry", "body": "Add websocket"})
    assert response.status == 200
    body = await response.json()
    assert body["sent"] == 1
    performance = body["performance"]
    assert performance["state"] == "talking"
    assert performance["anim"] in ONE_SHOTS
    assert "strawberry" in performance["text"] and "Add websocket" in performance["text"]
    assert await ws.receive_json(timeout=2) == performance
    await ws.close()


async def test_critical_notification_reacts_as_alert(client):
    response = await client.post(
        "/event", json={"source": "notification", "app": "Battery", "title": "5% left", "urgency": "CRITICAL"}
    )
    performance = (await response.json())["performance"]
    assert performance["emotion"] == "alert"
    assert performance["anim"] == "alert_snap"
    assert performance["reaction"] == "shiver"


async def test_message_notification_waves_hops_and_carries_the_icon(client, tmp_path):
    icon = tmp_path / "whatsapp.png"
    icon.write_bytes(b"\x89PNG")
    response = await client.post(
        "/event",
        json={"source": "notification", "app": "WhatsApp", "title": "James", "body": "hi", "category": "im.received", "icon": str(icon)},
    )
    performance = (await response.json())["performance"]
    assert performance["reaction"] == "wave"
    assert performance["hop"] is True
    assert performance["icon"] == str(icon)
    assert "anim" not in performance


async def test_pre_push_event_is_a_happy_push_line(client):
    response = await client.post(
        "/event", json={"source": "git", "app": "pre-push", "title": "strawberry", "body": "pushing 2 commits on main to origin"}
    )
    performance = (await response.json())["performance"]
    assert performance["emotion"] == "happy"
    assert performance["text"] == "strawberry: pushing 2 commits on main to origin"


async def test_media_event_is_a_happy_now_playing_line(client):
    response = await client.post("/event", json={"source": "media", "app": "Spotify", "title": "Daft Punk — Around the World"})
    performance = (await response.json())["performance"]
    assert performance["state"] == "talking"
    assert performance["emotion"] == "happy"
    assert performance["reaction"] == "nod" and "anim" not in performance  # she is dancing; a hop would break it
    assert performance["text"] == "Now playing: Daft Punk — Around the World"


async def test_event_requires_source(client):
    response = await client.post("/event", json={"title": "x"})
    assert response.status == 400
    assert "source is required" in (await response.json())["error"]


async def test_widget_ping_gets_a_pong(client):
    ws = await client.ws_connect("/ws")
    await ws.send_json({"type": "ping"})
    assert await ws.receive_json(timeout=2) == {"type": "pong"}
    await ws.close()


async def test_widget_typed_line_takes_the_voice_path(client, daemon):
    ws = await client.ws_connect("/ws")
    await ws.send_json({"type": "heard", "text": "  "})          # blank: ignored
    await ws.send_json({"type": "heard", "text": "hello there"})
    reply = await ws.receive_json(timeout=5)
    assert reply["state"] == "talking" and reply.get("text")
    assert daemon.ledger.to_list()[-1]["said"] == "hello there"   # same funnel as speech: it is in the ledger
    await ws.send_json({"type": "ping"})
    assert await ws.receive_json(timeout=2) == {"type": "pong"}  # nothing else came out of the blank line
    await ws.close()


async def test_shutdown_closes_widgets_with_going_away(aiohttp_client, daemon):
    from aiohttp import WSCloseCode, WSMsgType

    client = await aiohttp_client(create_app(daemon))
    ws = await client.ws_connect("/ws")
    await client.server.app.shutdown()
    msg = await ws.receive(timeout=2)
    assert msg.type == WSMsgType.CLOSE
    assert msg.data == WSCloseCode.GOING_AWAY
    assert daemon.hub.count == 0


async def test_hub_forgets_a_closed_widget(client):
    ws = await client.ws_connect("/ws")
    await ws.close()
    for _ in range(20):
        if (await (await client.get("/health")).json())["widgets"] == 0:
            break
        await asyncio.sleep(0.05)
    assert (await (await client.get("/health")).json())["widgets"] == 0
    assert (await (await client.post("/perform", json={"state": "idle"})).json())["sent"] == 0


async def test_command_reaches_the_widget(client):
    """The tray's way in: POST /command, out to every widget socket (WIRING.md §14)."""
    ws = await client.ws_connect("/ws")
    response = await client.post("/command", json={"command": "hide"})
    assert response.status == 200
    assert (await response.json()) == {"sent": 1, "command": {"command": "hide"}}
    assert await ws.receive_json(timeout=2) == {"command": "hide"}

    await client.post("/command", json={"command": "quiet", "value": 3600})
    assert await ws.receive_json(timeout=2) == {"command": "quiet", "value": 3600}
    await client.post("/command", json={"command": "mute", "value": True})
    assert await ws.receive_json(timeout=2) == {"command": "mute", "value": True}
    await ws.close()


async def test_command_refuses_what_a_widget_would_not_understand(client):
    for bad, reason in (
        ({"command": "explode"}, "unknown command"),
        ({"command": "mute", "value": "yes"}, "needs a bool"),
        ({"command": "quiet", "value": -1}, "value >= 0"),
        ({"command": "quiet", "value": True}, "needs a number"),
        ({"command": "volume", "value": 2}, "between 0 and 1"),
        ({"command": "sleep_after", "value": -5}, "value >= 0"),
        ({"command": "skin"}, "needs a str"),
        ({"command": "hide", "colour": "blue"}, "unknown fields"),
    ):
        response = await client.post("/command", json=bad)
        assert response.status == 400, bad
        assert reason in (await response.json())["error"], bad
    assert (await client.post("/command", json={"command": "quit"},
                              headers={"Origin": "https://evil.example"})).status == 403


async def test_health_carries_the_state_the_tray_shows(client):
    assert (await (await client.get("/health")).json())["state"] == "idle"
    await client.post("/perform", json={"state": "thinking"})
    assert (await (await client.get("/health")).json())["state"] == "thinking"
    await client.post("/perform", json={"state": "dancing"})
    body = await (await client.get("/health")).json()
    assert body["state"] == "dancing" and body["rest_state"] == "dancing"


async def test_a_stale_transient_falls_back_to_her_resting_state(daemon):
    from strawberry.contract import Performance

    await daemon.perform(Performance(state="dancing"))
    await daemon.perform(Performance(state="talking"))
    assert daemon.current_state() == "talking"
    daemon.state_at -= daemon.TRANSIENT_S + 1      # the widget went back to dancing on its own
    assert daemon.current_state() == "dancing"
