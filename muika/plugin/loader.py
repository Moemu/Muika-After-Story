"""
实现插件的加载和管理

Attributes:
    _plugins (Dict[str, Plugin]): 插件注册表，存储已加载的插件
    _declared_plugins (Set[str]): 已声明插件注册表（不一定加载成功）
    _loading_plugin (ContextVar): 当前正在加载的插件包名，供 ``on_alconna`` /
        ``on_function_call`` 在注册时标记所有权

Functions:
    load_plugin: 加载单个插件
    load_plugins: 加载指定目录下的所有插件
    unload_plugin: 卸载单个插件并清理其注册的 commands / func_calls
    reload_plugin: 卸载后重新加载
    get_plugins: 获取已加载的插件列表
"""

import importlib
import inspect
import os
import sys
from contextvars import ContextVar
from pathlib import Path
from typing import Dict, Optional, Set

from muika.config import mas_config
from muika.utils.logger import logger

from .lifecycle import clear_hooks, run_load_hooks, run_unload_hooks
from .models import Plugin, PluginMetadata
from .utils import path_to_module_name

_plugins: Dict[str, Plugin] = {}
"""插件注册表"""
_declared_plugins: Set[str] = set()
"""已声明插件注册表（不一定加载成功）"""
_loading_plugin: ContextVar[Optional[str]] = ContextVar("_loading_plugin", default=None)
"""当前正在加载的插件包名；供 on_alconna / on_function_call 读取以标记所有权。"""


def load_plugin(module_name: str) -> Optional[Plugin]:
    """
    加载单个插件模块。

    加载期间通过 :data:`_loading_plugin` ContextVar 广播当前包名，
    模块内触发的 ``on_alconna`` / ``on_function_call`` 注册会读到该值，
    从而建立"命令/工具 → 插件"的归属关系，便于后续按插件卸载。

    :param module_name: 模块的全限定包名（如 ``"plugins.notes"``）
    :return: 插件对象，若已有同名插件声明则返回 None
    """
    try:
        if module_name in _declared_plugins:
            logger.warning(f"插件 '{module_name}' 包名出现冲突，跳过加载")
            return None
        _declared_plugins.add(module_name)

        logger.debug(f"加载 MAS 插件: {module_name}")
        token = _loading_plugin.set(module_name)
        try:
            module = importlib.import_module(module_name)
        finally:
            _loading_plugin.reset(token)

        metadata: Optional[PluginMetadata] = getattr(module, "metadata", None)

        plugin = Plugin(
            name=metadata.name if metadata else module_name.rsplit(".", 1)[-1],
            module=module,
            package_name=module_name,
            meta=metadata,
        )

        _plugins[plugin.package_name] = plugin
        run_load_hooks(module_name)
        logger.success(f"插件 '{plugin.name}' ({module_name}) 已加载")

        return plugin

    except Exception as e:
        logger.error(f"加载插件 '{module_name}' 失败: {e}")
        # 回滚失败加载的部分副作用：避免 _declared_plugins 永久毒化、
        # 或残留半途注册的 commands / func_calls / hooks（否则修复后无法重新加载）
        _declared_plugins.discard(module_name)
        _plugins.pop(module_name, None)
        _purge_side_effects(module_name)
        return None


def _purge_side_effects(package_name: str) -> None:
    """清理指定插件包注册的全部 commands / func_calls 及 sys.modules 条目。"""
    # 延迟 import 避免循环依赖
    from .command import remove_commands_for_plugin
    from .func_call.caller import remove_callers_for_plugin

    removed_cmds = remove_commands_for_plugin(package_name)
    removed_calls = remove_callers_for_plugin(package_name)
    logger.debug(f"[PluginLoader] purge {package_name!r}: removed {removed_cmds} commands, {removed_calls} func_calls")

    # 清除 sys.modules 中该 package 及其子模块，以便下次加载重新执行模块代码
    for mod_name in [m for m in sys.modules if m == package_name or m.startswith(f"{package_name}.")]:
        del sys.modules[mod_name]
    clear_hooks(package_name)


def unload_plugin(package_name: str) -> bool:
    """卸载单个插件：从注册表移除并清理其注册的 commands / func_calls / sys.modules。

    :param package_name: 插件的 package_name（即模块全限定名）
    :return: 是否成功卸载（插件不存在返回 False）
    """
    if package_name not in _plugins:
        logger.warning(f"[PluginLoader] unload: plugin {package_name!r} not loaded")
        return False

    run_unload_hooks(package_name)
    _purge_side_effects(package_name)
    del _plugins[package_name]
    _declared_plugins.discard(package_name)

    logger.success(f"[PluginLoader] Plugin {package_name!r} unloaded")
    return True


def reload_plugin(package_name: str) -> Optional[Plugin]:
    """卸载后重新加载指定插件；若插件从未加载过则直接加载。

    :param package_name: 插件的 package_name
    :return: 重新加载后的 Plugin 对象；卸载失败或加载失败返回 None
    """
    if package_name in _plugins and not unload_plugin(package_name):
        return None
    return load_plugin(package_name)


def load_plugins(*plugins_dirs: Path | str, base_path=Path.cwd()) -> set[Plugin]:
    """
    加载传入插件目录中的所有插件

    :param plugins_dirs: 插件目录
    :param base_path: 外部插件的基准路径
    :return: 插件对象集合
    """

    plugins = set()

    for plugin_dir in plugins_dirs:
        plugin_dir_path = Path(plugin_dir) if isinstance(plugin_dir, str) else plugin_dir
        if not plugin_dir_path.exists():
            logger.debug(f"插件目录 '{plugin_dir_path}' 不存在，跳过")
            continue

        for plugin in os.listdir(plugin_dir_path):
            plugin_path = Path(os.path.join(plugin_dir_path, plugin))
            module_name = None

            if plugin_path.is_file() and plugin_path.suffix == ".py" and plugin_path.name != "__init__.py":
                module_name = path_to_module_name(plugin_path.with_suffix(""), base_path)
            elif plugin_path.is_dir() and (plugin_path / Path("__init__.py")).exists():
                module_name = path_to_module_name(plugin_path, base_path)
            elif plugin_path.is_dir() and (plugin_path / plugin_path.name.lower().replace("-", "_")).exists():
                module_name = path_to_module_name(plugin_path / plugin_path.name.lower().replace("-", "_"), base_path)
            if module_name and (loaded_plugin := load_plugin(module_name)):
                plugins.add(loaded_plugin)

    return plugins


def _get_caller_plugin_name() -> Optional[str]:
    """
    获取当前调用插件名
    （默认跳过 `MAS` 本身及其内嵌插件）
    """
    current_frame = inspect.currentframe()
    if current_frame is None:
        return None

    # find plugin
    frame = current_frame
    while frame := frame.f_back:  # type: ignore
        module_name = (module := inspect.getmodule(frame)) and module.__name__

        if module_name is None:
            return None

        # skip muika it self
        package_name = module_name.split(".", maxsplit=1)[0]
        if package_name == "muika" and not module_name.startswith("muika.builtin_plugins"):
            continue

        # 将模块路径拆解为层级列表（例如 a.b.c → ["a", "a.b", "a.b.c"]）
        module_segments = module_name.split(".")
        candidate_paths = [".".join(module_segments[: i + 1]) for i in range(len(module_segments))]

        # 从长到短查找最长匹配
        for candidate in reversed(candidate_paths):
            if candidate in _declared_plugins:
                return candidate.split(".")[-1]

    return None


def get_plugins() -> Dict[str, Plugin]:
    """
    获取插件列表
    """
    return _plugins


def get_plugin_by_module_name(module_name: str) -> Optional[Plugin]:
    """
    通过包名获取插件对象
    """
    while True:
        if module_name in _plugins:
            return _plugins[module_name]
        if "." not in module_name:
            return None
        module_name = module_name.rsplit(".", 1)[0]


def get_plugin_data_dir() -> Path:
    """
    获取 Muika-After-Story 插件数据目录

    对于 MAS 的插件，它们的插件目录位于 MAS 的插件目录中下的 `plugins` 文件夹，并以插件名命名
    """
    plugin_name = _get_caller_plugin_name()
    plugin_name = plugin_name or ".unknown"

    plugin_dir = mas_config.data_dir / "plugin"
    plugin_dir = plugin_dir.joinpath(plugin_name).resolve()
    plugin_dir.mkdir(parents=True, exist_ok=True)

    logger.debug(plugin_dir)

    return plugin_dir
