import asyncio
from datetime import datetime
from typing import Optional

from muika.utils.logger import logger

from .actions.intents import Persistence, PlanFutureEventIntent
from .events import ScheduledTriggerEvent, ScheduledTriggerPayload


class Scheduler:
    def __init__(self, event_queue: asyncio.Queue):
        self.event_queue = event_queue
        # self._tasks = []

    def parse_time(self, natural_time: str) -> Optional[datetime]:
        import dateparser

        # settings={'PREFER_DATES_FROM': 'future'} 确保 '8am' 是明天的如果今天已经过了
        return dateparser.parse(natural_time, settings={"PREFER_DATES_FROM": "future"})

    async def schedule(self, intent: PlanFutureEventIntent):  # pragma: no cover
        if intent.trigger_at and intent.trigger_in_seconds is not None:
            logger.error("trigger_at 与 trigger_in_seconds 不能同时设置")
            return

        if intent.trigger_at:
            target_time = self.parse_time(intent.trigger_at)
            if not target_time:
                logger.error(f"无法解析时间: {intent.trigger_at}")
                return
            delay_seconds = (target_time - datetime.now()).total_seconds()
            when_text = intent.trigger_at
        elif intent.trigger_in_seconds is not None:
            delay_seconds = float(intent.trigger_in_seconds)
            when_text = f"in {delay_seconds:.0f} seconds"
        else:
            logger.error("必须提供 trigger_at 或 trigger_in_seconds")
            return

        if delay_seconds < 0:
            logger.warning("预定时间已过，立即触发")
            delay_seconds = 0

        payload = ScheduledTriggerPayload(when=when_text, what=intent.event)
        logger.info(f"计划在 {delay_seconds:.0f}s 后触发事件: {payload}")

        asyncio.create_task(
            self._wait_and_trigger(
                delay=delay_seconds,
                payload=payload,
                persistence=intent.persistence,
                repeat_interval_seconds=intent.repeat_interval_seconds,
            )
        )

    async def _wait_and_trigger(  # pragma: no cover
        self,
        delay: float,
        payload: ScheduledTriggerPayload,
        persistence: Persistence,
        repeat_interval_seconds: Optional[float],
    ):
        next_delay = delay
        while True:
            await asyncio.sleep(next_delay)
            event = ScheduledTriggerEvent(payload=payload)
            await self.event_queue.put(event)

            if persistence != Persistence.REPEAT:
                break
            if not repeat_interval_seconds or repeat_interval_seconds <= 0:
                logger.warning("repeat 模式下 repeat_interval_seconds 无效，自动降级为一次性触发")
                break
            next_delay = float(repeat_interval_seconds)
