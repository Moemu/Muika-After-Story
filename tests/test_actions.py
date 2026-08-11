"""``actions/schema.py`` 与 ``actions/intents.py`` 的 handle() 契约测试。"""

import pytest

from muika.core.actions.intents import BaseIntent, PlanFutureEventIntent
from muika.core.actions.schema import BaseAction


async def test_base_action_handle_not_implemented():
    with pytest.raises(NotImplementedError):
        await BaseAction().handle(state=None, executor=None)


async def test_base_intent_handle_not_implemented():
    with pytest.raises(NotImplementedError):
        await BaseIntent().handle(state=None, executor=None)


async def test_plan_event_intent_without_executor():
    intent = PlanFutureEventIntent(event="x", trigger_in_seconds=5)
    out = await intent.handle(state=None, executor=None)
    assert out.content == "Executor is required for scheduled actions."


async def test_plan_event_intent_schedules():
    intent = PlanFutureEventIntent(event="x", trigger_in_seconds=5)
    recorded: list = []

    class FakeScheduler:
        async def schedule(self, intent):
            recorded.append(intent)

    class FakeExecutor:
        scheduler = FakeScheduler()

    out = await intent.handle(state=None, executor=FakeExecutor())
    assert out.content == "Future event planned successfully."
    assert recorded == [intent]
