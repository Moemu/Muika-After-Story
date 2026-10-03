"""验证提醒在没有 Core 时保持持久，并通过原会话投递。

失败边界：状态服务重启丢提醒；提醒触发早于接管导致消失；同一触发重复认领；
设备切换后把提醒投递到另一会话。
"""

import asyncio
import hashlib
from datetime import datetime

import pytest
from aiohttp.test_utils import TestServer

from muika.database.db import close_db, init_db
from muika.ipc.node_client import NodeClient
from muika.ipc.node_protocol import Acquire, Claim, SaveRuntime, ScheduleRequest
from muika.ipc.state_server import NodeCredential, StateServer
from muika.node.schedule_protocol import CreateSchedule, ScheduleRecord
from muika.node.turn_protocol import ClientRoute, RuntimeSnapshot

pytestmark = pytest.mark.e2e


async def test_persistent_reminder_survives_state_service_restart(tmp_path, recorder):
    path = tmp_path / "state.db"
    credentials = [
        NodeCredential(id=name, role=role, token_sha256=hashlib.sha256(name.encode()).hexdigest())
        for name, role in (("core", "core"), ("chat", "bot"))
    ]
    await init_db(path)
    server = TestServer(StateServer(credentials).app)
    try:
        await server.start_server()
        async with NodeClient(str(server.make_url("/node/ws")), "core") as core:
            lease = (await core.request(Acquire())).lease
            assert lease is not None
            await core.request(
                SaveRuntime(
                    epoch=lease.epoch,
                    runtime=RuntimeSnapshot(route=ClientRoute(client_id="chat", conversation_id="poetry")),
                )
            )
            await core.request(
                ScheduleRequest(
                    epoch=lease.epoch,
                    body=CreateSchedule(
                        schedule=ScheduleRecord(
                            id="poetry",
                            event="提醒他回来读诗",
                            when="tonight",
                            due_at=datetime.now().timestamp() + 0.1,
                            route=ClientRoute(client_id="chat", conversation_id="poetry"),
                        )
                    ),
                )
            )
        await server.close()
        await close_db()
        await asyncio.sleep(0.15)
        await init_db(path)
        server = TestServer(StateServer(credentials).app)
        await server.start_server()
        async with NodeClient(str(server.make_url("/node/ws")), "core") as core:
            lease = (await core.request(Acquire())).lease
            assert lease is not None
            claim = None
            async with asyncio.timeout(5):
                while claim is None:
                    claim = (await core.request(Claim(epoch=lease.epoch))).claim
                    await asyncio.sleep(0.05)
            assert claim.message.id == "schedule:poetry:0"
            assert claim.message.conversation_id == "poetry"
            assert claim.message.event is not None
            assert claim.message.event.what == "提醒他回来读诗"
            recorder.record("checked", invariant="reminder_survives_service_restart_and_keeps_conversation")
    finally:
        await server.close()
        await close_db()
