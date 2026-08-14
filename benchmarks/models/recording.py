"""RecordingModel：包装任意模型，透传 ask 并记录请求/响应/错误。"""

from __future__ import annotations

from typing import Any

from muika.llm._schema import ModelCompletions, ModelRequest


class RecordingModel:
    """记录每次 ask 的请求/响应/错误，不修改输出。

    模型调用失败时 ``generate_reply`` 会吞掉异常并返回字面 fallback，因此
    试验是否失败应依据 ``errors`` 判断，而不是字符串匹配。
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.requests: list[ModelRequest] = []
        self.responses: list[ModelCompletions] = []
        self.errors: list[BaseException] = []
        self.call_count = 0

    def reset(self) -> None:
        """清空本次试验的录制（RecordingModel 跨试验复用，需按试验重置）。"""
        self.requests.clear()
        self.responses.clear()
        self.errors.clear()
        self.call_count = 0

    async def ask(self, request: ModelRequest, *, stream: bool = False) -> ModelCompletions:
        self.call_count += 1
        self.requests.append(request)
        try:
            response = await self.inner.ask(request, stream=stream)
        except BaseException as exc:  # noqa: BLE001 - 录制后照常抛出
            self.errors.append(exc)
            raise
        self.responses.append(response)
        return response
