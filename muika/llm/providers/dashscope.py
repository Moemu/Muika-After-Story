import asyncio
import json
from dataclasses import dataclass
from functools import partial
from typing import (
    Any,
    AsyncGenerator,
    Generator,
    List,
    Literal,
    Optional,
    Union,
    cast,
    overload,
)

import dashscope
from dashscope.api_entities.dashscope_response import (
    GenerationResponse,
    MultiModalConversationResponse,
)

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
from ..utils.tools import function_call_handler


@dataclass
class FunctionCallStream:
    enable: bool = False
    id: str = ""
    function_name: str = ""
    function_args: str = ""

    def from_chunk(self, chunk: GenerationResponse | MultiModalConversationResponse):
        tool_calls = chunk.output.choices[0].message.tool_calls
        tool_call = tool_calls[0]

        if tool_call.get("id", ""):
            self.id = tool_call["id"]

        if tool_call.get("function", {}).get("name", ""):
            self.function_name = tool_call.get("function").get("name")

        function_arg = tool_call.get("function", {}).get("arguments", "")

        if function_arg and self.function_args != function_arg:
            self.function_args += function_arg

        self.enable = True


class ThoughtStream:
    def __init__(self):
        self.is_insert_think_label: bool = False

    def process_chunk(self, chunk: GenerationResponse | MultiModalConversationResponse) -> str:
        choice = chunk.output.choices[0].message
        answer_content = choice.content
        reasoning_content = choice.get("reasoning_content", "")
        reasoning_content = reasoning_content.replace("\n</think>", "") if reasoning_content else ""

        # 处理模型可能输出的 reasoning（思考内容）
        if reasoning_content:
            if not self.is_insert_think_label:
                self.is_insert_think_label = True
                return f"<think>{reasoning_content}"
            else:
                return reasoning_content

        if not answer_content:
            answer_content = ""

        if isinstance(answer_content, list):
            answer_content = answer_content[0].get("text", "")

        if self.is_insert_think_label:
            self.is_insert_think_label = False
            return f"</think>{answer_content}"

        return answer_content


# Dashscope 的同步 / 流式返回类型（非多模态与多模态接口的返回值联合）
SyncResponse = Union[GenerationResponse, MultiModalConversationResponse]
StreamResponse = Union[
    Generator[GenerationResponse, None, None],
    Generator[MultiModalConversationResponse, None, None],
]
CallResponse = Union[SyncResponse, StreamResponse]


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

        self.extra_headers = (
            {"X-DashScope-DataInspection": '{"input":"cip","output":"cip"}'} if self.config.content_security else {}
        )

        self.stream = False

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

    async def _GenerationResponse_handle(
        self,
        messages: list,
        tools: List[dict],
        response_format: Optional[dict],
        response: GenerationResponse | MultiModalConversationResponse,
        total_usage: Usage | None = None,
    ) -> ModelCompletions:
        """
        处理 Dashscope 的非流式返回对象

        :param message: 总消息列表，用于工具调用
        :param tools: 工具列表
        :param response_format: 消息回复格式
        :param response: 迭代器主体
        :param total_usage: 整体用量
        """
        if total_usage is None:
            total_usage = Usage()
        completions = ModelCompletions()

        if response.status_code != 200:
            completions.succeed = False
            logger.error(f"模型调用失败: {response.status_code}({response.code})")
            logger.error(f"{response.message}")
            completions.text = f"模型调用失败: {response.status_code}({response.code})"
            return completions

        total_usage.input_tokens += response.usage.input_tokens
        total_usage.output_tokens += response.usage.output_tokens
        prompt_tokens_details = getattr(response.usage, "prompt_tokens_details", None)
        if prompt_tokens_details:
            total_usage.cached_tokens += getattr(prompt_tokens_details, "cached_tokens", 0)
        completions.usage = total_usage

        message = response.output.choices[0].message
        if getattr(message, "tool_calls", None):
            return await self._tool_calls_handle_sync(messages, tools, response_format, response, total_usage)

        if response.output.text:
            completions.text = response.output.text
            return completions

        message_content = message.content
        if message_content:
            completions.text = message_content if isinstance(message_content, str) else message_content[0].get("text")
            return completions

        return completions

    async def _Generator_handle(
        self,
        messages: list,
        tools: List[dict],
        response_format: Optional[dict],
        response: Generator[GenerationResponse, None, None] | Generator[MultiModalConversationResponse, None, None],
        total_usage: Usage | None = None,
    ) -> AsyncGenerator[ModelStreamCompletions, None]:
        """
        处理 Dashscope 的流式迭代器

        :param message: 总消息列表，用于工具调用
        :param tools: 工具列表
        :param response_format: 消息回复格式
        :param response: 迭代器主体
        :param total_usage: 整体用量
        """
        if total_usage is None:
            total_usage = Usage()
        func_stream = FunctionCallStream()
        thought_stream = ThoughtStream()

        for chunk in response:
            logger.debug(chunk)
            stream_completions = ModelStreamCompletions()

            if chunk.status_code != 200:
                logger.error(f"模型调用失败: {chunk.status_code}({chunk.code})")
                logger.error(f"{chunk.message}")
                stream_completions.chunk = f"模型调用失败: {chunk.status_code}({chunk.code})"
                stream_completions.succeed = False

                yield stream_completions
                return

            # 更新 token 消耗
            total_usage.input_tokens = chunk.usage.input_tokens
            total_usage.output_tokens = chunk.usage.output_tokens
            prompt_tokens_details = getattr(chunk.usage, "prompt_tokens_details", None)
            if prompt_tokens_details:
                total_usage.cached_tokens = getattr(prompt_tokens_details, "cached_tokens", 0)
            stream_completions.usage = total_usage

            # 优先判断是否是工具调用（OpenAI-style function calling）
            if chunk.output.choices and chunk.output.choices[0].message.get("tool_calls", []):
                func_stream.from_chunk(chunk)
                # 工具调用也可能在输出文本之后发生

            # DashScope 的 text 模式（非标准接口）
            if hasattr(chunk.output, "text") and chunk.output.text:
                stream_completions.chunk = chunk.output.text
                yield stream_completions
                continue

            if chunk.output.choices is None:
                continue

            stream_completions.chunk = thought_stream.process_chunk(chunk)
            yield stream_completions

        # 流式处理工具调用响应
        if func_stream.enable:
            async for final_chunk in await self._tool_calls_handle_stream(
                messages, tools, response_format, func_stream, total_usage
            ):
                yield final_chunk

    async def _tool_calls_handle_sync(
        self,
        messages: List,
        tools: List[dict],
        response_format: Optional[dict],
        response: GenerationResponse | MultiModalConversationResponse,
        total_usage: Usage | None = None,
    ) -> ModelCompletions:
        """
        处理非流式工具调用流

        :param messages: 消息列表
        :param tools: 工具列表
        :param response_format: 消息回复格式
        :param func_stream: 工具调用流实例
        :param total_usage: 整体用量
        """
        if total_usage is None:
            total_usage = Usage()
        tool_call = response.output.choices[0].message.tool_calls[0]
        tool_call_id = tool_call["id"]
        function_name = tool_call["function"]["name"]
        function_args = json.loads(tool_call["function"]["arguments"])

        function_return = await function_call_handler(function_name, function_args)

        messages.append(response.output.choices[0].message)
        messages.append({"role": "tool", "content": function_return, "tool_call_id": tool_call_id})

        return await self._ask(messages, tools, response_format, total_usage)  # type: ignore

    async def _tool_calls_handle_stream(
        self,
        messages: List,
        tools: List[dict],
        response_format: Optional[dict],
        func_stream: FunctionCallStream,
        total_usage: Usage | None = None,
    ) -> AsyncGenerator[ModelStreamCompletions, None]:
        """
        处理流式工具调用流

        :param messages: 消息列表
        :param tools: 工具列表
        :param response_format: 消息回复格式
        :param func_stream: 工具调用流实例
        :param total_usage: 整体用量
        """
        if total_usage is None:
            total_usage = Usage()
        function_args = json.loads(func_stream.function_args)

        function_return = await function_call_handler(func_stream.function_name, function_args)  # type: ignore

        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": func_stream.id,
                        "function": {
                            "arguments": func_stream.function_args,
                            "name": func_stream.function_name,
                        },
                        "type": "function",
                        "index": 0,
                    }
                ],
            }
        )
        messages.append({"role": "tool", "content": function_return, "tool_call_id": func_stream.id})

        return await self._ask(messages, tools, response_format, total_usage)  # type: ignore

    async def _ask(
        self, messages: list, tools: List[dict], response_format: Optional[dict], total_usage: Usage | None = None
    ) -> Union[ModelCompletions, AsyncGenerator[ModelStreamCompletions, None]]:
        # 因为 Dashscope 对于多模态模型的接口不同，所以这里不能统一函数
        if total_usage is None:
            total_usage = Usage()
        if not self.config.multimodal:
            call_kwargs: dict[str, Any] = {
                "api_key": self.api_key,
                "model": self.model,
                "messages": messages,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "top_p": self.top_p,
                "repetition_penalty": self.repetition_penalty,
                "stream": self.stream,
                "tools": tools,
                "parallel_tool_calls": True,
                "enable_search": self.enable_search,
                "incremental_output": self.incremental_output,
                "headers": self.extra_headers,
                "enable_thinking": self.enable_thinking,
                "thinking_budget": self.thinking_budget,
                "response_format": response_format,
            }
        else:
            call_kwargs = {
                "api_key": self.api_key,
                "model": self.model,
                "messages": messages,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "top_p": self.top_p,
                "repetition_penalty": self.repetition_penalty,
                "stream": self.stream,
                "tools": tools,
                "parallel_tool_calls": True,
                "enable_search": self.enable_search,
                "incremental_output": self.incremental_output,
                "response_format": response_format,
            }

        # 过滤未配置的可选参数（Dashscope 部分参数声明为非可选类型但允许缺省）
        call_kwargs = {k: v for k, v in call_kwargs.items() if v is not None}

        if self.config.multimodal:
            response = cast(
                CallResponse,
                await asyncio.to_thread(partial(dashscope.MultiModalConversation.call, **call_kwargs)),
            )
        else:
            response = cast(
                CallResponse,
                await asyncio.to_thread(partial(dashscope.Generation.call, **call_kwargs)),
            )

        if isinstance(response, GenerationResponse) or isinstance(response, MultiModalConversationResponse):
            return await self._GenerationResponse_handle(messages, tools, response_format, response, total_usage)
        return self._Generator_handle(messages, tools, response_format, response, total_usage)

    @overload
    async def ask(self, request: ModelRequest, *, stream: Literal[False] = False) -> ModelCompletions: ...

    @overload
    async def ask(
        self, request: ModelRequest, *, stream: Literal[True] = True
    ) -> AsyncGenerator[ModelStreamCompletions, None]: ...

    async def ask(
        self, request: ModelRequest, *, stream: bool = False
    ) -> Union[ModelCompletions, AsyncGenerator[ModelStreamCompletions, None]]:
        self.stream = stream if stream is not None else False

        tools = request.tools if request.tools else []
        messages = self._build_messages(request)
        if request.format == "json" and request.json_schema:
            # logger.warning("该模型加载器不支持传入 Json Schema 模型，请确保您已经在模型提示词中传入了相关 json 字段")
            response_format = {"type": "json_object"}
        else:
            response_format = None

        return await self._ask(messages, tools, response_format)
