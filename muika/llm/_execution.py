"""在模型请求之间顺序执行工具。"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import replace
from time import perf_counter
from typing import TYPE_CHECKING
from uuid import uuid4

from muika.plugin.command import ensure_resource_path
from muika.plugin.func_call.context import ToolContext, get_dependencies
from muika.utils.logger import logger

from ._retry import LLMRequestError
from ._schema import (
    MediaReference,
    ModelCompletions,
    ModelMessage,
    ModelRequest,
    ModelStreamCompletions,
    Tool,
    ToolCall,
    ToolResult,
    Usage,
)
from .context import ContextPreparer, fit_budget, request_tokens
from .utils.tools import dispatch_tool

if TYPE_CHECKING:
    from ._base import BaseLLM


async def _timed_model_step(
    model: BaseLLM, request: ModelRequest, messages: Sequence[ModelMessage], *, stream: bool
) -> AsyncGenerator[ModelStreamCompletions, None]:
    """记录提供者请求的等待和用量，不记录提示或私有思考正文。"""
    request_id = uuid4().hex[:8]
    started = perf_counter()
    first_chunk: float | None = None
    usage = Usage()
    status = "interrupted"
    model_name = model.config.model_name or model.config.provider
    error_reason = ""
    logger.debug(
        f"[Model] request submitted - {request.purpose} | request={request_id} model={model_name} "
        f"messages={len(messages)} estimated_input={request_tokens(request, messages)} stream={stream}"
    )
    try:
        async for chunk in model.request_step(request, messages, stream=stream):
            if first_chunk is None:
                first_chunk = perf_counter() - started
            usage = chunk.usage
            status = chunk.stop_reason if chunk.succeed else "error"
            if not chunk.succeed and not error_reason:
                error_reason = chunk.chunk
            yield chunk
    except Exception as exc:
        status = "error"
        error_reason = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        first = f"{first_chunk:.3f}" if first_chunk is not None else "none"
        detail = f" error={error_reason[:120]}" if status == "error" and error_reason else ""
        secs = perf_counter() - started
        logger.debug(
            f"[Model] request completed - {request.purpose} | request={request_id} seconds={secs:.2f} "
            f"first_chunk_seconds={first} status={status} input_tokens={usage.input_tokens} "
            f"output_tokens={usage.output_tokens} cached_tokens={usage.cached_tokens}{detail}"
        )


async def step(
    model: BaseLLM,
    request: ModelRequest,
    messages: Sequence[ModelMessage],
    *,
    stream: bool,
    prepare_context: ContextPreparer | None = None,
) -> AsyncGenerator[ModelStreamCompletions, None]:
    """执行单步模型请求：预算内准备上下文；超长压缩重试一次，非流式超时降级流式一次。"""

    async def prepare(request: ModelRequest, messages: Sequence[ModelMessage], force: bool):
        started = perf_counter()
        before = request_tokens(request, messages)
        prepared = (
            await prepare_context(request, messages, force)
            if prepare_context is not None
            else await fit_budget(model, request, messages, force=force)
        )
        logger.debug(
            f"[Context] prepared | seconds={perf_counter() - started:.3f} force={force} "
            f"input_before={before} input_after={request_tokens(*prepared)}"
        )
        return prepared

    request, current = await prepare(request, messages, False)
    while True:
        received = False
        try:
            async for chunk in _timed_model_step(model, request, current, stream=stream):
                received = True
                yield chunk
            return
        except LLMRequestError as exc:
            if received:
                raise
            if exc.kind == "context_length":
                smaller, compacted = await prepare(request, current, True)
                if request_tokens(smaller, compacted) >= request_tokens(request, current):
                    raise
                request, current = smaller, compacted
            elif exc.kind == "timeout" and not stream and model.config.stream_fallback_on_timeout:
                stream = True
            else:
                raise


async def execute_call(call: ToolCall, tools: Mapping[str, Tool]) -> ToolResult:
    """执行请求声明的工具并收集本次新增的资源。

    人格任务拦截器只接管共享工具；请求私有工具（如审查读取器）始终直接执行。
    """
    tool = tools.get(call.name)
    if tool is None:
        logger.warning(f"[Tool] {call.name} failed | not declared for this request")
        return ToolResult(
            text=f"Tool {call.name!r} is not declared for this request. Use only the declared tools.",
            is_error=True,
        )
    context = get_dependencies().get(ToolContext)
    if isinstance(context, ToolContext) and context.execute_tool is not None and tool.shared:
        result = await context.execute_tool(call)
        context.resources.extend(ref.to_resource() for ref in result.resources)
        return result
    return await dispatch_call(call, tools)


async def dispatch_call(call: ToolCall, tools: Mapping[str, Tool]) -> ToolResult:
    """派发已取得执行权的动作并收集资源，不重复进入人格拦截器。"""
    context = get_dependencies().get(ToolContext)
    if not isinstance(context, ToolContext):
        return await dispatch_tool(call, tools)
    offset = len(context.resources)
    result = await dispatch_tool(call, tools)
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
    model: BaseLLM, request: ModelRequest, *, stream: bool, max_steps: int = 0
) -> AsyncGenerator[ModelStreamCompletions, None]:
    """保持模型调用的工具循环和累计用量；请求未声明的工具不会执行。

    :param max_steps: 工具循环步数上限；0 表示不限制，超出后按失败结束。
    """
    messages: list[ModelMessage] = []
    tools: Mapping[str, Tool] = {tool.name: tool for tool in request.tools or []}
    total = Usage()
    steps = 0
    while True:
        steps += 1
        if max_steps and steps > max_steps:
            yield ModelStreamCompletions(
                chunk=f"Tool loop did not finish within {max_steps} steps.",
                usage=total,
                succeed=False,
                stop_reason="error",
            )
            return
        completion = ModelCompletions()
        if stream:
            try:
                async for chunk in step(model, request, messages, stream=True):
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
            try:
                completion = await model.collect_stream(step(model, request, messages, stream=model.config.stream))
            except LLMRequestError as exc:
                completion = ModelCompletions(text=str(exc), succeed=False, stop_reason="error")
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
            result = await execute_call(call, tools)
            messages.append(result_message(call, result))
            resources.extend(result.resources)
        if resources:
            messages.append(observation_message(resources, multimodal=model.config.multimodal))
