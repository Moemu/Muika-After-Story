"""插件生命周期钩子注册表：登记并执行 load / unload 钩子。

本模块不 import loader（避免循环依赖），由 loader 延迟 import 接入。
钩子属于插件的"代码"侧：卸载时清除（旧模块闭包必须死），重载时随重新 import 再次注册。

Attributes:
    _load_hooks: load 钩子注册表（按注册顺序执行）
    _unload_hooks: unload 钩子注册表（按注册逆序执行，LIFO）

Functions:
    register_load_hook: 注册 load 钩子
    register_unload_hook: 注册 unload 钩子
    run_load_hooks: 按注册顺序执行 load 钩子（异常向上传播）
    run_unload_hooks: 按注册逆序执行 unload 钩子（尽力而为）
    clear_hooks: 清除指定插件的全部钩子
"""

from __future__ import annotations

from typing import Callable, Dict, List

from muika.utils.logger import logger

_load_hooks: Dict[str, List[Callable[[], object]]] = {}
"""load 钩子注册表：package_name -> 钩子列表（按注册顺序执行）"""
_unload_hooks: Dict[str, List[Callable[[], object]]] = {}
"""unload 钩子注册表：package_name -> 钩子列表（按注册逆序执行）"""


def _hook_name(hook: Callable[[], object]) -> str:
    """获取钩子的可打印名称。"""
    return getattr(hook, "__name__", repr(hook))


def register_load_hook(package_name: str, hook: Callable[[], object]) -> None:
    """为指定插件注册 load 钩子（导入成功后执行）。"""
    _load_hooks.setdefault(package_name, []).append(hook)


def register_unload_hook(package_name: str, hook: Callable[[], object]) -> None:
    """为指定插件注册 unload 钩子（卸载前执行）。"""
    _unload_hooks.setdefault(package_name, []).append(hook)


def run_load_hooks(package_name: str) -> None:
    """按注册顺序执行指定插件的 load 钩子。

    异常向上传播：调用方（``load_plugin``）将钩子失败视为加载失败并整体回滚。
    """
    for hook in _load_hooks.get(package_name, []):
        logger.debug(f"[PluginLifecycle] Running load hook {_hook_name(hook)!r} for {package_name!r}")
        hook()


def run_unload_hooks(package_name: str) -> None:
    """按注册逆序（LIFO，解嵌套资源）执行指定插件的 unload 钩子。

    尽力而为：单个钩子异常仅记录日志，不阻塞后续钩子与卸载本身。
    """
    for hook in reversed(_unload_hooks.get(package_name, [])):
        try:
            logger.debug(f"[PluginLifecycle] Running unload hook {_hook_name(hook)!r} for {package_name!r}")
            hook()
        except Exception:
            logger.exception(f"[PluginLifecycle] Unload hook {_hook_name(hook)!r} of {package_name!r} failed")


def clear_hooks(package_name: str) -> None:
    """清除指定插件的全部钩子。

    旧模块的闭包必须死；重载时钩子会随重新 import 再次注册。
    """
    _load_hooks.pop(package_name, None)
    _unload_hooks.pop(package_name, None)
