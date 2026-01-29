from typing import Any, AsyncGenerator, List, Literal, Union, overload

from .. import (
    BaseLLM,
    ModelCompletions,
    ModelConfig,
    ModelRequest,
    ModelStreamCompletions,
    register,
)
from ..utils.images import get_file_base64


@register("_echo")
class Echo(BaseLLM):
    """
    一个模拟用模型类，不产生实际模型调用，只返回请求信息
    """

    def __init__(self, model_config: ModelConfig) -> None:
        super().__init__(model_config)

    def _build_multi_messages(self, request: ModelRequest) -> dict:
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
                file_data = f"data:audio/{file_format};base64,{get_file_base64(local_path=resource.path)}"
                user_content.append({"type": "input_audio", "input_audio": {"data": file_data, "format": file_format}})

            elif resource.type == "image":
                file_format = resource.path.split(".")[-1]
                file_data = f"data:image/{file_format};base64,{get_file_base64(local_path=resource.path)}"
                user_content.append({"type": "image_url", "image_url": {"url": file_data}})

            elif resource.type == "video":
                file_format = resource.path.split(".")[-1]
                file_data = f"data:video/{file_format};base64,{get_file_base64(local_path=resource.path)}"
                user_content.append({"type": "video_url", "video_url": {"url": file_data}})

            elif resource.type == "file":
                file_format = resource.path.split(".")[-1]
                file_data = f"data:;base64,{get_file_base64(local_path=resource.path)}"
                user_content.append({"type": "file", "file": {"file_data": file_data}})

        return {"role": "user", "content": user_content}

    def _build_messages(self, request: ModelRequest) -> list[dict[str, str]]:
        messages = []

        if request.system:
            messages.append({"role": "system", "content": request.system})

        for item in request.history:
            user_content = (
                {"role": "user", "content": item.message}
                if not all([item.resources, self.config.multimodal])
                else self._build_multi_messages(ModelRequest(item.message, resources=item.resources))
            )

            messages.append(user_content)
            messages.append({"role": "assistant", "content": item.respond})

        user_content = (
            {"role": "user", "content": request.prompt}
            if not request.resources
            else self._build_multi_messages(request)
        )

        messages.append(user_content)

        return messages

    async def _ask_sync(
        self, messages: list[dict[str, str]], tools: Any, response_format: Any, total_tokens: int = 0
    ) -> ModelCompletions:
        """
        同步模型调用
        """
        total_tokens += len(messages)

        request_info = f"Model: {self.__class__.__name__};\n"
        request_info += f"Messages: {messages}\n"
        request_info += f"Tools: {tools}\n"
        request_info += f"Format: {response_format}\n"
        request_info += f"Input Length: {len(messages)}\n\n"

        return ModelCompletions(text=request_info, usage=total_tokens)

    async def _ask_stream(
        self, messages: list[dict[str, str]], tools: Any, response_format: Any, total_tokens: int = 0
    ) -> AsyncGenerator[ModelStreamCompletions, None]:
        """
        流式输出
        """
        request_info = f"Model: {self.__class__.__name__};\n"
        request_info += f"Messages: {messages}\n"
        request_info += f"Tools: {tools}\n"
        request_info += f"Format: {response_format}\n"
        request_info += f"Input Length: {len(messages)}\n\n"

        for line in request_info.splitlines(keepends=True):
            total_tokens += len(line)
            yield ModelStreamCompletions(chunk=line, usage=total_tokens)

    @overload
    async def ask(self, request: ModelRequest, *, stream: Literal[False] = False) -> ModelCompletions: ...

    @overload
    async def ask(
        self, request: ModelRequest, *, stream: Literal[True] = True
    ) -> AsyncGenerator["ModelStreamCompletions", None]: ...

    async def ask(
        self, request: ModelRequest, *, stream: bool = False
    ) -> Union[ModelCompletions, AsyncGenerator[ModelStreamCompletions, None]]:
        """
        模型交互询问

        :param request: 模型调用请求体
        :param stream: 是否开启流式对话

        :return: 模型输出体
        """
        messages = self._build_messages(request)

        if stream:
            return self._ask_stream(messages, request.tools, response_format=request.format)

        return await self._ask_sync(messages, request.tools, response_format=request.format)
