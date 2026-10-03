"""保存推导结果和原子回合提交，不迁移正在运行的进程。"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from muika.core.agent.task_store import TaskChange, TaskRecord
from muika.core.memory_models import StateUpdate

from .clock import LocalTime, WireTimeModel
from .models import ClaimedMessage, OutgoingMessage, ResourceReference
from .task_protocol import TransferFile


class ClientRoute(BaseModel):
    client_id: str
    conversation_id: str


class ActiveTopicSnapshot(BaseModel):
    topic_id: str
    topic_seed: str
    topic_type: str
    started_at: LocalTime
    user_engaged: bool


class RuntimeSnapshot(BaseModel):
    attention: float = 1.0
    loneliness: float = 0.0
    curiosity: float = 0.5
    boredom: float = 0.0
    last_interaction: LocalTime = Field(default_factory=datetime.now)
    last_proactive_at: LocalTime | None = None
    active_topic: ActiveTopicSnapshot | None = None
    route: ClientRoute | None = None
    selected_executor: str | None = None
    bootstrapped: bool = False
    handoff_target: str | None = None
    handoff_id: str | None = None
    god_mode: bool = False
    god_mode_pending: bool = False
    session_end_triggered: bool = False
    timeout_set_at: LocalTime | None = None
    timeout_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class HandoffRecord(BaseModel):
    id: str
    source: str
    target: str
    route: ClientRoute | None = None
    expires_at: float
    granted_epoch: int | None = None


class GeneratedReply(BaseModel):
    text: str
    resources: list[ResourceReference] = Field(default_factory=list)


class TurnRecord(BaseModel):
    turn_id: str
    generations: dict[str, GeneratedReply] = Field(default_factory=dict)
    completed: bool = False


class TurnAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: str = Field(min_length=1, max_length=256)
    claim: ClaimedMessage | None = None


class LoadTurn(TurnAction):
    action: Literal["load_turn"] = "load_turn"


class SaveGeneration(TurnAction):
    action: Literal["save_generation"] = "save_generation"
    stage: str = Field(min_length=1, max_length=128)
    generated: GeneratedReply


class CompleteTurn(TurnAction):
    action: Literal["complete_turn"] = "complete_turn"
    content: str | None = None
    resources: list[ResourceReference] = Field(default_factory=list)
    state_updates: list[WireTimeModel[StateUpdate]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    replies: list[OutgoingMessage] = Field(default_factory=list)
    tasks: list[TaskRecord] = Field(default_factory=list)
    task_changes: list[TaskChange] = Field(default_factory=list)
    task_files: list[TransferFile] = Field(default_factory=list)
    runtime: RuntimeSnapshot | None = None


TurnOperation = Annotated[LoadTurn | SaveGeneration | CompleteTurn, Field(discriminator="action")]
