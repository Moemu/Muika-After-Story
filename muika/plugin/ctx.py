"""插件生命周期装饰器门面：``ctx`` 单例。

用法::

    from muika.plugin.ctx import ctx

    state = ctx.state          # import 期绑定本插件的持久 dict（模块顶层绑定一次）

    @ctx.load
    def setup():
        state.setdefault("db", create_engine(...))   # 热重载后取回同一对象

    @ctx.unload
    def teardown():
        ...                       # 只释放代码侧资源，不动 state

约定：
- 钩子仅支持同步函数；``@ctx.load`` 为裸装饰器（不支持 ``@ctx.load()``）
- ``state`` 必须在模块顶层绑定一次：钩子执行时加载上下文已复位，
  运行期再访问 ``ctx.state`` 会抛 RuntimeError
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from muika.utils.logger import logger

from .lifecycle import register_load_hook, register_unload_hook
from .state import get_state

_F = TypeVar("_F", bound=Callable[[], object])


def _hook_name(fn: Callable[[], object]) -> str:
    """获取函数的可打印名称。"""
    return getattr(fn, "__name__", repr(fn))


class _PluginCtx:
    """插件生命周期装饰器门面。详见模块文档。"""

    def load(self, fn: _F) -> _F:
        """注册 load 钩子：加载成功（模块导入完成）后按注册顺序执行。

        所有权由装饰时刻的加载上下文判定；上下文缺失时警告并原样返回。
        """
        from .loader import _loading_plugin

        package_name = _loading_plugin.get()
        if package_name is None:
            logger.warning(f"[PluginCtx] @ctx.load used outside plugin loading: {_hook_name(fn)!r} not registered")
            return fn
        register_load_hook(package_name, fn)
        return fn

    def unload(self, fn: _F) -> _F:
        """注册 unload 钩子：卸载前按注册逆序（LIFO）执行。

        所有权由装饰时刻的加载上下文判定；上下文缺失时警告并原样返回。
        """
        from .loader import _loading_plugin

        package_name = _loading_plugin.get()
        if package_name is None:
            logger.warning(f"[PluginCtx] @ctx.unload used outside plugin loading: {_hook_name(fn)!r} not registered")
            return fn
        register_unload_hook(package_name, fn)
        return fn

    @property
    def state(self) -> dict[str, Any]:
        """当前插件的持久状态 dict（进程生命周期，热重载后存活）。

        必须在模块顶层绑定一次：``state = ctx.state``。
        :raises RuntimeError: 在加载上下文之外访问（防止静默写丢状态）。
        """
        from .loader import _loading_plugin

        package_name = _loading_plugin.get()
        if package_name is None:
            raise RuntimeError(
                "ctx.state accessed outside plugin loading; bind it once at module top level: state = ctx.state"
            )
        return get_state(package_name)


ctx = _PluginCtx()
"""插件侧使用的生命周期门面单例。"""
