"""``Scheduler.parse_time`` 自然语言时间解析测试。"""

import asyncio
from datetime import datetime

from muika.core.scheduler import Scheduler


def _scheduler() -> Scheduler:
    return Scheduler(event_queue=asyncio.Queue())


def test_parse_time_valid():
    parsed = _scheduler().parse_time("8am")
    assert parsed is not None
    assert parsed.hour == 8


def test_parse_time_tomorrow_is_future():
    parsed = _scheduler().parse_time("tomorrow")
    assert parsed is not None
    assert parsed > datetime.now()


def test_parse_time_garbage_none():
    assert _scheduler().parse_time("xyzzy not a time") is None
