"""传输层夹具：在既有 CoreApp 之上，挂载真实 CoreWsServer 并驱动其事件回调。

只验证 *Core 侧真实传输行为*（鉴权、路由、错误包、两条外发通道），
不重新实现 Muika 循环：事件处理器直接调用 CoreApp 的真实队列与真实派发器。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from aiohttp import ClientWebSocketResponse, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from muika.config import mas_config
from muika.core.events import (
    SessionBootstrapEvent,
    SessionEndEvent,
    UserMessageEvent,
    UserMessagePayload,
)
from muika.ipc.protocol import (
    ActionResponse,
    CommandResult,
    SendMessage,
)
from muika.ipc.server import CoreWsServer
from muika.models import AdapterInfo, Message, Resource

from .core_app import CoreApp

_MISSING = object()
"""区分“客户端未提供该字段”和“字段值为 None”的哨兵，避免测试误用 None 触发负路径。"""


class IpcWire:
    """让 CoreApp 走真实 WebSocket 服务器接收 Bot 事件的薄包装。

    :param app: 已启动的 CoreApp；复用其真实队列、真实派发器和外发收集
    """

    def __init__(self, app: CoreApp, secret: str = "e2e-secret") -> None:
        self.app = app
        self.secret = secret
        self.received: list[dict[str, Any]] = []
        self._outbound: list[Any] = []
        self._triggering: str | None = None
        self.app.attach_ipc_outbound(self._outbound)
        self._client: TestClient | None = None
        self._ws: ClientWebSocketResponse | None = None
        self._server = CoreWsServer(secret=secret)
        self._install_handlers()

    def _record(self, direction: str, **data: Any) -> None:
        self.app.recorder.record(f"ipc_{direction}", **data)

    def _install_handlers(self) -> None:
        """注册与 CoreBootstrap 等价的真实事件回调。"""

        async def on_user_message(payload: dict, adapter: AdapterInfo) -> None | ActionResponse:
            assert self.app.muika is not None, "call start() first"
            self.app.recorder.record("event_in", type="user_message", summary=payload.get("message", ""))
            message = Message(
                userid=mas_config.master_id,
                message=payload.get("message", ""),
                resources=[Resource(**item) for item in payload.get("resources", [])],
            )
            await self.app.muika.create_event(UserMessageEvent(payload=UserMessagePayload(message=message)))
            sentinel = SendMessage(content="", resources=[])
            self._outbound.append(sentinel)
            self._triggering = adapter.client_name
            try:
                await self.app.wait_processed("user_message", timeout=15.0)
            finally:
                self._triggering = None
            self._triggering = adapter.client_name
            try:
                await self._drain(sentinel)
            finally:
                self._triggering = None
            return None

        async def on_command(payload: dict, adapter: AdapterInfo) -> None | ActionResponse:
            self.app.recorder.record("command_in", raw=payload.get("raw", ""))
            before = len(self.app.command_replies)
            self._triggering = adapter.client_name
            try:
                await self.app.say_command(payload.get("raw", ""))
            finally:
                self._triggering = None
            result = CommandResult(content=self.app.command_replies[before])
            self._triggering = adapter.client_name
            try:
                await self._drain(result)
            finally:
                self._triggering = None
            return None

        async def on_bootstrap(payload: dict, adapter: AdapterInfo) -> ActionResponse:
            assert self.app.muika is not None, "call start() first"
            self.app.recorder.record("event_in", type="session_bootstrap", summary="")
            await self.app.muika.create_event(SessionBootstrapEvent())
            return ActionResponse(action="session_bootstrap", status="queued")

        async def on_session_end(payload: dict, adapter: AdapterInfo) -> ActionResponse:
            assert self.app.muika is not None, "call start() first"
            self.app.recorder.record("event_in", type="session_end", summary="")
            await self.app.muika.create_event(SessionEndEvent())
            return ActionResponse(action="session_end", status="queued")

        self._server.register_handler("user_message", on_user_message)
        self._server.register_handler("command", on_command)
        self._server.register_handler("session_bootstrap", on_bootstrap)
        self._server.register_handler("session_end", on_session_end)

    async def _drain(self, sentinel: Any) -> None:
        """把本次事件及其之前 wire 队列的真实外发，按生产语义发往触发 Bot。

        ``sentinel`` 标识本次事件自己的回复，与之前事件积压的外发区分先后；
        生产 ``send_to_bot`` 先处理历史积压再发当前消息，连接断开则统一暂存。
        """
        pending = list(self._outbound)
        del self._outbound[:]
        queued = [message for message in pending if message is not sentinel]
        if sentinel not in pending:
            queued.append(sentinel)
        for message in queued:
            self._record("frame", **message.model_dump())
            await self._server.send_to_bot(message, target=self._triggering)

    async def open(self, *, client_name: str = "e2e-bot", secret: Any = _MISSING) -> None:
        """启动真实服务端并以 Bot 身份建链；鉴权失败时抛出握手异常。"""
        app = web.Application()
        app.router.add_get("/ws", self._server._handle_ws)
        self._client = TestClient(TestServer(app))
        await self._client.start_server()
        headers = {"X-Client-Name": client_name}
        if secret is _MISSING:
            headers["X-Auth-Token"] = self.secret
        elif secret is not None:
            headers["X-Auth-Token"] = secret
        try:
            self._ws = await self._client.ws_connect("/ws", headers=headers)
        except Exception:
            await self.close()
            raise
        self._record("open", client=client_name)

    async def close(self) -> None:
        """断开 Bot 连接并停止测试服务端。"""
        ws, self._ws = self._ws, None
        if ws is not None:
            if not ws.closed:
                await ws.close()
            try:
                async with asyncio.timeout(10):
                    await ws.receive()
            except (asyncio.TimeoutError, RuntimeError):
                pass
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    async def next_frame(self, timeout: float = 15.0) -> dict[str, Any]:
        """接收下一条 Core→Bot 的 JSON 帧并记录。"""
        assert self._ws is not None, "call open() first"
        message = await asyncio.wait_for(self._ws.receive(), timeout)
        assert message.type == WSMsgType.TEXT, f"expected TEXT frame, got {message.type}"
        body = json.loads(message.data)
        self.received.append(body)
        self._record("frame", **body)
        return body

    async def send_event(self, body: dict[str, Any]) -> None:
        """以 Bot 身份原样发送一条事件 JSON（可用于构造非法输入的负路径）。"""
        assert self._ws is not None, "call open() first"
        await self._ws.send_json(body)
        self._record("send", **body)

    def by_type(self, message_type: str) -> list[dict[str, Any]]:
        """返回已接收帧中指定类型信封的全部记录。"""
        return [frame for frame in self.received if frame.get("type") == message_type]
