"""定义可以跨 Core 接管恢复的提醒。"""

from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .turn_protocol import ClientRoute


class ScheduleRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: uuid4().hex)
    event: str = Field(min_length=1)
    when: str
    due_at: float = Field(allow_inf_nan=False)
    repeat_interval: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    route: ClientRoute


class CreateSchedule(BaseModel):
    action: Literal["create_schedule"] = "create_schedule"
    schedule: ScheduleRecord


class CancelSchedule(BaseModel):
    action: Literal["cancel_schedule"] = "cancel_schedule"
    id: str


class ListSchedules(BaseModel):
    action: Literal["list_schedules"] = "list_schedules"


ScheduleOperation = Annotated[CreateSchedule | CancelSchedule | ListSchedules, Field(discriminator="action")]
