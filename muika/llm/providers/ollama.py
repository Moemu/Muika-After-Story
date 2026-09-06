import json
from collections.abc import Sequence
from typing import Any, AsyncGenerator, Literal, Union, overload
from uuid import uuid4

import ollama
from ollama import ResponseError
from pydantic import TypeAdapter

from muika.utils.logger import logger

from .. import (
    BaseLLM,
    ModelCompletions,
    ModelConfig,
    ModelRequest,
    ModelStreamCompletions,
    Usage,
    register,
)
from .._retry import RequestRetry
from .._schema import ModelMessage, ToolCall
from ..utils.images import get_file_base64
from ..utils.protocol import json_arguments, stop_reason


@register("ollama")
class Ollama(BaseLLM):
    """
    使用 Ollama 模型服务调用模型
    """

    def __init__(self, model_config: ModelConfig) -> None:
        super().__init__(model_config)
        self._require("model_name")
        self.model = self.config.model_name
        self.host = self.config.api_host if self.config.api_host else "http://localhost:11434"
        self.top_k = self.config.top_k
        self.top_p = self.config.top_p
        self.temperature = self.config.temperature
        self.repeat_penalty = self.config.repetition_penalty or 1
        self.presence_penalty = self.config.presence_penalty or 0
        self.frequency_penalty = self.config.frequency_penalty or 1
        self.stream = self.config.stream

        try:
            self.client = ollama.AsyncClient(host=self.host)
            self.is_running = True
        except (ResponseError, ConnectionError) as e:
            text = f"加载 Ollama 加载器时发生错误： {e}"
            logger.error(text)
            raise RuntimeError(text) from e

    def __build_multi_messages(self, request: ModelRequest) -> dict:
        """
        构建多模态类型

        当前模型加载器支持的多模态类型: `image`
        """
        images = []

        for resource in request.resources:
            if resource.path is None:
                continue
            image_base64 = get_file_base64(local_path=resource.path)
            images.append(image_base64)

        message = {"role": "user", "content": request.prompt, "images": images}

        return message

    def _build_messages(self, request: ModelRequest) -> list:
        messages = []

        if request.system:
            messages.append({"role": "system", "content": request.system})

        history = self._normalize_session_turns(request.history)

        for item in history:
            if item.role == "user":
                messages.append(self.__build_multi_messages(ModelRequest(item.content, resources=item.resources)))
            else:
                messages.append({"role": "assistant", "content": item.content})

        message = self.__build_multi_messages(request)

        messages.append(message)

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
                message["thinking"] = item.reasoning
            if item.tool_calls:
                message["tool_calls"] = [
                    {"function": {"name": call.name, "arguments": json.loads(call.arguments)}}
                    for call in item.tool_calls
                ]
            if item.role == "tool":
                message["tool_name"] = item.name
            messages.append(message)
        return messages

    async def request_step(
        self, request: ModelRequest, messages: Sequence[ModelMessage], *, stream: bool
    ) -> AsyncGenerator[ModelStreamCompletions, None]:
        response_format = None
        if request.format == "json":
            response_format = (
                request.json_schema.json_schema()
                if isinstance(request.json_schema, TypeAdapter)
                else request.json_schema.model_json_schema() if request.json_schema else "json"
            )

        async def send_request():
            return await self.client.chat(
                model=self.model,
                messages=self._conversation_messages(request, messages),
                tools=request.tools,
                stream=stream,
                format=response_format,
                options={
                    "temperature": self.temperature,
                    "top_p": self.top_p,
                    "top_k": self.top_k,
                    "repeat_penalty": self.repeat_penalty,
                    "presence_penalty": self.presence_penalty,
                    "frequency_penalty": self.frequency_penalty,
                },
            )

        async def responses():
            if stream:
                async for chunk in RequestRetry[Any](self.config).stream(send_request):
                    yield chunk
            else:
                yield await RequestRetry[Any](self.config).run(send_request)

        message = ModelMessage(role="assistant")
        calls: list[ToolCall] = []
        usage = Usage()
        thinking = False
        finish = None
        async for response in responses():
            raw = response.message.model_dump(exclude_none=True)
            text = raw.get("content", "")
            thought = raw.get("thinking", "")
            message.content += text
            message.reasoning += thought
            for call in raw.get("tool_calls", []):
                function = call["function"]
                calls.append(
                    ToolCall(
                        id=f"call_{uuid4().hex}",
                        name=function["name"],
                        arguments=json_arguments(function["arguments"]),
                    )
                )
            if thought:
                yield ModelStreamCompletions(chunk=("" if thinking else "<think>") + thought)
                thinking = True
            if text:
                yield ModelStreamCompletions(chunk=("</think>" if thinking else "") + text)
                thinking = False
            if response.done:
                finish = response.done_reason
                usage.input_tokens = response.prompt_eval_count or 0
                usage.output_tokens = response.eval_count or 0
        if thinking:
            yield ModelStreamCompletions(chunk="</think>")
        message.tool_calls = calls
        yield ModelStreamCompletions(
            message=message, usage=usage, stop_reason=stop_reason(finish, has_tools=bool(message.tool_calls))
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
