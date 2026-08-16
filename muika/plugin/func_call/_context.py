"""
Butler Agent 上下文变量，用于在 function_call 执行链中传递 state、executor 和 resources。
工具函数通过 get_state() / get_executor() 获取上下文，通过 add_resource() 收集资源。
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
