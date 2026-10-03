"""在状态服务中将到期提醒原子转为持久输入。"""

import asyncio
import math
import time
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from muika.database.db import get_session
from muika.database.orm_models import RuntimeInboxORM, RuntimeScheduleORM
from muika.utils.logger import logger

from .event_protocol import RuntimeEvent
from .models import IncomingMessage
from .schedule_protocol import (
    CancelSchedule,
    CreateSchedule,
    ListSchedules,
    ScheduleOperation,
    ScheduleRecord,
)


class ScheduleService:
    """提醒独立于 Core 的活动任期；一次触发仅有一个输入身份。"""

    async def execute(self, db: AsyncSession, action: ScheduleOperation) -> list[ScheduleRecord]:
        if isinstance(action, CreateSchedule):
            saved = await db.get(RuntimeScheduleORM, action.schedule.id)
            if saved is not None:
                previous = ScheduleRecord.model_validate_json(saved.payload)
                if previous.model_dump(exclude={"due_at"}) != action.schedule.model_dump(exclude={"due_at"}):
                    raise ValueError("Reminder identity was reused with different content.")
            else:
                db.add(
                    RuntimeScheduleORM(
                        id=action.schedule.id,
                        payload=action.schedule.model_dump_json(),
                        due_at=action.schedule.due_at,
                        occurrence=0,
                        enabled=True,
                    )
                )
        elif isinstance(action, CancelSchedule):
            saved = await db.get(RuntimeScheduleORM, action.id)
            if saved is None:
                raise ValueError("Reminder does not exist.")
            saved.enabled = False
        elif isinstance(action, ListSchedules):
            rows = await db.scalars(
                select(RuntimeScheduleORM)
                .where(RuntimeScheduleORM.enabled.is_(True))
                .order_by(RuntimeScheduleORM.due_at)
            )
            return [
                ScheduleRecord.model_validate_json(row.payload).model_copy(update={"due_at": row.due_at})
                for row in rows
            ]
        return []

    async def run(self) -> None:
        while True:
            try:
                await self.fire_due()
            except Exception:
                logger.exception("[Scheduler] Could not persist due reminders.")
            await asyncio.sleep(0.2)

    async def fire_due(self) -> None:
        now = time.time()
        async with get_session() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            rows = await db.scalars(
                select(RuntimeScheduleORM).where(RuntimeScheduleORM.enabled.is_(True), RuntimeScheduleORM.due_at <= now)
            )
            for row in rows:
                schedule = ScheduleRecord.model_validate_json(row.payload)
                timestamp = datetime.fromtimestamp(row.due_at)
                message = IncomingMessage(
                    id=f"schedule:{row.id}:{row.occurrence}",
                    client_id=schedule.route.client_id,
                    conversation_id=schedule.route.conversation_id,
                    kind="runtime_event",
                    occurred_at=timestamp,
                    event=RuntimeEvent(
                        type="scheduled_trigger", timestamp=timestamp, when=schedule.when, what=schedule.event
                    ),
                )
                db.add(
                    RuntimeInboxORM(
                        client_id=message.client_id, message_id=message.id, payload=message.model_dump_json()
                    )
                )
                row.occurrence += 1
                if schedule.repeat_interval is None:
                    row.enabled = False
                else:
                    # 重启只补一次过期提醒，后续恢复正常节奏。
                    skipped = math.floor((now - row.due_at) / schedule.repeat_interval) + 1
                    row.due_at += skipped * schedule.repeat_interval
