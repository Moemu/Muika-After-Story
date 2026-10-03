"""验证设备动作只执行一次，并拒绝未知结果的盲目重试。

失败边界：新 Core 再次执行已完成写入；执行设备变化时复用本地路径；
旧任期发起新的动作；换调用 ID 绕过同一任务的未知动作。
"""

import asyncio
import hashlib

import pytest
from aiohttp.test_utils import TestServer

from muika.database.db import close_db, init_db
from muika.ipc.node_client import NodeClient, NodeRequestError
from muika.ipc.node_protocol import Acquire, ExecutionRequest, Handoff, RegisterNode
from muika.ipc.state_server import NodeCredential, StateServer
from muika.llm._schema import ToolCall
from muika.node.execution_protocol import (
    ExecutionSpec,
    InspectExecution,
    SubmitExecution,
    ToolCapability,
)
from muika.node.executor_node import ExecutorNode
from muika.plugin.func_call import get_function_calls, on_function_call

pytestmark = pytest.mark.e2e


async def test_actual_device_effect_reuses_result_after_core_takeover(tmp_path, recorder):
    target = tmp_path / "written-on-device.txt"

    @on_function_call("Append one execution marker.")
    async def append_marker() -> str:
        with target.open("a", encoding="utf-8") as file:
            file.write("once\n")
        return "Written on the registered device."

    await init_db(tmp_path / "state.db")
    service = StateServer(
        [
            NodeCredential(id=name, role=role, token_sha256=hashlib.sha256(name.encode()).hexdigest())
            for name, role in (("pc", "core"), ("server", "core"), ("device", "executor"))
        ]
    )
    server = TestServer(service.app)
    job = None
    try:
        await server.start_server()
        address = str(server.make_url("/node/ws"))
        executor = ExecutorNode(address, "device", "device", tmp_path / "device")
        job = asyncio.create_task(executor.run())
        await asyncio.wait_for(executor.ready.wait(), 10)
        async with NodeClient(address, "pc") as pc, NodeClient(address, "server") as fallback:
            await pc.request(
                RegisterNode(
                    tools=[
                        ToolCapability(
                            name="append_marker",
                            scope="device",
                            retry="verify",
                            tool_schema=get_function_calls()["append_marker"].data(),
                        )
                    ]
                )
            )
            lease = (await pc.request(Acquire())).lease
            assert lease is not None
            spec = ExecutionSpec(
                id="task-1-call-1",
                node_id="device",
                source_id="task:1",
                call=ToolCall(id="1", name="append_marker", arguments="{}"),
            )
            await pc.request(ExecutionRequest(epoch=lease.epoch, body=SubmitExecution(spec=spec)))
            result = None
            async with asyncio.timeout(10):
                while result is None:
                    result = (
                        await pc.request(ExecutionRequest(epoch=lease.epoch, body=InspectExecution(id=spec.id)))
                    ).execution
                    if result is None or result.status != "completed":
                        result = None
                        await asyncio.sleep(0.05)
            assert result.result is not None
            assert target.read_text(encoding="utf-8") == "once\n"
            await pc.request(Handoff(epoch=lease.epoch, target="server"))
            next_lease = (await fallback.request(Acquire())).lease
            assert next_lease is not None
            repeated = await fallback.request(ExecutionRequest(epoch=next_lease.epoch, body=SubmitExecution(spec=spec)))
            assert repeated.execution is not None and repeated.execution.status == "completed"
            assert target.read_text(encoding="utf-8") == "once\n"
            with pytest.raises(NodeRequestError, match="lease"):
                await pc.request(
                    ExecutionRequest(
                        epoch=lease.epoch, body=SubmitExecution(spec=spec.model_copy(update={"id": "stale"}))
                    )
                )
            recorder.record("checked", invariant="real_device_effect_once_and_result_reused_across_core_epochs")
    finally:
        if job is not None:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        get_function_calls().pop("append_marker", None)
        await server.close()
        await close_db()
