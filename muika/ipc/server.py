"""Core 进程的 WebSocket 服务端。"""

from __future__ import annotations

import json
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Coroutine, Dict, List, Optional

from aiohttp import WSMsgType, web

from muika.config import mas_config
from muika.utils.logger import logger

from .protocol import CoreToBotMessage, ErrorMessage

# 默认监听地址和端口
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# Bot 未连接时暂存消息的上限
_MAX_PENDING_MESSAGES = 256


EVENT_HANDLER = Callable[[dict, "AdapterInfo"], Coroutine[Any, Any, Optional[CoreToBotMessage]]]
"""消息处理器签名：接收解析后的 JSON dict 和来源适配器名称，可选地返回响应数据"""
ADAPTER_CALLBACK_FUNC = Callable[["AdapterInfo"], Coroutine[Any, Any, None]]
"""适配器事件回调函数: 适配器名作为传入参数"""


@dataclass
class AdapterInfo:
    """单个适配器连接的元数据。"""

    ws: web.WebSocketResponse
    client_name: str
    connected_at: datetime = field(default_factory=datetime.now)
    last_active_at: datetime = field(default_factory=datetime.now)
    bootstrapped: bool = False

    def __repr__(self) -> str:
        return self.client_name

    def __str__(self) -> str:
        return self.client_name


class CoreWsServer:
    """Core 侧 WebSocket 服务器。

    :param host: 监听地址，默认 127.0.0.1
    :param port: 监听端口，默认 8765
    :param secret: IPC 预共享密钥，Bot 连接时需在 ``X-Auth-Token`` header 中携带
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        secret: str = mas_config.ipc_secret,
    ) -> None:
        self._host = host
        self._port = port
        self._secret = secret
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None

        # 适配器连接注册表: client_name → AdapterInfo
        self._connections: Dict[str, AdapterInfo] = {}

        # Bot 未连接时暂存的消息
        self._pending: deque[CoreToBotMessage] = deque(maxlen=_MAX_PENDING_MESSAGES)

        # 消息处理器注册表: type → handler
        self._handlers: Dict[str, EVENT_HANDLER] = {}

        # 最近一次触发消息（user_message / command）的来源适配器
        self._last_triggering_adapter: Optional[str] = None

        # 适配器连接 / 断开回调（由 CoreBootstrap 注册）
        self._on_adapter_connected: Optional[ADAPTER_CALLBACK_FUNC] = None
        self._on_adapter_disconnected: Optional[ADAPTER_CALLBACK_FUNC] = None

    # ── 公共 API ──────────────────────────────────────────────────────────

    def register_handler(self, message_type: str, handler: EVENT_HANDLER) -> None:
        """
        注册一个消息类型处理器。

        :param message_type: 消息 ``type`` 字段的值（如 ``"user_message"``, ``"command"``）
        :param handler: async 处理函数，接收解析后的 JSON dict 和来源 client_name
        """
        self._handlers[message_type] = handler
        logger.debug(f"[CoreWsServer] Registered handler for type={message_type!r}")

    def on_adapter_connected(self, callback: ADAPTER_CALLBACK_FUNC) -> None:
        """注册适配器连接回调。"""
        self._on_adapter_connected = callback

    def on_adapter_disconnected(self, callback: ADAPTER_CALLBACK_FUNC) -> None:
        """注册适配器断开回调。"""
        self._on_adapter_disconnected = callback

    async def start(self) -> None:
        """启动 WebSocket 服务器（非阻塞，在后台运行）。"""
        self._app = web.Application()
        self._app.router.add_get("/ws", self._handle_ws)

        # 健康检查端点
        self._app.router.add_get("/health", self._handle_health)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.success(f"[CoreWsServer] Listening on ws://{self._host}:{self._port}/ws")

    async def stop(self) -> None:
        """优雅关闭 WebSocket 服务器。"""
        for info in list(self._connections.values()):
            if not info.ws.closed:
                await info.ws.close(code=1001, message=b"Server shutting down")
        self._connections.clear()

        # 清空待发送队列
        self._pending.clear()

        # 停止 aiohttp
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

        logger.info("[CoreWsServer] Server stopped")

    @property
    def has_connection(self) -> bool:
        """是否有适配器已连接。"""
        return len(self._connections) > 0

    async def send_to_bot(self, message: CoreToBotMessage, target: Optional[str] = None) -> bool:
        """
        向 Bot 发送一条消息。

        :param message: 要发送的 IPC 消息
        :param target: 目标适配器名称。
        """
        if not self.has_connection:
            logger.warning(f"[CoreWsServer] No adapter connected — queueing message: {message}")
            return self._queue_or_drop(message)

        ws = self._resolve_target(target)
        if ws is None:
            logger.warning("[CoreWsServer] Failed to resolve target adapter — falling back to queue")
            return self._queue_or_drop(message)

        try:
            await ws.send_str(message.model_dump_json())
            return True
        except Exception as e:
            logger.warning(f"[CoreWsServer] Failed to send message: {e}")
            return self._queue_or_drop(message)

    def _resolve_target(self, target: Optional[str]) -> Optional[web.WebSocketResponse]:
        """
        解析目标适配器的 WebSocket 连接。

        target 为 None 时按以下优先级：
        - 最近触发消息的适配器（``_last_triggering_adapter``）
        - 最近活跃的适配器
        - 任一已连接的适配器
        """
        if target and target in self._connections:
            return self._connections[target].ws

        # target 不存在或为 None：fallback 到最近触发适配器
        if self._last_triggering_adapter and self._last_triggering_adapter in self._connections:
            return self._connections[self._last_triggering_adapter].ws

        # 再 fallback 到最近活跃的适配器
        if self._connections:
            most_active = max(self._connections.values(), key=lambda i: i.last_active_at)
            return most_active.ws

        return None

    def _queue_or_drop(self, message: CoreToBotMessage) -> bool:
        if len(self._pending) >= _MAX_PENDING_MESSAGES:
            logger.warning(f"[CoreWsServer] Pending queue full ({_MAX_PENDING_MESSAGES}) — dropping message")
            return False
        self._pending.append(message)
        logger.debug(f"[CoreWsServer] Queued message (pending={len(self._pending)})")
        return True

    async def flush_pending(self) -> int:
        """将暂存的消息全部发送给最近活跃的 Bot。"""
        if not self.has_connection:
            return 0

        ws = self._resolve_target(None)
        if ws is None:
            return 0

        sent = 0
        while self._pending:
            msg = self._pending.popleft()
            try:
                await ws.send_str(msg.model_dump_json())
                sent += 1
            except Exception as e:
                logger.warning(f"[CoreWsServer] Failed to flush pending message: {e}")
                self._pending.appendleft(msg)
                break
        if sent:
            logger.info(f"[CoreWsServer] Flushed {sent} pending message(s)")
        return sent

    def set_triggering_adapter(self, client_name: str) -> None:
        """记录最近一次触发消息的来源适配器。"""
        self._last_triggering_adapter = client_name
        if client_name in self._connections:
            self._connections[client_name].last_active_at = datetime.now()

    def mark_bootstrapped(self, client_name: str):
        """
        将 client 标记为就绪（已发送 bootstrap 事件）
        """
        if client_name in self._connections:
            self._connections[client_name].bootstrapped = True

    # ── HTTP 端点 ─────────────────────────────────────────────────────────

    def get_adapter_names(self) -> List[str]:
        """返回当前已连接的所有适配器名称。"""
        return list(self._connections.keys())

    async def _handle_health(self, request: web.Request) -> web.Response:
        """GET /health —— 健康检查端点。"""
        return web.json_response(
            {
                "status": "ok",
                "bot_connected": self.has_connection,
                "connected_adapters": self.get_adapter_names(),
                "pending_messages": len(self._pending),
            }
        )

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """GET /ws —— WebSocket 连接处理器。"""

        # 认证校验
        if self._secret:
            token = request.headers.get("X-Auth-Token", "")
            if token != self._secret:
                logger.warning("[CoreWsServer] Rejected unauthenticated connection")
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                await ws.close(code=4003, message=b"Unauthorized")
                return ws

        # 提取客户端声明
        client_name = request.headers.get("X-Client-Name", "").strip()

        # 未声明名称时自动分配
        if not client_name:
            client_name = f"unknown-{uuid.uuid4().hex[:6]}"
            logger.warning(f"[CoreWsServer] No X-Client-Name header — assigned {client_name!r}")

        # 拒绝同名重复连接
        if client_name in self._connections:
            logger.warning(f"[CoreWsServer] Duplicate client_name={client_name!r} — rejecting")
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.close(code=4000, message=f"Client name {client_name!r} is already connected".encode())
            return ws

        ws = web.WebSocketResponse()
        await ws.prepare(request)

        # 注册连接
        adapter = AdapterInfo(
            ws=ws,
            client_name=client_name,
        )
        self._connections[client_name] = adapter
        logger.success(f"[CoreWsServer] Adapter connected: {client_name!r}")

        # 通知回调
        if self._on_adapter_connected:
            try:
                await self._on_adapter_connected(adapter)
            except Exception:
                logger.exception("[CoreWsServer] on_adapter_connected callback raised")

        # 连接建立后立即发送暂存的消息
        await self.flush_pending()

        # 消息接收循环
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._dispatch(msg.data, adapter)
                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"[CoreWsServer] WebSocket error on {client_name!r}: {ws.exception()}")
                    break
                elif msg.type == WSMsgType.CLOSE:
                    logger.info(f"[CoreWsServer] Adapter {client_name!r} disconnected (code={ws.close_code})")
                    break
        except Exception as e:
            logger.error(f"[CoreWsServer] Unexpected error in WS handler for {client_name!r}: {e}")
        finally:
            # 注销连接
            self._connections.pop(client_name, None)
            if self._last_triggering_adapter == client_name:
                self._last_triggering_adapter = None
            logger.info(f"[CoreWsServer] Adapter {client_name!r} connection closed")

            # 通知回调
            if self._on_adapter_disconnected:
                try:
                    await self._on_adapter_disconnected(adapter)
                except Exception:
                    logger.exception("[CoreWsServer] on_adapter_disconnected callback raised")

        return ws

    # ── 消息分发 ──────────────────────────────────────────────────────────

    async def _dispatch(self, raw: str, adapter: AdapterInfo) -> None:
        """解析 JSON 并分发给注册的处理器。"""
        client_name = adapter.client_name
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning(f"[CoreWsServer] Invalid JSON from {client_name!r}: {e}")
            await self.send_to_bot(ErrorMessage(message=f"Invalid JSON: {e}"), target=client_name)
            return

        msg_type = data.get("type", "")
        handler = self._handlers.get(msg_type)

        if handler is None:
            logger.warning(f"[CoreWsServer] No handler for type={msg_type!r} — ignoring")
            await self.send_to_bot(
                ErrorMessage(message=f"Unknown type: {msg_type!r}"),
                target=client_name,
            )
            return

        try:
            result = await handler(data, adapter)
            if result is not None:
                await self.send_to_bot(result, target=client_name)
        except Exception:
            logger.exception(f"[CoreWsServer] Handler for type={msg_type!r} raised")
            await self.send_to_bot(
                ErrorMessage(
                    message=f"Internal error handling {msg_type!r}",
                    detail=str(data),
                ),
                target=client_name,
            )
