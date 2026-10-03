"""
MAS Function Call Plugin
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from .caller import (
    get_function_calls,
    get_function_list,
    on_function_call,
)

__all__ = ["get_function_calls", "get_function_list", "on_function_call", "get_tool_list"]
_tool_catalog: ContextVar[Callable[[], list[dict[str, Any]]] | None] = ContextVar("tool_catalog", default=None)
_read_only_tools: ContextVar[Callable[[str], bool] | None] = ContextVar("read_only_tools", default=None)


@contextmanager
def tool_catalog(provider: Callable[[], list[dict[str, Any]]], read_only: Callable[[str], bool]) -> Iterator[None]:
    tools_token = _tool_catalog.set(provider)
    retry_token = _read_only_tools.set(read_only)
    try:
        yield
    finally:
        _read_only_tools.reset(retry_token)
        _tool_catalog.reset(tools_token)


def is_read_only_tool(name: str) -> bool:
    if provider := _read_only_tools.get():
        return provider(name)
    caller = get_function_calls().get(name)
    return bool(caller and caller.read_only)


def get_tool_list() -> list[dict[str, Any]]:
    """组装当前注册工具和已初始化的 MCP 工具，不保存请求间缓存。"""
    if provider := _tool_catalog.get():
        return provider()
    from muika.plugin.mcp import get_mcp_list

    return get_function_list() + get_mcp_list()
