from __future__ import annotations

import asyncio
from functools import wraps
from typing import TYPE_CHECKING, AsyncGenerator, Awaitable, Callable, TypeAlias, Union

from muika.config import get_name_from_config
from muika.database.crud import UsageORM
from muika.database.db import get_session
from muika.plugin.loader import _get_caller_plugin_name

from ._schema import (
    EmbeddingsBatchResult,
    ModelCompletions,
    ModelRequest,
    ModelStreamCompletions,
    Usage,
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
        model_config = get_name_from_config(self.config)

        # Handle non-streaming response
        if isinstance(response, ModelCompletions):
            usg = response.usage

            async with _usage_write_lock:
                async with get_session() as session:
                    await UsageORM.save_usage(
                        session,
                        plugin_name,
                        model_config,
                        input_tokens=usg.input_tokens,
                        output_tokens=usg.output_tokens,
                        cached_tokens=usg.cached_tokens,
                    )

            return response

        # Handle streaming response
        # elif isinstance(response, AsyncGenerator):
        async def generator_wrapper() -> AsyncGenerator[ModelStreamCompletions, None]:
            last_usage = Usage()
            try:
                async for chunk in response:
                    if not chunk.succeed:
                        continue

                    last_usage = chunk.usage
                    yield chunk
            finally:
                async with _usage_write_lock:
                    async with get_session() as session:
                        await UsageORM.save_usage(
                            session,
                            plugin_name,
                            model_config,
                            input_tokens=last_usage.input_tokens,
                            output_tokens=last_usage.output_tokens,
                            cached_tokens=last_usage.cached_tokens,
                        )

        return generator_wrapper()

    return wrapper
