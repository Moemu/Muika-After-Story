"""定义固定入口的版本化业务协议，不传输 SQL 或运行时对象。"""

from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from muika.core.devices import ExecutionEnvironment
from muika.database.activity import ActivityOperation, ActivityResult
from muika.node.bundle import CognitiveBundle
from muika.node.execution_protocol import (
    DATA_REVISION,
    RUNTIME_ABI,
    ExecutionOperation,
    ExecutionRecord,
    ToolCapability,
)
from muika.node.memory_protocol import MemoryOperation, MemoryResult
from muika.node.models import (
    ClaimedMessage,
    CoreLease,
    IncomingMessage,
    OutgoingMessage,
)
from muika.node.schedule_protocol import ScheduleOperation, ScheduleRecord
from muika.node.task_protocol import TaskOperation, TaskResult
from muika.node.turn_protocol import RuntimeSnapshot, TurnOperation, TurnRecord

PROTOCOL_VERSION = 2
HEARTBEAT_INTERVAL_SECONDS = 20
CANDIDATE_TIMEOUT_SECONDS = 3 * HEARTBEAT_INTERVAL_SECONDS


class Request(BaseModel):
    """为单次传输关联响应；业务去重使用输入或调用自己的身份。"""

    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(default_factory=lambda: uuid4().hex, max_length=128)


class Acquire(Request):
    operation: Literal["acquire"] = "acquire"
    duration: float = Field(default=15, gt=0, le=300, allow_inf_nan=False)


class Renew(Request):
    operation: Literal["renew"] = "renew"
    epoch: int
    duration: float = Field(default=15, gt=0, le=300, allow_inf_nan=False)


class Release(Request):
    operation: Literal["release"] = "release"
    epoch: int


class Handoff(Request):
    operation: Literal["handoff"] = "handoff"
    epoch: int
    target: str


class Claim(Request):
    operation: Literal["claim"] = "claim"
    epoch: int


class CoreReady(Request):
    operation: Literal["core_ready"] = "core_ready"
    epoch: int


class Commit(Request):
    operation: Literal["commit"] = "commit"
    epoch: int
    claim: ClaimedMessage
    replies: list[OutgoingMessage]


class Receive(Request):
    operation: Literal["receive"] = "receive"
    message: IncomingMessage


class Pending(Request):
    operation: Literal["pending"] = "pending"
    limit: int = Field(default=100, ge=1, le=1000)


class Acknowledge(Request):
    operation: Literal["acknowledge"] = "acknowledge"
    message_id: str


class MemoryRequest(Request):
    operation: Literal["memory"] = "memory"
    epoch: int
    body: MemoryOperation


class TurnRequest(Request):
    operation: Literal["turn"] = "turn"
    epoch: int
    body: TurnOperation


class TaskRequest(Request):
    operation: Literal["task"] = "task"
    epoch: int
    body: TaskOperation


class LoadRuntime(Request):
    operation: Literal["load_runtime"] = "load_runtime"
    epoch: int


class SaveRuntime(Request):
    operation: Literal["save_runtime"] = "save_runtime"
    epoch: int
    runtime: RuntimeSnapshot


class PublishEvent(Request):
    operation: Literal["publish_event"] = "publish_event"
    epoch: int
    message: IncomingMessage


class Emit(Request):
    operation: Literal["emit"] = "emit"
    epoch: int
    message: OutgoingMessage


class Status(Request):
    operation: Literal["status"] = "status"


class NodeStatus(BaseModel):
    id: str
    role: Literal["bot", "core", "executor"]
    connected: bool
    compatible: bool = True
    tools: list[str] = Field(default_factory=list)
    capabilities: list[ToolCapability] = Field(default_factory=list)
    environment: ExecutionEnvironment | None = None


class RegisterNode(Request):
    operation: Literal["register_node"] = "register_node"
    runtime_abi: int = RUNTIME_ABI
    data_revision: str = DATA_REVISION
    tools: list[ToolCapability] = Field(default_factory=list)
    environment: ExecutionEnvironment | None = None


class ExecutionRequest(Request):
    operation: Literal["execution"] = "execution"
    epoch: int
    body: ExecutionOperation


class PollExecutions(Request):
    operation: Literal["poll_executions"] = "poll_executions"


class StartExecution(Request):
    operation: Literal["start_execution"] = "start_execution"
    id: str
    epoch: int


class ValidateExecution(Request):
    operation: Literal["validate_execution"] = "validate_execution"
    id: str
    epoch: int


class ReportExecution(Request):
    operation: Literal["report_execution"] = "report_execution"
    record: ExecutionRecord


class ScheduleRequest(Request):
    operation: Literal["schedule"] = "schedule"
    epoch: int
    body: ScheduleOperation


class ActivityRequest(Request):
    operation: Literal["activity"] = "activity"
    epoch: int
    body: ActivityOperation


class LoadBundle(Request):
    operation: Literal["load_bundle"] = "load_bundle"


class SaveBundle(Request):
    operation: Literal["save_bundle"] = "save_bundle"
    epoch: int
    expected_digest: str
    bundle: CognitiveBundle


NodeRequest = Annotated[
    Acquire
    | Renew
    | Release
    | Handoff
    | CoreReady
    | Claim
    | Commit
    | Receive
    | Pending
    | Acknowledge
    | MemoryRequest
    | TurnRequest
    | TaskRequest
    | LoadRuntime
    | SaveRuntime
    | PublishEvent
    | Emit
    | Status
    | RegisterNode
    | ExecutionRequest
    | PollExecutions
    | StartExecution
    | ValidateExecution
    | ReportExecution
    | ScheduleRequest
    | ActivityRequest
    | LoadBundle
    | SaveBundle,
    Field(discriminator="operation"),
]
REQUEST_ADAPTER: TypeAdapter[NodeRequest] = TypeAdapter(NodeRequest)


class NodeResponse(BaseModel):
    """返回对应操作的类型化结果或明确错误。"""

    model_config = ConfigDict(extra="forbid")
    request_id: str
    error: str | None = None
    error_code: Literal["checkpoint_unavailable"] | None = None
    handoff_accepted: bool | None = None
    sequence: int | None = None
    lease: CoreLease | None = None
    claim: ClaimedMessage | None = None
    replies: list[OutgoingMessage] = Field(default_factory=list)
    memory: MemoryResult | None = None
    turn: TurnRecord | None = None
    task: TaskResult | None = None
    runtime: RuntimeSnapshot | None = None
    nodes: list[NodeStatus] = Field(default_factory=list)
    execution: ExecutionRecord | None = None
    executions: list[ExecutionRecord] = Field(default_factory=list)
    schedules: list[ScheduleRecord] = Field(default_factory=list)
    activity: ActivityResult | None = None
    bundle: CognitiveBundle | None = None
