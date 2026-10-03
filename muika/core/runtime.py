"""定义认知循环的持久事件边界，由部署运行时实现。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from muika.models import Resource

from .agent.task_store import TaskControl, TaskRecord
from .devices import ExecutionEnvironment
from .events import Event
from .memory_models import StateUpdate
from .state import ActiveTopicState


@dataclass
class TaskIntent:
    instruction: str
    original_request: str
    intention_id: str | None = None


@dataclass
class RuntimeControls:
    god_mode: bool = False
    god_mode_pending: bool = False
    session_end_triggered: bool = False
    timeout_set_at: datetime | None = None
    timeout_seconds: float | None = None


Generation = Callable[[], Awaitable[tuple[str, list[Resource]]]]
EventHandler = Callable[[Event, float], Awaitable[None]]


class CognitiveRuntime(Protocol):
    """保存已推导结果，并把回合效果提交给权威状态服务。"""

    async def process_event(self, event: Event, dt: float, handler: EventHandler) -> None: ...

    async def execution_environment(self, task: TaskRecord) -> ExecutionEnvironment: ...

    async def generate(self, stage: str, operation: Generation) -> tuple[str, list[Resource]]: ...

    async def commit_reply(
        self,
        content: str | None,
        resources: list[Resource],
        target: str | None,
        updates: list[StateUpdate],
        notes: list[str],
        intents: list[TaskIntent],
        *,
        controls: list[TaskControl] | None = None,
        timeout: float | None = None,
        god_mode: bool = False,
        topic: ActiveTopicState | None = None,
    ) -> list[TaskRecord]: ...
