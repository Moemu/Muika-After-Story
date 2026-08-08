"""
实现插件的加载和管理

Attributes:
    _plugins (Dict[str, Plugin]): 插件注册表，存储已加载的插件
Functions:
    load_plugin: 加载单个插件
    load_plugins: 加载指定目录下的所有插件
    get_plugins: 获取已加载的插件列表
"""

import importlib
import inspect
import os
from pathlib import Path
from typing import Dict, Optional, Set

from muika.config import mas_config
from muika.utils.logger import logger

from .models import Plugin, PluginMetadata
from .utils import path_to_module_name

_plugins: Dict[str, Plugin] = {}
"""插件注册表"""
_declared_plugins: Set[str] = set()
"""已声明插件注册表（不一定加载成功）"""


def load_plugin(module_name: str) -> Optional[Plugin]:
    """
    加载单个插件模块。

    :param module_name: 模块的全限定包名（如 ``"plugins.notes"``）
    :return: 插件对象，若已有同名插件声明则返回 None
    """
    try:
        if module_name in _declared_plugins:
            logger.warning(f"插件 '{module_name}' 包名出现冲突，跳过加载")
            return None
        _declared_plugins.add(module_name)

        logger.debug(f"加载 MAS 插件: {module_name}")
        module = importlib.import_module(module_name)

        metadata: Optional[PluginMetadata] = getattr(module, "metadata", None)

        plugin = Plugin(
            name=metadata.name if metadata else module_name.rsplit(".", 1)[-1],
            module=module,
            package_name=module_name,
            meta=metadata,
        )

        _plugins[plugin.package_name] = plugin
        logger.success(f"插件 '{plugin.name}' ({module_name}) 已加载")

        return plugin

    except Exception as e:
        logger.error(f"加载插件 '{module_name}' 失败: {e}")
        return None


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
