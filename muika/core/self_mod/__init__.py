"""Muika 自我修改的策略、校验与版本管理。"""

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
