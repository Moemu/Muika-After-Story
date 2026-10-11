"""指令派发：命令直达 handler 并作用于真实核心状态，全程不经过认知管线与 LLM。"""

import asyncio
from functools import partial
from pathlib import Path

import pytest
from aiohttp import ClientSession, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer
from arclet.alconna import Alconna

import muika
from muika.core.actions.tools.rss._schema import RSSSource
from muika.database.crud import RssDigestCacheCRUD, TopicHistoryCRUD, UsageORM
from muika.database.db import get_session, observe_commits
from muika.ipc.bootstrap import CoreBootstrap
from muika.ipc.sync_store import SyncStore
from muika.llm._wrapper import record_plugin_usage
from muika.models import AdapterInfo
from muika.plugin.command import on_alconna
from muika.plugin.loader import load_plugins, unload_plugin
from tests.e2e.harness import ScriptedTurn
from tests.e2e.harness.scripted_llm import ScriptedLLM

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


@pytest.mark.parametrize("waiting_at", ["rss", "article", "model"])
async def test_background_reading_keeps_commands_and_messages_available(
    core_app_factory, builtin_plugins, monkeypatch, waiting_at
):
    app = await core_app_factory(
        turns=[
            ScriptedTurn(
                text='{"score":85,"keep":true,"reason":"Shared curiosity","primary_theme":"culture",'
                '"summary":"A reading note about how books connect people."}',
                when=lambda request: request.purpose == "digest_topic_fit",
                name="reading_note",
            ),
            ScriptedTurn(text="我在读书，也在听你说话。", when="你还在吗", name="conversation_during_reading"),
        ]
    )
    await app.start()
    assert app.muika is not None
    store = SyncStore("reading-test")
    store.state = app.muika.state
    await store.initialize()
    observe_commits(store.record)
    entered, release = asyncio.Event(), asyncio.Event()
    counted_ask = record_plugin_usage(ScriptedLLM.ask)

    async def reading_model(request, **kwargs):
        if request.purpose == "digest_topic_fit" and waiting_at == "model":
            entered.set()
            await release.wait()
        return await counted_ask(app.scripted, request, **kwargs)

    async def source(request):
        stage = "rss" if request.path == "/rss" else "article"
        if stage == waiting_at:
            entered.set()
            await release.wait()
        if stage == "rss":
            return web.Response(
                text=f'<rss version="2.0"><channel><title>Reading</title><item><title>Books and people</title>'
                f"<link>{request.scheme}://{request.host}/article</link><description>Reading together</description>"
                "</item></channel></rss>",
                content_type="application/rss+xml",
            )
        return web.Response(
            text="<html><body><article><p>"
            + "Books connect people across time and place. " * 40
            + "</p></article></body></html>",
            content_type="text/html",
        )

    monkeypatch.setattr(app.scripted, "ask", reading_model)
    monkeypatch.setattr("muika.llm._wrapper.get_name_from_config", lambda config: "reading-test")
    monkeypatch.setattr("muika.core.digest_agent.refresh_model", lambda model, config: model)
    application = web.Application()
    application.router.add_get("/rss", source)
    application.router.add_get("/article", source)
    async with TestClient(TestServer(application)) as client:
        monkeypatch.setattr(
            "muika.core.digest_agent.RSS_SOURCES",
            {
                "reading": RSSSource(
                    id="reading",
                    name="Reading",
                    url=str(client.make_url("/rss")),
                    language="en",
                    topics=["culture"],
                    digest=True,
                )
            },
        )
        reading = asyncio.create_task(app.muika.digest_agent.fetch_and_digest())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.wait_for(app.say_command(".usage"), 2)
            assert app.command_replies[-1]
            await app.user_says("你还在吗")
            assert await app.next_reply(timeout=2) == "我在读书，也在听你说话。"
            release.set()
            await asyncio.wait_for(reading, 5)
            topic = app.muika.topic_manager._event_queue[0]
            async with get_session() as db:
                usage = await UsageORM.get_usage_records(db, days=None)
                cached = await RssDigestCacheCRUD.get_cached(db, topic.id, 7)
                history = await TopicHistoryCRUD.get_by_topic_id(db, topic.id)
            assert usage and sum(row.input_tokens for row in usage) > 0
            assert cached is not None and cached.keep == 1
            assert history is not None
            app.recorder.record(
                "reading_and_conversation_completed", waiting_at=waiting_at, usage_rows=len(usage), topic=topic.id
            )
        finally:
            release.set()
            reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)
            observe_commits(None)


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


async def test_direct_stop_waits_past_aiohttp_shutdown_timeout(core_app_factory, monkeypatch):
    app = await core_app_factory(turns=[])
    await app.start()
    core = CoreBootstrap(port=0, ipc_secret="stop-test")
    core._muika = app.muika
    core._register_handlers()
    entered, release, disconnected = [asyncio.Event() for _ in range(3)]
    monkeypatch.setattr("muika.plugin.command._commands", [])
    registry = on_alconna(Alconna("stop-test"))

    @registry.handle()
    async def delayed_reply():
        entered.set()
        await release.wait()
        return "finished-before-stop"

    async def offline(adapter):
        disconnected.set()

    server = core._ws_server
    server.on_adapter_disconnected(offline)
    monkeypatch.setattr(web, "AppRunner", partial(web.AppRunner, shutdown_timeout=0.01))
    await server.start()
    port = server._runner.addresses[0][1]
    async with ClientSession() as client:
        ws = await client.ws_connect(
            f"http://127.0.0.1:{port}/ws",
            headers={"X-Auth-Token": "stop-test", "X-Client-Name": "control"},
        )
        stopping = None
        try:
            await ws.send_json({"type": "command", "raw": ".stop-test"})
            await asyncio.wait_for(entered.wait(), 2)
            stopping = asyncio.create_task(server.stop())
            await asyncio.sleep(0.1)
            assert not stopping.done()
            release.set()
            await asyncio.wait_for(stopping, 2)
            assert disconnected.is_set()
            assert not server._pending and not server.has_connection
            app.recorder.record("direct_stop_drained", timeout=0.01, disconnected=True)
        finally:
            release.set()
            if stopping is not None:
                await stopping
            await ws.close()
            await server.stop()


@pytest.mark.parametrize("blocked_by", ["command", "connect"])
async def test_direct_socket_reads_while_business_waits(core_app_factory, monkeypatch, blocked_by):
    app = await core_app_factory(turns=[])
    await app.start()
    core = CoreBootstrap(port=0, ipc_secret="receive-test")
    core._muika = app.muika
    core._register_handlers()
    entered, release, disconnected = [asyncio.Event() for _ in range(3)]
    monkeypatch.setattr("muika.plugin.command._commands", [])
    registry = on_alconna(Alconna("receive-test"))

    @registry.handle()
    async def delayed_reply():
        if blocked_by == "command":
            entered.set()
            await release.wait()
        return "origin-only"

    async def connected(adapter):
        if blocked_by == "connect" and adapter.client_name == "control":
            entered.set()
            await release.wait()

    async def offline(adapter):
        if adapter.client_name == "control":
            disconnected.set()

    server = core._ws_server
    server.on_adapter_connected(connected)
    server.on_adapter_disconnected(offline)
    application = web.Application()
    application.router.add_get("/ws", server._handle_ws)
    async with TestClient(TestServer(application)) as client:
        headers = {"X-Auth-Token": "receive-test", "X-Client-Name": "control"}
        control = await client.ws_connect("/ws", headers=headers, autoping=False)
        qq = await client.ws_connect("/ws", headers={**headers, "X-Client-Name": "qq"})
        try:
            await control.send_json({"type": "command", "raw": ".receive-test"})
            await asyncio.wait_for(entered.wait(), 2)
            await qq.send_json({"type": "command", "raw": ".unknown"})
            assert (await asyncio.wait_for(qq.receive_json(), 2))["content"] == "未知的指令"
            await asyncio.wait_for(qq.receive_json(), 2)
            await control.ping(b"slow-business")
            assert (await asyncio.wait_for(control.receive(), 1)).type == WSMsgType.PONG
            await control.close()
            assert not disconnected.is_set()
            release.set()
            await asyncio.wait_for(disconnected.wait(), 2)
            control = await client.ws_connect("/ws", headers=headers)
            result = await asyncio.wait_for(control.receive_json(), 2)
            assert result["type"] == "command_result" and result["content"] == "origin-only"
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(qq.receive_json(), 0.1)
            assert app.scripted.calls == []
            app.recorder.record("direct_receive_drained", blocked_by=blocked_by, pong=True, result=result)
        finally:
            release.set()
            await control.close()
            await qq.close()
