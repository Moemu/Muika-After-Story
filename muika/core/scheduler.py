"""将内存中的定时提醒投递到主事件队列。"""

import asyncio
import math
from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import select

from muika.database.db import get_session
from muika.database.orm_models import SyncStateORM

from .events import ScheduledTriggerEvent, ScheduledTriggerPayload


class Reminder(BaseModel):
    """保存提醒时间和已触发状态，不保存进程任务。"""

    id: str
    what: str
    when: float
    repeat: float | None = None
    pending: bool = True


class Scheduler:
    """管理单次和重复提醒，关闭时取消所有待执行任务。"""

    def __init__(self, event_queue: asyncio.Queue):
        self.event_queue = event_queue
        self._tasks: set[asyncio.Task] = set()
        self._closed = False
        self.persistent = False
        self.active = True

    async def activate(self, active: bool) -> None:
        """暂停备用设备的提醒，接管时从本地历史恢复待触发提醒。"""
        self.active = active
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.persistent and active:
            async with get_session(record_activity=False) as db:
                reminders = [
                    Reminder.model_validate_json(row.payload)
                    for row in await db.scalars(select(SyncStateORM).where(SyncStateORM.key.like("reminder:%")))
                ]
            for reminder in reminders:
                if reminder.pending:
                    self._arm(reminder)

    def _arm(self, reminder: Reminder) -> None:
        payload = ScheduledTriggerPayload(datetime.fromtimestamp(reminder.when).isoformat(), reminder.what, reminder.id)
        task = asyncio.create_task(
            self._wait_and_trigger(max(0.0, reminder.when - datetime.now().timestamp()), payload, reminder.repeat)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def schedule(
        self,
        event: str,
        *,
        trigger_in_seconds: float | None = None,
        trigger_at: str | None = None,
        repeat_interval_seconds: float | None = None,
    ) -> None:
        """校验提醒并创建任务。

        :param event: 交给 Muika 处理的提醒内容。
        :param trigger_in_seconds: 首次触发前的非负秒数。
        :param trigger_at: 首次触发的 ISO 时间，无时区时使用本地时间。
        :param repeat_interval_seconds: 重复间隔，空值表示单次提醒。
        :raises ValueError: 内容或时间参数无效。
        :raises RuntimeError: 调度器已关闭。
        """
        if self._closed:
            raise RuntimeError("Scheduler is closed.")
        if not event.strip():
            raise ValueError("Event must not be empty.")
        if (trigger_in_seconds is None) == (trigger_at is None):
            raise ValueError("Provide exactly one of trigger_in_seconds and trigger_at.")
        if repeat_interval_seconds is not None:
            if not math.isfinite(repeat_interval_seconds) or repeat_interval_seconds <= 0:
                raise ValueError("Repeat interval must be finite and positive.")
        if trigger_at is not None:
            target = datetime.fromisoformat(trigger_at.replace("Z", "+00:00"))
            delay = max(0.0, target.timestamp() - datetime.now().timestamp())
            when = trigger_at
        else:
            assert trigger_in_seconds is not None
            if not math.isfinite(trigger_in_seconds) or trigger_in_seconds < 0:
                raise ValueError("Delay must be finite and non-negative.")
            delay = trigger_in_seconds
            when = f"in {delay:g} seconds"

        if self.persistent:
            reminder = Reminder(
                id=uuid4().hex,
                what=event.strip(),
                when=datetime.now().timestamp() + delay,
                repeat=repeat_interval_seconds,
            )
            async with get_session() as db:
                db.add(SyncStateORM(key="reminder:" + reminder.id, payload=reminder.model_dump_json()))
            if self.active:
                self._arm(reminder)
            return
        payload = ScheduledTriggerPayload(when=when, what=event.strip())
        task = asyncio.create_task(self._wait_and_trigger(delay, payload, repeat_interval_seconds))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        """取消并等待所有提醒任务，禁止创建新提醒。"""
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _wait_and_trigger(
        self, delay: float, payload: ScheduledTriggerPayload, repeat_interval: float | None
    ) -> None:
        """按间隔投递事件，直到单次提醒完成或任务被取消。"""
        while True:
            await asyncio.sleep(delay)
            if self.persistent and payload.source:
                async with get_session() as db:
                    saved = await db.get(SyncStateORM, "reminder:" + payload.source)
                    if saved is None:
                        return
                    reminder = Reminder.model_validate_json(saved.payload)
                    if not reminder.pending:
                        return
                    reminder.pending = repeat_interval is not None
                    reminder.when = datetime.now().timestamp() + (repeat_interval or 0)
                    saved.payload = reminder.model_dump_json()
            await self.event_queue.put(ScheduledTriggerEvent(payload=payload))
            if repeat_interval is None:
                return
            delay = repeat_interval
