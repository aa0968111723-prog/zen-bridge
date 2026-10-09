"""The host mic state is a room event, not a caption, and it stays in that room."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app.settings import Settings
from app.translate import Translator
from tests.test_round2 import Socket, app_for, auth, open_room, stop, token_of


@pytest.mark.anyio
async def test_session_active_tells_only_that_room():
    app = app_for(
        settings=Settings(allow_testclient=True, translate=False),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            await open_room(client, token, "other")
            async with Socket(app, "/ws/listen?room_id=class", client=("127.0.0.1", 5201)) as room, Socket(
                app, "/ws/listen?room_id=other", client=("127.0.0.1", 5202)
            ) as other:
                hello = await room.recv()
                assert hello["type"] == "hello"
                assert hello["host_live"] is False
                other_hello = await other.recv()
                assert other_hello["host_live"] is False
                turned_on = await client.post(
                    "/api/session/active",
                    json={"room_id": "class", "active": True},
                    headers={**auth(token), "content-type": "application/json"},
                )
                assert turned_on.status_code == 200, turned_on.text
                note = await room.recv()
                assert note == {"type": "room", "room_id": "class", "live": True}
                with pytest.raises(asyncio.TimeoutError):
                    await other.recv(timeout=0.3)
                turned_off = await client.post(
                    "/api/session/active",
                    json={"room_id": "class", "active": False},
                    headers={**auth(token), "content-type": "application/json"},
                )
                assert turned_off.status_code == 200, turned_off.text
                again = await room.recv()
                assert again["type"] == "room"
                assert again["live"] is False
                assert again["room_id"] == "class"
                assert "listen_key" not in again
    finally:
        await stop(app)
