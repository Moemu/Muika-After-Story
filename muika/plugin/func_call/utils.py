from functools import wraps
from typing import Any

from ._types import ASYNC_FUNCTION_CALL_FUNC, SYNC_FUNCTION_CALL_FUNC


def async_wrap(func: SYNC_FUNCTION_CALL_FUNC) -> ASYNC_FUNCTION_CALL_FUNC:
    """
    装饰器，将同步函数包装为异步函数
    """

    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    return wrapper
