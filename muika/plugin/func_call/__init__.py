"""
MAS Function Call Plugin
"""

from .caller import (
    get_function_calls,
    get_function_list,
    on_function_call,
)

__all__ = ["get_function_calls", "get_function_list", "on_function_call", "get_tool_list"]


def get_tool_list() -> list[dict[str, dict]]:
    """组装当前注册工具和已初始化的 MCP 工具，不保存请求间缓存。"""
    from muika.plugin.mcp import get_mcp_list

    return get_function_list() + get_mcp_list()
