"""传输任务检查点及其必要文件引用。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from muika.core.agent.task_store import CallRecord, TaskRecord

from .models import ResourceReference


class TransferFile(BaseModel):
    key: str
    reference: ResourceReference


class TaskAction(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoadTasks(TaskAction):
    action: Literal["load_tasks"] = "load_tasks"


class LoadCalls(TaskAction):
    action: Literal["load_calls"] = "load_calls"
    task_id: str


class SaveTask(TaskAction):
    action: Literal["save_task"] = "save_task"
    task: TaskRecord
    call: CallRecord | None = None
    files: list[TransferFile] = Field(default_factory=list)


TaskOperation = Annotated[LoadTasks | LoadCalls | SaveTask, Field(discriminator="action")]


class TaskResult(BaseModel):
    tasks: list[TaskRecord] = Field(default_factory=list)
    calls: list[CallRecord] = Field(default_factory=list)
    files: list[TransferFile] = Field(default_factory=list)
