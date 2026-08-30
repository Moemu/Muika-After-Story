from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

from muika.utils.logger import logger

from ._config import ModelConfig

FailureKind = Literal[
    "timeout",
    "connection",
    "congestion",
    "server",
    "authentication",
    "invalid_request",
]

T = TypeVar("T")


@dataclass
class LLMRequestError(RuntimeError):
    message: str
    kind: FailureKind
    status_code: int | None = None
    retry_after: float | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message


@dataclass
class _CongestionState:
    blocked_until: float = 0.0


class RequestRetry(Generic[T]):
    def __init__(self, config: ModelConfig) -> None:
        self.config = config
        self.key = (config.provider, config.api_host, config.model_name)

    async def run(self, operation: Callable[[], Awaitable[T]]) -> T:
        """执行一次带分类重试的模型传输。"""
        attempt = 0
        while True:
            attempt += 1
            await _wait_for_congestion(self.key)
            try:
                async with asyncio.timeout(self.config.request_timeout_seconds):
                    return await operation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = classify_request_error(exc)
                max_attempts = (
                    self.config.congestion_retry_attempts if error.kind == "congestion" else self.config.retry_attempts
                )
                if error.kind == "timeout" and self.config.stream_fallback_on_timeout:
                    max_attempts = 1
                if not _is_retryable(error) or attempt >= max_attempts:
                    raise error from exc
                delay = _retry_delay(error, attempt)
                if error.kind == "congestion":
                    _defer_congestion(self.key, delay)
                logger.warning(f"LLM request failed ({error.kind}); retry {attempt + 1}/{max_attempts} in {delay:.2f}s")
                await asyncio.sleep(delay)

    async def stream(
        self,
        operation: Callable[[], Awaitable[AsyncIterator[T]]],
    ) -> AsyncIterator[T]:
        """重试尚未收到首个数据块的流式传输。"""
        attempt = 0
        while True:
            attempt += 1
            await _wait_for_congestion(self.key)
            received = False
            try:
                async with asyncio.timeout(self.config.request_timeout_seconds):
                    response = await operation()
                iterator = response.__aiter__()
                while True:
                    try:
                        async with asyncio.timeout(self.config.request_timeout_seconds):
                            item = await anext(iterator)
                    except StopAsyncIteration:
                        return
                    received = True
                    yield item
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = classify_request_error(exc)
                max_attempts = (
                    self.config.congestion_retry_attempts if error.kind == "congestion" else self.config.retry_attempts
                )
                if received or not _is_retryable(error) or attempt >= max_attempts:
                    raise error from exc
                delay = _retry_delay(error, attempt)
                if error.kind == "congestion":
                    _defer_congestion(self.key, delay)
                logger.warning(
                    f"LLM stream failed before first chunk ({error.kind}); "
                    f"retry {attempt + 1}/{max_attempts} in {delay:.2f}s"
                )
                await asyncio.sleep(delay)


_congestion_states: dict[tuple[str, str, str], _CongestionState] = {}


def classify_request_error(exc: Exception) -> LLMRequestError:
    """将 SDK 异常转换为统一请求异常。"""
    if isinstance(exc, LLMRequestError):
        return exc
    status_code = _status_code(exc)
    retry_after = _retry_after(exc)
    code = str(getattr(exc, "code", ""))
    name = type(exc).__name__.lower()
    text = str(exc)
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timeout" in name:
        kind: FailureKind = "timeout"
    elif status_code in {429, 529} or "throttl" in code.lower() or "rate" in code.lower():
        kind = "congestion"
    elif status_code in {401, 403}:
        kind = "authentication"
    elif status_code is not None and 500 <= status_code < 600:
        kind = "server"
    elif status_code is not None and 400 <= status_code < 500:
        kind = "invalid_request"
    elif "connection" in name or "connect" in name:
        kind = "connection"
    else:
        kind = "invalid_request"
    return LLMRequestError(text or type(exc).__name__, kind, status_code, retry_after)


def error_from_status(
    status_code: int,
    message: str,
    *,
    code: str = "",
    retry_after: float | None = None,
) -> LLMRequestError:
    """根据 HTTP 状态创建统一请求异常。"""
    proxy = RuntimeError(message)
    proxy.status_code = status_code  # type: ignore[attr-defined]
    proxy.code = code  # type: ignore[attr-defined]
    proxy.retry_after = retry_after  # type: ignore[attr-defined]
    return classify_request_error(proxy)


def _is_retryable(error: LLMRequestError) -> bool:
    return error.kind in {"timeout", "connection", "congestion", "server"}


def _retry_delay(error: LLMRequestError, attempt: int) -> float:
    if error.retry_after is not None:
        return max(0.0, min(60.0, error.retry_after))
    ceiling = min(60.0, float(2 ** (attempt - 1)))
    return random.uniform(0.0, ceiling)


def _status_code(exc: Exception) -> int | None:
    value = getattr(exc, "status_code", None)
    if isinstance(value, int):
        return value
    value = getattr(exc, "code", None)
    return value if isinstance(value, int) else None


def _retry_after(exc: Exception) -> float | None:
    value = getattr(exc, "retry_after", None)
    if isinstance(value, (int, float)):
        return float(value)
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        raw = headers.get("retry-after") or headers.get("Retry-After")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    return None


async def _wait_for_congestion(key: tuple[str, str, str]) -> None:
    state = _congestion_states.get(key)
    if state is None:
        return
    delay = state.blocked_until - time.monotonic()
    if delay > 0:
        await asyncio.sleep(delay)


def _defer_congestion(key: tuple[str, str, str], delay: float) -> None:
    state = _congestion_states.setdefault(key, _CongestionState())
    state.blocked_until = max(state.blocked_until, time.monotonic() + delay)
