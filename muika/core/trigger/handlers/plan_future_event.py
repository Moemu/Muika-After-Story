from __future__ import annotations

from typing import TYPE_CHECKING

from ..intents import PlanFutureEventIntent
from ..registry import register_intent

if TYPE_CHECKING:
    from ...executor import Executor


@register_intent("plan_future_event")
async def handle_plan_future_event(intent: PlanFutureEventIntent, executor: "Executor") -> str:
    await executor.scheduler.schedule(intent)
    return "Future event planned."
