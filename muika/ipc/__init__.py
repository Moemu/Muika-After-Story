"""IPC (Inter-Process Communication) 模块 —— Bot ↔ Core 之间的 WebSocket 通信。

提供：
- :mod:`muika.ipc.protocol` —— 消息类型定义（Pydantic discriminated union）
- :mod:`muika.ipc.server` —— Core 侧 WebSocket 服务端
"""

from .protocol import (
    ActionResponse,
    BotToCoreMessage,
    CommandEvent,
    CommandResult,
    CoreToBotMessage,
    ErrorMessage,
    IPCMessage,
    SendMessage,
    SessionBootstrapEvent,
    SessionEndEvent,
    UserMessageEvent,
)
from .server import DEFAULT_HOST, DEFAULT_PORT, CoreWsServer

__all__ = [
    "IPCMessage",
    "UserMessageEvent",
    "CommandEvent",
    "SessionBootstrapEvent",
    "SessionEndEvent",
    "BotToCoreMessage",
    "SendMessage",
    "CommandResult",
    "ActionResponse",
    "ErrorMessage",
    "CoreToBotMessage",
    "CoreWsServer",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
]
