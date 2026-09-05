import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from arclet.alconna import Alconna, Args, Arparma
from pydantic import BaseModel

from muika.core.executor import Executor
from muika.core.memory import MemoryManager
from muika.core.state import MuikaState
from muika.llm.utils.tools import function_call_handler
from muika.plugin.command import CommandDispatcher
from muika.plugin.func_call.caller import Caller, FunctionCallValidationError
from muika.plugin.func_call.context import ToolContext, get_dependencies, tool_context


class _CountParams(BaseModel):
    count: int


async def test_caller_validates_and_coerces_model_arguments():
    caller = Caller("count", params=_CountParams)

    async def target(count: int) -> int:
        return count

    caller.function = target
    assert await caller.run(count="3") == 3


async def test_caller_rejects_unknown_model_arguments():
    caller = Caller("count", params=_CountParams)

    async def target(count: int) -> int:
        return count

    caller.function = target
    with pytest.raises(FunctionCallValidationError, match="line_start"):
        await caller.run(count=3, line_start="120")


async def test_function_call_error_is_returned_to_model(monkeypatch):
    class BrokenCaller:
        async def run(self, **kwargs: Any):
            raise FunctionCallValidationError("Unexpected argument: line_start")

    monkeypatch.setattr(
        "muika.llm.utils.tools.get_function_calls",
        lambda: {"read_file": BrokenCaller()},
    )

    result = await function_call_handler("read_file", {"line_start": "120"})

    assert result.startswith("Tool error (read_file): FunctionCallValidationError")
    assert result.endswith("Correct the arguments and retry.")


async def test_tool_dependencies_follow_each_concurrent_context():
    caller = Caller("dependency probe", params=_CountParams)
    both_entered = asyncio.Event()
    entered = 0

    async def target(count: int, executor: "Executor", state: MuikaState, memory: MemoryManager):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), 1)
        return count, executor, state, memory

    caller.function = target
    assert set(caller.data()["function"]["parameters"]["properties"]) == {"count"}

    async def invoke(count):
        memory = MemoryManager()
        state = MuikaState(memory=memory)
        executor = Executor(asyncio.Queue(), AsyncMock())
        with tool_context(state, executor):
            result = await caller.run(count=str(count))
            assert result == (count, executor, state, memory)
        await executor.scheduler.close()

    await asyncio.gather(invoke(1), invoke(2))


@pytest.mark.parametrize("params", [None, _CountParams])
async def test_model_cannot_supply_dependency_and_missing_context_does_not_run(params):
    caller = Caller("dependency probe", params=params)
    called = False

    async def target(executor: Executor, count: int = 3):
        nonlocal called
        called = True
        return count

    caller.function = target
    with pytest.raises(FunctionCallValidationError, match="Dependencies cannot be supplied"):
        await caller.run(count=1, executor="forged")
    with pytest.raises(TypeError, match="Executor.*unavailable"):
        await caller.run(count=1)
    assert not called


async def test_tool_without_parameter_model_keeps_defaults_and_rejects_unknown_arguments():
    caller = Caller("defaults")

    async def target(executor: Executor, count: int = 3):
        return executor, count

    caller.function = target
    executor = Executor(asyncio.Queue(), AsyncMock())
    with tool_context(MuikaState(), executor):
        assert "executor" not in caller.data()["function"]["parameters"]["properties"]
        assert await caller.run() == (executor, 3)
        with pytest.raises(FunctionCallValidationError, match="unknown"):
            await caller.run(unknown=1)
    await executor.scheduler.close()


async def test_command_injection_preserves_result_arguments_defaults_and_reply():
    dispatcher = CommandDispatcher(MagicMock(), AsyncMock())
    executor = dispatcher._injections[Executor]
    parsed = Alconna("di_probe", Args["count", int]).parse("di_probe 7")
    registry = MagicMock()
    registry.finish = AsyncMock()
    received = []

    async def handler(executor: Executor, result: Arparma, count: int, suffix: str = "ok"):
        received.append((executor, result, count, suffix))
        return "done"

    await dispatcher._invoke(handler, registry, parsed)
    assert received == [(executor, parsed, 7, "ok")]
    registry.finish.assert_awaited_once_with("done")


def test_nested_tool_context_restores_outer_request_after_error():
    state = MuikaState()
    executor = MagicMock()
    with tool_context(state, executor) as outer:
        with pytest.raises(RuntimeError, match="failed"):
            with tool_context(MuikaState(), executor) as inner:
                assert get_dependencies()[ToolContext] is inner
                raise RuntimeError("failed")
        assert get_dependencies()[ToolContext] is outer
        assert get_dependencies()[MuikaState] is state
    assert get_dependencies()[ToolContext] is None
