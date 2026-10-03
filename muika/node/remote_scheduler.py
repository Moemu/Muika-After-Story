"""使用与单机提醒一致的参数，把提醒提交到常驻状态服务。"""

import asyncio
import math
from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

from muika.core.scheduler import Scheduler
from muika.ipc.node_client import NodeClient
from muika.ipc.node_protocol import ScheduleRequest
from muika.plugin.func_call.context import ToolContext, get_dependencies

from .schedule_protocol import CreateSchedule, ScheduleRecord
from .turn_protocol import ClientRoute


class RemoteScheduler(Scheduler):
    persistent = True

    def __init__(
        self, queue: asyncio.Queue, client: NodeClient, epoch: int, route: Callable[[], ClientRoute | None]
    ) -> None:
        super().__init__(queue)
        self.client, self.epoch, self.route = client, epoch, route

    async def schedule(
        self,
        event: str,
        *,
        trigger_in_seconds: float | None = None,
        trigger_at: str | None = None,
        repeat_interval_seconds: float | None = None,
    ) -> None:
        if self._closed:
            raise RuntimeError("Scheduler is closed.")
        if not event.strip() or (trigger_in_seconds is None) == (trigger_at is None):
            raise ValueError("Provide an event and exactly one delay or absolute time.")
        if repeat_interval_seconds is not None and (
            not math.isfinite(repeat_interval_seconds) or repeat_interval_seconds <= 0
        ):
            raise ValueError("Repeat interval must be finite and positive.")
        if trigger_at is not None:
            due_at = datetime.fromisoformat(trigger_at.replace("Z", "+00:00")).timestamp()
            when = trigger_at
        else:
            if trigger_in_seconds is None or not math.isfinite(trigger_in_seconds) or trigger_in_seconds < 0:
                raise ValueError("Delay must be finite and non-negative.")
            due_at = datetime.now().timestamp() + trigger_in_seconds
            when = f"in {trigger_in_seconds:g} seconds"
        route = self.route()
        if route is None:
            raise ValueError("A reminder needs a known conversation route.")
        context = get_dependencies().get(ToolContext)
        id = context.execution_id if isinstance(context, ToolContext) and context.execution_id else uuid4().hex
        await self.client.request(
            ScheduleRequest(
                epoch=self.epoch,
                body=CreateSchedule(
                    schedule=ScheduleRecord(
                        id=id,
                        event=event.strip(),
                        when=when,
                        due_at=due_at,
                        repeat_interval=repeat_interval_seconds,
                        route=route,
                    )
                ),
            )
        )
