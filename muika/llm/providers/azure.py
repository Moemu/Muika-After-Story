import os
from collections.abc import Sequence
from typing import Any, AsyncGenerator, List, Literal, Union, overload

from azure.ai.inference.aio import ChatCompletionsClient
from azure.ai.inference.models import (
    AssistantMessage,
    AudioContentItem,
    ChatCompletionsToolCall,
    ChatCompletionsToolDefinition,
    ChatRequestMessage,
    ContentItem,
    FunctionCall,
    FunctionDefinition,
    ImageContentItem,
    ImageDetailLevel,
    ImageUrl,
    InputAudio,
    JsonSchemaFormat,
    SystemMessage,
    TextContentItem,
    ToolMessage,
    UserMessage,
)
from azure.core.credentials import AzureKeyCredential
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
from .._schema import ModelMessage
from ..utils.protocol import ToolCallBuffer, ToolDelta, stop_reason


@register("azure")
class Azure(BaseLLM):
    def __init__(self, model_config: ModelConfig) -> None:
        super().__init__(model_config)
        self._require("model_name")
        self.model_name = self.config.model_name
        self.max_tokens = self.config.max_tokens
        self.temperature = self.config.temperature
        self.top_p = self.config.top_p
        self.frequency_penalty = self.config.frequency_penalty
        self.presence_penalty = self.config.presence_penalty
        self.token = os.getenv("AZURE_API_KEY", self.config.api_key)
        self.endpoint = self.config.api_host if self.config.api_host else "https://models.inference.ai.azure.com"

        logger.warning(
            "对 Azure SDK 的支持将于 1.6 或更高的版本结束，考虑配置 OpenAI 兼容端口或其他模型服务提供商",
            DeprecationWarning,
        )

    def __build_multi_messages(self, request: ModelRequest) -> UserMessage:
        """
        构建多模态类型

        此模型加载器支持的多模态类型: `audio` `image`
        """
        multi_content_items: List[ContentItem] = []

        for resource in request.resources:
            if resource.path is None:
                continue
            elif resource.type == "audio":
                multi_content_items.append(
                    AudioContentItem(
                        input_audio=InputAudio.load(audio_file=resource.path, audio_format=resource.path.split(".")[-1])
                    )
                )
            elif resource.type == "image":
                multi_content_items.append(
                    ImageContentItem(
                        image_url=ImageUrl.load(
                            image_file=resource.path,
                            image_format=resource.path.split(".")[-1],
                            detail=ImageDetailLevel.AUTO,
                        )
                    )
                )

        content = [TextContentItem(text=request.prompt)] + multi_content_items

        return UserMessage(content=content)

    def __build_tools_definition(self, tools: List[dict]) -> List[ChatCompletionsToolDefinition]:
        tool_definitions = []

        for tool in tools:
            tool_definition = ChatCompletionsToolDefinition(
                function=FunctionDefinition(
                    name=tool["function"]["name"],
                    description=tool["function"]["description"],
                    parameters=tool["function"]["parameters"],
                )
            )
            tool_definitions.append(tool_definition)

        return tool_definitions

    def _build_messages(self, request: ModelRequest) -> List[ChatRequestMessage]:
        messages: List[ChatRequestMessage] = []

        if request.system:
            messages.append(SystemMessage(request.system))

        history = self._normalize_session_turns(request.history)

        for msg in history:
            if msg.role == "user":
                user_msg = (
                    UserMessage(msg.content)
                    if not msg.resources
                    else self.__build_multi_messages(ModelRequest(msg.content, resources=msg.resources))
                )
                messages.append(user_msg)
            else:
                messages.append(AssistantMessage(msg.content))

        user_message = UserMessage(request.prompt) if not request.resources else self.__build_multi_messages(request)

        messages.append(user_message)

        return messages

    def _conversation_messages(self, request: ModelRequest, conversation: Sequence[ModelMessage]) -> list:
        messages = self._build_messages(request)
        for item in conversation:
            if item.role == "assistant":
                messages.append(
                    AssistantMessage(
                        content=item.content,
                        tool_calls=[
                            ChatCompletionsToolCall(
                                id=call.id, function=FunctionCall(name=call.name, arguments=call.arguments)
                            )
                            for call in item.tool_calls
                        ]
                        or None,
                    )
                )
            elif item.role == "tool":
                messages.append(ToolMessage(tool_call_id=item.tool_call_id or "", content=item.content))
            elif item.resources:
                messages.append(
                    self.__build_multi_messages(
                        ModelRequest(item.content, resources=[r.to_resource() for r in item.resources])
                    )
                )
            else:
                messages.append(UserMessage(item.content))
        return messages

    async def request_step(
        self, request: ModelRequest, messages: Sequence[ModelMessage], *, stream: bool
    ) -> AsyncGenerator[ModelStreamCompletions, None]:
        response_format = None
        if request.json_schema:
            schema = (
                request.json_schema.json_schema()
                if isinstance(request.json_schema, TypeAdapter)
                else request.json_schema.model_json_schema()
            )
            response_format = JsonSchemaFormat(name="response", schema=schema)
        client = ChatCompletionsClient(endpoint=self.endpoint, credential=AzureKeyCredential(self.token), retry_total=0)
        try:

            async def send_request():
                return await client.complete(
                    messages=self._conversation_messages(request, messages),
                    model=self.model_name,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    top_p=self.top_p,
                    frequency_penalty=self.frequency_penalty,
                    presence_penalty=self.presence_penalty,
                    stream=stream,
                    tools=self.__build_tools_definition(request.tools) if request.tools else None,
                    response_format=response_format,
                )

            async def responses():
                if stream:
                    async for chunk in RequestRetry[Any](self.config).stream(send_request):
                        yield chunk
                else:
                    yield await RequestRetry[Any](self.config).run(send_request)

            buffer = ToolCallBuffer()
            message = ModelMessage(role="assistant")
            finish = None
            usage = Usage()
            async for response in responses():
                if response.usage:
                    usage.input_tokens = response.usage.prompt_tokens or 0
                    usage.output_tokens = response.usage.completion_tokens or 0
                    details = response.usage.get("prompt_tokens_details") or {}
                    usage.cached_tokens = details.get("cached_tokens", 0)
                if not response.choices:
                    continue
                choice = response.choices[0]
                finish = choice.finish_reason or finish
                raw = choice.delta if stream else choice.message
                text = raw.get("content") or ""
                message.content += text
                if text:
                    yield ModelStreamCompletions(chunk=text)
                for index, call in enumerate(raw.get("tool_calls") or []):
                    buffer.add(ToolDelta.model_validate({"index": index, **call.as_dict()}))
            message.tool_calls = buffer.finish()
            reason = stop_reason(finish, has_tools=bool(message.tool_calls))
            yield ModelStreamCompletions(message=message, usage=usage, stop_reason=reason, succeed=reason != "filtered")
        finally:
            await client.close()

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
