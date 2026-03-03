"""
Action Intents — scheduled/longer-lifecycle actions.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Literal, Optional

from pydantic import Field

from .schema import ActionMode, ActionOutput, BaseAction

if TYPE_CHECKING:
    from muika.core.executor import Executor
    from muika.core.state import MuikaState


class Persistence(str, Enum):
    ONCE = "once"
    REPEAT = "repeat"


class BaseIntent(BaseAction):
    """Base class for scheduled actions."""

    mode: Literal[ActionMode.SCHEDULED] = ActionMode.SCHEDULED

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        raise NotImplementedError(f"{type(self).__name__} must implement handle()")


class PlanFutureEventIntent(BaseIntent):
    name: Literal["plan_future_event"] = "plan_future_event"
    event: str = Field(..., description="Description of the future event or action to perform.")
    trigger_in_seconds: Optional[float] = Field(
        None,
        description="Seconds from now to trigger the event. Mutually exclusive with trigger_at.",
    )
    trigger_at: Optional[str] = Field(
        None,
        description="ISO 8601 datetime string for when to trigger. Mutually exclusive with trigger_in_seconds.",
    )
    persistence: Persistence = Field(
        Persistence.ONCE,
        description="Whether the event should trigger once or repeat.",
    )
    repeat_interval_seconds: Optional[float] = Field(
        None,
        description="If persistence is 'repeat', how many seconds between each trigger.",
    )

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        if executor is None:
            return ActionOutput(content="Executor is required for scheduled actions.")
        await executor.scheduler.schedule(self)
        return ActionOutput(content="Future event planned successfully.")
