"""IPC (Inter-Process Communication) 模块 —— Bot ↔ Core 之间的 WebSocket 通信。

提供：
- :mod:`muika.ipc.protocol` —— 消息类型定义（Pydantic 模型）
- :mod:`muika.ipc.server` —— Core 侧 WebSocket 服务端
- :mod:`muika.ipc.client` —— Bot 侧 WebSocket 客户端
- :mod:`muika.ipc.bridge` —— Core 侧 IpcBridge
"""

from .protocol import (
    BotToCoreMessage,
    ConfigChangedMessage,
    CoreToBotMessage,
    DebugMessage,
    ErrorMessage,
    EventMessage,
    EventPayload,
    IPCMessage,
    QueryMessage,
    QueryResponse,
    SendMessage,
    StateUpdate,
)
from .server import DEFAULT_HOST, DEFAULT_PORT, CoreWsServer

__all__ = [
    "IPCMessage",
    "EventMessage",
    "EventPayload",
    "QueryMessage",
    "DebugMessage",
    "ConfigChangedMessage",
    "BotToCoreMessage",
    "SendMessage",
    "StateUpdate",
    "QueryResponse",
    "ErrorMessage",
    "CoreToBotMessage",
    "CoreWsServer",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
]
