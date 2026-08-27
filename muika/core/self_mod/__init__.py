"""Muika 自我修改子系统（L1/L2 自我迭代的策略、校验与版本管理核心）。"""

from .manager import SelfModManager, get_self_mod_manager
from .policy import SelfModError
from .proposals import CoreProposalError, CoreProposalManager, get_core_proposal_manager

__all__ = [
    "SelfModManager",
    "get_self_mod_manager",
    "SelfModError",
    "CoreProposalError",
    "CoreProposalManager",
    "get_core_proposal_manager",
]
