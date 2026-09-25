"""IPC 传输层：真实 WebSocket 往返里的 envelope 鉴别、鉴权与负路径；命令通道区分。

沿用 L1 的真实核心回路（剧本 LLM + 真实 Muika），新增的只是 Bot↔Core 之间的线：
用户消息与对话回复走 ``send_message``，命令与命令结果走 ``command_result``。
"""

import asyncio

import pytest
from aiohttp import WSMsgType
from harness import IpcWire, ScriptedTurn, assert_clean_visible

pytestmark = pytest.mark.e2e

GREETING_REPLY = "收到，我在听。"


@pytest.fixture
def builtin_plugins():
    """复用与 L1 指令场景相同的真实内建插件加载方式。"""
    from pathlib import Path

    import muika
    from muika.plugin.loader import load_plugins, unload_plugin

    package_dir = Path(muika.__file__).resolve().parent
    loaded = load_plugins(package_dir / "builtin_plugins", base_path=package_dir.parent)
    yield loaded
    for plugin in loaded:
        unload_plugin(plugin.package_name)


async def _open_ipc(core_app_factory, turns, secret="e2e-secret", client_name="e2e-bot"):
    """启动 CoreApp、桥接外发并建链；返回 ``(app, wire)``，关闭由调用方负责。"""
    app = await core_app_factory(turns=turns)
    await app.start()
    app.bridge_executor()
    wire = IpcWire(app, secret=secret)
    await wire.open(client_name=client_name)
    return app, wire


async def test_user_message_round_trip_returns_send_message(core_app_factory):
    """Bot→Core 的用户消息经真实传输层进入事件循环，回复以 send_message 原样返回。"""
    app, wire = await _open_ipc(
        core_app_factory,
        [ScriptedTurn(when="wire 你好", name="wire_reply", text=GREETING_REPLY)],
    )
    try:
        await wire.send_event({"type": "user_message", "message": "wire 你好"})
        frame = await wire.next_frame()
        assert frame["type"] == "send_message", frame
        assert frame["content"] == GREETING_REPLY
        assert_clean_visible(frame["content"])
        assert wire.by_type("command_result") == []
        assert app.sent == [GREETING_REPLY]
    finally:
        await wire.close()


async def test_command_round_trip_returns_command_result(core_app_factory, builtin_plugins):
    """命令经真实传输层直达派发器，结果走 command_result 通道，不占用对话外发。"""
    app, wire = await _open_ipc(core_app_factory, [])
    try:
        await wire.send_event({"type": "command", "raw": ".help"})
        frame = await wire.next_frame()
        assert frame["type"] == "command_result", frame
        assert frame["content"].startswith("可用命令:")
        assert ".session" in frame["content"]
        assert wire.by_type("send_message") == []
        assert app.sent == []
        assert app.scripted.calls == []
    finally:
        await wire.close()


async def test_ipc_negative_paths(core_app_factory):
    """传输层负路径：未知类型、非法 JSON、缺鉴权头各行其是，互不干扰。"""
    app, wire = await _open_ipc(core_app_factory, [])
    assert app.muika is not None
    try:
        await wire.send_event({"type": "definitely_not_a_real_type"})
        unknown = await wire.next_frame()
        assert unknown["type"] == "error", unknown
        assert "Unknown type" in unknown["message"]

        assert wire._ws is not None
        await wire._ws.send_str("{not json")
        invalid = await wire.next_frame()
        assert invalid["type"] == "error", invalid
        assert "Invalid JSON" in invalid["message"]

        # 坏输入只产生 server 侧错误帧，不进入事件循环、不触发 LLM、不外发
        assert wire.by_type("send_message") == []
        assert wire.by_type("command_result") == []
        assert app.scripted.calls == []
        assert app.sent == []
    finally:
        await wire.close()

    # 缺鉴权头：握手直接失败，不建链、不影响后续用例
    denied = IpcWire(app, secret="e2e-secret")
    with pytest.raises(Exception, match="401|handshake|Handshake|Unauthorized|CLOSED|closed|CLOSE|Close"):
        await denied.open(client_name="e2e-intruder", secret=None)
        async with asyncio.timeout(10):
            while True:
                assert denied._ws is not None
                message = await denied._ws.receive()
                if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING, WSMsgType.ERROR):
                    raise ConnectionError(f"unauthenticated socket closed by server: {message.type}")
        raise AssertionError("connection without X-Auth-Token should have been rejected")
