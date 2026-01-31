import asyncio
from datetime import datetime
from typing import Optional

import dateparser
from nonebot import logger

from .action import PlanFutureEventIntent
from .events import ScheduledTriggerEvent, ScheduledTriggerPayload


class Scheduler:
    def __init__(self, event_queue: asyncio.Queue):
        self.event_queue = event_queue
        # self._tasks = []

    def parse_time(self, natural_time: str) -> Optional[datetime]:
        # settings={'PREFER_DATES_FROM': 'future'} 确保 '8am' 是明天的如果今天已经过了
        return dateparser.parse(natural_time, settings={"PREFER_DATES_FROM": "future"})

    async def schedule(self, intent: PlanFutureEventIntent):
        when_str = intent.when
        what_str = intent.what
        payload = ScheduledTriggerPayload(when_str, what_str)

        target_time = self.parse_time(when_str)
        if not target_time:
            logger.error(f"无法解析时间: {when_str}")
            return

        now = datetime.now()
        delay_seconds = (target_time - now).total_seconds()

        if delay_seconds < 0:
            logger.warning("预定时间已过，立即触发")
            delay_seconds = 0

        logger.info(f"计划在 {target_time} ({delay_seconds:.0f}s 后) 触发事件: {payload}")

        # 创建后台任务等待
        asyncio.create_task(self._wait_and_trigger(delay_seconds, payload))

    async def _wait_and_trigger(self, delay: float, payload: ScheduledTriggerPayload):
        await asyncio.sleep(delay)

        # 时间到了！生产一个事件回传给 Muika
        event = ScheduledTriggerEvent(payload=payload)
        await self.event_queue.put(event)
