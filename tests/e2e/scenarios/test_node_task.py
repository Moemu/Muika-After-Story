"""验证真实行动任务使用原设备环境，并把外来附件交给执行设备。

失败边界：任务没有保存输入附件；模型误用 Core 的环境；远程工具直读 Core 路径；
行动报告没有回到原会话；工具完成后任务仍停留在运行状态。
"""

import asyncio
import hashlib
import json

import pytest
from aiohttp.test_utils import TestServer
from harness.core_app import CoreApp
from harness.scripted_llm import ScriptedTurn

from muika.database.db import close_db, init_db
from muika.ipc.node_client import NodeClient
from muika.ipc.node_protocol import Pending, Receive
from muika.ipc.state_server import NodeCredential, StateServer
from muika.llm._schema import ToolCall
from muika.models import Resource
from muika.node.core_node import CoreNode
from muika.node.executor_node import ExecutorNode
from muika.node.models import IncomingMessage
from muika.node.resources import ResourceVault

pytestmark = pytest.mark.e2e


async def test_task_reads_attachment_on_selected_device(monkeypatch, tmp_path, recorder):
    content = "An attachment from a different working directory."
    digest = hashlib.sha256(content.encode()).hexdigest()
    core_path = tmp_path / "core" / "resources" / (digest + ".txt")
    app = CoreApp(
        monkeypatch,
        recorder,
        turns=[
            ScriptedTurn(when="[User]", text="我读一下。<agent>读取随消息附上的文件并报告。</agent>"),
            ScriptedTurn(when="[User]", text="另一个入口的消息，我也听到了。"),
            ScriptedTurn(
                when=lambda request: bool(request.tools),
                name="read_attachment",
                text="",
                tool_calls=[ToolCall(id="read", name="read_file", arguments=json.dumps({"path": str(core_path)}))],
            ),
            ScriptedTurn(
                when=lambda request: bool(request.tools),
                name="task_report",
                text='<agent_result status="completed">{"summary":"Read the attachment.",'
                '"verification":["read_file returned the supplied content"]}</agent_result>',
            ),
            ScriptedTurn(when="[Action result]", text="文件读完了，我会记得你给我的这段文字。"),
        ],
    )
    started, proceed = asyncio.Event(), asyncio.Event()
    step = app.scripted.step

    async def delayed_step(request, messages=(), **kwargs):
        if request.tools and not started.is_set():
            started.set()
            await proceed.wait()
        return await step(request, messages, **kwargs)

    monkeypatch.setattr(app.scripted, "step", delayed_step)
    await init_db(tmp_path / "state.db")
    service = StateServer(
        [
            NodeCredential(id=name, role=role, token_sha256=hashlib.sha256(name.encode()).hexdigest())
            for name, role in (("pc", "core"), ("device", "executor"), ("chat", "bot"), ("other", "bot"))
        ]
    )
    server = TestServer(service.app)
    jobs = []
    try:
        await server.start_server()
        address = str(server.make_url("/node/ws"))
        executor = ExecutorNode(address, "device", "device", tmp_path / "device")
        jobs.append(asyncio.create_task(executor.run()))
        await asyncio.wait_for(executor.ready.wait(), 10)
        core = CoreNode(address, "pc", "pc", tmp_path / "core", lease_seconds=3)
        jobs.append(asyncio.create_task(core.run()))
        await asyncio.wait_for(core.ready.wait(), 10)
        await core.select_device("device")
        origin = tmp_path / "bot-working-directory" / "attachment.txt"
        origin.parent.mkdir()
        origin.write_text(content, encoding="utf-8")
        async with NodeClient(address, "chat") as bot, NodeClient(address, "other") as other:
            reference = await bot.upload_resource(
                Resource(type="file", path=str(origin)), ResourceVault(tmp_path / "bot")
            )
            origin.unlink()
            await bot.request(
                Receive(
                    message=IncomingMessage(
                        id="attachment",
                        client_id="chat",
                        conversation_id="original-route",
                        text="读一下这个文件。",
                        resources=[reference],
                    )
                )
            )
            await asyncio.wait_for(started.wait(), 10)
            await other.request(
                Receive(
                    message=IncomingMessage(
                        id="other", client_id="other", conversation_id="other-route", text="在另一个入口打个招呼。"
                    )
                )
            )
            async with asyncio.timeout(10):
                while not (await other.request(Pending())).replies:
                    await asyncio.sleep(0.05)
            proceed.set()
            async with asyncio.timeout(20):
                while True:
                    replies = (await bot.request(Pending())).replies
                    if any("文件读完" in reply.text for reply in replies):
                        break
                    await asyncio.sleep(0.05)
            assert [reply.text for reply in (await other.request(Pending())).replies] == [
                "另一个入口的消息，我也听到了。"
            ]
        tasks = await service.tasks.load()
        assert len(tasks) == 1 and tasks[0].status == "completed"
        assert tasks[0].execution_node_id == "device" and tasks[0].resources
        calls = await service.tasks.calls(tasks[0].id)
        assert len(calls) == 1 and calls[0].status == "completed"
        assert calls[0].result is not None and content in calls[0].result.text
        assert all(reply.conversation_id == "original-route" for reply in replies)
        action = next(call for call in app.scripted.calls if call["name"] == "read_attachment")
        environment = service.registrations["device"].environment
        assert environment is not None and environment.describe() in action["system"]
        assert str(core_path) in action["prompt"]
        assert app.scripted.pending_turns == 0
        recorder.record("checked", invariant="real_task_attachment_device_environment_and_original_reply_route")
    finally:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await server.close()
        await close_db()
