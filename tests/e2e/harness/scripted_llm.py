"""剧本化 LLM：用确定性文本替代真实模型。

常驻路由（route）响应记忆检索等内部管线调用，可重复命中；场景回合（turn）
按声明顺序消费，``when`` 谓词命中时出队。剧本耗尽即抛错，绝不静默编造回复。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence, Union

from muika.llm import ModelCompletions, ModelConfig, ModelRequest, Usage
from muika.llm._schema import ModelMessage, ToolCall
from muika.llm.context import ContextPreparer
from muika.llm.utils.tools import dispatch_tool

from .trace import TraceRecorder

WhenPredicate = Union[str, Callable[[ModelRequest], bool], None]

_MAX_TOOL_ROUNDS = 8
"""单次请求内允许的工具调用轮数上限，防止剧本错误导致死循环。"""


def _observation(messages: Sequence[ModelMessage]) -> str:
    """摘要最近一条消息，作为模型确实看到工具结果的轨迹证据。"""
    if not messages:
        return ""
    last = messages[-1]
    return f"{last.role}: {last.content[:160]}".replace("\n", " ")


@dataclass
class ScriptedTurn:
    """一条剧本回复。

    :param text: 模型应输出的完整原文（可含控制标签，由真实管线解析）
    :param when: 命中条件——字符串表示需出现在 prompt 中，或可调用谓词；缺省匹配任意请求
    :param name: 轨迹记录与断言中使用的可读名称
    :param tool_calls: 本回合声明的工具调用；``step`` 只声明，交给任务层真实执行，
        ``ask`` 则在内部执行后继续消费下一回合
    """

    text: str
    when: WhenPredicate = None
    name: Optional[str] = None
    tool_calls: Sequence[ToolCall] = field(default_factory=tuple)

    def matches(self, request: ModelRequest) -> bool:
        """判断本回合是否命中当前请求。"""
        if self.when is None:
            return True
        if isinstance(self.when, str):
            return self.when in request.prompt
        return self.when(request)


class ScriptedLLM:
    """确定性 LLM 替身：路由优先，其次按顺序消费剧本回合。

    不继承 BaseLLM，避免用量记账等装饰副作用；仅实现核心管线实际使用的
    ``config`` / ``compactor`` / ``step`` / ``ask`` 接口。
    """

    def __init__(self, turns: Sequence[ScriptedTurn] = (), *, recorder: Optional[TraceRecorder] = None) -> None:
        # provider 需为合法加载器名；_echo 是内置测试 provider，此处仅作配置占位
        self.config = ModelConfig(provider="_echo")
        self.compactor = None
        self._turns: deque[ScriptedTurn] = deque(turns)
        self._routes: list[ScriptedTurn] = []
        self.calls: list[dict] = []
        self._recorder = recorder or TraceRecorder()

    def add_route(self, *, when: WhenPredicate, text: str, name: str) -> None:
        """注册常驻路由（如记忆查询扩写），可重复命中且不消耗场景剧本。"""
        self._routes.append(ScriptedTurn(text=text, when=when, name=name))

    @property
    def pending_turns(self) -> int:
        """尚未消费的剧本回合数。"""
        return len(self._turns)

    def _consume(self, request: ModelRequest) -> tuple[ScriptedTurn, bool]:
        """选出本次请求命中的回合：常驻路由优先，其次按声明顺序消费场景剧本。

        :raises AssertionError: 没有任何路由或剧本回合命中当前请求。
        """
        for route in self._routes:
            if route.matches(request):
                return route, True
        for candidate in list(self._turns):
            if candidate.matches(request):
                self._turns.remove(candidate)
                return candidate, False
        prompt_head = request.prompt[:120].replace("\n", " ")
        raise AssertionError(
            "ScriptedLLM: no scripted reply left for request "
            f"(prompt head: {prompt_head!r}; system head: {(request.system or '')[:80]!r})"
        )

    def _take(self, request: ModelRequest, messages: Sequence[ModelMessage]) -> ModelCompletions:
        """取出一条剧本回合并记录轨迹，返回单步响应。

        工具调用只作为声明放进 ``message.tool_calls``，由任务层真实执行，
        与真实 provider 的 ``step`` 语义一致。
        """
        turn, route_hit = self._consume(request)
        prompt_head = request.prompt[:120].replace("\n", " ")
        call = {
            "name": turn.name or turn.text[:24],
            "route": route_hit,
            "prompt": request.prompt,
            "prompt_head": prompt_head,
            "system": request.system or "",
            "format": request.format,
            "history_len": len(request.history),
            "reply": turn.text,
            "tool_calls": [tool.name for tool in turn.tool_calls],
        }
        self.calls.append(call)
        self._recorder.record(
            "llm_call",
            name=call["name"],
            route=route_hit,
            format=request.format,
            history_len=len(request.history),
            system=request.system or "",
            prompt=request.prompt,
            reply=turn.text,
            tool_calls=call["tool_calls"],
            saw=_observation(messages),
        )
        return ModelCompletions(
            text=turn.text,
            usage=Usage(input_tokens=len(request.prompt), output_tokens=len(turn.text)),
            message=ModelMessage(role="assistant", content=turn.text, tool_calls=list(turn.tool_calls)),
            stop_reason="tool_calls" if turn.tool_calls else "stop",
        )

    async def step(
        self,
        request: ModelRequest,
        messages: Sequence[ModelMessage] = (),
        *,
        prepare_context: Optional[ContextPreparer] = None,
    ) -> ModelCompletions:
        """执行一步模型请求，返回单条响应。

        :param prepare_context: 真实的上下文准备函数；提供时按其真实语义先整理上下文
        """
        if prepare_context is not None:
            request, messages = await prepare_context(request, messages, False)
        return self._take(request, messages)

    async def ask(self, request: ModelRequest, **_: object) -> ModelCompletions:
        """按路由优先、剧本其次的顺序生成确定性回复。

        命中带 ``tool_calls`` 的回合时，通过真实工具管线执行调用并记录结果，
        随后在同一请求内继续消费下一回合，直到产出纯文本回复，
        对齐真实 ``BaseLLM.ask`` 在内部完成工具循环的行为。
        """
        tool_round = 0
        while True:
            completion = self._take(request, ())
            if not completion.message or not completion.message.tool_calls:
                return completion
            tool_round += 1
            if tool_round > _MAX_TOOL_ROUNDS:
                raise AssertionError(f"ScriptedLLM: tool loop exceeded {_MAX_TOOL_ROUNDS} rounds in one request")
            for tool_call in completion.message.tool_calls:
                result = await dispatch_tool(tool_call)
                self._recorder.record(
                    "tool_exec",
                    name=tool_call.name,
                    arguments=tool_call.arguments,
                    is_error=result.is_error,
                    result=result.text,
                )
