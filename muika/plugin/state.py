"""保存插件在当前进程内的热重载状态。"""

from __future__ import annotations

from typing import Any

_store: dict[str, dict[str, Any]] = {}


def get_state(package_name: str) -> dict[str, Any]:
    """返回插件状态，不存在时创建。

    :param package_name: 插件模块名
    :return: 插件状态字典
    """
    return _store.setdefault(package_name, {})


def drop_state(package_name: str) -> None:
    """删除指定插件的进程内状态。

    :param package_name: 插件模块名
    """
    _store.pop(package_name, None)
