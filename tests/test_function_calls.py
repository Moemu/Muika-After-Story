from typing import Any

import pytest
from pydantic import BaseModel

from muika.llm.utils.tools import function_call_handler
from muika.plugin.func_call.caller import Caller, FunctionCallValidationError


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
