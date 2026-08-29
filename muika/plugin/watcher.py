"""插件目录文件监听：检测 plugins/ 下新增 / 修改 / 删除，自动触发 reload / unload。

只监听用户插件目录（``mas_config.plugins_dir``），builtin 插件不在范围内。
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from muika.config import mas_config
from muika.plugin.utils import path_to_module_name
from muika.utils.logger import logger

if TYPE_CHECKING:
    from muika.plugin.manager import PluginManager


class PluginFileHandler(FileSystemEventHandler):
    """插件目录文件事件处理器：1s 冷却防抖，合并同插件多次事件为一次 reload / unload。"""

    def __init__(self, manager: PluginManager, plugins_dir: Path, base_path: Path) -> None:
        self._manager = manager
        self._plugins_dir = plugins_dir.resolve()
        self._base_path = base_path
        self._cooldown = 1.0
        self._last_triggered: dict[tuple[str, bool], float] = {}
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定主事件循环；事件处理通过 call_soon_threadsafe 调度到主循环。"""
        self._loop = loop

    def _on_any_event(self, event) -> None:
        src = Path(event.src_path).resolve()
        if event.event_type == "moved":
            self._handle_path(src, is_delete=True)
            dest_path = getattr(event, "dest_path", None)
            if dest_path:
                self._handle_path(Path(dest_path).resolve(), is_delete=False)
            return
        self._handle_path(src, is_delete=event.event_type == "deleted")

    def _handle_path(self, src: Path, is_delete: bool) -> None:
        try:
            src.relative_to(self._plugins_dir)
        except ValueError:
            return

        rel = src.relative_to(self._plugins_dir)
        if any(p in {"_staging", "_quarantine", "__pycache__"} for p in rel.parts):
            return
        if src.suffix in {".pyc", ".tmp"} or src.name.startswith(".") or src.name.endswith("~"):
            return

        package_name = self._derive_package_name(src)
        if package_name is None or self._manager.is_watcher_suppressed(package_name):
            return

        current = time.monotonic()
        trigger_key = (package_name, is_delete)
        with self._lock:
            if current - self._last_triggered.get(trigger_key, 0.0) < self._cooldown:
                return
            self._last_triggered[trigger_key] = current

        action = "deleted" if is_delete else "changed"
        logger.info(
            f"[PluginWatcher] {action} {src.relative_to(self._plugins_dir)} "
            f"→ {'unload' if is_delete else 'reload'} {package_name!r}"
        )

        if self._loop is not None:
            self._loop.call_soon_threadsafe(
                asyncio.create_task,
                self._dispatch(package_name, is_delete),
            )
        else:
            self._dispatch_sync(package_name, is_delete)

    on_created = _on_any_event
    on_modified = _on_any_event
    on_deleted = _on_any_event
    on_moved = _on_any_event

    def _derive_package_name(self, src: Path) -> Optional[str]:
        """由事件路径推导插件 package_name。

        规则与 :func:`load_plugins` 一致：
        - ``plugins/<name>.py`` → module name = path_to_module_name(<name>)
        - ``plugins/<name>/`` 含 ``__init__.py`` → path_to_module_name(<name>/)
        - ``plugins/<name>/<name_lower>/`` → path_to_module_name(<name>/<name_lower>/)
        """
        rel = src.relative_to(self._plugins_dir)
        parts = rel.parts
        if not parts:
            return None

        top = self._plugins_dir / parts[0]

        if len(parts) == 1 and top.suffix == ".py" and top.name != "__init__.py":
            return path_to_module_name(top.with_suffix(""), self._base_path)

        if not top.exists():
            return path_to_module_name(top, self._base_path)
        if not top.is_dir():
            return None
        init = top / "__init__.py"
        if init.exists():
            return path_to_module_name(top, self._base_path)
        if src.name == "__init__.py":
            return path_to_module_name(top, self._base_path)
        subdir_name = top.name.lower().replace("-", "_")
        subdir = top / subdir_name
        if subdir.is_dir():
            return path_to_module_name(subdir, self._base_path)
        return None

    async def _dispatch(self, package_name: str, is_delete: bool) -> None:
        if is_delete:
            self._manager.unload(package_name)
        else:
            self._manager.reload(package_name)

    def _dispatch_sync(self, package_name: str, is_delete: bool) -> None:
        if is_delete:
            self._manager.unload(package_name)
        else:
            self._manager.reload(package_name)


_observer: Optional[BaseObserver] = None


def start_plugin_watcher(
    manager: PluginManager,
    plugins_dir: Optional[Path] = None,
    base_path: Optional[Path] = None,
) -> Optional[BaseObserver]:
    """启动插件目录监听。

    :param manager: 插件生命周期协调器
    :param plugins_dir: 用户插件目录，默认 ``mas_config.plugins_dir``
    :param base_path: 计算 module name 的基准路径，默认 ``Path.cwd()``
    :return: Observer 实例；监听未启动时返回 None
    """
    global _observer
    plugins_dir = Path(plugins_dir) if plugins_dir else Path(mas_config.plugins_dir)
    base_path = base_path or Path.cwd()

    if not plugins_dir.exists():
        logger.debug(f"[PluginWatcher] plugins dir {plugins_dir} does not exist, not starting watcher")
        return None

    handler = PluginFileHandler(manager, plugins_dir, base_path)
    try:
        handler.bind_loop(asyncio.get_running_loop())
    except RuntimeError:
        logger.debug("[PluginWatcher] no running loop at watcher start; using sync dispatch fallback")

    observer = Observer()
    observer.schedule(handler, str(plugins_dir), recursive=True)
    observer.start()
    _observer = observer
    logger.info(f"[PluginWatcher] Started watching {plugins_dir}")
    return observer


def stop_plugin_watcher() -> None:
    """停止插件目录监听。"""
    global _observer
    if _observer is not None:
        _observer.stop()
        _observer.join()
        _observer = None
        logger.info("[PluginWatcher] Stopped")
