import json
from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path
from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    List,
    Literal,
    Optional,
    Type,
    Union,
    overload,
)
from uuid import uuid4

from google import genai
from google.genai.types import (
    AutomaticFunctionCallingConfig,
    Content,
    ContentOrDict,
    FunctionCall,
    GenerateContentConfig,
    GoogleSearch,
    HarmBlockThreshold,
    HarmCategory,
    Part,
    SafetySetting,
    Tool,
)
from pydantic import BaseModel, TypeAdapter

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
from ..utils.protocol import json_arguments, stop_reason


@register("gemini")
class Gemini(BaseLLM):
    def __init__(self, model_config: ModelConfig) -> None:
        super().__init__(model_config)
        self._require("model_name", "api_key")

        self.model_name = self.config.model_name
        self.api_key = self.config.api_key
        self.enable_search = self.config.online_search

        self.client = genai.Client(api_key=self.api_key)

        self.gemini_config = GenerateContentConfig(
            temperature=self.config.temperature,
            top_p=self.config.top_p,
            top_k=self.config.top_k,
            max_output_tokens=self.config.max_tokens,
            presence_penalty=self.config.presence_penalty,
            frequency_penalty=self.config.frequency_penalty,
            response_modalities=[m.upper() for m in self.config.modalities if m in {"image", "text"}],
            safety_settings=(
                [
                    SafetySetting(
                        category=HarmCategory.HARM_CATEGORY_HARASSMENT,
                        threshold=HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
                    ),
                    SafetySetting(
                        category=HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                        threshold=HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
                    ),
                    SafetySetting(
                        category=HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                        threshold=HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
                    ),
                    SafetySetting(
                        category=HarmCategory.HARM_CATEGORY_CIVIC_INTEGRITY,
                        threshold=HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
                    ),
                ]
                if self.config.content_security
                else []
            ),
        )

    def _build_gemini_config(
        self, tools: Optional[List[dict]], response_format: Optional[Union[Type[BaseModel], TypeAdapter, dict]]
    ) -> GenerateContentConfig:
        gemini_config = self.gemini_config.model_copy()
        format_tools = []

        # build tools
        for tool in tools if tools else []:
            function = deepcopy(tool["function"])
            format_tools.append(function)

        function_tools = Tool(function_declarations=format_tools)

        tool_list = [function_tools] if tools else []
        if self.enable_search:
            tool_list.append(Tool(google_search=GoogleSearch()))
        gemini_config.tools = [tool for tool in tool_list] or None
        gemini_config.automatic_function_calling = AutomaticFunctionCallingConfig(disable=True)

        # build response format
        if response_format:
            schema = response_format
            if isinstance(response_format, TypeAdapter):
                # 将 TypeAdapter 转换为 json schema dict
                schema = response_format.json_schema()

            gemini_config.response_mime_type = "application/json"
            gemini_config.response_schema = schema

        return gemini_config

    def _build_user_parts(self, request: ModelRequest) -> list[Part]:
        user_parts: list[Part] = [Part.from_text(text=request.prompt)]

        if not request.resources:
            return user_parts

        for resource in request.resources:
            if resource.type == "image" and resource.path is not None:
                user_parts.append(
                    Part.from_bytes(data=Path(resource.path).read_bytes(), mime_type=resource.mimetype or "image/jpeg")
                )

        return user_parts

    def _build_messages(self, request: ModelRequest) -> list[ContentOrDict]:
        messages: List[ContentOrDict] = []

        if request.history:
            history = self._normalize_session_turns(request.history)
            for item in history:
                if item.role != "user":
                    messages.append(Content(role="model", parts=[Part.from_text(text=item.content)]))
                else:
                    messages.append(
                        Content(
                            role="user",
                            parts=self._build_user_parts(ModelRequest(item.content, resources=item.resources)),
                        )
                    )

        messages.append(Content(role="user", parts=self._build_user_parts(request)))

        return messages

    def _conversation_messages(self, request: ModelRequest, conversation: Sequence[ModelMessage]) -> list:
        messages = self._build_messages(request)
        for item in conversation:
            if item.role == "assistant":
                native = item.provider_data.get("gemini_content")
                if native:
                    messages.append(Content.model_validate(native))
                else:
                    parts = [Part.from_text(text=item.content)] if item.content else []
                    parts.extend(
                        Part(function_call=FunctionCall(id=call.id, name=call.name, args=json.loads(call.arguments)))
                        for call in item.tool_calls
                    )
                    messages.append(Content(role="model", parts=parts))
            elif item.role == "tool":
                part = Part.from_function_response(name=item.name or "", response={"result": item.content})
                if part.function_response:
                    part.function_response.id = item.tool_call_id
                # 同一批工具结果组成一个 user 内容，保持调用与响应的数量对应。
                if messages and isinstance(messages[-1], Content) and messages[-1].role == "user":
                    parts = messages[-1].parts or []
                    if parts and all(p.function_response for p in parts):
                        parts.append(part)
                        continue
                messages.append(Content(role="user", parts=[part]))
            else:
                messages.append(
                    Content(
                        role="user",
                        parts=self._build_user_parts(
                            ModelRequest(item.content, resources=[r.to_resource() for r in item.resources])
                        ),
                    )
                )
        return messages

    async def request_step(
        self, request: ModelRequest, messages: Sequence[ModelMessage], *, stream: bool
    ) -> AsyncGenerator[ModelStreamCompletions, None]:
        config = self._build_gemini_config(request.tools, request.json_schema)
        config.system_instruction = request.system
        if request.format == "json":
            config.response_mime_type = "application/json"

        async def send_request():
            kwargs = dict(model=self.model_name, contents=self._conversation_messages(request, messages), config=config)
            if stream:
                response = self.client.aio.models.generate_content_stream(**kwargs)
                return await response if isinstance(response, Awaitable) else response
            return await self.client.aio.models.generate_content(**kwargs)

        async def responses():
            if stream:
                async for chunk in RequestRetry[Any](self.config).stream(send_request):
                    yield chunk
            else:
                yield await RequestRetry[Any](self.config).run(send_request)

        message = ModelMessage(role="assistant")
        parts: list[Part] = []
        usage = Usage()
        finish = None
        thinking = False
        async for response in responses():
            if response.usage_metadata:
                usage.input_tokens = response.usage_metadata.prompt_token_count or 0
                usage.output_tokens = (response.usage_metadata.candidates_token_count or 0) + (
                    response.usage_metadata.thoughts_token_count or 0
                )
                usage.cached_tokens = response.usage_metadata.cached_content_token_count or 0
            if not response.candidates:
                continue
            candidate = response.candidates[0]
            finish = candidate.finish_reason or finish
            if not candidate.content:
                continue
            for part in candidate.content.parts or []:
                parts.append(part)
                if part.text:
                    if part.thought:
                        message.reasoning += part.text
                        yield ModelStreamCompletions(chunk=("" if thinking else "<think>") + part.text)
                        thinking = True
                    else:
                        message.content += part.text
                        yield ModelStreamCompletions(chunk=("</think>" if thinking else "") + part.text)
                        thinking = False
                if part.function_call:
                    call = part.function_call
                    call_id = call.id or f"call_{uuid4().hex}"
                    message.tool_calls.append(
                        ToolCall(id=call_id, name=call.name or "", arguments=json_arguments(call.args or {}))
                    )
                if part.inline_data and part.inline_data.data:
                    yield ModelStreamCompletions(
                        resources=[
                            Resource(type="image", raw=part.inline_data.data, mimetype=part.inline_data.mime_type)
                        ]
                    )
        if thinking:
            yield ModelStreamCompletions(chunk="</think>")
        message.provider_data = {"gemini_content": Content(role="model", parts=parts).model_dump(mode="json")}
        reason = stop_reason(finish, has_tools=bool(message.tool_calls))
        yield ModelStreamCompletions(message=message, usage=usage, stop_reason=reason, succeed=reason != "filtered")

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
