"""主动预算：上下限切换、全局间隔与全局每日上限。"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from harness import ScriptedTurn

from muika.core.topic_manager import (
    GLOBAL_DAILY_LIMIT,
    GLOBAL_MIN_GAP_MINUTES,
    KIND_BUDGETS,
    TopicKind,
)
from muika.database.crud import TopicHistoryCRUD
from muika.database.db import get_session

pytestmark = pytest.mark.e2e


@pytest.fixture
def budget_clock(monkeypatch):
    """固定到 2026-03-10 10:00，并同步各模块的本地时间。"""
    clock = MagicMock(wraps=datetime)
    clock.now.return_value = datetime(2026, 3, 10, 10, 0)
    for target in (
        "muika.core.loop.datetime",
        "muika.core.memory.datetime",
        "muika.core.topic_manager.datetime",
        "muika.database.crud.datetime",
    ):
        monkeypatch.setattr(target, clock)
    return clock


async def _record(topic_id: str, when: datetime, clock: MagicMock, *, engaged: bool = False) -> None:
    clock.now.return_value = when
    async with get_session() as db_session:
        await TopicHistoryCRUD.record(db_session, topic_id=topic_id, user_engaged=engaged)


async def test_low_interaction_uses_base_limit(core_app_factory, budget_clock):
    app = await core_app_factory(turns=[ScriptedTurn(text="早上好。")])
    await app.start()
    now = budget_clock.now()
    base = KIND_BUDGETS[TopicKind.GREETING].base_daily
    for index in range(base):
        await _record(f"greeting:morning:2026-03-10:{index}", now - timedelta(hours=3 + index), budget_clock)
    reason = await app.muika.topic_manager.kind_budget_block(TopicKind.GREETING, now)
    assert reason == "daily_budget"
    app.recorder.record("budget_base_limit", topic_kind="greeting", base=base, reason=reason)


async def test_high_interaction_allows_max_limit(core_app_factory, budget_clock):
    app = await core_app_factory(turns=[ScriptedTurn(text="早上好。")])
    await app.start()
    now = budget_clock.now()
    budget = KIND_BUDGETS[TopicKind.GREETING]
    # 近 7 天 2 条被回应的记录 -> ratio=1.0 -> 使用 max_daily
    for index in range(2):
        await _record(f"reflection:engaged:{index}", now - timedelta(days=2, hours=index), budget_clock, engaged=True)
    for index in range(budget.max_daily - 1):
        await _record(f"greeting:morning:2026-03-10:{index}", now - timedelta(hours=3 + index), budget_clock)
    assert await app.muika.topic_manager.kind_budget_block(TopicKind.GREETING, now) is None
    await _record("greeting:noon:2026-03-10", now - timedelta(hours=2), budget_clock)
    assert await app.muika.topic_manager.kind_budget_block(TopicKind.GREETING, now) == "daily_budget"
    app.recorder.record("budget_max_limit", topic_kind="greeting", max_daily=budget.max_daily)


async def test_global_gap_blocks_any_kind(core_app_factory, budget_clock):
    app = await core_app_factory(turns=[ScriptedTurn(text="早上好。")])
    await app.start()
    now = budget_clock.now()
    await _record("reflection:recent", now - timedelta(minutes=GLOBAL_MIN_GAP_MINUTES - 10), budget_clock)
    reason = await app.muika.topic_manager.kind_budget_block(TopicKind.GREETING, now)
    assert reason == "global_gap"
    app.recorder.record("budget_global_gap", reason=reason)


async def test_global_daily_limit_blocks_any_kind(core_app_factory, budget_clock):
    app = await core_app_factory(turns=[ScriptedTurn(text="早上好。")])
    await app.start()
    now = budget_clock.now()
    for index in range(GLOBAL_DAILY_LIMIT):
        await _record(f"reflection:{index}", now - timedelta(hours=20 - index * 2), budget_clock)
    reason = await app.muika.topic_manager.kind_budget_block(TopicKind.GREETING, now)
    assert reason == "global_budget"
    app.recorder.record("budget_global_limit", limit=GLOBAL_DAILY_LIMIT, reason=reason)
