from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("dashscope")

from muika.llm import ModelCompletions, ModelConfig  # noqa: E402
from muika.llm._retry import LLMRequestError  # noqa: E402
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


async def test_dashscope_uses_async_sdk_and_normalizes_status(monkeypatch):
    from dashscope.api_entities.dashscope_response import GenerationResponse

    config = ModelConfig(
        provider="dashscope",
        model_name="qwen-test",
        api_key="test-key",
        congestion_retry_attempts=1,
    )
    provider = Dashscope(config)
    call = AsyncMock(return_value=GenerationResponse(status_code=529, code="overloaded", message="busy"))
    monkeypatch.setattr("dashscope.AioGeneration.call", call)

    with pytest.raises(LLMRequestError) as exc_info:
        await provider._ask([], [], None, False)

    assert exc_info.value.kind == "congestion"
    call.assert_awaited_once()


async def test_dashscope_retries_stream_error_before_content(monkeypatch):
    from dashscope.api_entities.dashscope_response import GenerationResponse

    config = ModelConfig(
        provider="dashscope",
        model_name="qwen-test",
        api_key="test-key",
        stream=True,
        congestion_retry_attempts=3,
    )
    provider = Dashscope(config)
    attempts = 0

    async def call(**kwargs):
        nonlocal attempts
        attempts += 1

        async def response():
            yield GenerationResponse(status_code=529, code="overloaded", message="busy")

        return response()

    monkeypatch.setattr("dashscope.AioGeneration.call", call)
    monkeypatch.setattr("muika.llm._retry._retry_delay", lambda error, attempt: 0.0)

    stream = await provider._ask([], [], None, True)
    with pytest.raises(LLMRequestError):
        async for _ in stream:
            pass

    assert attempts == 3
