"""工具调用的依赖与资源作用域。"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from muika.core.executor import Executor
    from muika.core.state import MuikaState
    from muika.models import Resource


@dataclass
class ToolContext:
    """保存一次模型请求的依赖和工具资源。"""

    state: MuikaState
    executor: Executor
    resources: list[Resource] = field(default_factory=list)


_tool_context: ContextVar[ToolContext | None] = ContextVar("tool_context", default=None)


@contextmanager
def tool_context(state: MuikaState, executor: Executor) -> Iterator[ToolContext]:
    """隔离本次调用的资源，并在退出时恢复外层上下文。"""
    context = ToolContext(state, executor)
    token = _tool_context.set(context)
    try:
        yield context
    finally:
        _tool_context.reset(token)


def get_dependencies() -> dict[type, object]:
    """返回当前工具调用的依赖，未设置的依赖返回空值。"""
    # 核心模块会导入工具注册器，运行时导入避免循环依赖。
    from muika.core.executor import Executor
    from muika.core.memory import MemoryManager
    from muika.core.state import MuikaState

    context = _tool_context.get()
    state = context.state if context else None
    return {
        MuikaState: state,
        Executor: context.executor if context else None,
        MemoryManager: state.memory if state else None,
        ToolContext: context,
    }
