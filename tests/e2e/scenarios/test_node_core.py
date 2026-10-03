"""验证真实认知循环在租约过期后继续陪伴。

失败边界：PC 死亡依赖退出钩子；接管后成为初次相遇；重复输入产生重复回复；
私有独白泄露；后台主动思考被禁止；一个 Bot 的输入被回复到另一个 Bot。
"""

import asyncio
import hashlib

import pytest
from aiohttp.test_utils import TestServer
from harness.core_app import CoreApp
from harness.scripted_llm import ScriptedTurn

from muika.database.db import close_db, init_db
from muika.ipc.node_client import NodeClient
from muika.ipc.node_protocol import Pending, Receive
from muika.ipc.state_server import NodeCredential, StateServer
from muika.node.core_node import CoreNode
from muika.node.models import IncomingMessage

pytestmark = pytest.mark.e2e


async def wait_reply(bot, text):
    async with asyncio.timeout(10):
        while True:
            replies = (await bot.request(Pending())).replies
            if any(text in reply.text for reply in replies):
                return replies
            await asyncio.sleep(0.05)


async def test_real_core_resumes_after_pc_disappears(monkeypatch, tmp_path, recorder):
    CoreApp(
        monkeypatch,
        recorder,
        turns=[
            ScriptedTurn(
                text='<heart>我想把这句话留在心里。</heart>我记住我们的约定了。<state>{"mood":"期待","reason":"一起读诗。"}</state>'
            ),
            ScriptedTurn(text="我还记得，我们约好了读诗。"),
        ],
    )
    await init_db(tmp_path / "state.db")
    server = TestServer(
        StateServer(
            [
                NodeCredential(id=name, role=role, token_sha256=hashlib.sha256(name.encode()).hexdigest())
                for name, role in (("pc", "core"), ("server", "core"), ("chat", "bot"), ("other", "bot"))
            ]
        ).app
    )
    jobs = []
    try:
        await server.start_server()
        address = str(server.make_url("/node/ws"))
        pc = CoreNode(address, "pc", "pc", tmp_path / "pc", lease_seconds=1)
        fallback = CoreNode(address, "server", "server", tmp_path / "server", lease_seconds=1)
        jobs.append(asyncio.create_task(pc.run()))
        await asyncio.wait_for(pc.ready.wait(), 10)
        jobs.append(asyncio.create_task(fallback.run()))
        async with NodeClient(address, "chat") as bot, NodeClient(address, "other") as other:
            incoming = IncomingMessage(id="promise", client_id="chat", conversation_id="master", text="下次一起读诗。")
            await bot.request(Receive(message=incoming))
            replies = await wait_reply(bot, "约定")
            assert all("heart" not in reply.text and "留在心里" not in reply.text for reply in replies)
            assert (await other.request(Pending())).replies == []
            assert pc.muika is not None
            session = pc.muika.memory.session.session_id
            # 直接取消运行任务；不发送 release，也不运行交接请求。
            jobs[0].cancel()
            await asyncio.gather(jobs[0], return_exceptions=True)
            await asyncio.wait_for(fallback.ready.wait(), 10)
            assert fallback.muika is not None
            assert fallback.muika.memory.session.session_id == session
            assert fallback.muika.memory.persistent.mood == "期待"
            await bot.request(Receive(message=incoming))
            await bot.request(
                Receive(
                    message=IncomingMessage(id="return", client_id="chat", conversation_id="master", text="我回来了。")
                )
            )
            replies = await wait_reply(bot, "还记得")
            assert len([reply for reply in replies if "约定" in reply.text]) == 1
            recorder.record("checked", invariant="real_core_lease_failover_identity_privacy_and_routing")
    finally:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await server.close()
        await close_db()
