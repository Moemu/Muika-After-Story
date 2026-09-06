from unittest.mock import AsyncMock

import pytest

pytest.importorskip("dashscope")

from muika.llm import ModelConfig, ModelRequest  # noqa: E402
from muika.llm._retry import LLMRequestError  # noqa: E402
from muika.llm.providers.dashscope import Dashscope  # noqa: E402


async def test_non_stream_response_prioritizes_tool_calls(monkeypatch):
    from dashscope.api_entities.dashscope_response import GenerationResponse

    provider = Dashscope(ModelConfig(provider="dashscope", model_name="qwen-test", api_key="test-key"))
    response = GenerationResponse(
        status_code=200,
        usage={"input_tokens": 1, "output_tokens": 1},
        output={
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "I will read it first.",
                        "tool_calls": [
                            {"id": "call-1", "function": {"name": "read_file", "arguments": '{"path":"one.py"}'}},
                            {"id": "call-2", "function": {"name": "read_file", "arguments": '{"path":"two.py"}'}},
                        ],
                    },
                }
            ]
        },
    )
    call = AsyncMock(return_value=response)
    monkeypatch.setattr("dashscope.AioGeneration.call", call)
    result = await provider._collect_stream(provider.request_step(ModelRequest("read"), [], stream=False))
    assert result.message is not None
    assert [tool.id for tool in result.message.tool_calls] == ["call-1", "call-2"]
    assert result.stop_reason == "tool_calls"
    assert result.text == "I will read it first."


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
        await provider._collect_stream(provider.request_step(ModelRequest("test"), [], stream=False))

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

    stream = provider.request_step(ModelRequest("test"), [], stream=True)
    with pytest.raises(LLMRequestError):
        async for _ in stream:
            pass

    assert attempts == 3
