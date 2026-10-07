"""
MAS Function Call Plugin
"""

from typing import TYPE_CHECKING

from .caller import (
    get_function_calls,
    on_function_call,
)

if TYPE_CHECKING:
    from muika.llm._schema import Tool

__all__ = ["get_function_calls", "on_function_call", "get_tool_list"]


def get_tool_list() -> list["Tool"]:
    """组装当前注册工具和已初始化的 MCP 工具对象，不保存请求间缓存。"""
    from muika.plugin.mcp import get_mcp_list

    return [*get_function_calls().values(), *get_mcp_list()]
