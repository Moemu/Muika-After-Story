"""IPC 消息协议定义 —— Bot ↔ Core 之间通过 WebSocket 传输的 JSON 消息模型。

所有消息继承自 ``IPCMessage`` / ``IPCResponse``，使用 Pydantic discriminated union
进行序列化和反序列化，与项目中 Butler Agent 的 Action 联合类型风格一致。
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
    """消息唯一 ID，用于请求-响应匹配"""

    type: Any
    """消息类型鉴别器"""

    ts: str = Field(default_factory=lambda: datetime.now().isoformat())
    """消息时间戳 (ISO 8601)"""


# ═══════════════════════════════════════════════════════════════════════════
# Bot → Core 消息
# ═══════════════════════════════════════════════════════════════════════════


class EventPayload(BaseModel):
    """事件负载——由 Bot 转发给 Core 的外部事件。"""

    event_type: str
    """事件类型: user_message | session_bootstrap | session_end | scheduled_trigger"""

    payload: Dict[str, Any] = Field(default_factory=dict)
    """事件的具体数据，结构与 muika.core.events 中的 Event dataclass 对应"""


class EventMessage(IPCMessage):
    """Bot 转发一个用户/系统事件到 Core。"""

    type: Literal["event"] = "event"
    event: EventPayload


class QueryMessage(IPCMessage):
    """Bot 向 Core 查询状态（调试命令）。"""

    type: Literal["query"] = "query"
    query: Literal["state"] = "state"


class DebugMessage(IPCMessage):
    """Bot 请求 Core 执行调试操作。"""

    type: Literal["debug"] = "debug"
    action: Literal["trigger_topic", "set_state", "reset_topic"]
    field: Optional[str] = None
    """set_state 时的字段名"""
    value: Optional[Any] = None
    """set_state 时的字段值"""


class ConfigChangedMessage(IPCMessage):
    """Bot 通知 Core 模型配置已变更。"""

    type: Literal["config_changed"] = "config_changed"
    config_name: Optional[str] = None
    """变更的配置名，为空表示默认配置"""


# Bot → Core 联合类型
BotToCoreMessage = Annotated[
    Union[EventMessage, QueryMessage, DebugMessage, ConfigChangedMessage],
    Field(discriminator="type"),
]

# ═══════════════════════════════════════════════════════════════════════════
# Core → Bot 消息
# ═══════════════════════════════════════════════════════════════════════════


class SendMessage(IPCMessage):
    """Core 要求 Bot 向用户发送一条消息。"""

    type: Literal["send_message"] = "send_message"
    content: str
    """要发送的文本内容"""
    resources: List[Dict[str, Any]] = Field(default_factory=list)
    """附属的多模态资源（Resource.to_dict() 格式）"""


class StateUpdate(IPCMessage):
    """Core 推送 MuikaState 的快照给 Bot（供调试命令读取）。"""

    type: Literal["state_update"] = "state_update"
    state: Dict[str, Any]
    """MuikaState 的序列化字典"""


class QueryResponse(IPCMessage):
    """Core 对 Bot 查询的响应。"""

    type: Literal["query_response"] = "query_response"
    query: str
    """对应的查询类型"""
    data: Dict[str, Any]
    """查询结果数据"""


class ActionResponse(IPCMessage):

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


# Core → Bot 联合类型
CoreToBotMessage = Annotated[
    Union[ActionResponse, SendMessage, StateUpdate, QueryResponse, ErrorMessage],
    Field(discriminator="type"),
]
