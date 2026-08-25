"""插件生命周期协调器：集中 unload / reload / Butler.refresh_tools 调用链。

模块级单例通过 :func:`get_plugin_manager` 访问。builtin_plugins 不允许通过该
协调器卸载（会破坏核心命令）——仅用户 plugins/ 下的插件可热重载。
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Optional

from muika.plugin.exceptions import PluginLoadError
from muika.plugin.lifecycle import run_unload_hooks
from muika.plugin.loader import (
    _plugins,
    get_plugins,
    reload_plugin,
    unload_plugin,
)
from muika.utils.logger import logger

if TYPE_CHECKING:
    from muika.core.butler.agent import ButlerAgent


_BUILTIN_PREFIX = "muika.builtin_plugins"
"""builtin 插件的 module 前缀；拒绝通过本管理器卸载。"""


class PluginManager:
    """插件生命周期协调器。

    持有 ButlerAgent 引用以便在 reload 后重建 LLM 工具列表。
    """

    def __init__(self, butler: Optional[ButlerAgent] = None) -> None:
        self._butler = butler
        self._watcher_suppression: dict[str, float] = {}
        self._watcher_lock = threading.Lock()

    def bind_butler(self, butler: ButlerAgent) -> None:
        """延迟绑定 Butler 实例（CoreBootstrap 启动后调用）。"""
        self._butler = butler

    def unload(self, package_name: str) -> bool:
        """卸载指定插件并在成功后刷新 Butler 工具列表。

        builtin 插件拒绝卸载（返回 False）。
        """
        if package_name.startswith(_BUILTIN_PREFIX):
            logger.warning(f"[PluginManager] refusing to unload builtin plugin {package_name!r}")
            return False
        if not unload_plugin(package_name):
            return False
        self.refresh_butler()
        return True

    def reload(self, package_name: str) -> bool:
        """重载指定插件并在成功后刷新 Butler 工具列表。"""
        if package_name.startswith(_BUILTIN_PREFIX):
            logger.warning(f"[PluginManager] refusing to reload builtin plugin {package_name!r}")
            return False
        try:
            reload_plugin(package_name)
            self.refresh_butler()
            return True
        except PluginLoadError as exc:
            logger.error(str(exc))
            return False

    def reload_all_user_plugins(self) -> list[str]:
        """重载所有用户插件（非 builtin）。返回成功重载的 package_name 列表。"""
        reloaded: list[str] = []
        for package_name in list(get_plugins().keys()):
            if package_name.startswith(_BUILTIN_PREFIX):
                continue
            if self.reload(package_name):
                reloaded.append(package_name)
        return reloaded

    def refresh_butler(self) -> int:
        """重建 Butler 工具列表，返回新的工具数；未绑定 Butler 时返回 0。"""
        if self._butler is None:
            logger.debug("[PluginManager] refresh_butler: no butler bound, skipping")
            return 0
        count = self._butler.refresh_tools()
        logger.info(f"[PluginManager] Butler tools refreshed: {count} tools")
        return count

    def suppress_watcher(self, package_name: str, seconds: float = 3.0) -> None:
        """临时忽略指定插件的文件监听事件。"""
        with self._watcher_lock:
            self._watcher_suppression[package_name] = time.monotonic() + seconds

    def is_watcher_suppressed(self, package_name: str) -> bool:
        """检查指定插件的文件监听事件是否仍被忽略。"""
        with self._watcher_lock:
            deadline = self._watcher_suppression.get(package_name)
            if deadline is None:
                return False
            if deadline <= time.monotonic():
                self._watcher_suppression.pop(package_name, None)
                return False
            return True

    def shutdown_all(self) -> None:
        """对所有已加载插件执行 unload 钩子。"""
        for package_name in list(get_plugins()):
            run_unload_hooks(package_name)

    @staticmethod
    def list_loaded() -> dict[str, dict]:
        """列出已加载插件的概要信息。

        返回 ``{package_name: {"name": ..., "commands": N, "func_calls": M, "is_builtin": bool}}``。
        """
        from muika.plugin.command import _commands
        from muika.plugin.func_call.caller import _caller_data

        result: dict[str, dict] = {}
        for package_name, plugin in _plugins.items():
            cmd_count = sum(1 for c in _commands if c.plugin_package == package_name)
            call_count = sum(1 for c in _caller_data.values() if c.plugin_package == package_name)
            result[package_name] = {
                "name": plugin.name,
                "commands": cmd_count,
                "func_calls": call_count,
                "is_builtin": package_name.startswith(_BUILTIN_PREFIX),
            }
        return result


_plugin_manager: Optional[PluginManager] = None


def get_plugin_manager() -> PluginManager:
    """获取单例 PluginManager。"""
    global _plugin_manager
    if _plugin_manager is None:
        _plugin_manager = PluginManager()
    return _plugin_manager
