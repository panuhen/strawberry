import asyncio

import pytest

from strawberryd.contract import ONE_SHOTS
from strawberryd.daemon import Daemon
from strawberryd.server import create_app


@pytest.fixture
def daemon():
    return Daemon()


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


async def test_perform_rejects_bad_blob_with_reason(client):
    response = await client.post("/perform", json={"state": "zoomies"})
    assert response.status == 400
    assert "state must be one of" in (await response.json())["error"]


async def test_perform_rejects_non_json(client):
    response = await client.post("/perform", data=b"not json")
    assert response.status == 400
    assert (await response.json())["error"] == "body must be JSON"


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


async def test_event_requires_source(client):
    response = await client.post("/event", json={"title": "x"})
    assert response.status == 400
    assert "source is required" in (await response.json())["error"]


async def test_hub_forgets_a_closed_widget(client):
    ws = await client.ws_connect("/ws")
    await ws.close()
    for _ in range(20):
        if (await (await client.get("/health")).json())["widgets"] == 0:
            break
        await asyncio.sleep(0.05)
    assert (await (await client.get("/health")).json())["widgets"] == 0
    assert (await (await client.post("/perform", json={"state": "idle"})).json())["sent"] == 0
