"""Muika 自我修改的策略、校验、工作区与插件部署。"""

from .manager import SelfModManager, get_self_mod_manager
from .policy import SelfModError
from .workspace import (
    SelfWorkspaceError,
    SelfWorkspaceManager,
    get_self_workspace_manager,
)

__all__ = [
    "SelfModManager",
    "get_self_mod_manager",
    "SelfModError",
    "SelfWorkspaceManager",
    "SelfWorkspaceError",
    "get_self_workspace_manager",
]
