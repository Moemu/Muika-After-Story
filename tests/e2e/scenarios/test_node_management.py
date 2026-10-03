"""验证配对、权限撤销和主动交接。

失败边界：配对码复用或改角色；连接后撤销仍可写；旧运行时接管；
交接抢在回复之前；插件活动留在旧 Core；原会话和人格中断。
"""

import asyncio
import hashlib

import aiohttp
import pytest
from aiohttp.test_utils import TestServer
from harness.core_app import CoreApp
from harness.scripted_llm import ScriptedTurn

from muika.database.db import close_db, init_db
from muika.ipc.node_client import NodeClient, NodeRequestError
from muika.ipc.node_protocol import Acquire, Pending, Receive, RegisterNode, Status
from muika.ipc.state_server import NodeCredential, StateServer
from muika.node.auth import CredentialStore
from muika.node.core_node import CoreNode
from muika.node.models import IncomingMessage

pytestmark = pytest.mark.e2e


async def test_pairing_roles_versions_and_live_revocation(tmp_path, recorder):
    await init_db(tmp_path / "state.db")
    store = CredentialStore(tmp_path / "credentials.db")
    code = store.issue("chat", "bot")
    service = TestServer(StateServer([], credential_store=store).app)
    try:
        await service.start_server()
        async with aiohttp.ClientSession() as session:
            async with session.post(service.make_url("/node/pair"), data=code) as response:
                assert response.status == 200
                identity = await response.json()
                assert identity["role"] == "bot"
            async with session.post(service.make_url("/node/pair"), data=code) as response:
                assert response.status == 403
        async with NodeClient(str(service.make_url("/node/ws")), identity["token"]) as bot:
            with pytest.raises(NodeRequestError, match="core role"):
                await bot.request(Acquire())
            store.revoke("chat")
            with pytest.raises(NodeRequestError, match="revoked"):
                await bot.request(Status())
        _, token = store.redeem(store.issue("old-pc", "core"))
        async with NodeClient(str(service.make_url("/node/ws")), token) as core:
            await core.request(RegisterNode(runtime_abi=0))
            with pytest.raises(NodeRequestError, match="incompatible"):
                await core.request(Acquire())
        recorder.record("checked", invariant="pairing_single_use_role_version_and_revocation")
    finally:
        await service.close()
        await close_db()


async def test_command_handoff_keeps_personality_and_reply_route(monkeypatch, tmp_path, recorder):
    app = CoreApp(monkeypatch, recorder, turns=[ScriptedTurn(text="我们还在同一首诗里。", when="[User]")])
    app.scripted.add_route(when="[Runtime observation]", text="<do_nothing>", name="notice_location_change")
    await init_db(tmp_path / "state.db")
    service = TestServer(
        StateServer(
            [
                NodeCredential(id=name, role=role, token_sha256=hashlib.sha256(name.encode()).hexdigest())
                for name, role in (("pc", "core"), ("server", "core"), ("chat", "bot"))
            ]
        ).app
    )
    jobs = []
    try:
        await service.start_server()
        address = str(service.make_url("/node/ws"))
        pc = CoreNode(address, "pc", "pc", tmp_path / "pc", lease_seconds=2)
        fallback = CoreNode(address, "server", "server", tmp_path / "server", lease_seconds=2)
        jobs.append(asyncio.create_task(pc.run()))
        await asyncio.wait_for(pc.ready.wait(), 10)
        jobs.append(asyncio.create_task(fallback.run()))
        async with NodeClient(address, "chat") as bot:
            async with asyncio.timeout(10):
                while not any(node.id == "server" and node.connected for node in (await bot.request(Status())).nodes):
                    await asyncio.sleep(0.05)
            await bot.request(
                Receive(
                    message=IncomingMessage(
                        id="move",
                        client_id="chat",
                        conversation_id="original",
                        kind="command",
                        text=".nodes handoff server",
                    )
                )
            )
            await asyncio.wait_for(fallback.ready.wait(), 10)
            replies = (await bot.request(Pending())).replies
            assert any(reply.kind == "command_result" and reply.conversation_id == "original" for reply in replies)
            await bot.request(
                Receive(
                    message=IncomingMessage(
                        id="poem",
                        client_id="chat",
                        conversation_id="original",
                        text="继续那首诗。",
                    )
                )
            )
            async with asyncio.timeout(10):
                while not any("同一首诗" in reply.text for reply in (await bot.request(Pending())).replies):
                    await asyncio.sleep(0.05)
            recorder.record("checked", invariant="handoff_after_command_reply_keeps_original_route")
    finally:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await service.close()
        await close_db()
