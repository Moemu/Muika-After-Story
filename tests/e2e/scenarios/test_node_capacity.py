"""记录个人部署真实拓扑的吞吐与延迟，核对 SQLite 业务事务是否可用。

失败边界：多 Bot 输入丢失；候选竞争影响写入；读取和检查点争用锁；
回复路由串线；排队期间 Core 租约丢失；用模拟检查点代替人格路径。
"""

import asyncio
import hashlib
import statistics
import time

import pytest
from aiohttp.test_utils import TestServer
from harness.core_app import CoreApp
from harness.scripted_llm import ScriptedTurn

from muika.database.db import close_db, init_db
from muika.ipc.bot_client import DurableBotClient
from muika.ipc.node_protocol import Status
from muika.ipc.state_server import NodeCredential, StateServer
from muika.node.core_node import CoreNode
from muika.node.executor_node import ExecutorNode
from muika.node.models import IncomingMessage

pytestmark = pytest.mark.e2e


async def test_personal_topology_processes_burst_without_loss(monkeypatch, tmp_path, recorder):
    count = 60
    CoreApp(monkeypatch, recorder, turns=[ScriptedTurn(text=f"Reply {index}") for index in range(count)])
    await init_db(tmp_path / "state.db")
    credentials = [("pc", "core"), ("backup", "core"), ("device", "executor")]
    credentials += [(f"bot{index}", "bot") for index in range(3)]
    server = TestServer(
        StateServer(
            [
                NodeCredential(id=name, role=role, token_sha256=hashlib.sha256(name.encode()).hexdigest())
                for name, role in credentials
            ]
        ).app
    )
    jobs, bots, received, latencies = [], [], [], []
    queued = {}
    round_trips = []
    started = time.monotonic()
    try:
        await server.start_server()
        address = str(server.make_url("/node/ws"))
        core = CoreNode(address, "pc", "pc", tmp_path / "pc", lease_seconds=5)
        jobs.append(asyncio.create_task(core.run()))
        await asyncio.wait_for(core.ready.wait(), 10)
        jobs.append(
            asyncio.create_task(CoreNode(address, "backup", "backup", tmp_path / "backup", lease_seconds=5).run())
        )
        jobs.append(asyncio.create_task(ExecutorNode(address, "device", "device", tmp_path / "device").run()))
        for index in range(3):
            name = f"bot{index}"

            async def deliver(message, resources, owner=name):
                assert message.client_id == owner and message.conversation_id.startswith(owner + ":")
                received.append(message.id)
                latencies.append(time.monotonic() - queued[message.conversation_id])

            bot = DurableBotClient(address, name, name, tmp_path / name, deliver, input_timeout=0)
            bots.append(bot)
            jobs.append(asyncio.create_task(bot.run()))
            await asyncio.wait_for(bot.connected.wait(), 10)
        for index in range(count):
            bot = bots[index % len(bots)]
            conversation = f"{bot.client_id}:{index}"
            queued[conversation] = time.monotonic()
            await bot.queue_input(
                IncomingMessage(
                    id=str(index), client_id=bot.client_id, conversation_id=conversation, text=f"Message {index}"
                )
            )
        async with asyncio.timeout(90):
            while len(received) < count:
                before = time.monotonic()
                status = await core.connection().request(Status())
                round_trips.append(time.monotonic() - before)
                assert status.lease is not None and status.lease.owner == "pc"
                await asyncio.sleep(0.1)
        assert len(received) == len(set(received)) == count
        elapsed = time.monotonic() - started
        recorder.record(
            "capacity",
            topology="state+2_core+executor+3_bot",
            messages=count,
            model="scripted, API latency excluded",
            elapsed_seconds=round(elapsed, 3),
            replies_per_second=round(count / elapsed, 3),
            round_trip_p50_seconds=round(statistics.median(round_trips), 4),
            round_trip_p95_seconds=round(sorted(round_trips)[int(len(round_trips) * 0.95) - 1], 4),
            burst_reply_p95_seconds=round(sorted(latencies)[int(count * 0.95) - 1], 3),
            loss=0,
            duplicates=0,
            decision="SQLite remains suitable for this tested personal topology",
        )
    finally:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        for bot in bots:
            await bot.close()
        await server.close()
        await close_db()
