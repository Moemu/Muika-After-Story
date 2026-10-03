"""描述有设备归属和重试契约的工具动作。"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from muika.llm._schema import ToolCall, ToolResult

from .models import ResourceReference
from .task_protocol import TransferFile

RUNTIME_ABI = 3
DATA_REVISION = "8b78e38ffdfb"


class ToolCapability(BaseModel):
    name: str
    scope: Literal["core", "device"] = "device"
    retry: Literal["read_only", "idempotent", "verify"] = "verify"
    tool_schema: dict[str, JsonValue]


class ExecutionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=256)
    node_id: str
    source_id: str
    call: ToolCall
    task_id: str | None = None
    file_versions: dict[str, str] = Field(default_factory=dict)
    review_context: str = ""
    inputs: list[ResourceReference] = Field(default_factory=list)


class ExecutionRecord(BaseModel):
    spec: ExecutionSpec
    epoch: int
    status: Literal["pending", "running", "completed", "unknown", "reconciled"]
    result: ToolResult | None = None
    files: list[TransferFile] = Field(default_factory=list)
    file_versions: dict[str, str] = Field(default_factory=dict)
    completed_at: datetime | None = None


class ExecutionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubmitExecution(ExecutionAction):
    action: Literal["submit_execution"] = "submit_execution"
    spec: ExecutionSpec


class InspectExecution(ExecutionAction):
    action: Literal["inspect_execution"] = "inspect_execution"
    id: str


ExecutionOperation = Annotated[SubmitExecution | InspectExecution, Field(discriminator="action")]
