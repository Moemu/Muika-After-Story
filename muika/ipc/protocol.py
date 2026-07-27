"""
IPC 消息协议定义 —— Bot ↔ Core 之间通过 WebSocket 传输的 JSON 消息模型。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field

# ═══════════════════════════════════════════════════════════════════════════
# 基础信封
# ═══════════════════════════════════════════════════════════════════════════


class IPCMessage(BaseModel):
    """所有 IPC 消息的通用信封。"""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    """消息唯一 ID"""

    type: Any
    """消息类型鉴别器"""

    ts: str = Field(default_factory=lambda: datetime.now().isoformat())
    """消息时间戳 (ISO 8601)"""


# ═══════════════════════════════════════════════════════════════════════════
# Bot → Core 消息
# ═══════════════════════════════════════════════════════════════════════════


class UserMessageEvent(IPCMessage):
    """用户发送了一条对话消息。"""

    type: Literal["user_message"] = "user_message"
    message: str = ""
    """消息文本"""
    resources: List[Dict[str, Any]] = Field(default_factory=list)
    """多模态资源（Resource.to_dict() 格式）"""


class CommandEvent(IPCMessage):
    """用户发送了一条命令。"""

    type: Literal["command"] = "command"
    raw: str
    """原始命令文本（含前缀，如 ``".debug state"``）"""


class SessionBootstrapEvent(IPCMessage):
    """Bot 已连接，请求开始新会话。"""

    type: Literal["session_bootstrap"] = "session_bootstrap"


class SessionEndEvent(IPCMessage):
    """用户主动请求当前会话结束"""

    type: Literal["session_end"] = "session_end"


BotToCoreMessage = Annotated[
    Union[UserMessageEvent, CommandEvent, SessionBootstrapEvent, SessionEndEvent],
    Field(discriminator="type"),
]

# ═══════════════════════════════════════════════════════════════════════════
# Core → Bot 消息
# ═══════════════════════════════════════════════════════════════════════════


class SendMessage(IPCMessage):
    """Core 要求 Bot 发送一条 LLM 对话消息。"""

    type: Literal["send_message"] = "send_message"
    content: str = ""
    """文本内容"""
    resources: List[Dict[str, Any]] = Field(default_factory=list)
    """多模态资源（Resource.to_dict() 格式）"""


class CommandResult(IPCMessage):
    """Core 返回一条命令执行结果。"""

    type: Literal["command_result"] = "command_result"
    content: str = ""
    """文本结果"""
    resources: List[Dict[str, Any]] = Field(default_factory=list)
    """多模态资源（Resource.to_dict() 格式）"""


class ActionResponse(IPCMessage):
    """Core 对 Bot 事件的确认响应。"""

    type: Literal["action_response"] = "action_response"
    action: str
    status: str


class ErrorMessage(IPCMessage):
    """Core 报告一个错误。"""

    type: Literal["error"] = "error"
    message: str
    """错误描述"""
    detail: Optional[str] = None
    """可选的详细错误信息"""


CoreToBotMessage = Annotated[
    Union[SendMessage, CommandResult, ActionResponse, ErrorMessage],
    Field(discriminator="type"),
]
