"""验证提醒参数、真实投递和关闭行为。"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from muika.core.executor import Executor
from muika.core.scheduler import Scheduler
from muika.core.state import MuikaState
from muika.llm.utils.tools import function_call_handler
from muika.plugin.func_call import get_function_calls
from muika.plugin.func_call._context import clear_butler_context, set_butler_context


async def test_registered_tool_delivers_one_event():
    queue = asyncio.Queue()
    executor = Executor(queue, AsyncMock())
    set_butler_context(MuikaState(), executor)
    try:
        report = await get_function_calls()["plan_future_event"].run(event="drink water", trigger_in_seconds=0)
        assert "scheduled" in report
        event = await asyncio.wait_for(queue.get(), 1)
        assert event.type == "scheduled_trigger"
        assert event.payload.what == "drink water"
        await executor.scheduler.close()
        assert queue.empty()
    finally:
        clear_butler_context()
        await executor.scheduler.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"trigger_at": "2026-09-05T12:00", "trigger_in_seconds": 1},
        {"trigger_in_seconds": -1},
        {"trigger_in_seconds": float("nan")},
        {"trigger_in_seconds": float("inf")},
        {"trigger_at": "tomorrow"},
        {"trigger_in_seconds": 0, "repeat_interval_seconds": 0},
        {"trigger_in_seconds": 0, "repeat_interval_seconds": -1},
        {"trigger_in_seconds": 0, "repeat_interval_seconds": float("inf")},
    ],
)
async def test_invalid_schedule_creates_no_event(kwargs):
    queue = asyncio.Queue()
    scheduler = Scheduler(queue)
    with pytest.raises(ValueError):
        await scheduler.schedule("reminder", **kwargs)
    await scheduler.close()
    assert queue.empty()


async def test_tool_reports_failure_without_scheduling():
    clear_butler_context()
    assert "Executor" in await function_call_handler("plan_future_event", {"event": "test", "trigger_in_seconds": 0})
    executor = Executor(asyncio.Queue(), AsyncMock())
    set_butler_context(MuikaState(), executor)
    try:
        assert "Cannot schedule" in await get_function_calls()["plan_future_event"].run(event=" ", trigger_in_seconds=0)
        await executor.scheduler.close()
        assert "Cannot schedule" in await get_function_calls()["plan_future_event"].run(
            event="test", trigger_in_seconds=0
        )
        assert executor.scheduler.event_queue.empty()
    finally:
        clear_butler_context()
        await executor.scheduler.close()


async def test_repeating_event_stops_on_close():
    queue = asyncio.Queue()
    scheduler = Scheduler(queue)
    try:
        await scheduler.schedule("repeat", trigger_in_seconds=0, repeat_interval_seconds=0.01)
        first = await asyncio.wait_for(queue.get(), 1)
        second = await asyncio.wait_for(queue.get(), 1)
        assert first.payload.what == second.payload.what == "repeat"
        await scheduler.close()
        assert queue.empty()
        with pytest.raises(RuntimeError):
            await scheduler.schedule("late", trigger_in_seconds=0)
    finally:
        await scheduler.close()


@pytest.mark.parametrize("zone", [None, timezone.utc, timezone(timedelta(hours=8))])
async def test_absolute_time_uses_actual_instant(monkeypatch, zone):
    now = datetime(2026, 9, 5, 4, tzinfo=timezone.utc)
    target = (now + timedelta(seconds=30)).astimezone(zone)
    if zone is None:
        target = target.replace(tzinfo=None)

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr("muika.core.scheduler.datetime", FixedClock)
    scheduler = Scheduler(asyncio.Queue())
    wait = AsyncMock()
    monkeypatch.setattr(scheduler, "_wait_and_trigger", wait)
    await scheduler.schedule("future", trigger_at=target.isoformat())
    await asyncio.sleep(0)
    assert wait.await_args.args[0] == 30
    await scheduler.close()


async def test_past_time_fires_immediately_and_future_is_cancelled():
    queue = asyncio.Queue()
    scheduler = Scheduler(queue)
    try:
        await scheduler.schedule("past", trigger_at="2000-01-01T00:00:00Z")
        assert (await asyncio.wait_for(queue.get(), 1)).payload.what == "past"
        await scheduler.schedule("future", trigger_in_seconds=3600)
        await scheduler.close()
        assert queue.empty()
    finally:
        await scheduler.close()
