"""节日/问候触发链：优先级、当天去重与发送即记录。"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from harness import ScriptedTurn

from muika.core.topic_manager import TopicKind

pytestmark = pytest.mark.e2e


@pytest.fixture
def occasion_clock(monkeypatch):
    """固定到 2026-02-17（春节）10:00，并同步各模块的本地时间。"""
    clock = MagicMock(wraps=datetime)
    clock.now.return_value = datetime(2026, 2, 17, 10, 0)
    for target in (
        "muika.core.loop.datetime",
        "muika.core.memory.datetime",
        "muika.core.topic_manager.datetime",
        "muika.database.crud.datetime",
    ):
        monkeypatch.setattr(target, clock)
    return clock


async def _advance(app) -> None:
    await app.advance_time()
    await app.wait_processed("time_tick")


async def test_occasion_fires_once_per_day(core_app_factory, occasion_clock):
    app = await core_app_factory(turns=[ScriptedTurn(text="春节快乐，沐沐。")])
    await app.start()
    app.muika.topic_manager.store.is_empty = lambda: True
    app.muika.state.boredom = 0.9
    app.muika.state.last_interaction = occasion_clock.now() - timedelta(minutes=30)

    await _advance(app)
    assert await app.next_reply() == "春节快乐，沐沐。"

    available = await app.muika.topic_manager._topic_available(
        "occasion:chinese_new_year:2026-02-17", TopicKind.OCCASION, occasion_clock.now()
    )
    assert available is False
    app.recorder.record("occasion_once_per_day", occasion="春节")


async def test_greeting_once_per_slot(core_app_factory, occasion_clock):
    occasion_clock.now.return_value = datetime(2026, 3, 10, 10, 0)
    app = await core_app_factory(turns=[ScriptedTurn(text="早上好呀。")])
    await app.start()
    app.muika.topic_manager.store.is_empty = lambda: True
    app.muika.state.boredom = 0.9
    app.muika.state.last_interaction = occasion_clock.now() - timedelta(minutes=30)

    await _advance(app)
    assert await app.next_reply() == "早上好呀。"

    now = occasion_clock.now()
    assert (
        await app.muika.topic_manager._topic_available("greeting:morning:2026-03-10", TopicKind.GREETING, now) is False
    )
    assert await app.muika.topic_manager._topic_available("greeting:noon:2026-03-10", TopicKind.GREETING, now) is True
    app.recorder.record("greeting_once_per_slot", slot="morning", cross_slot_available=True)


async def test_occasion_is_written_when_sent(core_app_factory, occasion_clock):
    from muika.database.crud import TopicHistoryCRUD
    from muika.database.db import get_session

    app = await core_app_factory(turns=[ScriptedTurn(text="春节快乐，沐沐。")])
    await app.start()
    app.muika.topic_manager.store.is_empty = lambda: True
    app.muika.state.boredom = 0.9
    app.muika.state.last_interaction = occasion_clock.now() - timedelta(minutes=30)

    await _advance(app)
    await app.next_reply()

    async with get_session() as db_session:
        record = await TopicHistoryCRUD.get_by_topic_id(db_session, "occasion:chinese_new_year:2026-02-17")
    assert record is not None
    assert record.use_count == 1
    app.recorder.record("occasion_recorded_on_send", topic_id="occasion:chinese_new_year:2026-02-17")
