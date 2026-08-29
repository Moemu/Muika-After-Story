"""插件用于注册生命周期钩子和保存热重载状态的 ``ctx`` 单例。

用法::

    from muika.plugin.ctx import ctx

    state = ctx.state

    @ctx.load
    def setup():
        state.setdefault("client", create_client())

    @ctx.unload
    def teardown():
        close_resources()

钩子必须是同步函数。请在模块顶层保存 ``ctx.state``。卸载不会删除该状态。
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from muika.utils.logger import logger

from .lifecycle import register_load_hook, register_unload_hook
from .loader import _loading_plugin
from .state import get_state

_F = TypeVar("_F", bound=Callable[[], object])


def _hook_name(fn: Callable[[], object]) -> str:
    """获取函数的可打印名称。"""
    return getattr(fn, "__name__", repr(fn))


class _PluginCtx:
    """为当前加载中的插件提供生命周期接口。"""

    def load(self, fn: _F) -> _F:
        """注册插件加载成功后按声明顺序执行的同步钩子。"""
        package_name = _loading_plugin.get()
        if package_name is None:
            logger.warning(f"[PluginCtx] @ctx.load used outside plugin loading: {_hook_name(fn)!r} not registered")
            return fn
        register_load_hook(package_name, fn)
        return fn

    def unload(self, fn: _F) -> _F:
        """注册插件卸载前按声明逆序执行的同步钩子。"""
        package_name = _loading_plugin.get()
        if package_name is None:
            logger.warning(f"[PluginCtx] @ctx.unload used outside plugin loading: {_hook_name(fn)!r} not registered")
            return fn
        register_unload_hook(package_name, fn)
        return fn

    @property
    def state(self) -> dict[str, Any]:
        """返回当前插件在本次进程内跨热重载保留的状态。

        :return: 当前插件的状态字典
        :raises RuntimeError: 当前不在插件加载过程
        """
        package_name = _loading_plugin.get()
        if package_name is None:
            raise RuntimeError(
                "ctx.state accessed outside plugin loading; bind it once at module top level: state = ctx.state"
            )
        return get_state(package_name)


ctx = _PluginCtx()
