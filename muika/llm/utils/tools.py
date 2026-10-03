import json
from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter
from typing import Any, Callable, Literal, Optional

from muika.llm._schema import ToolCall, ToolResult
from muika.plugin.func_call import get_function_calls
from muika.plugin.func_call.caller import FunctionCallValidationError
from muika.utils.logger import logger

handle_mcp_tool: Optional[Callable] = None
"""惰性缓存的 MCP 工具处理器，首次调用时加载。"""


class ToolError(str):
    """保留字符串接口，同时明确表示工具操作失败。"""

    outcome: Literal["completed", "not_executed", "unknown"] = "completed"

    def __new__(
        cls, text: str, *, outcome: Literal["completed", "not_executed", "unknown"] = "completed"
    ) -> "ToolError":
        value = super().__new__(cls, text)
        value.outcome = outcome
        return value


ToolRouter = Callable[[ToolCall], Awaitable[ToolResult]]
_tool_router: ContextVar[ToolRouter | None] = ContextVar("tool_router", default=None)


@contextmanager
def route_tools(router: ToolRouter | None) -> Iterator[None]:
    """让活动运行时拥有动作路由，执行端关闭路由以调用本地工具。"""
    token = _tool_router.set(router)
    try:
        yield
    finally:
        _tool_router.reset(token)


async def dispatch_tool(call: ToolCall) -> ToolResult:
    """解析原始参数并返回结构化工具结果。"""
    router = _tool_router.get()
    if router is not None:
        return await router(call)
    try:
        arguments = json.loads(call.arguments)
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        return ToolResult(text=f"Invalid arguments for {call.name}: {exc}. Correct the JSON and retry.", is_error=True)
    started = perf_counter()
    try:
        result = await function_call_handler(call.name, arguments)
    finally:
        logger.debug(f"[Tool] end | name={call.name} call={call.id} seconds={perf_counter() - started:.3f}")
    if isinstance(result, ToolResult):
        return result
    return ToolResult(
        text=result if isinstance(result, str) else str(result),
        is_error=isinstance(result, ToolError),
        outcome=result.outcome if isinstance(result, ToolError) else "completed",
    )


async def function_call_handler(func: str, arguments: dict[str, Any] | None = None) -> Any:
    """
    模型 Function Call 请求处理
    """
    arguments = arguments if arguments and arguments != {"dummy_param": ""} else {}

    if func_caller := get_function_calls().get(func):
        logger.debug(f"Function call 请求 {func}, 参数: {arguments}")
        try:
            result = await func_caller.run(**arguments)
        except FunctionCallValidationError as exc:
            logger.warning(f"Function call {func} refused: {exc}")
            return ToolError(
                f"Tool error ({func}): FunctionCallValidationError: {exc}. Correct the arguments and retry.",
                outcome="not_executed",
            )
        except Exception as exc:
            logger.warning(f"Function call {func} failed: {type(exc).__name__}: {exc}")
            return ToolError(
                f"Tool error ({func}): {type(exc).__name__}: {exc}. Verify its outcome before another action.",
                outcome="not_executed" if func_caller.read_only else "unknown",
            )
        result_text = result if isinstance(result, str) else str(result)
        log = f"{func} -> {result_text if len(result_text) < 50 else f'Length: {len(result_text)}'}"
        if isinstance(result, ToolError) or isinstance(result, ToolResult) and result.is_error:
            logger.warning(log)
        else:
            logger.debug(log)
        return result

    global handle_mcp_tool
    try:
        if handle_mcp_tool is None:
            from muika.plugin.mcp import handle_mcp_tool as _handle_mcp_tool

            handle_mcp_tool = _handle_mcp_tool

        mcp_result = await handle_mcp_tool(func, arguments)
    except Exception as exc:
        logger.warning(f"MCP tool {func} failed: {type(exc).__name__}: {exc}")
        return ToolError(
            f"Tool error ({func}): {type(exc).__name__}: {exc}. Verify its outcome before another action.",
            outcome="unknown",
        )

    if mcp_result:
        if isinstance(mcp_result, ToolError) or isinstance(mcp_result, ToolResult) and mcp_result.is_error:
            logger.warning(f"MCP tool {func} failed: {mcp_result}")
        else:
            logger.debug(f"MCP tool {func} completed")
        return mcp_result

    return ToolError(f"Unknown function: {func}. Refresh the available tools before continuing.")
