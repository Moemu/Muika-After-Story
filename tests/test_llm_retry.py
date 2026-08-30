import asyncio
from collections.abc import AsyncGenerator

import pytest

from muika.llm import ModelCompletions, ModelConfig, ModelStreamCompletions, Usage
from muika.llm._base import BaseLLM
from muika.llm._retry import (
    LLMRequestError,
    RequestRetry,
    _congestion_states,
    _defer_congestion,
    error_from_status,
)
from muika.models import Resource


def _config(**values) -> ModelConfig:
    data: dict[str, object] = {"provider": "_echo", "model_name": "retry-probe", "stream": False}
    data.update(values)
    return ModelConfig.model_validate(data)


async def test_request_retry_succeeds_on_third_attempt(monkeypatch):
    monkeypatch.setattr("muika.llm._retry._retry_delay", lambda error, attempt: 0.0)
    attempts = 0

    async def operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("temporary")
        return "ok"

    assert await RequestRetry(_config()).run(operation) == "ok"
    assert attempts == 3


async def test_request_retry_does_not_retry_invalid_request():
    attempts = 0

    async def operation():
        nonlocal attempts
        attempts += 1
        raise error_from_status(400, "bad request")

    with pytest.raises(LLMRequestError) as exc_info:
        await RequestRetry(_config()).run(operation)
    assert exc_info.value.kind == "invalid_request"
    assert attempts == 1


async def test_congestion_uses_ten_total_attempts(monkeypatch):
    monkeypatch.setattr("muika.llm._retry._retry_delay", lambda error, attempt: 0.0)
    attempts = 0

    async def operation():
        nonlocal attempts
        attempts += 1
        raise error_from_status(529, "overloaded")

    with pytest.raises(LLMRequestError) as exc_info:
        await RequestRetry(_config(congestion_retry_attempts=10)).run(operation)
    assert exc_info.value.kind == "congestion"
    assert attempts == 10


async def test_congestion_delay_is_shared(monkeypatch):
    _congestion_states.clear()
    now = 100.0
    delays = []

    async def sleep(delay: float):
        delays.append(delay)

    monkeypatch.setattr("muika.llm._retry.time.monotonic", lambda: now)
    monkeypatch.setattr("muika.llm._retry.asyncio.sleep", sleep)
    config = _config()
    key = (config.provider, config.api_host, config.model_name)
    _defer_congestion(key, 4.0)

    result = await RequestRetry(config).run(lambda: _return("ok"))

    assert result == "ok"
    assert delays == [4.0]


async def test_request_retry_propagates_cancellation():
    attempts = 0

    async def operation():
        nonlocal attempts
        attempts += 1
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await RequestRetry(_config()).run(operation)
    assert attempts == 1


async def test_stream_retries_before_first_chunk(monkeypatch):
    monkeypatch.setattr("muika.llm._retry._retry_delay", lambda error, attempt: 0.0)
    attempts = 0

    async def operation():
        nonlocal attempts
        attempts += 1

        async def response():
            if attempts < 3:
                raise ConnectionError("temporary")
            yield "ok"

        return response()

    result = [item async for item in RequestRetry[str](_config()).stream(operation)]
    assert result == ["ok"]
    assert attempts == 3


async def test_stream_does_not_retry_after_first_chunk(monkeypatch):
    monkeypatch.setattr("muika.llm._retry._retry_delay", lambda error, attempt: 0.0)
    attempts = 0

    async def operation():
        nonlocal attempts
        attempts += 1

        async def response():
            yield "partial"
            raise ConnectionError("lost")

        return response()

    received = []
    with pytest.raises(LLMRequestError):
        async for item in RequestRetry[str](_config()).stream(operation):
            received.append(item)
    assert received == ["partial"]
    assert attempts == 1


async def test_timeout_is_left_for_stream_fallback():
    attempts = 0

    async def operation():
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(1)

    config = _config(request_timeout_seconds=0.01, stream_fallback_on_timeout=True)
    with pytest.raises(LLMRequestError) as exc_info:
        await RequestRetry(config).run(operation)
    assert exc_info.value.kind == "timeout"
    assert attempts == 1


class _CompletionProbe:
    def __init__(self, config: ModelConfig) -> None:
        self.config = config

    _collect_stream = BaseLLM._collect_stream
    _complete_response = BaseLLM._complete_response


async def _return(value):
    return value


async def test_complete_response_collects_configured_stream():
    probe = _CompletionProbe(_config(stream=True))
    sync_called = False
    usage = Usage(input_tokens=3, output_tokens=2)
    resource = Resource(type="image", raw=b"image")

    async def sync_call() -> ModelCompletions:
        nonlocal sync_called
        sync_called = True
        return ModelCompletions(text="sync")

    async def stream_call() -> AsyncGenerator[ModelStreamCompletions, None]:
        yield ModelStreamCompletions(chunk="hel")
        yield ModelStreamCompletions(chunk="lo", usage=usage, resources=[resource])

    result = await probe._complete_response(sync_call, stream_call)

    assert result.text == "hello"
    assert result.usage is usage
    assert result.resources == [resource]
    assert sync_called is False


async def test_complete_response_falls_back_after_timeout():
    probe = _CompletionProbe(_config())

    async def sync_call() -> ModelCompletions:
        raise LLMRequestError("timeout", "timeout")

    async def stream_call() -> AsyncGenerator[ModelStreamCompletions, None]:
        yield ModelStreamCompletions(chunk="recovered")

    result = await probe._complete_response(sync_call, stream_call)
    assert result.text == "recovered"
