import json
import os
from typing import AsyncGenerator, List, Literal, Optional, Union, overload

from azure.ai.inference.aio import ChatCompletionsClient
from azure.ai.inference.models import (
    AssistantMessage,
    AudioContentItem,
    ChatCompletionsToolCall,
    ChatCompletionsToolDefinition,
    ChatRequestMessage,
    CompletionsFinishReason,
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
from azure.core.exceptions import HttpResponseError
from nonebot import logger

from .. import (
    BaseLLM,
    ModelCompletions,
    ModelConfig,
    ModelRequest,
    ModelStreamCompletions,
    register,
)
from ..utils.tools import function_call_handler


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

        for msg in request.history:
            user_msg = (
                UserMessage(msg.message)
                if not msg.resources
                else self.__build_multi_messages(ModelRequest(msg.message, resources=msg.resources))
            )
            messages.append(user_msg)
            messages.append(AssistantMessage(msg.respond))

        user_message = UserMessage(request.prompt) if not request.resources else self.__build_multi_messages(request)

        messages.append(user_message)

        return messages

    def _tool_messages_precheck(self, tool_calls: Optional[List[ChatCompletionsToolCall]] = None) -> bool:
        if not (tool_calls and len(tool_calls) == 1):
            return False

        tool_call = tool_calls[0]

        if isinstance(tool_call, ChatCompletionsToolCall):
            return True

        return False

    async def _ask_sync(
        self,
        messages: List[ChatRequestMessage],
        tools: List[ChatCompletionsToolDefinition],
        response_format: Optional[JsonSchemaFormat],
        total_tokens: int = 0,
    ) -> ModelCompletions:
        client = ChatCompletionsClient(endpoint=self.endpoint, credential=AzureKeyCredential(self.token))

        completions = ModelCompletions()
        current_total_tokens = total_tokens

        try:
            response = await client.complete(
                messages=messages,
                model=self.model_name,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                top_p=self.top_p,
                frequency_penalty=self.frequency_penalty,
                presence_penalty=self.presence_penalty,
                stream=False,
                tools=tools,
                response_format=response_format,
            )
            finish_reason = response.choices[0].finish_reason
            if response.usage:
                current_total_tokens += response.usage.total_tokens

            if finish_reason == CompletionsFinishReason.STOPPED:
                completions.text = response.choices[0].message.content

            elif finish_reason == CompletionsFinishReason.CONTENT_FILTERED:
                completions.succeed = False
                completions.text = "(模型内部错误: 被内容过滤器阻止)"

            elif finish_reason == CompletionsFinishReason.TOKEN_LIMIT_REACHED:
                completions.succeed = False
                completions.text = "(模型内部错误: 达到了最大 token 限制)"

            elif finish_reason == CompletionsFinishReason.TOOL_CALLS:
                tool_calls = response.choices[0].message.tool_calls
                messages.append(AssistantMessage(tool_calls=tool_calls))
                if (tool_calls is None) or (not self._tool_messages_precheck(tool_calls=tool_calls)):
                    completions.succeed = False
                    completions.text = "(模型内部错误: tool_calls 内容为空)"
                    completions.usage = current_total_tokens
                    return completions

                tool_call = tool_calls[0]
                function_args = json.loads(tool_call.function.arguments.replace("'", '"'))

                function_return = await function_call_handler(tool_call.function.name, function_args)

                # Append the function call result fo the chat history
                messages.append(ToolMessage(tool_call_id=tool_call.id, content=function_return))

                return await self._ask_sync(messages, tools, response_format, current_total_tokens)

            else:
                completions.succeed = False
                completions.text = "(模型内部错误: 达到了最大 token 限制)"

        except HttpResponseError as e:
            logger.error(f"模型响应失败: {e.status_code} ({e.reason})")
            logger.error(f"{e.message}")
            completions.succeed = False
            completions.text = f"模型响应失败: {e.status_code} ({e.reason})"

        finally:
            await client.close()
            completions.usage = current_total_tokens
            return completions

    async def _ask_stream(
        self,
        messages: List[ChatRequestMessage],
        tools: List[ChatCompletionsToolDefinition],
        response_format: Optional[JsonSchemaFormat],
        total_tokens: int = 0,
    ) -> AsyncGenerator[ModelStreamCompletions, None]:
        client = ChatCompletionsClient(endpoint=self.endpoint, credential=AzureKeyCredential(self.token))
        current_total_tokens = total_tokens

        try:
            response = await client.complete(
                messages=messages,
                model=self.model_name,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                top_p=self.top_p,
                frequency_penalty=self.frequency_penalty,
                presence_penalty=self.presence_penalty,
                stream=True,
                tools=tools,
                model_extras={"stream_options": {"include_usage": True}},  # 需要显式声明获取用量
                response_format=response_format,
            )

            tool_call_id: str = ""
            function_name: str = ""
            function_args: str = ""

            async for chunk in response:
                stream_completions = ModelStreamCompletions()

                if chunk.usage:  # chunk.usage 只会在最后一个包中被提供，此时choices为空
                    current_total_tokens += chunk.usage.total_tokens if chunk.usage else 0
                    stream_completions.usage = current_total_tokens

                if not chunk.choices:
                    yield stream_completions
                    continue

                finish_reason = chunk.choices[0].finish_reason

                if chunk.choices and chunk.choices[0].get("delta", {}).get("content", ""):
                    stream_completions.chunk = chunk["choices"][0]["delta"]["content"]

                elif chunk.choices[0].delta.tool_calls is not None:
                    tool_call = chunk.choices[0].delta.tool_calls[0]

                    if tool_call.function.name is not None:
                        function_name = tool_call.function.name
                    if tool_call.id is not None:
                        tool_call_id = tool_call.id
                    function_args += tool_call.function.arguments or ""
                    continue

                elif finish_reason == CompletionsFinishReason.CONTENT_FILTERED:
                    stream_completions.succeed = False
                    stream_completions.chunk = "(模型内部错误: 被内容过滤器阻止)"

                elif finish_reason == CompletionsFinishReason.TOKEN_LIMIT_REACHED:
                    stream_completions.succeed = False
                    stream_completions.chunk = "(模型内部错误: 达到了最大 token 限制)"

                elif finish_reason == CompletionsFinishReason.TOOL_CALLS:
                    messages.append(
                        AssistantMessage(
                            tool_calls=[
                                ChatCompletionsToolCall(
                                    id=tool_call_id, function=FunctionCall(name=function_name, arguments=function_args)
                                )
                            ]
                        )
                    )

                    function_arg = json.loads(function_args.replace("'", '"'))

                    function_return = await function_call_handler(function_name, function_arg)

                    # Append the function call result fo the chat history
                    messages.append(ToolMessage(tool_call_id=tool_call_id, content=function_return))

                    async for content in self._ask_stream(messages, tools, response_format, current_total_tokens):
                        yield content

                    return

                yield stream_completions

        except HttpResponseError as e:
            logger.error(f"模型响应失败: {e.status_code} ({e.reason})")
            logger.error(f"{e.message}")
            stream_completions = ModelStreamCompletions()
            stream_completions.chunk = f"模型响应失败: {e.status_code} ({e.reason})"
            stream_completions.succeed = False
            yield stream_completions

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
        messages = self._build_messages(request)

        tools = self.__build_tools_definition(request.tools) if request.tools else []

        if request.format == "json" and request.json_schema:
            response_format = JsonSchemaFormat(
                name="Recipe_JSON_Schema",
                schema=request.json_schema.model_json_schema(),
                description=request.prompt,
                strict=True,
            )
        else:
            response_format = None

        if stream:
            return self._ask_stream(messages, tools, response_format)

        return await self._ask_sync(messages, tools, response_format)
