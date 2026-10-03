"""验证处理检查点把人格、任务和回复一起提交。

失败边界：保存模型结果后死亡导致再次推导；人格已保存但回复丢失；
回复重复提交产生第二个任务；旧 Core 提交迟到结果；任务附件指向原设备。
"""

import hashlib

import pytest
from aiohttp.test_utils import TestServer

from muika.core.agent.task_store import TaskRecord
from muika.core.memory_models import StateUpdate
from muika.database.db import close_db, init_db
from muika.ipc.node_client import NodeClient
from muika.ipc.node_protocol import (
    Acquire,
    Claim,
    Handoff,
    Pending,
    Receive,
    TurnRequest,
)
from muika.ipc.state_server import NodeCredential, StateServer
from muika.node.models import IncomingMessage, OutgoingMessage
from muika.node.remote_memory import RemoteMemoryManager
from muika.node.turn_protocol import (
    CompleteTurn,
    GeneratedReply,
    LoadTurn,
    SaveGeneration,
)

pytestmark = pytest.mark.e2e


async def test_saved_generation_and_atomic_completion_survive_transfer(tmp_path, recorder):
    await init_db(tmp_path / "state.db")
    service = StateServer(
        [
            NodeCredential(id=name, role=role, token_sha256=hashlib.sha256(name.encode()).hexdigest())
            for name, role in (("pc", "core"), ("server", "core"), ("chat", "bot"))
        ]
    )
    server = TestServer(service.app)
    try:
        await server.start_server()
        address = str(server.make_url("/node/ws"))
        async with (
            NodeClient(address, "pc") as pc,
            NodeClient(address, "server") as fallback,
            NodeClient(address, "chat") as bot,
        ):
            await bot.request(
                Receive(
                    message=IncomingMessage(id="1", client_id="chat", conversation_id="master", text="回来以后继续读。")
                )
            )
            lease = (await pc.request(Acquire())).lease
            assert lease is not None
            claim = (await pc.request(Claim(epoch=lease.epoch))).claim
            assert claim is not None
            turn_id = f"input:{claim.sequence}"
            await pc.request(
                TurnRequest(
                    epoch=lease.epoch,
                    body=SaveGeneration(
                        turn_id=turn_id,
                        claim=claim,
                        stage="brain",
                        generated=GeneratedReply(
                            text='我会接着读。<state>{"mood":"安心","reason":"他会回来。"}</state>'
                        ),
                    ),
                )
            )
            await pc.request(Handoff(epoch=lease.epoch, target="server"))
            next_lease = (await fallback.request(Acquire())).lease
            assert next_lease is not None
            next_claim = (await fallback.request(Claim(epoch=next_lease.epoch))).claim
            assert next_claim is not None
            saved = await fallback.request(
                TurnRequest(epoch=next_lease.epoch, body=LoadTurn(turn_id=turn_id, claim=next_claim))
            )
            assert saved.turn is not None
            assert saved.turn.generations["brain"].text.startswith("我会接着读。")
            reply = OutgoingMessage(
                id=f"{turn_id}:reply:0", client_id="chat", conversation_id="master", text="我会接着读。"
            )
            completion = CompleteTurn(
                turn_id=turn_id,
                claim=next_claim,
                content="我会接着读。",
                state_updates=[StateUpdate(mood="安心", reason="他会回来。")],
                notes=["一起读的书还没读完。"],
                replies=[reply],
                tasks=[TaskRecord(id="continue-reading", original_request="读书", instruction="找下一首诗")],
            )
            await fallback.request(TurnRequest(epoch=next_lease.epoch, body=completion))
            await fallback.request(TurnRequest(epoch=next_lease.epoch, body=completion))
            assert (await bot.request(Pending())).replies == [reply]
            memory = RemoteMemoryManager(fallback, next_lease.epoch, tmp_path / "resources")
            await memory.load()
            assert memory.persistent.mood == "安心"
            assert [turn.content for turn in memory.recent_turns if turn.role == "muika"] == ["我会接着读。"]
            assert sum(turn.content.startswith("Task continue-reading queued.") for turn in memory.recent_turns) == 1
            assert len(await service.tasks.load()) == 1
            recorder.record("checked", invariant="saved_generation_atomic_personality_reply_and_task_commit")
    finally:
        await server.close()
        await close_db()
