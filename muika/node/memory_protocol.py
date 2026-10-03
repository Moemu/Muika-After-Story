"""定义记忆业务接口及其跨节点工作视图。"""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from muika.core.memory_models import (
    Diary,
    DreamResult,
    Experience,
    Fact,
    MemoryCategory,
    MemoryQuery,
    MemorySnapshot,
    RecallHit,
    StateUpdate,
)

from .clock import LocalTime, WireTimeModel
from .models import ResourceReference


class MemoryTurn(BaseModel):
    role: Literal["user", "muika", "agent"]
    content: str
    timestamp: LocalTime
    resources: list[ResourceReference] = Field(default_factory=list)
    id: int


class MemoryView(BaseModel):
    snapshot: WireTimeModel[MemorySnapshot]
    facts: list[WireTimeModel[Fact]]
    turns: list[MemoryTurn]


class MemoryAction(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoadMemory(MemoryAction):
    action: Literal["load_memory"] = "load_memory"


class AddMaterial(MemoryAction):
    action: Literal["add_material"] = "add_material"
    kind: Literal["user", "muika", "agent", "note", "state", "legacy"]
    content: str
    timestamp: LocalTime | None = None
    resources: list[ResourceReference] = Field(default_factory=list)
    source: str | None = None


class NewSession(MemoryAction):
    action: Literal["new_session"] = "new_session"


class UpdateState(MemoryAction):
    action: Literal["update_state"] = "update_state"
    update: WireTimeModel[StateUpdate]


class MarkConsidered(MemoryAction):
    action: Literal["mark_considered"] = "mark_considered"


class LinkIntention(MemoryAction):
    action: Literal["link_intention"] = "link_intention"
    intention_id: str
    task_id: str


class RecordTaskResult(MemoryAction):
    action: Literal["record_task_result"] = "record_task_result"
    task_id: str
    status: str


class ForgetMemory(MemoryAction):
    action: Literal["forget_memory"] = "forget_memory"
    category: MemoryCategory
    key: str


class PendingDays(MemoryAction):
    action: Literal["pending_days"] = "pending_days"
    now: LocalTime
    include_today: bool = False


class DayMaterial(MemoryAction):
    action: Literal["day_material"] = "day_material"
    day: date


class RecentDiaries(MemoryAction):
    action: Literal["recent_diaries"] = "recent_diaries"
    before: date
    limit: int = Field(default=5, ge=1, le=100)


class SaveDream(MemoryAction):
    action: Literal["save_dream"] = "save_dream"
    day: date
    result: DreamResult
    through: int
    allowed_refs: set[str]


class SearchMemory(MemoryAction):
    action: Literal["search_memory"] = "search_memory"
    query: MemoryQuery
    limit: int = Field(default=30, ge=1, le=100)


class ReadSource(MemoryAction):
    action: Literal["read_source"] = "read_source"
    ref: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=6000, ge=1, le=100000)


class SaveWorkingContext(MemoryAction):
    action: Literal["save_working_context"] = "save_working_context"
    session_id: str
    summary: str
    through: int


MemoryOperation = Annotated[
    LoadMemory
    | AddMaterial
    | NewSession
    | UpdateState
    | MarkConsidered
    | LinkIntention
    | RecordTaskResult
    | ForgetMemory
    | PendingDays
    | DayMaterial
    | RecentDiaries
    | SaveDream
    | SearchMemory
    | ReadSource
    | SaveWorkingContext,
    Field(discriminator="action"),
]


class MemoryResult(BaseModel):
    view: MemoryView
    material_id: int | None = None
    saved: bool | None = None
    days: list[date] = Field(default_factory=list)
    materials: list[WireTimeModel[Experience]] = Field(default_factory=list)
    diaries: list[WireTimeModel[Diary]] = Field(default_factory=list)
    hits: list[WireTimeModel[RecallHit]] = Field(default_factory=list)
    text: str = ""
