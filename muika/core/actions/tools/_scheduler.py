"""供 Muika 创建定时提醒的工具。"""

from pydantic import BaseModel, Field

from muika.plugin.func_call import on_function_call
from muika.plugin.func_call._context import get_executor


class PlanFutureEventParams(BaseModel):
    event: str = Field(..., description="Reminder or event for Muika to act on.")
    trigger_in_seconds: float | None = Field(None, description="Non-negative delay; excludes trigger_at.")
    trigger_at: str | None = Field(None, description="ISO datetime; local time if no timezone. Excludes delay.")
    repeat_interval_seconds: float | None = Field(
        None, description="Positive repeat interval; omit for one occurrence."
    )


@on_function_call(
    "Schedule a future event for Muika. Reminders exist only in memory and are lost when Core restarts.",
    params=PlanFutureEventParams,
)
async def plan_future_event(
    event: str,
    trigger_in_seconds: float | None = None,
    trigger_at: str | None = None,
    repeat_interval_seconds: float | None = None,
) -> str:
    """通过当前执行器创建提醒，只在成功调度后报告完成。"""
    executor = get_executor()
    if executor is None:
        return "Cannot schedule an event without an active executor."
    try:
        await executor.scheduler.schedule(
            event,
            trigger_in_seconds=trigger_in_seconds,
            trigger_at=trigger_at,
            repeat_interval_seconds=repeat_interval_seconds,
        )
    except (ValueError, RuntimeError, OverflowError, OSError) as exc:
        return f"Cannot schedule event: {exc}"
    return "Future event scheduled. This reminder will be lost if Core restarts."
