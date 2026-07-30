from typing import Any, Callable, Optional

from muika.plugin.func_call import get_function_calls
from muika.utils.logger import logger

handle_mcp_tool: Optional[Callable] = None
"""惰性缓存的 MCP 工具处理器，首次调用时加载。"""


async def function_call_handler(func: str, arguments: dict[str, str] | None = None) -> Any:
    """
    模型 Function Call 请求处理
    """
    arguments = arguments if arguments and arguments != {"dummy_param": ""} else {}

    if func_caller := get_function_calls().get(func):
        logger.info(f"Function call 请求 {func}, 参数: {arguments}")
        result = await func_caller.run(**arguments)
        logger.success(f"Function call 成功，返回: {result}")
        return result

    global handle_mcp_tool
    if handle_mcp_tool is None:
        from muika.plugin.mcp import handle_mcp_tool as _handle_mcp_tool

        handle_mcp_tool = _handle_mcp_tool

    if mcp_result := await handle_mcp_tool(func, arguments):
        logger.success(f"MCP 工具执行成功，返回: {mcp_result}")
        return mcp_result

    return "(Unknown Function)"
