"""Bot 侧 WebSocket 客户端。

连接到 Core 进程的 WebSocket 服务端，负责：

1. 将用户消息/系统事件转发给 Core
2. 接收 Core 的 ``send_message`` 指令并通过 NoneBot 发送
3. 缓存 Core 推送的 ``state_update`` 供调试命令读取
4. 自动重连（exponential backoff）
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any, Callable, Coroutine, Dict, Optional, overload

import aiohttp
from aiohttp import WSMsgType

from muika.utils.logger import logger

# 重连参数
_INITIAL_RECONNECT_DELAY = 1.0
_MAX_RECONNECT_DELAY = 30.0
_MAX_RECONNECT_ATTEMPTS = 5

# 待发送事件队列上限（Core 不可用时暂存）
_MAX_PENDING_EVENTS = 100

# 消息处理器签名
MessageHandler = Callable[[Dict[str, Any]], Coroutine[None, None, None]]


class IpcClient:
    """Bot 侧的 Core IPC 客户端。

    在 ``bot_connect`` 时建立 WebSocket 连接，在整个 Bot 生命周期中
    维持连接并处理消息收发。

    Parameters
    ----------
    core_url: Core WebSocket 地址，如 ``"ws://127.0.0.1:8765/ws"``
    """

    def __init__(self, core_url: str = "ws://127.0.0.1:8765/ws") -> None:
        self._url = core_url
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None

        # 消息处理器: type → handler
        self._handlers: Dict[str, MessageHandler] = {}

        # 最近一次收到的 state 快照
        self._cached_state: Dict[str, Any] = {}

        # 待发送事件队列（Core 不可用时暂存）
        self._pending_events: deque[Dict[str, Any]] = deque(maxlen=_MAX_PENDING_EVENTS)

        # 连接状态
        self._connected = False
        self._running = False
        self._reconnect_count = 0

        # 连接建立事件（供 startup 等待）
        self._connected_event = asyncio.Event()

    # ── 公共 API ──────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def cached_state(self) -> Dict[str, Any]:
        """最近一次从 Core 收到的 MuikaState 快照。"""
        return self._cached_state

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
        self._session = aiohttp.ClientSession()

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
            if self._reconnect_count > _MAX_RECONNECT_ATTEMPTS:
                logger.error(f"[IpcClient] Max reconnect attempts ({_MAX_RECONNECT_ATTEMPTS}) reached — giving up")
                break

            delay = min(_INITIAL_RECONNECT_DELAY * (2 ** (self._reconnect_count - 1)), _MAX_RECONNECT_DELAY)
            logger.info(f"[IpcClient] Reconnecting in {delay:.1f}s (attempt {self._reconnect_count})...")
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
        logger.info("[IpcClient] Disconnected")

    async def send_event(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> bool:
        """向 Core 发送一个事件。

        Parameters
        ----------
        event_type: 事件类型 (``"user_message"``, ``"session_bootstrap"``, ...)
        payload: 事件负载

        Returns
        -------
        bool
            True 表示已发送或已暂存
        """
        msg = {
            "type": "event",
            "event": {
                "event_type": event_type,
                "payload": payload or {},
            },
        }
        return await self._send_or_queue(msg)

    async def send_query(self, query_type: str) -> bool:
        """向 Core 发送查询。"""
        msg = {"type": "query", "query": query_type}
        return await self._send_or_queue(msg)

    async def send_debug(self, action: str, field: Optional[str] = None, value: Optional[Any] = None) -> bool:
        """向 Core 发送调试命令。"""
        msg: Dict[str, Any] = {"type": "debug", "action": action}
        if field is not None:
            msg["field"] = field
        if value is not None:
            msg["value"] = value
        return await self._send_or_queue(msg)

    async def send_config_changed(self, config_name: Optional[str] = None) -> bool:
        """通知 Core 模型配置已变更。"""
        msg: Dict[str, Any] = {"type": "config_changed"}
        if config_name:
            msg["config_name"] = config_name
        return await self._send_or_queue(msg)

    # ── 内部方法 ──────────────────────────────────────────────────────────

    async def _connect_once(self) -> None:
        """单次连接尝试。"""
        logger.info(f"[IpcClient] Connecting to Core at {self._url}...")
        self._ws = await self._session.ws_connect(self._url)  # type: ignore[union-attr]
        self._connected = True
        self._reconnect_count = 0
        self._connected_event.set()
        logger.success("[IpcClient] Connected to Core")

        # 发送所有暂存的事件
        await self._flush_pending()

        # 消息接收循环
        try:
            async for msg in self._ws:
                if msg.type == WSMsgType.TEXT:
                    await self._dispatch(msg.data)
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSED):
                    break
        finally:
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
        except Exception:
            logger.exception(f"[IpcClient] Handler for type={msg_type!r} raised")

    async def _send_or_queue(self, msg: Dict[str, Any]) -> bool:
        """发送或暂存消息。"""
        if self._connected and self._ws and not self._ws.closed:
            try:
                await self._ws.send_json(msg)
                return True
            except Exception as e:
                logger.warning(f"[IpcClient] Send failed: {e}")
                self._connected = False
                # 回退到暂存

        if len(self._pending_events) >= _MAX_PENDING_EVENTS:
            logger.warning(f"[IpcClient] Pending queue full — dropping event of type {msg.get('type')!r}")
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
                if self._ws and not self._ws.closed:
                    await self._ws.send_json(msg)
                    sent += 1
                else:
                    self._pending_events.appendleft(msg)
                    break
            except Exception as e:
                logger.warning(f"[IpcClient] Failed to flush pending event: {e}")
                self._pending_events.appendleft(msg)
                break
        if sent:
            logger.info(f"[IpcClient] Flushed {sent} pending event(s)")
        return sent
