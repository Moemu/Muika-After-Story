"""验证真实记忆服务的跨节点恢复和事务边界。

失败边界：过期 Core 改写人格；远程工作视图丢失对话或心愿；
已提交回复在重投时重复写入；工具资源仍指向另一台主机的路径。
"""

import hashlib

import pytest
from aiohttp.test_utils import TestServer

from muika.core.memory_models import Intention, StateUpdate
from muika.database.db import close_db, init_db
from muika.ipc.node_client import NodeClient, NodeRequestError
from muika.ipc.node_protocol import Acquire, Handoff
from muika.ipc.state_server import NodeCredential, StateServer
from muika.node.remote_memory import RemoteMemoryManager

pytestmark = pytest.mark.e2e


async def test_memory_and_inner_state_resume_on_another_core(tmp_path, recorder):
    await init_db(tmp_path / "state.db")
    credentials = [
        NodeCredential(id=name, role="core", token_sha256=hashlib.sha256(name.encode()).hexdigest())
        for name in ("pc", "server")
    ]
    server = TestServer(StateServer(credentials).app)
    try:
        await server.start_server()
        address = str(server.make_url("/node/ws"))
        async with NodeClient(address, "pc") as pc, NodeClient(address, "server") as fallback:
            lease = (await pc.request(Acquire())).lease
            assert lease is not None
            memory = RemoteMemoryManager(pc, lease.epoch, tmp_path / "pc-resources")
            await memory.load()
            session_id = memory.session.session_id
            await memory.add_context("user", "下次一起读诗吧。", source="input:poetry")
            await memory.update_state(
                StateUpdate(
                    mood="期待",
                    reason="他答应回来读诗。",
                    intentions=[Intention(id="read-poetry", description="一起读诗")],
                )
            )
            await memory.add_context("muika", "我会记得的。", source="reply:poetry")
            await pc.request(Handoff(epoch=lease.epoch, target="server"))
            next_lease = (await fallback.request(Acquire())).lease
            assert next_lease is not None
            resumed = RemoteMemoryManager(fallback, next_lease.epoch, tmp_path / "server-resources")
            await resumed.load()
            assert resumed.session.session_id == session_id
            assert resumed.has_history
            assert resumed.persistent.mood == "期待"
            assert resumed.persistent.intentions[0].id == "read-poetry"
            assert [turn.content for turn in resumed.recent_turns] == ["下次一起读诗吧。", "我会记得的。"]
            await resumed.add_context("user", "下次一起读诗吧。", source="input:poetry")
            assert len(resumed.recent_turns) == 2
            with pytest.raises(NodeRequestError, match="lease"):
                await memory.update_state(StateUpdate(mood="被旧实例覆盖", reason="stale writer"))
            recorder.record("checked", invariant="same_identity_memory_and_intention_after_core_transfer")
    finally:
        await server.close()
        await close_db()
