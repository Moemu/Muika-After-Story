import json
from typing import Any, Callable, Optional

from muika.llm._schema import ToolCall, ToolResult
from muika.plugin.func_call import get_function_calls
from muika.utils.logger import logger

handle_mcp_tool: Optional[Callable] = None
"""惰性缓存的 MCP 工具处理器，首次调用时加载。"""


class ToolError(str):
    """保留字符串接口，同时明确表示工具操作失败。"""


async def dispatch_tool(call: ToolCall) -> ToolResult:
    """解析原始参数并返回结构化工具结果。"""
    try:
        arguments = json.loads(call.arguments)
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        return ToolResult(text=f"Invalid arguments for {call.name}: {exc}. Correct the JSON and retry.", is_error=True)
    result = await function_call_handler(call.name, arguments)
    if isinstance(result, ToolResult):
        return result
    return ToolResult(text=result if isinstance(result, str) else str(result), is_error=isinstance(result, ToolError))


async def function_call_handler(func: str, arguments: dict[str, Any] | None = None) -> Any:
    """
    模型 Function Call 请求处理
    """
    arguments = arguments if arguments and arguments != {"dummy_param": ""} else {}

    if func_caller := get_function_calls().get(func):
        logger.info(f"Function call 请求 {func}, 参数: {arguments}")
        try:
            result = await func_caller.run(**arguments)
        except Exception as exc:
            logger.warning(f"Function call {func} failed: {type(exc).__name__}: {exc}")
            return ToolError(f"Tool error ({func}): {type(exc).__name__}: {exc}. Correct the arguments and retry.")
        result_text = result if isinstance(result, str) else str(result)
        log = f"{func} -> {result_text if len(result_text) < 50 else f'Length: {len(result_text)}'}"
        if isinstance(result, ToolError) or isinstance(result, ToolResult) and result.is_error:
            logger.warning(log)
        else:
            logger.success(log)
        return result

    global handle_mcp_tool
    try:
        if handle_mcp_tool is None:
            from muika.plugin.mcp import handle_mcp_tool as _handle_mcp_tool

            handle_mcp_tool = _handle_mcp_tool

        mcp_result = await handle_mcp_tool(func, arguments)
    except Exception as exc:
        logger.warning(f"MCP tool {func} failed: {type(exc).__name__}: {exc}")
        return ToolError(f"Tool error ({func}): {type(exc).__name__}: {exc}. Correct the arguments and retry.")

    if mcp_result:
        if isinstance(mcp_result, ToolError) or isinstance(mcp_result, ToolResult) and mcp_result.is_error:
            logger.warning(f"MCP tool {func} failed: {mcp_result}")
        else:
            logger.success(f"MCP tool {func} completed")
        return mcp_result

    return ToolError(f"Unknown function: {func}. Refresh the available tools before continuing.")
