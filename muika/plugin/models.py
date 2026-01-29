from __future__ import annotations

from dataclasses import dataclass, field
from types import ModuleType
from typing import TYPE_CHECKING, Any, Optional, Type, Union

from nonebot.plugin import PluginMetadata as NonebotPluginMetadata

if TYPE_CHECKING:
    from pydantic import BaseModel


class PluginMetadata(NonebotPluginMetadata):
    """MAS 插件元数据"""

    name: str
    """插件名"""
    description: str
    """插件描述"""
    usage: str
    """插件用法"""
    homepage: Optional[str] = None
    """(可选) 插件主页，通常为开源存储库地址"""
    config: Optional[Type["BaseModel"]] = None
    """插件配置项类，如无需配置可不填写"""
    extra: dict[Any, Any] = field(default_factory=dict)
    """不知道干嘛的 extra 信息，我至今都没搞懂，喜欢的可以填"""


@dataclass
class Plugin:
    """MAS 插件对象"""

    name: str
    """插件名称"""
    module: ModuleType
    """插件模块对象"""
    package_name: str
    """模块包名"""
    meta: Optional[Union[PluginMetadata, NonebotPluginMetadata]] = None
    """插件元数据"""

    def __hash__(self) -> int:
        return hash(self.package_name)

    def __eq__(self, other: Any) -> bool:
        return self.package_name == other.package_name if hasattr(other, "package_name") else False

    def __str__(self) -> str:
        return self.package_name

    class Config:
        arbitrary_types_allowed = True
