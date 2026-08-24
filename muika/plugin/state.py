"""插件状态存储：按插件隔离的私有 dict，进程生命周期内存活。

状态属于插件的"数据"侧：卸载时**不清除**，热重载后新模块从这里取回同一
dict 对象恢复状态。可存放活对象（DB engine、缓存等），不做序列化。

跨进程重启的持久化归 :func:`muika.plugin.loader.get_plugin_data_dir`，
两者职责分明：状态存储管热重载恢复，data dir 管重启恢复。

Attributes:
    _store: 状态存储（卸载不清除）

Functions:
    get_state: 获取插件的状态 dict（按需创建，跨重载返回同一对象）
    drop_state: 丢弃插件状态（预留接口，暂不接命令）
"""

from __future__ import annotations

from typing import Any, Dict

_store: Dict[str, Dict[str, Any]] = {}
"""状态存储：package_name -> 插件私有 dict（卸载不清除）"""


def get_state(package_name: str) -> Dict[str, Any]:
    """获取指定插件的状态 dict，按需创建。

    重复调用（含热重载后）返回同一对象。
    """
    return _store.setdefault(package_name, {})


def drop_state(package_name: str) -> None:
    """丢弃指定插件的状态（预留接口，暂不接命令）。"""
    _store.pop(package_name, None)
