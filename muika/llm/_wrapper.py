from __future__ import annotations

import asyncio
from functools import wraps
from typing import TYPE_CHECKING, AsyncGenerator, Awaitable, Callable, TypeAlias, Union

from muika.database.crud import UsageORM
from muika.database.db import get_session
from muika.plugin.loader import _get_caller_plugin_name

from ._schema import (
    EmbeddingsBatchResult,
    ModelCompletions,
    ModelRequest,
    ModelStreamCompletions,
)

if TYPE_CHECKING:
    from ._base import BaseLLM

ASK_FUNC: TypeAlias = Callable[..., Awaitable[Union[ModelCompletions, AsyncGenerator[ModelStreamCompletions, None]]]]
EMBED_FUNC: TypeAlias = Callable[..., Awaitable[EmbeddingsBatchResult]]

_usage_write_lock = asyncio.Lock()


def record_plugin_usage(func: ASK_FUNC):
    """
    记录插件用量的装饰器
    """

    @wraps(func)
    async def wrapper(self: "BaseLLM", request: ModelRequest, *, stream: bool = False):
        plugin_name = _get_caller_plugin_name() or "muika"

        # Call the original 'ask' method
        response = await func(self, request, stream=stream)

        # Handle non-streaming response
        if isinstance(response, ModelCompletions):
            total_usage = response.usage if response.usage > 0 else 0

            async with _usage_write_lock:
                async with get_session() as session:
                    await UsageORM.save_usage(session, plugin_name, total_usage)

            return response

        # Handle streaming response
        # elif isinstance(response, AsyncGenerator):
        async def generator_wrapper() -> AsyncGenerator[ModelStreamCompletions, None]:
            total_usage = 0
            try:
                async for chunk in response:
                    if not chunk.succeed:
                        continue

                    total_usage = chunk.usage if chunk.usage > 0 else 0
                    yield chunk
            finally:
                async with _usage_write_lock:
                    async with get_session() as session:
                        await UsageORM.save_usage(session, plugin_name, total_usage)

        return generator_wrapper()

    return wrapper
