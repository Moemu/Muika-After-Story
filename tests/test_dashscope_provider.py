from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("dashscope")

from muika.llm import ModelCompletions  # noqa: E402
from muika.llm.providers.dashscope import Dashscope  # noqa: E402


async def test_non_stream_response_prioritizes_tool_calls(monkeypatch):
    provider = Dashscope.__new__(Dashscope)
    expected = ModelCompletions(text="tool result")
    handler = AsyncMock(return_value=expected)
    monkeypatch.setattr(provider, "_tool_calls_handle_sync", handler)
    message = SimpleNamespace(content="I will read it first.", tool_calls=[{"id": "call-1"}])
    response = SimpleNamespace(
        status_code=200,
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, prompt_tokens_details=None),
        output=SimpleNamespace(text="I will read it first.", choices=[SimpleNamespace(message=message)]),
    )

    result = await provider._GenerationResponse_handle([], [], None, response)

    assert result is expected
    handler.assert_awaited_once()
