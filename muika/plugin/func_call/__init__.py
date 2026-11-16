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


def get_tool_list(permission: str | None = None) -> list["Tool"]:
    """组装当前注册工具和已初始化的 MCP 工具对象，不保存请求间缓存。

    只读权限下只暴露只读工具；其他权限暴露全部（执行仍走审批）。
    """
    from muika.plugin.mcp import get_mcp_list

    tools: list["Tool"] = [*get_function_calls().values(), *get_mcp_list()]
    if permission == "read_only":
        return [tool for tool in tools if bool(getattr(tool, "read_only", False))]
    return tools
