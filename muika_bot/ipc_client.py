"""Bot 侧 WebSocket 客户端。

连接到 Core 进程的 WebSocket 服务端，负责：

1. 将用户消息/系统事件转发给 Core
2. 接收 Core 的 ``send_message`` 指令并通过 NoneBot 发送
3. 自动重连（exponential backoff）
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any, Callable, Coroutine, Dict, Optional, overload

import aiohttp
from aiohttp import WSMsgType

from muika.config import mas_config
from muika.ipc.attachments import AttachmentTransfer
from muika.ipc.protocol import (
    BotToCoreMessage,
    CommandEvent,
    SessionBootstrapEvent,
    SessionEndEvent,
    UserMessageEvent,
)
from muika.models import Resource
from muika.utils.logger import logger

# 重连参数
_INITIAL_RECONNECT_DELAY = 1.0
_MAX_RECONNECT_DELAY = 30.0

# 待发送事件队列上限（Core 不可用时暂存）
_MAX_PENDING_EVENTS = 100

# 消息处理器签名
MessageHandler = Callable[[Dict[str, Any]], Coroutine[None, None, None]]


class IpcClient:
    """Bot 侧的 Core IPC 客户端。

    在 ``bot_connect`` 时建立 WebSocket 连接，在整个 Bot 生命周期中
    维持连接并处理消息收发。
    """

    def __init__(
        self,
        core_url: str = mas_config.core_ws_url,
        secret: str = mas_config.ipc_secret,
        client_name: str = "nonebot-qq",
        fallback_urls: list[str] | None = None,
    ) -> None:
        self._url = core_url
        self._endpoints = [core_url, *(mas_config.core_fallback_urls if fallback_urls is None else fallback_urls)]
        self._endpoint_index = 0
        self._return_primary = False
        self._secret = secret
        self.client_name = client_name
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None

        # 消息处理器: type → handler
        self._handlers: Dict[str, MessageHandler] = {}

        # 待发送事件队列（Core 不可用时暂存）
        self._pending_events: deque[Dict[str, Any]] = deque(maxlen=_MAX_PENDING_EVENTS)

        # 连接状态
        self._connected = False
        self._running = False
        self._reconnect_count = 0

        # 连接建立事件（供 startup 等待）
        self._connected_event = asyncio.Event()

    def set_client_info(self, name: str) -> None:
        """更新适配器身份信息（在 NoneBot 适配器就绪后调用）。"""
        self.client_name = name
        logger.debug(f"[IpcClient] Client identity set: name={name!r}")

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def endpoint(self) -> str:
        """返回当前连接或正在尝试的地址。"""
        return self._endpoints[self._endpoint_index]

    def attachment_transfer(self) -> AttachmentTransfer:
        return AttachmentTransfer(self.endpoint, self._secret, mas_config.data_dir / "chat_attachments")

    async def _prefer_primary(self) -> None:
        """使用备用地址时检查主入口，恢复后重新连接主入口。"""
        while self._running and self._endpoint_index:
            await asyncio.sleep(10)
            assert self._session is not None
            base = AttachmentTransfer(self._url, self._secret, mas_config.data_dir).base
            try:
                async with self._session.get(base + "/health", timeout=aiohttp.ClientTimeout(total=3)) as response:
                    if response.status == 200 and self._ws is not None:
                        self._return_primary = True
                        await self._ws.close()
                        return
            except (aiohttp.ClientError, TimeoutError):
                continue

    async def _connect_once(self) -> None:
        """单次连接尝试。"""
        logger.debug(f"[IpcClient] Connecting to Core at {self._url}...")
        headers = {
            "X-Client-Name": self.client_name,
        }
        if self._secret:
            headers["X-Auth-Token"] = self._secret
        assert self._session is not None
        self._ws = await self._session.ws_connect(
            self.endpoint,
            headers=headers,
            heartbeat=20,
            timeout=aiohttp.ClientWSTimeout(ws_close=5),
            receive_timeout=None,
        )
        self._connected = True
        self._reconnect_count = 0
        self._connected_event.set()
        logger.success("Connected to Muika.")

        # 发送所有暂存的事件
        await self._flush_pending()
        primary = asyncio.create_task(self._prefer_primary())

        # 消息接收循环
        try:
            async for msg in self._ws:
                if msg.type == WSMsgType.TEXT:
                    await self._dispatch(msg.data)
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSED):
                    break
        finally:
            primary.cancel()
            await asyncio.gather(primary, return_exceptions=True)
            self._connected = False
            self._connected_event.clear()
            self._ws = None
            logger.warning("[IpcClient] Connection to Core lost")

    async def _dispatch(self, raw: str) -> None:
        """分发收到的消息给注册的处理器。"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"[IpcClient] Invalid JSON from Core: {raw[:100]!r}")
            return

        msg_type = data.get("type", "")
        handler = self._handlers.get(msg_type)

        if handler is None:
            logger.debug(f"[IpcClient] No handler for type={msg_type!r}")
            return

        try:
            await handler(data)
        except Exception as exc:
            logger.exception(f"[IpcClient] Handler for type={msg_type!r} raised: {exc}")

    async def _send_or_queue(self, message: BotToCoreMessage) -> bool:
        """发送或暂存消息。"""
        msg = message.model_dump(mode="json")
        if self._connected and self._ws and not self._ws.closed:
            try:
                await self._send_packet(msg)
                logger.debug(f"[IpcClient] Sent {msg}")
                return True
            except Exception as e:
                logger.warning(f"[IpcClient] Send failed: {e}")
                self._connected = False
                # 回退到暂存

        if len(self._pending_events) >= _MAX_PENDING_EVENTS:
            logger.warning(f"[IpcClient] Pending queue full — dropping event of type {message.type!r}")
            return False

        self._pending_events.append(msg)
        logger.debug(f"[IpcClient] Queued event (pending={len(self._pending_events)})")
        return True

    async def _flush_pending(self) -> int:
        """发送所有暂存的事件。"""
        sent = 0
        while self._pending_events:
            msg = self._pending_events.popleft()
            try:
                if self._ws and self.is_connected:
                    await self._send_packet(msg)
                    sent += 1
                else:
                    self._pending_events.appendleft(msg)
                    break
            except Exception as e:
                logger.warning(f"[IpcClient] Failed to flush pending event: {e}")
                self._pending_events.appendleft(msg)
                break
        if sent:
            logger.debug(f"[IpcClient] Flushed {sent} pending event(s)")
        return sent

    async def _send_packet(self, message: dict) -> None:
        """在当前连接上传附件，保留事件自身的稳定编号。"""
        assert self._ws is not None
        if message.get("resources"):
            message = {
                **message,
                "resources": [
                    await self.attachment_transfer().upload(Resource(**item)) for item in message["resources"]
                ],
            }
        await self._ws.send_json(message)

    @overload
    def on_message(self, msg_type: str, handler: None = None) -> Callable[[MessageHandler], MessageHandler]: ...

    @overload
    def on_message(
        self,
        msg_type: str,
        handler: MessageHandler,
    ) -> MessageHandler: ...

    def on_message(
        self, msg_type: str, handler: Optional[MessageHandler] = None
    ) -> Callable[[MessageHandler], MessageHandler] | MessageHandler:
        """Register a message handler for *msg_type*.

        Can be used as a direct call ``on_message(type, handler)`` or as a
        decorator ``@on_message(type)``.
        """
        if handler is not None:
            self._handlers[msg_type] = handler
            return handler

        # Decorator usage: @ipc_client.on_message("type")
        def decorator(fn: MessageHandler) -> MessageHandler:
            self._handlers[msg_type] = fn
            return fn

        return decorator

    async def wait_connected(self, timeout: float = 10.0) -> bool:
        """等待连接建立。

        Returns
        -------
        bool
            True 表示连接成功，False 表示超时
        """
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
            return self._connected
        except asyncio.TimeoutError:
            return False

    async def connect(self) -> None:
        """建立到 Core 的 WebSocket 连接并开始消息循环。"""
        self._running = True
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

        while self._running:
            try:
                await self._connect_once()
            except aiohttp.ClientError as e:
                logger.warning(f"[IpcClient] Connection failed: {e}")
            except Exception as e:
                logger.error(f"[IpcClient] Unexpected error: {e}")

            if not self._running:
                break

            # 重连
            self._reconnect_count += 1
            self._endpoint_index = 0 if self._return_primary else (self._endpoint_index + 1) % len(self._endpoints)
            self._return_primary = False
            delay = min(_INITIAL_RECONNECT_DELAY * (2 ** min(self._reconnect_count - 1, 5)), _MAX_RECONNECT_DELAY)
            logger.debug(f"[IpcClient] Reconnecting in {delay:.1f}s (attempt {self._reconnect_count})...")
            await asyncio.sleep(delay)

        # 清理
        if self._session:
            await self._session.close()
            self._session = None

    async def disconnect(self) -> None:
        """断开连接并停止重连。"""
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
            self._ws = None
        logger.info("Disconnected from Muika.")

    async def send_user_message(self, message: str, resources: Optional[list[dict]] = None) -> bool:
        """向 Core 发送用户对话消息。"""
        msg = UserMessageEvent(message=message, resources=resources or [])
        return await self._send_or_queue(msg)

    async def send_command(self, raw: str) -> bool:
        """向 Core 发送命令。"""
        msg = CommandEvent(raw=raw)
        return await self._send_or_queue(msg)

    async def send_session_bootstrap(self) -> bool:
        """通知 Core 开始新会话。"""
        msg = SessionBootstrapEvent()
        return await self._send_or_queue(msg)

    async def send_session_end(self) -> bool:
        """通知 Core 会话结束。"""
        msg = SessionEndEvent()
        return await self._send_or_queue(msg)
