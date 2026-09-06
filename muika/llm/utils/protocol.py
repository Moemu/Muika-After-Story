"""处理聊天协议中的工具分片和停止原因。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from uuid import uuid4

from pydantic import BaseModel

from muika.llm._schema import StopReason, ToolCall


class FunctionDelta(BaseModel):
    name: str | None = None
    arguments: str | None = None


class ToolDelta(BaseModel):
    index: int = 0
    id: str | None = None
    function: FunctionDelta | None = None


@dataclass
class ToolCallBuffer:
    """按索引拼接交错到达的多个工具调用。"""

    calls: dict[int, ToolCall] = field(default_factory=dict)

    def add(self, delta: ToolDelta, *, incremental: bool = True) -> None:
        call = self.calls.setdefault(delta.index, ToolCall(id="", name="", arguments=""))
        if delta.id:
            call.id = delta.id
        if delta.function is None:
            return
        if delta.function.name:
            if not incremental or delta.function.name == call.name:
                call.name = delta.function.name
            else:
                call.name += delta.function.name
        if delta.function.arguments is not None:
            if incremental:
                call.arguments += delta.function.arguments
            else:
                call.arguments = delta.function.arguments

    def finish(self) -> list[ToolCall]:
        return [
            call.model_copy(update={"id": call.id or f"call_{uuid4().hex}"}) for _, call in sorted(self.calls.items())
        ]


def stop_reason(value: str | None, *, has_tools: bool = False) -> StopReason:
    """将提供者的结束状态转换为公共状态。"""
    if value and value.lower() in {"length", "max_tokens", "max_token", "token_limit_reached"}:
        return "length"
    if value and value.lower() in {"content_filter", "content_filtered", "safety", "recitation", "blocklist"}:
        return "filtered"
    return "tool_calls" if has_tools else "stop"


def tool_payload(call: ToolCall) -> dict:
    """生成 OpenAI 风格的调用记录。"""
    return {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}}


def json_arguments(value: str | dict) -> str:
    """保留 JSON 字符串或编码已经解析的参数。"""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
