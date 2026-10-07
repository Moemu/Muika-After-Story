from collections.abc import AsyncGenerator, Sequence
from typing import Literal, Union, overload

from .. import (
    BaseLLM,
    ModelCompletions,
    ModelConfig,
    ModelRequest,
    ModelStreamCompletions,
    Usage,
    register,
)
from .._schema import ModelMessage
from ..utils.images import get_file_base64


@register("_echo")
class Echo(BaseLLM):
    """
    一个模拟用模型类，不产生实际模型调用，只返回请求信息
    """

    def __init__(self, model_config: ModelConfig) -> None:
        super().__init__(model_config)

    async def request_step(
        self, request: ModelRequest, messages: Sequence[ModelMessage], *, stream: bool
    ) -> AsyncGenerator[ModelStreamCompletions, None]:
        conversation = self._build_messages(request) + [m.model_dump() for m in messages]
        text = (
            f"Model: {self.__class__.__name__};\n"
            f"Messages: {conversation}\n"
            f"Tools: {request.tools}\n"
            f"Format: {request.format}\n"
            f"Input Length: {len(conversation)}\n\n"
        )
        yield ModelStreamCompletions(
            chunk=text,
            usage=Usage(input_tokens=len(conversation)),
            message=ModelMessage(role="assistant", content=text),
        )

    def _build_multi_messages(self, request: ModelRequest) -> dict:
        """
        构建多模态类型

        此模型加载器支持的多模态类型: `audio` `image` `video` `file`
        """
        user_content: list[dict] = [{"type": "text", "text": request.prompt}]

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

        if request.history:
            history = self._normalize_session_turns(request.history)
            for item in history:
                if item.role == "user":
                    user_content = (
                        {"role": "user", "content": item.content}
                        if not all([item.resources, self.config.multimodal])
                        else self._build_multi_messages(ModelRequest(item.content, resources=item.resources))
                    )
                    messages.append(user_content)
                else:
                    messages.append({"role": "assistant", "content": item.content})

        user_content = (
            {"role": "user", "content": request.prompt}
            if not request.resources
            else self._build_multi_messages(request)
        )

        messages.append(user_content)

        return messages

    @overload
    async def ask(self, request: ModelRequest, *, stream: Literal[False] = False) -> ModelCompletions: ...

    @overload
    async def ask(
        self, request: ModelRequest, *, stream: Literal[True] = True
    ) -> AsyncGenerator["ModelStreamCompletions", None]: ...

    async def ask(
        self, request: ModelRequest, *, stream: bool = False
    ) -> Union[ModelCompletions, AsyncGenerator["ModelStreamCompletions", None]]:
        """
        模型交互询问

        :param request: 模型调用请求体
        :param stream: 是否开启流式对话

        :return: 模型输出体
        """
        return await self.run_conversation(request, stream=stream)
