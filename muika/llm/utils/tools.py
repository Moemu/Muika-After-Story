from typing import Any

from nonebot import logger

from muika.plugin.func_call import get_function_calls
from muika.plugin.mcp import handle_mcp_tool


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

    if mcp_result := await handle_mcp_tool(func, arguments):
        logger.success(f"MCP 工具执行成功，返回: {mcp_result}")
        return mcp_result

    return "(Unknown Function)"
