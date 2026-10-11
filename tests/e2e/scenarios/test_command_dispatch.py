"""指令派发：命令直达 handler 并作用于真实核心状态，全程不经过认知管线与 LLM。"""

import asyncio
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from arclet.alconna import Alconna

import muika
from muika.ipc.bootstrap import CoreBootstrap
from muika.models import AdapterInfo
from muika.plugin.command import on_alconna
from muika.plugin.loader import load_plugins, unload_plugin

pytestmark = pytest.mark.e2e


@pytest.fixture
def builtin_plugins():
    """按 run_core 的方式加载真实内建插件，测试后逐一卸载以清理注册副作用。"""
    package_dir = Path(muika.__file__).resolve().parent
    loaded = load_plugins(package_dir / "builtin_plugins", base_path=package_dir.parent)
    yield loaded
    for plugin in loaded:
        unload_plugin(plugin.package_name)


async def test_command_dispatch_bypasses_llm(core_app_factory, builtin_plugins):
    # 不给任何剧本：任何一次 LLM 调用都会当场失败
    app = await core_app_factory(turns=[])
    await app.start()

    # 真实内建命令经完整解析链路回复
    await app.say_command(".help")
    assert app.command_replies[0].startswith("可用命令:")
    assert ".session" in app.command_replies[0]

    # 未匹配命令走兜底回复
    await app.say_command(".definitely_not_a_command")
    assert app.command_replies[-1] == "未知的指令"

    # 依赖注入的命令真实作用于核心：投递 session_end 事件并轮换 session
    old_session_id = app.muika.memory.session.session_id
    await app.say_command(".session new")
    assert "已发送新会话请求" in app.command_replies[-1]
    await app.wait_processed("session_end")
    assert app.muika.memory.session.session_id != old_session_id

    # 行为不变量：命令不触发认知管线，也不占用对话外发通道
    assert app.scripted.calls == []
    assert app.sent == []


@pytest.mark.parametrize("reply_mode", ["return", "finish"])
async def test_delayed_command_keeps_origin_after_disconnect(core_app_factory, monkeypatch, reply_mode):
    app = await core_app_factory(turns=[])
    await app.start()
    core = CoreBootstrap(port=0, ipc_secret="routing-test")
    core._muika = app.muika
    core._register_handlers()
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr("muika.plugin.command._commands", [])
    registry = on_alconna(Alconna("routing-test"))

    @registry.handle()
    async def delayed_reply():
        entered.set()
        await release.wait()
        if reply_mode == "finish":
            await registry.finish("origin-only")
        return "origin-only"

    server = core._ws_server
    application = web.Application()
    application.router.add_get("/ws", server._handle_ws)
    async with TestClient(TestServer(application)) as client:
        headers = {"X-Auth-Token": "routing-test", "X-Client-Name": "control"}
        control = await client.ws_connect("/ws", headers=headers)
        qq = await client.ws_connect("/ws", headers={**headers, "X-Client-Name": "qq"})
        task = asyncio.create_task(
            core._handle_event({"type": "command", "raw": ".routing-test"}, AdapterInfo(client_name="control"))
        )
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await control.close()
            await qq.send_json({"type": "command", "raw": ".unknown"})
            unknown = await asyncio.wait_for(qq.receive_json(), 2)
            assert unknown["type"] == "command_result" and unknown["content"] == "未知的指令"
            await asyncio.wait_for(qq.receive_json(), 2)
            release.set()
            await asyncio.wait_for(task, 2)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(qq.receive_json(), 0.2)
            control = await client.ws_connect("/ws", headers=headers)
            result = await asyncio.wait_for(control.receive_json(), 2)
            assert result["type"] == "command_result" and result["content"] == "origin-only"
            app.recorder.record("origin_reconnected", mode=reply_mode, result=result, rerouted=0)
            assert app.scripted.calls == []
        finally:
            release.set()
            await task
            await control.close()
            await qq.close()
