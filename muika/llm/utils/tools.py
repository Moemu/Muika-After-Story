import json
from collections.abc import Mapping
from time import perf_counter
from typing import TYPE_CHECKING

from muika.llm._schema import ToolCall, ToolResult
from muika.utils.logger import logger

if TYPE_CHECKING:
    from muika.llm._schema import Tool

_TOOL_PREVIEW_CHARS = 120
"""工具结果摘要长度，完整正文留在 ToolResult 里进入上下文。"""


class ToolError(str):
    """保留字符串接口，同时明确表示工具操作失败。"""


async def dispatch_tool(call: ToolCall, tools: Mapping[str, "Tool"]) -> ToolResult:
    """执行请求声明的工具对象：解析参数、计时并包装失败；未声明的调用不会执行。"""
    tool = tools.get(call.name)
    if tool is None:
        logger.warning(f"[Tool] {call.name} failed | not declared for this request")
        return ToolResult(
            text=f"Tool {call.name!r} is not declared for this request. Use only the declared tools.",
            is_error=True,
        )
    try:
        arguments = json.loads(call.arguments)
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(f"[Tool] {call.name} failed | invalid arguments: {exc}")
        return ToolResult(text=f"Invalid arguments for {call.name}: {exc}. Correct the JSON and retry.", is_error=True)
    started = perf_counter()
    try:
        result = await tool.run(**arguments)
    except Exception as exc:
        result = ToolError(f"Tool error ({call.name}): {type(exc).__name__}: {exc}. Correct the arguments and retry.")
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
