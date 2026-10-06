import json
from time import perf_counter
from typing import Any, Callable, Optional

from muika.llm._schema import ToolCall, ToolResult
from muika.plugin.func_call import get_function_calls
from muika.utils.logger import logger

handle_mcp_tool: Optional[Callable] = None
"""惰性缓存的 MCP 工具处理器，首次调用时加载。"""


class ToolError(str):
    """保留字符串接口，同时明确表示工具操作失败。"""


_TOOL_PREVIEW_CHARS = 120
"""工具结果摘要长度，完整正文留在 ToolResult 里进入上下文。"""


async def dispatch_tool(call: ToolCall) -> ToolResult:
    """解析原始参数并返回结构化工具结果。"""
    try:
        arguments = json.loads(call.arguments)
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(f"[Tool] {call.name} failed | invalid arguments: {exc}")
        return ToolResult(text=f"Invalid arguments for {call.name}: {exc}. Correct the JSON and retry.", is_error=True)
    started = perf_counter()
    result = await function_call_handler(call.name, arguments)
    if isinstance(result, ToolResult):
        text, is_error = result.text, result.is_error
    else:
        text, is_error = str(result), isinstance(result, ToolError)
    preview = text if len(text) <= _TOOL_PREVIEW_CHARS else f"{text[:_TOOL_PREVIEW_CHARS]}...({len(text)} chars)"
    status = "failed" if is_error else "ok"
    logger.log(
        "WARNING" if is_error else "DEBUG",
        f"[Tool] {call.name} {status} {perf_counter() - started:.3f}s | args={arguments} -> {preview!r}",
    )
    logger.info(f"[Tool] {call.name} {status}")
    return result if isinstance(result, ToolResult) else ToolResult(text=text, is_error=is_error)


async def function_call_handler(func: str, arguments: dict[str, Any] | None = None) -> Any:
    """
    模型 Function Call 请求处理
    """
    arguments = arguments if arguments and arguments != {"dummy_param": ""} else {}

    if func_caller := get_function_calls().get(func):
        try:
            return await func_caller.run(**arguments)
        except Exception as exc:
            return ToolError(f"Tool error ({func}): {type(exc).__name__}: {exc}. Correct the arguments and retry.")

    global handle_mcp_tool
    try:
        if handle_mcp_tool is None:
            from muika.plugin.mcp import handle_mcp_tool as _handle_mcp_tool

            handle_mcp_tool = _handle_mcp_tool

        mcp_result = await handle_mcp_tool(func, arguments)
    except Exception as exc:
        return ToolError(f"Tool error ({func}): {type(exc).__name__}: {exc}. Correct the arguments and retry.")

    return mcp_result or ToolError(f"Unknown function: {func}. Refresh the available tools before continuing.")
