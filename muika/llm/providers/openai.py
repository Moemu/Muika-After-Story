import base64
import wave
from collections.abc import Sequence
from io import BytesIO
from typing import Any, AsyncGenerator, List, Literal, Union, overload

import openai
from openai import NOT_GIVEN, NotGiven
from openai.types.chat import ChatCompletionToolParam
from openai.types.shared_params.response_format_json_schema import (
    JSONSchema,
    ResponseFormatJSONSchema,
)
from pydantic import TypeAdapter

from muika.models import Resource

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
from ..utils.protocol import ToolCallBuffer, ToolDelta, stop_reason, tool_payload


@register("openai")
class Openai(BaseLLM):
    _tools: List[ChatCompletionToolParam]
    modalities: Union[List[Literal["text", "audio"]], NotGiven]

    def __init__(self, model_config: ModelConfig) -> None:
        super().__init__(model_config)
        self._require("api_key", "model_name")
        self.api_key = self.config.api_key
        self.model = self.config.model_name
        self.api_base = self.config.api_host or "https://api.openai.com/v1"
        self.max_tokens = self.config.max_tokens
        self.temperature = self.config.temperature
        self.top_p = self.config.top_p
        self.presence_penalty = self.config.presence_penalty
        self.frequency_penalty = self.config.frequency_penalty
        self.stream = self.config.stream
        self.modalities = [m for m in self.config.modalities if m in {"text", "audio"}] or NOT_GIVEN  # type: ignore
        self.audio = self.config.audio if (self.modalities and self.config.audio) else NOT_GIVEN
        self.extra_body = self.config.extra_body

        self.client = openai.AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.api_base,
            timeout=self.config.request_timeout_seconds,
            max_retries=0,
        )

    def _sampling_kwargs(self) -> dict:
        """
        组装采样相关的可选参数（仅在配置非 None 时传入）

        避免向 OpenAI 兼容端点显式传递 None，防止部分服务拒绝未知空参数。
        """
        kwargs = {"top_p": self.top_p}
        if self.presence_penalty is not None:
            kwargs["presence_penalty"] = self.presence_penalty
        if self.frequency_penalty is not None:
            kwargs["frequency_penalty"] = self.frequency_penalty
        return kwargs

    def __build_multi_messages(self, request: ModelRequest) -> dict:
        """
        构建多模态类型

        此模型加载器支持的多模态类型: `audio` `image` `video` `file`
        """
        user_content: List[dict] = [{"type": "text", "text": request.prompt}]

        for resource in request.resources:
            if resource.path is None:
                continue

            elif resource.type == "audio":
                file_format = resource.path.split(".")[-1]
                file_data = f"data:;base64,{get_file_base64(local_path=resource.path)}"
                user_content.append({"type": "input_audio", "input_audio": {"data": file_data, "format": file_format}})

            elif resource.type == "image":
                file_format = resource.path.split(".")[-1]
                file_data = f"data:image/{file_format};base64,{get_file_base64(local_path=resource.path)}"
                user_content.append({"type": "image_url", "image_url": {"url": file_data}})

            elif resource.type == "video":
                file_format = resource.path.split(".")[-1]
                file_data = f"data:;base64,{get_file_base64(local_path=resource.path)}"
                user_content.append({"type": "video_url", "video_url": {"url": file_data}})

            elif resource.type == "file":
                file_format = resource.path.split(".")[-1]
                file_data = f"data:;base64,{get_file_base64(local_path=resource.path)}"
                user_content.append({"type": "file", "file": {"file_data": file_data}})

        return {"role": "user", "content": user_content}

    def _build_messages(self, request: ModelRequest) -> list:
        messages = []

        if request.system:
            messages.append({"role": "system", "content": request.system})

        if request.history:
            history = self._normalize_session_turns(request.history)
            for item in history:
                if item.role == "user":
                    user_content = (
                        {"role": "user", "content": item.content}
                        if not all([item.resources, self.config.multimodal])
                        else self.__build_multi_messages(ModelRequest(item.content, resources=item.resources))
                    )
                    messages.append(user_content)
                else:
                    messages.append({"role": "assistant", "content": item.content})

        user_content = (
            {"role": "user", "content": request.prompt}
            if not request.resources
            else self.__build_multi_messages(request)
        )

        messages.append(user_content)

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

    def _response_format(self, request: ModelRequest):
        if request.format != "json":
            return NOT_GIVEN
        if request.json_schema is None:
            return {"type": "json_object"}
        schema = (
            request.json_schema.json_schema()
            if isinstance(request.json_schema, TypeAdapter)
            else request.json_schema.model_json_schema()
        )
        return ResponseFormatJSONSchema(
            type="json_schema",
            json_schema=JSONSchema(
                name=request.json_schema.__name__ if isinstance(request.json_schema, type) else "response",
                schema=schema,
                strict=True,
            ),
        )

    async def request_step(
        self, request: ModelRequest, messages: Sequence[ModelMessage], *, stream: bool
    ) -> AsyncGenerator[ModelStreamCompletions, None]:
        kwargs = dict(
            audio=self.audio,
            model=self.model,
            modalities=self.modalities,
            messages=self._conversation_messages(request, messages),
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            stream=stream,
            tools=request.tools or NOT_GIVEN,
            extra_body=self.extra_body,
            response_format=self._response_format(request),
            **self._sampling_kwargs(),
        )
        if stream:
            kwargs["stream_options"] = {"include_usage": True}

        async def send_request():
            return await self.client.chat.completions.create(**kwargs)

        usage = Usage()
        if not stream:
            response = await RequestRetry[Any](self.config).run(send_request)
            choice = response.choices[0]
            raw = choice.message.model_dump(exclude_none=True)
            message = ModelMessage(
                role="assistant",
                content=raw.get("content", ""),
                reasoning=raw.get("reasoning_content", ""),
                tool_calls=[
                    ToolCall(id=call["id"], name=call["function"]["name"], arguments=call["function"]["arguments"])
                    for call in raw.get("tool_calls", [])
                ],
            )
            if response.usage:
                usage.input_tokens = response.usage.prompt_tokens or 0
                usage.output_tokens = response.usage.completion_tokens or 0
                details = response.usage.prompt_tokens_details
                usage.cached_tokens = details.cached_tokens or 0 if details else 0
            resources = []
            if choice.message.audio:
                resources.append(Resource(type="audio", raw=base64.b64decode(choice.message.audio.data)))
            reason = stop_reason(choice.finish_reason, has_tools=bool(message.tool_calls))
            yield ModelStreamCompletions(
                chunk=(f"<think>{message.reasoning}</think>" if message.reasoning else "") + message.content,
                message=message,
                usage=usage,
                resources=resources,
                stop_reason=reason,
                succeed=reason != "filtered",
            )
            return

        buffer = ToolCallBuffer()
        content = ""
        reasoning = ""
        thinking = False
        finish = None
        audio_data = ""
        async for chunk in RequestRetry[Any](self.config).stream(send_request):
            if chunk.usage:
                usage.input_tokens = chunk.usage.prompt_tokens or 0
                usage.output_tokens = chunk.usage.completion_tokens or 0
                details = chunk.usage.prompt_tokens_details
                usage.cached_tokens = details.cached_tokens or 0 if details else 0
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            finish = choice.finish_reason or finish
            raw = choice.delta.model_dump(exclude_none=True)
            for call in raw.get("tool_calls", []):
                buffer.add(ToolDelta.model_validate(call))
            thought = raw.get("reasoning_content", "")
            text = raw.get("content", "")
            if thought:
                reasoning += thought
                yield ModelStreamCompletions(chunk=("" if thinking else "<think>") + thought)
                thinking = True
            if text:
                content += text
                yield ModelStreamCompletions(chunk=("</think>" if thinking else "") + text)
                thinking = False
            audio = raw.get("audio", {})
            audio_data += audio.get("data", "")
            if audio.get("transcript"):
                content += audio["transcript"]
                yield ModelStreamCompletions(chunk=audio["transcript"])
        if thinking:
            yield ModelStreamCompletions(chunk="</think>")
        message = ModelMessage(role="assistant", content=content, reasoning=reasoning, tool_calls=buffer.finish())
        reason = stop_reason(finish, has_tools=bool(message.tool_calls))
        resources = []
        if audio_data:
            output = BytesIO()
            with wave.open(output, "wb") as audio_file:
                audio_file.setnchannels(1)
                audio_file.setsampwidth(2)
                audio_file.setframerate(24000)
                audio_file.writeframes(base64.b64decode(audio_data))
            resources.append(Resource(type="audio", raw=output.getvalue()))
        yield ModelStreamCompletions(
            message=message, usage=usage, resources=resources, stop_reason=reason, succeed=reason != "filtered"
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
