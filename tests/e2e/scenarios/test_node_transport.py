"""验证固定入口的角色认证、出站连接、可靠收发与重连。

失败边界：旧协议误接入；Bot 冒充 Core；伪造输入来源；断连后错投回复；
同一节点重复建立活动连接；不同节点复用请求的处理权。
"""

import hashlib

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from muika.database.db import close_db, init_db
from muika.ipc.node_client import NodeClient, NodeRequestError
from muika.ipc.node_protocol import (
    Acknowledge,
    Acquire,
    Claim,
    Commit,
    Pending,
    Receive,
)
from muika.ipc.state_server import NodeCredential, StateServer
from muika.node.models import IncomingMessage, OutgoingMessage

pytestmark = pytest.mark.e2e


async def test_fixed_entry_routes_multiple_clients_and_fences_roles(tmp_path, recorder):
    """多个客户端只连接固定地址，Core 不需要提供入站端口。"""
    await init_db(tmp_path / "state.db")
    credentials = [
        NodeCredential(id=name, role=role, token_sha256=hashlib.sha256(name.encode()).hexdigest())
        for name, role in [("bot-a", "bot"), ("bot-b", "bot"), ("pc", "core")]
    ]
    service = StateServer(credentials)
    server = TestServer(service.app)
    try:
        await server.start_server()
        duplicate = TestServer(StateServer(credentials).app)
        with pytest.raises(RuntimeError, match="already owns"):
            await duplicate.start_server()
        await duplicate.close()
        address = str(server.make_url("/node/ws"))
        async with aiohttp.ClientSession() as raw:
            with pytest.raises(aiohttp.WSServerHandshakeError) as failure:
                await raw.ws_connect(address, headers={"Authorization": "Bearer bot-a", "X-MAS-Protocol": "1"})
            assert failure.value.status == 426
            with pytest.raises(aiohttp.WSServerHandshakeError) as failure:
                await raw.ws_connect(address, headers={"Authorization": "Bearer wrong", "X-MAS-Protocol": "2"})
            assert failure.value.status == 401
        async with (
            NodeClient(address, "bot-a") as a,
            NodeClient(address, "bot-b") as b,
            NodeClient(address, "pc") as core,
        ):
            async with aiohttp.ClientSession() as raw:
                with pytest.raises(aiohttp.WSServerHandshakeError) as failure:
                    await raw.ws_connect(address, headers={"Authorization": "Bearer pc", "X-MAS-Protocol": "2"})
                assert failure.value.status == 409
            with pytest.raises(NodeRequestError, match="role"):
                await a.request(Acquire())
            incoming = IncomingMessage(id="a-1", client_id="bot-a", conversation_id="room", text="Hello")
            with pytest.raises(NodeRequestError, match="identity"):
                await b.request(Receive(message=incoming))
            await a.request(Receive(message=incoming))
            lease = (await core.request(Acquire())).lease
            assert lease is not None
            claim = (await core.request(Claim(epoch=lease.epoch))).claim
            assert claim is not None
            reply = OutgoingMessage(id="reply", client_id="bot-a", conversation_id="room", text="Hello again")
            await core.request(Commit(epoch=lease.epoch, claim=claim, replies=[reply]))
            assert (await b.request(Pending())).replies == []
            assert (await a.request(Pending())).replies == [reply]
        async with NodeClient(address, "bot-a") as a:
            assert (await a.request(Pending())).replies == [reply]
            await a.request(Acknowledge(message_id=reply.id))
            assert (await a.request(Pending())).replies == []
        recorder.record("checked", invariant="authenticated_fixed_entry_with_durable_reply_reconnect")
    finally:
        await server.close()
        await close_db()
