"""定义节点间传输的业务记录。"""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .event_protocol import RuntimeEvent


class ResourceReference(BaseModel):
    """描述可以通过 IPC 获取的不可变资源。"""

    model_config = ConfigDict(extra="forbid")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0, le=64 * 1024 * 1024)
    media_type: str
    name: str = Field(default="resource", max_length=256)
    kind: Literal["audio", "image", "video", "file"] = "file"


class IncomingMessage(BaseModel):
    """保存客户端已经固定身份和会话的输入。"""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=256)
    client_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=256)
    kind: Literal["user_message", "command", "session_bootstrap", "session_end", "runtime_event"] = "user_message"
    text: str = ""
    resources: list[ResourceReference] = Field(default_factory=list)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    event: RuntimeEvent | None = None
    member_ids: list[str] = Field(default_factory=list)


class OutgoingMessage(BaseModel):
    """保存目标明确、可以重投的回复。"""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=256)
    client_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=256)
    kind: Literal["send_message", "command_result", "status"] = "send_message"
    text: str
    resources: list[ResourceReference] = Field(default_factory=list)


class CoreLease(BaseModel):
    """返回服务端授予的控制权及剩余期限。"""

    cluster_id: str
    owner: str
    epoch: int
    remaining_seconds: float


class ClaimedMessage(BaseModel):
    """关联持久输入和本次处理权。"""

    sequence: int
    message: IncomingMessage
    owner: str
    epoch: int
