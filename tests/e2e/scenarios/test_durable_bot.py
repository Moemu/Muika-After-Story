"""验证 Bot 离线队列、合并窗口和重连投递。

失败边界：发送前进程退出丢消息；冻结批次重传时换身份；回复确认丢失后再次发送；
发送中断被误判为未执行；附件依赖 Core 的本地路径。
"""

import asyncio
import hashlib

import pytest
from aiohttp.test_utils import TestServer

from muika.database.db import close_db, init_db
from muika.ipc.bot_client import DurableBotClient
from muika.ipc.node_client import NodeClient
from muika.ipc.node_protocol import Acquire, Claim, Commit
from muika.ipc.state_server import NodeCredential, StateServer
from muika.node.models import IncomingMessage, OutgoingMessage

pytestmark = pytest.mark.e2e


async def test_offline_inputs_and_delivery_ack_survive_bot_restart(tmp_path, recorder):
    directory = tmp_path / "bot"
    received = []

    async def deliver(message, resources):
        received.append(message.id)

    offline = DurableBotClient("ws://127.0.0.1:1/node/ws", "chat", "chat", directory, deliver, input_timeout=0.1)
    await offline.queue_input(IncomingMessage(id="native-1", client_id="chat", conversation_id="master", text="第一句"))
    await offline.queue_input(IncomingMessage(id="native-2", client_id="chat", conversation_id="master", text="第二句"))
    await offline.close()
    await init_db(tmp_path / "state.db")
    server = TestServer(
        StateServer(
            [
                NodeCredential(id=name, role=role, token_sha256=hashlib.sha256(name.encode()).hexdigest())
                for name, role in (("core", "core"), ("chat", "bot"))
            ]
        ).app
    )
    job = None
    try:
        await server.start_server()
        address = str(server.make_url("/node/ws"))
        bot = DurableBotClient(address, "chat", "chat", directory, deliver, input_timeout=0.1)
        job = asyncio.create_task(bot.run())
        await asyncio.wait_for(bot.connected.wait(), 5)
        async with NodeClient(address, "core") as core:
            lease = (await core.request(Acquire())).lease
            assert lease is not None
            claim = None
            async with asyncio.timeout(5):
                while claim is None:
                    claim = (await core.request(Claim(epoch=lease.epoch))).claim
                    await asyncio.sleep(0.05)
            assert claim.message.text == "第一句第二句"
            assert claim.message.member_ids == ["native-1", "native-2"]
            await core.request(
                Commit(
                    epoch=lease.epoch,
                    claim=claim,
                    replies=[
                        OutgoingMessage(id="reply-1", client_id="chat", conversation_id="master", text="我听见了。")
                    ],
                )
            )
            async with asyncio.timeout(5):
                while received != ["reply-1"]:
                    await asyncio.sleep(0.05)
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        await bot.close()
        bot = DurableBotClient(address, "chat", "chat", directory, deliver, input_timeout=0.1)
        job = asyncio.create_task(bot.run())
        await asyncio.wait_for(bot.connected.wait(), 5)
        await asyncio.sleep(0.3)
        assert received == ["reply-1"]
        recorder.record("checked", invariant="persistent_raw_inputs_frozen_merge_and_delivered_reply_dedup")
    finally:
        if job is not None:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
            await bot.close()
        await server.close()
        await close_db()
