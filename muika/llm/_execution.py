"""在模型请求之间顺序执行工具。"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from muika.plugin.command import ensure_resource_path
from muika.plugin.func_call.context import ToolContext, get_dependencies

from ._retry import LLMRequestError
from ._schema import (
    MediaReference,
    ModelCompletions,
    ModelMessage,
    ModelRequest,
    ModelStreamCompletions,
    ToolCall,
    ToolResult,
    Usage,
)
from .utils.tools import dispatch_tool

if TYPE_CHECKING:
    from ._base import BaseLLM


async def collect_step(model: BaseLLM, request: ModelRequest, messages: Sequence[ModelMessage]) -> ModelCompletions:
    """收集单步响应，仅在尚未执行工具时切换传输方式。"""
    try:
        try:
            return await model._collect_stream(model.request_step(request, messages, stream=model.config.stream))
        except LLMRequestError as exc:
            if model.config.stream or exc.kind != "timeout" or not model.config.stream_fallback_on_timeout:
                raise
            return await model._collect_stream(model.request_step(request, messages, stream=True))
    except LLMRequestError as exc:
        return ModelCompletions(text=str(exc), succeed=False, stop_reason="error")


async def execute_call(call: ToolCall) -> ToolResult:
    """执行工具并收集本次新增的资源。"""
    context = get_dependencies().get(ToolContext)
    if isinstance(context, ToolContext) and context.execute_tool is not None:
        result = await context.execute_tool(call)
        context.resources.extend(ref.to_resource() for ref in result.resources)
        return result
    return await dispatch_call(call)


async def dispatch_call(call: ToolCall) -> ToolResult:
    """派发已取得执行权的动作，不重复进入检查点拦截器。"""
    context = get_dependencies().get(ToolContext)
    offset = len(context.resources) if isinstance(context, ToolContext) else 0
    result = await dispatch_tool(call)
    if isinstance(context, ToolContext):
        for resource in context.resources[offset:]:
            await ensure_resource_path(resource)
            if resource.path:
                ref = MediaReference(type=resource.type, path=resource.path, mimetype=resource.mimetype)
                if ref not in result.resources:
                    result.resources.append(ref)
    return result


def result_message(call: ToolCall, result: ToolResult) -> ModelMessage:
    """将工具状态和正文编入对应调用的响应。"""
    return ModelMessage(
        role="tool",
        tool_call_id=call.id,
        name=call.name,
        content=("Tool failed: " if result.is_error else "") + result.text,
    )


def observation_message(resources: list[MediaReference], *, multimodal: bool) -> ModelMessage:
    """在完整工具响应之后提供可见资源或能力限制。"""
    return ModelMessage(
        role="user",
        content=(
            "Tool observations. Inspect these resources before judging the result."
            if multimodal
            else "Visual verification is unavailable: this model has multimodal input disabled. "
            + "Resources: "
            + ", ".join(r.path for r in resources)
        ),
        resources=resources if multimodal else [],
    )


async def run_conversation(
    model: BaseLLM, request: ModelRequest, *, stream: bool
) -> AsyncGenerator[ModelStreamCompletions, None]:
    """保持普通模型调用的工具循环和累计用量。"""
    messages: list[ModelMessage] = []
    total = Usage()
    while True:
        completion = ModelCompletions()
        if stream:
            try:
                async for chunk in model.request_step(request, messages, stream=True):
                    completion.text += chunk.chunk
                    completion.usage = chunk.usage
                    completion.message = chunk.message or completion.message
                    completion.stop_reason = chunk.stop_reason
                    completion.succeed = completion.succeed and chunk.succeed
                    if chunk.resources:
                        completion.resources.extend(chunk.resources)
                    yield replace(
                        chunk,
                        usage=Usage(
                            total.input_tokens + chunk.usage.input_tokens,
                            total.output_tokens + chunk.usage.output_tokens,
                            total.cached_tokens + chunk.usage.cached_tokens,
                        ),
                    )
            except LLMRequestError as exc:
                yield ModelStreamCompletions(chunk=str(exc), succeed=False, stop_reason="error", usage=total)
                return
        else:
            completion = await collect_step(model, request, messages)
        total.input_tokens += completion.usage.input_tokens
        total.output_tokens += completion.usage.output_tokens
        total.cached_tokens += completion.usage.cached_tokens
        message = completion.message
        if not completion.succeed or not message or not message.tool_calls:
            if not stream:
                yield ModelStreamCompletions(
                    chunk=completion.text,
                    usage=total,
                    resources=completion.resources,
                    succeed=completion.succeed,
                    message=message,
                    stop_reason=completion.stop_reason,
                )
            return
        if completion.stop_reason in {"length", "filtered", "error"}:
            yield ModelStreamCompletions(
                chunk="Model stopped before completing its tool calls.",
                usage=total,
                succeed=False,
                stop_reason=completion.stop_reason,
            )
            return
        messages.append(message)
        resources: list[MediaReference] = []
        for call in message.tool_calls:
            result = await execute_call(call)
            messages.append(result_message(call, result))
            resources.extend(result.resources)
        if resources:
            messages.append(observation_message(resources, multimodal=model.config.multimodal))
