import asyncio

import pytest

from muika.llm import ModelConfig, ModelRequest, ModelStreamCompletions
from muika.llm._base import BaseLLM
from muika.llm._execution import step
from muika.llm._retry import (
    LLMRequestError,
    RequestRetry,
    _congestion_states,
    error_from_status,
)


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
    """一次 congestion 失败后，同 provider/host/model 的下一次请求复用共享退避。"""
    delays: list[float] = []

    async def sleep(delay: float):
        delays.append(delay)

    monkeypatch.setattr("muika.llm._retry.time.monotonic", lambda: 100.0)
    monkeypatch.setattr("muika.llm._retry.asyncio.sleep", sleep)
    monkeypatch.setattr("muika.llm._retry._retry_delay", lambda error, attempt: 4.0)
    config = _config(model_name="shared-congestion-probe", congestion_retry_attempts=2)

    async def congested():
        raise error_from_status(529, "overloaded")

    try:
        # 第一次请求以 529 失败两次，其间记录共享退避，并等待它
        with pytest.raises(LLMRequestError) as exc_info:
            await RequestRetry(config).run(congested)
        assert exc_info.value.kind == "congestion"
        assert delays == pytest.approx([4.0, 4.0])  # 重试等待 + 下一次尝试前的共享退避

        first_request_delays = len(delays)
        # 第二次请求自身立刻成功，仍须等完上一次请求记录的退避
        assert await RequestRetry(config).run(lambda: _return("ok")) == "ok"
        assert len(delays) == first_request_delays + 1
        assert delays[-1] == pytest.approx(4.0)
    finally:
        _congestion_states.clear()


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


class _StepProbe(BaseLLM):
    """非流式请求超时，验证执行层自动降级为流式重试。"""

    def __init__(self, config: ModelConfig) -> None:
        super().__init__(config)

    async def ask(self, request, *, stream=False):
        raise NotImplementedError

    async def request_step(self, request, messages, *, stream):
        if not stream:
            raise LLMRequestError("timed out", "timeout")
        yield ModelStreamCompletions(chunk="recovered")


async def _return(value):
    return value


async def test_step_falls_back_to_stream_after_timeout():
    probe = _StepProbe(_config(stream_fallback_on_timeout=True))
    result = await probe.collect_stream(step(probe, ModelRequest("hi"), [], stream=False))
    assert result.text == "recovered"
