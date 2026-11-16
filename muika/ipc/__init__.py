"""IPC (Inter-Process Communication) 模块 —— Bot ↔ Core 之间的 WebSocket 通信。

提供：
- :mod:`muika.ipc.protocol` —— 消息类型定义（Pydantic discriminated union）
- :mod:`muika.ipc.server` —— Core 侧 WebSocket 服务端
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from .server import DEFAULT_HOST, DEFAULT_PORT, CoreWsServer


def __getattr__(name: str) -> type[CoreWsServer] | str | int:
    """访问服务端导出时才加载 Core 依赖，Bot 协议导入不读取 Core 配置。"""
    if name not in {"CoreWsServer", "DEFAULT_HOST", "DEFAULT_PORT"}:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from .server import DEFAULT_HOST, DEFAULT_PORT, CoreWsServer

    exports: dict[str, type[CoreWsServer] | str | int] = {
        "CoreWsServer": CoreWsServer,
        "DEFAULT_HOST": DEFAULT_HOST,
        "DEFAULT_PORT": DEFAULT_PORT,
    }
    return exports[name]


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
