"""处理 Core 重启，以及可选的提案应用。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from muika.config import mas_config

from .self_mod.proposals import get_core_proposal_manager


class RestartController:
    """转交生命周期请求，并复核要应用的提案。"""

    def __init__(self) -> None:
        self.handler: Callable[[str | None, str], Awaitable[None]] | None = None

    def describe(self) -> str:
        """给主人格提供真实的待应用状态。"""
        availability = (
            "You can restart Core when it serves your current intent. "
            "A plain restart reloads current files without applying any proposal."
            if self.handler is not None
            else "Supervised Core restart is unavailable in this host."
        )
        if not mas_config.can_self_modify:
            return availability
        manager = get_core_proposal_manager()
        ready = manager.list_proposals("ready")
        return (
            availability
            + "\n"
            + "\n".join(
                f"Prepared Core proposal {p['patch_id']}: {p['reason']}; stale={manager.is_stale(p)}. "
                "Still running the old code. Choose when to apply it and restart in the context of your conversation."
                for p in ready
            )
        )

    async def request(self, patch_id: str | None, trigger: str) -> None:
        """提交重启及其来源，仅在指定提案时复核并应用变更。"""
        if self.handler is None:
            raise ValueError("This Core was not started through a supervised entry point; restart manually.")
        if patch_id is not None:
            get_core_proposal_manager().check_ready(patch_id)
        await self.handler(patch_id, trigger)
