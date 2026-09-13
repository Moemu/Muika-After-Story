"""验证按自然日补做、空闲门控及失败重试。"""

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from muika.config import mas_config
from muika.core.memory import MemoryManager
from muika.core.memory_models import DreamResult
from muika.core.reflection import ReflectionAgent, seconds_until_reflection
from muika.core.state import MuikaState


def test_daily_boundary():
    assert seconds_until_reflection(datetime(2026, 9, 1, 4)) == 3600
    assert seconds_until_reflection(datetime(2026, 9, 1, 5)) == 0
    assert seconds_until_reflection(datetime(2026, 9, 1, 6)) == 23 * 3600


@pytest.fixture
def reflection(redirect_get_session):
    state = MuikaState(last_interaction=datetime.now() - timedelta(minutes=5))
    memory = MemoryManager()
    agent = MagicMock()
    agent.memory_reasoner.dream = AsyncMock()
    return ReflectionAgent(agent, memory, state, MagicMock(send_message=AsyncMock()))


async def test_auto_dream_is_independent_of_self_modification(reflection, monkeypatch):
    monkeypatch.setattr(mas_config, "action_permission", "write")
    monkeypatch.setattr(mas_config, "enable_auto_reflection", True)
    for days in (3, 2):
        await reflection._memory.add_context(
            "muika", "A thought of my own", timestamp=datetime.now() - timedelta(days=days)
        )
    await reflection.maybe_reflect()
    assert [call.args[0] for call in reflection._agent.memory_reasoner.dream.await_args_list] == [
        date.today() - timedelta(days=3),
        date.today() - timedelta(days=2),
    ]


async def test_active_conversation_defers_dream(reflection):
    await reflection._memory.add_context("user", "Yesterday", timestamp=datetime.now() - timedelta(days=2))
    reflection._state.last_interaction = datetime.now()
    await reflection.maybe_reflect()
    reflection._agent.memory_reasoner.dream.assert_not_awaited()


async def test_failed_day_stops_progress_and_retries_without_repeating_completed_day(reflection):
    memory = reflection._memory
    first, second = date.today() - timedelta(days=3), date.today() - timedelta(days=2)
    for day in (first, second):
        await memory.add_context("muika", "An experience", timestamp=datetime.combine(day, datetime.min.time()))
    fail = True

    async def dream(day, memory):
        if day == second and fail:
            raise RuntimeError("model unavailable")
        materials = await memory.day_material(day)
        return await memory.save_dream(
            day, DreamResult(diary=f"My day {day}"), materials[-1].id, {f"experience:{item.id}" for item in materials}
        )

    reflection._agent.memory_reasoner.dream.side_effect = dream
    await reflection.maybe_reflect()
    await reflection.maybe_reflect()
    assert reflection._agent.memory_reasoner.dream.await_count == 2
    assert await memory.pending_days(datetime.now()) == [second]
    fail = False
    reflection._retry_after = None
    await reflection.maybe_reflect()
    assert [call.args[0] for call in reflection._agent.memory_reasoner.dream.await_args_list] == [first, second, second]
    assert await memory.pending_days(datetime.now()) == []
