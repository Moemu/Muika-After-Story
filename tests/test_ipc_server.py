"""验证 IPC 连接只向核心暴露共享的适配器元数据。"""

import asyncio
from datetime import datetime, timedelta

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from muika.ipc.protocol import SendMessage
from muika.ipc.server import CoreWsServer
from muika.models import AdapterInfo


async def test_connection_callbacks_and_messages_share_metadata():
    server = CoreWsServer(secret="test")
    connected = asyncio.Event()
    disconnected = asyncio.Event()
    received: list[AdapterInfo] = []

    async def on_connected(info: AdapterInfo) -> None:
        received.append(info)
        connected.set()

    async def on_disconnected(info: AdapterInfo) -> None:
        received.append(info)
        disconnected.set()

    async def on_message(message: dict, info: AdapterInfo) -> SendMessage:
        received.append(info)
        server.set_triggering_adapter(info.client_name)
        return SendMessage(content="received")

    server.on_adapter_connected(on_connected)
    server.on_adapter_disconnected(on_disconnected)
    server.register_handler("probe", on_message)
    app = web.Application()
    app.router.add_get("/ws", server._handle_ws)
    async with TestClient(TestServer(app)) as client:
        ws = await client.ws_connect("/ws", headers={"X-Auth-Token": "test", "X-Client-Name": "test"})
        await asyncio.wait_for(connected.wait(), 1)
        info = received[0]
        assert type(info) is AdapterInfo
        previous_activity = datetime.now() - timedelta(hours=1)
        info.last_active_at = previous_activity
        await ws.send_json({"type": "probe"})
        reply = await asyncio.wait_for(ws.receive_json(), 1)
        assert reply["content"] == "received"
        assert info.last_active_at > previous_activity
        await ws.close()
        await asyncio.wait_for(disconnected.wait(), 1)
    assert len(received) == 3
    assert all(metadata is info for metadata in received)
    assert not server.has_connection
