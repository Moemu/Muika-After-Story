"""验证不确定动作、资源副本和控制提交的实际业务边界。

失败边界：动作发生后异常被误判为可重试；换 ID 重复动作；附件暴露任意文件；
控制已回复但未保存；失败控制事务留下回复；旧路径损坏资源。
"""

import asyncio
import hashlib
import json

import pytest
from aiohttp.test_utils import TestServer

from muika.config import mas_config
from muika.core.agent.task_store import TaskChange, TaskRecord
from muika.database.db import close_db, init_db
from muika.ipc.node_client import NodeClient, NodeRequestError
from muika.ipc.node_protocol import (
    Acquire,
    Claim,
    ExecutionRequest,
    Pending,
    Receive,
    TurnRequest,
)
from muika.ipc.state_server import NodeCredential, StateServer
from muika.llm._schema import ToolCall
from muika.models import Resource
from muika.node.execution_protocol import (
    ExecutionSpec,
    InspectExecution,
    SubmitExecution,
)
from muika.node.executor_node import ExecutorNode
from muika.node.models import IncomingMessage, OutgoingMessage
from muika.node.resources import ResourceVault
from muika.node.turn_protocol import CompleteTurn
from muika.plugin.func_call import get_function_calls, on_function_call

pytestmark = pytest.mark.e2e


async def outcome(core, epoch, id):
    async with asyncio.timeout(10):
        while True:
            record = (await core.request(ExecutionRequest(epoch=epoch, body=InspectExecution(id=id)))).execution
            if record.status not in {"pending", "running"}:
                return record
            await asyncio.sleep(0.05)


async def test_unknown_real_effect_blocks_new_ids_and_transferred_inputs_are_immutable(monkeypatch, tmp_path, recorder):
    marker = tmp_path / "marker.txt"

    @on_function_call("Write a marker then lose the result.")
    async def interrupted_write() -> str:
        with marker.open("a", encoding="utf-8") as file:
            file.write("once\n")
        raise OSError("Result channel was interrupted after the write.")

    monkeypatch.setattr(mas_config, "fs_allowed_paths", [])
    await init_db(tmp_path / "state.db")
    service = TestServer(
        StateServer(
            [
                NodeCredential(id=name, role=role, token_sha256=hashlib.sha256(name.encode()).hexdigest())
                for name, role in (("core", "core"), ("device", "executor"))
            ]
        ).app
    )
    job = None
    try:
        await service.start_server()
        address = str(service.make_url("/node/ws"))
        executor = ExecutorNode(address, "device", "device", tmp_path / "device")
        job = asyncio.create_task(executor.run())
        await asyncio.wait_for(executor.ready.wait(), 10)
        async with NodeClient(address, "core") as core:
            epoch = (await core.request(Acquire())).lease.epoch
            spec = ExecutionSpec(
                id="unknown",
                node_id="device",
                source_id="task:uncertain",
                call=ToolCall(id="first", name="interrupted_write", arguments="{}"),
            )
            await core.request(ExecutionRequest(epoch=epoch, body=SubmitExecution(spec=spec)))
            record = await outcome(core, epoch, spec.id)
            assert record.status == "unknown" and record.result.outcome == "unknown"
            for id in ("unknown", "bypass"):
                if id == spec.id:
                    same = (
                        await core.request(ExecutionRequest(epoch=epoch, body=SubmitExecution(spec=spec)))
                    ).execution
                    assert same.status == "unknown"
                else:
                    with pytest.raises(NodeRequestError, match="unknown outcome"):
                        await core.request(
                            ExecutionRequest(epoch=epoch, body=SubmitExecution(spec=spec.model_copy(update={"id": id})))
                        )
            assert marker.read_text(encoding="utf-8") == "once\n"
            source = tmp_path / "foreign-core" / "poem.txt"
            source.parent.mkdir()
            source.write_text("Our transferred poem", encoding="utf-8")
            reference = await core.upload_resource(
                Resource(type="file", path=str(source)), ResourceVault(source.parent / "vault")
            )
            source.unlink()
            read = ExecutionSpec(
                id="read",
                node_id="device",
                source_id="task:read",
                call=ToolCall(
                    id="read", name="read_file", arguments=json.dumps({"path": f"resource:{reference.sha256}"})
                ),
                inputs=[reference],
            )
            await core.request(ExecutionRequest(epoch=epoch, body=SubmitExecution(spec=read)))
            result = await outcome(core, epoch, read.id)
            assert not result.result.is_error and "Our transferred poem" in result.result.text
            assert (tmp_path / "device/execution_resources" / (reference.sha256 + ".txt")).is_file()
            monkeypatch.setattr(mas_config, "fs_allowed_paths", [str(tmp_path / "device/execution_resources")])
            monkeypatch.setattr(mas_config, "action_permission", "write")
            write = read.model_copy(
                update={
                    "id": "write",
                    "call": ToolCall(
                        id="write",
                        name="write_file",
                        arguments=json.dumps({"path": f"resource:{reference.sha256}", "content": "replace"}),
                    ),
                }
            )
            await core.request(ExecutionRequest(epoch=epoch, body=SubmitExecution(spec=write)))
            blocked = await outcome(core, epoch, write.id)
            assert blocked.result.is_error and "immutable" in blocked.result.text
            recorder.record("checked", invariant="real_unknown_effect_blocks_retries_and_foreign_attachment_read_only")
    finally:
        if job is not None:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        get_function_calls().pop("interrupted_write", None)
        await service.close()
        await close_db()


async def test_task_control_and_reply_commit_together_or_rollback(tmp_path, recorder):
    await init_db(tmp_path / "state.db")
    state = StateServer(
        [
            NodeCredential(id=name, role=role, token_sha256=hashlib.sha256(name.encode()).hexdigest())
            for name, role in (("core", "core"), ("chat", "bot"))
        ]
    )
    service = TestServer(state.app)
    try:
        await service.start_server()
        address = str(service.make_url("/node/ws"))
        async with NodeClient(address, "core") as core, NodeClient(address, "chat") as bot:
            epoch = (await core.request(Acquire())).lease.epoch
            task = TaskRecord(id="task", instruction="Read", original_request="Read")
            await state.tasks.save(task)
            await bot.request(
                Receive(message=IncomingMessage(id="cancel", client_id="chat", conversation_id="master", text="Cancel"))
            )
            claim = (await core.request(Claim(epoch=epoch))).claim
            reply = OutgoingMessage(id="cancel-reply", client_id="chat", conversation_id="master", text="Cancelled")
            controlled = task.model_copy(update={"revision": 2, "status": "cancelled", "cancel_requested": True})
            complete = CompleteTurn(
                turn_id=f"input:{claim.sequence}",
                claim=claim,
                replies=[reply],
                task_changes=[TaskChange(expected_revision=0, task=controlled)],
            )
            with pytest.raises(NodeRequestError, match="Task changed"):
                await core.request(TurnRequest(epoch=epoch, body=complete))
            assert (await bot.request(Pending())).replies == []
            assert (await state.tasks.load())[0].revision == 1
            complete.task_changes[0].expected_revision = 1
            await core.request(TurnRequest(epoch=epoch, body=complete))
            assert (await bot.request(Pending())).replies == [reply]
            restored = (await state.tasks.load())[0]
            assert restored.cancel_requested and restored.revision == 2
            recorder.record("checked", invariant="task_control_and_reply_atomic_commit_or_complete_rollback")
    finally:
        await service.close()
        await close_db()
