"""
Butler Agent 上下文变量，用于在 function_call 执行链中传递 state、executor 和 resources。
工具处理器可按类型注入上下文依赖；旧工具仍可使用访问函数。资源通过 add_resource() 收集。
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from muika.core.executor import Executor
    from muika.core.state import MuikaState
    from muika.models import Resource

_butler_context: ContextVar[Optional[dict[str, Any]]] = ContextVar("_butler_context", default=None)


def get_state() -> Optional[MuikaState]:
    """获取当前 Butler 上下文中的 MuikaState"""
    ctx = _butler_context.get()
    return ctx["state"] if ctx else None


def get_executor() -> Optional[Executor]:
    """获取当前 Butler 上下文中的 Executor"""
    ctx = _butler_context.get()
    return ctx["executor"] if ctx else None


def add_resource(resource: Resource) -> None:
    """向 Butler 上下文添加一个资源（图片/文件等）"""
    ctx = _butler_context.get()
    if ctx is not None:
        ctx["resources"].append(resource)


def pop_resources() -> list[Resource]:
    """获取当前 Butler 上下文中收集的所有资源"""
    ctx = _butler_context.get()
    return ctx.pop("resources", []) if ctx else []


def set_butler_context(state: MuikaState, executor: Executor) -> None:
    """设置 Butler 上下文"""
    _butler_context.set({"state": state, "executor": executor, "resources": []})


def clear_butler_context() -> None:
    """清除 Butler 上下文"""
    _butler_context.set(None)


def get_dependencies() -> dict[type, Any]:
    """返回当前调用的依赖表，不访问命令派发器的全局实例。"""
    from muika.core.executor import Executor
    from muika.core.memory import MemoryManager
    from muika.core.state import MuikaState

    state = get_state()
    return {MuikaState: state, Executor: get_executor(), MemoryManager: state.memory if state is not None else None}
