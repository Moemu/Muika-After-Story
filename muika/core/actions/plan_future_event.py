from __future__ import annotations

from typing import TYPE_CHECKING

from ..intents import PlanFutureEventIntent
from ._registry import register_action

if TYPE_CHECKING:
    from ..executor import Executor


@register_action("plan_future_event")
async def handle_plan_future_event(intent: PlanFutureEventIntent, executor: "Executor") -> str:
    await executor.scheduler.schedule(intent)
    return "Future event planned."
