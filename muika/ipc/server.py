"""Core 进程的 WebSocket 服务端。

在 Core 进程中运行，接受 Bot 进程的 WebSocket 连接，
将收到的消息分发给已注册的处理器，并为 ``IpcBridge`` 提供
``send_to_bot()`` 方法将消息推回 Bot。

设计要点：
- 同时只接受一个 Bot 连接（单用户场景）
- 当 Bot 未连接时，``send_to_bot()`` 将消息暂存到队列
- 使用 aiohttp（项目已有依赖），不引入新的 WebSocket 库
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any, Callable, Coroutine, Dict, Optional

from aiohttp import WSMsgType, web

from muika.config import mas_config
from muika.utils.logger import logger

from .protocol import CoreToBotMessage, ErrorMessage

# 默认监听地址和端口
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# Bot 未连接时暂存消息的上限
_MAX_PENDING_MESSAGES = 256


Handler = Callable[[dict], Coroutine[Any, Any, CoreToBotMessage]]
"""消息处理器签名：接收解析后的 JSON dict，可选地返回响应数据"""


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

        # 当前活跃的 Bot WebSocket 连接
        self._ws: Optional[web.WebSocketResponse] = None

        # Bot 未连接时暂存的消息
        self._pending: deque[CoreToBotMessage] = deque(maxlen=_MAX_PENDING_MESSAGES)

        # 消息处理器注册表: type → handler
        self._handlers: Dict[str, Handler] = {}

    # ── 公共 API ──────────────────────────────────────────────────────────

    def register_handler(self, message_type: str, handler: Handler) -> None:
        """注册一个消息类型处理器。

        当收到 ``type`` 匹配的消息时，调用 *handler* 并将返回值
        通过 ``query_response`` 发回 Bot。

        Parameters
        ----------
        message_type: 消息 ``type`` 字段的值（如 ``"event"``, ``"query"``, ``"debug"``）
        handler: async 处理函数，接收 ``BotToCoreMessage``，可选返回 dict
        """
        self._handlers[message_type] = handler
        logger.debug(f"[CoreWsServer] Registered handler for type={message_type!r}")

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
        # 关闭当前 Bot 连接
        if self._ws and not self._ws.closed:
            await self._ws.close(code=1001, message=b"Server shutting down")
            self._ws = None

        # 清空待发送队列
        self._pending.clear()

        # 停止 aiohttp
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

        logger.info("[CoreWsServer] Server stopped")

    @property
    def has_connection(self) -> bool:
        """Bot 是否已连接。"""
        return self._ws is not None and not self._ws.closed

    async def send_to_bot(self, message: CoreToBotMessage) -> bool:
        """
        向 Bot 发送一条消息。
        """
        if not self.has_connection:
            logger.warning(f"[CoreWsServer] Bot 未连接，将暂存消息: {message}")
            return self._queue_or_drop(message)

        try:
            await self._ws.send_str(message.model_dump_json())  # type: ignore[union-attr]
            return True
        except Exception as e:
            logger.warning(f"[CoreWsServer] Failed to send message to Bot: {e}")
            # 发送失败时回退到暂存队列
            return self._queue_or_drop(message)

    def _queue_or_drop(self, message: CoreToBotMessage) -> bool:
        if len(self._pending) >= _MAX_PENDING_MESSAGES:
            logger.warning(f"[CoreWsServer] Pending queue full ({_MAX_PENDING_MESSAGES}) — dropping message")
            return False
        self._pending.append(message)
        logger.debug(f"[CoreWsServer] Queued message (pending={len(self._pending)})")
        return True

    async def flush_pending(self) -> int:
        """将暂存的消息全部发送给 Bot。

        Returns
        -------
        int
            成功发送的消息数
        """
        if not self.has_connection:
            return 0

        sent = 0
        while self._pending:
            msg = self._pending.popleft()
            try:
                await self._ws.send_str(msg.model_dump_json())  # type: ignore[union-attr]
                sent += 1
            except Exception as e:
                logger.warning(f"[CoreWsServer] Failed to flush pending message: {e}")
                self._pending.appendleft(msg)
                break
        if sent:
            logger.info(f"[CoreWsServer] Flushed {sent} pending message(s)")
        return sent

    # ── HTTP 端点 ─────────────────────────────────────────────────────────

    async def _handle_health(self, request: web.Request) -> web.Response:
        """GET /health —— 健康检查端点。"""
        return web.json_response(
            {
                "status": "ok",
                "bot_connected": self.has_connection,
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

        # 拒绝重复连接
        if self.has_connection:
            logger.warning("[CoreWsServer] Rejecting duplicate Bot connection")
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.close(code=4000, message=b"Another Bot is already connected")
            return ws

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws = ws
        logger.success("[CoreWsServer] Bot connected")

        # 连接建立后立即发送暂存的消息
        await self.flush_pending()

        # 消息接收循环
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._dispatch(msg.data)
                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"[CoreWsServer] WebSocket error: {ws.exception()}")
                    break
                elif msg.type == WSMsgType.CLOSE:
                    logger.info(f"[CoreWsServer] Bot disconnected (code={ws.close_code})")
                    break
        except Exception as e:
            logger.error(f"[CoreWsServer] Unexpected error in WS handler: {e}")
        finally:
            self._ws = None
            logger.info("[CoreWsServer] Bot connection closed")

        return ws

    # ── 消息分发 ──────────────────────────────────────────────────────────

    async def _dispatch(self, raw: str) -> None:
        """解析 JSON 并分发给注册的处理器。"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning(f"[CoreWsServer] Invalid JSON from Bot: {e}")
            await self.send_to_bot(ErrorMessage(message=f"Invalid JSON: {e}"))
            return

        msg_type = data.get("type", "")
        handler = self._handlers.get(msg_type)

        if handler is None:
            logger.warning(f"[CoreWsServer] No handler for type={msg_type!r} — ignoring")
            return

        try:
            result = await handler(data)
            if result is not None:
                await self.send_to_bot(result)
        except Exception:
            logger.exception(f"[CoreWsServer] Handler for type={msg_type!r} raised")
            await self.send_to_bot(
                ErrorMessage(
                    message=f"Internal error handling {msg_type!r}",
                    detail=str(data),
                )
            )
