from collections.abc import Sequence
from typing import Any, AsyncGenerator, List, Literal, Union, overload

import dashscope
from dashscope.api_entities.dashscope_response import (
    GenerationResponse,
    MultiModalConversationResponse,
)

from .. import (
    BaseLLM,
    ModelCompletions,
    ModelConfig,
    ModelRequest,
    ModelStreamCompletions,
    Usage,
    register,
)
from .._retry import RequestRetry, error_from_status
from .._schema import ModelMessage, ToolCall
from ..utils.protocol import (
    ToolCallBuffer,
    ToolDelta,
    json_arguments,
    stop_reason,
    tool_payload,
)


def _text_content(content: str | list | None) -> str:
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content)
    return content or ""


@register("dashscope")
class Dashscope(BaseLLM):
    def __init__(self, model_config: ModelConfig) -> None:
        super().__init__(model_config)
        self._require("api_key", "model_name")
        self.api_key = self.config.api_key
        self.model = self.config.model_name
        self.max_tokens = self.config.max_tokens
        self.temperature = self.config.temperature
        self.top_p = self.config.top_p
        self.repetition_penalty = self.config.repetition_penalty
        self.enable_search = self.config.online_search
        self.enable_thinking = self.config.enable_thinking
        self.thinking_budget = self.config.thinking_budget
        self.incremental_output = self.config.incremental_output

        self.extra_headers = {"X-DashScope-Wait-Timeout": "30"}
        if self.config.content_security:
            self.extra_headers["X-DashScope-DataInspection"] = '{"input":"cip","output":"cip"}'

    def __build_multi_messages(self, request: ModelRequest) -> dict:
        """
        构建多模态类型

        此模型加载器支持的多模态类型: `audio` `image`
        """
        multi_contents: List[dict[str, str]] = []

        for item in request.resources:
            if item.type == "audio":
                multi_contents.append({"audio": item.path})

            elif item.type == "image":
                multi_contents.append({"image": item.path})

        user_content = [image_content for image_content in multi_contents]

        if not request.prompt:
            request.prompt = "请描述图像内容"
        user_content.append({"text": request.prompt})

        return {"role": "user", "content": user_content}

    def _build_messages(self, request: ModelRequest) -> List[dict]:
        messages = []

        if request.system:
            messages.append({"role": "system", "content": request.system})

        history = self._normalize_session_turns(request.history)

        for msg in history:
            if msg.role != "user":
                messages.append({"role": "assistant", "content": msg.content})
            else:
                user_msg = (
                    self.__build_multi_messages(ModelRequest(msg.content, resources=msg.resources))
                    if all((self.config.multimodal, msg.resources))
                    else {"role": "user", "content": msg.content}
                )
                messages.append(user_msg)

        user_msg = (
            {"role": "user", "content": request.prompt}
            if not request.resources
            else self.__build_multi_messages(ModelRequest(request.prompt, resources=request.resources))
        )

        messages.append(user_msg)

        return messages

    def _conversation_messages(self, request: ModelRequest, conversation: Sequence[ModelMessage]) -> list:
        messages = self._build_messages(request)
        for item in conversation:
            if item.role == "user" and item.resources:
                messages.append(
                    self.__build_multi_messages(
                        ModelRequest(item.content, resources=[r.to_resource() for r in item.resources])
                    )
                )
                continue
            message: dict = {"role": item.role, "content": item.content}
            if item.reasoning:
                message["reasoning_content"] = item.reasoning
            if item.tool_calls:
                message["tool_calls"] = [tool_payload(call) for call in item.tool_calls]
            if item.role == "tool":
                message["tool_call_id"] = item.tool_call_id
            messages.append(message)
        return messages

    async def request_step(
        self, request: ModelRequest, messages: Sequence[ModelMessage], *, stream: bool
    ) -> AsyncGenerator[ModelStreamCompletions, None]:
        kwargs: dict[str, Any] = {
            "api_key": self.api_key,
            "model": self.model,
            "messages": self._conversation_messages(request, messages),
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "stream": stream,
            "tools": request.tools or None,
            "parallel_tool_calls": True,
            "enable_search": self.enable_search,
            "incremental_output": self.incremental_output,
            "headers": self.extra_headers,
            "result_format": "message",
            "response_format": {"type": "json_object"} if request.format == "json" else None,
        }
        if not self.config.multimodal:
            kwargs.update(
                repetition_penalty=self.repetition_penalty,
                enable_thinking=self.enable_thinking,
                thinking_budget=self.thinking_budget,
            )
        kwargs = {k: v for k, v in kwargs.items() if v is not None}

        async def send_request():
            if self.config.multimodal:
                response = await dashscope.AioMultiModalConversation.call(**kwargs)
            else:
                response = await dashscope.AioGeneration.call(**kwargs)
            if stream:
                return self._validated_stream(response)
            self._validate_response(response)
            return response

        usage = Usage()
        if not stream:
            response = await RequestRetry[Any](self.config).run(send_request)
            choice = response.output["choices"][0]
            raw = choice["message"]
            message = ModelMessage(
                role="assistant",
                content=_text_content(raw.get("content")),
                reasoning=raw.get("reasoning_content") or "",
                tool_calls=[
                    ToolCall(
                        id=call["id"],
                        name=call["function"]["name"],
                        arguments=json_arguments(call["function"]["arguments"]),
                    )
                    for call in raw.get("tool_calls", [])
                ],
            )
            usage = self._usage(response)
            reason = stop_reason(choice.get("finish_reason"), has_tools=bool(message.tool_calls))
            yield ModelStreamCompletions(
                chunk=(f"<think>{message.reasoning}</think>" if message.reasoning else "") + message.content,
                message=message,
                usage=usage,
                stop_reason=reason,
                succeed=reason != "filtered",
            )
            return

        buffer = ToolCallBuffer()
        content = ""
        reasoning = ""
        thinking = False
        finish = None
        async for chunk in RequestRetry[Any](self.config).stream(send_request):
            usage = self._usage(chunk)
            if not chunk.output["choices"]:
                continue
            choice = chunk.output["choices"][0]
            raw = choice["message"]
            finish = choice.get("finish_reason") or finish
            for index, call in enumerate(raw.get("tool_calls") or []):
                delta = ToolDelta.model_validate({"index": index, **call})
                buffer.add(delta, incremental=self.incremental_output)
            thought = raw.get("reasoning_content") or ""
            text = _text_content(raw.get("content"))
            if not self.incremental_output:
                thought, reasoning = thought[len(reasoning) :], thought
                text, content = text[len(content) :], text
            else:
                reasoning += thought
                content += text
            if thought:
                yield ModelStreamCompletions(chunk=("" if thinking else "<think>") + thought)
                thinking = True
            if text:
                yield ModelStreamCompletions(chunk=("</think>" if thinking else "") + text)
                thinking = False
        if thinking:
            yield ModelStreamCompletions(chunk="</think>")
        message = ModelMessage(role="assistant", content=content, reasoning=reasoning, tool_calls=buffer.finish())
        reason = stop_reason(finish, has_tools=bool(message.tool_calls))
        yield ModelStreamCompletions(message=message, usage=usage, stop_reason=reason, succeed=reason != "filtered")

    @staticmethod
    def _validate_response(response: GenerationResponse | MultiModalConversationResponse) -> None:
        if response.status_code != 200:
            raise error_from_status(response.status_code, str(response.message), code=str(response.code))

    async def _validated_stream(self, response):
        async for chunk in response:
            self._validate_response(chunk)
            yield chunk

    @staticmethod
    def _usage(response: GenerationResponse | MultiModalConversationResponse) -> Usage:
        details = response.usage.get("prompt_tokens_details") or {}
        return Usage(
            input_tokens=response.usage.get("input_tokens", 0),
            output_tokens=response.usage.get("output_tokens", 0),
            cached_tokens=details.get("cached_tokens", 0),
        )

    @overload
    async def ask(self, request: ModelRequest, *, stream: Literal[False] = False) -> ModelCompletions: ...

    @overload
    async def ask(
        self, request: ModelRequest, *, stream: Literal[True] = True
    ) -> AsyncGenerator[ModelStreamCompletions, None]: ...

    async def ask(
        self, request: ModelRequest, *, stream: bool = False
    ) -> Union[ModelCompletions, AsyncGenerator[ModelStreamCompletions, None]]:
        return await self.run_conversation(request, stream=stream)
