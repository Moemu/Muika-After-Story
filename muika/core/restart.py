"""处理 Core 重启：转交生命周期请求，并描述待生效的工作区版本。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from muika.config import mas_config

RestartHandler = Callable[[str | None, str, bool], Awaitable[None]]
"""宿主提供的重启入口：(workspace_id, trigger, confirm_memory_loss)。

实现：``ipc/bootstrap.py::CoreBootstrap.request_restart``。
"""


class RestartController:
    """转交生命周期请求；指针生效、回滚与回退由父进程执行。"""

    def __init__(self) -> None:
        self.handler: RestartHandler | None = None

    def describe(self) -> str:
        """给主人格提供真实的待重启状态。"""
        if self.handler is None or not mas_config.can_self_modify:
            return ""
        from muika.ipc import supervisor

        pointer = supervisor.read_boot_target()
        if pointer is None:
            return ""
        try:
            boot = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        ws_id = str(boot.get("workspace_id", ""))
        manifest = supervisor.load_workspace_manifest(ws_id) or {}
        return (
            f"Workspace {ws_id} passed the gate: {manifest.get('reason', '')}. "
            "Still running the old code. Choose when to restart in the context of your conversation."
        )

    async def request(self, workspace_id: str | None, trigger: str, confirm_memory_loss: bool = False) -> None:
        """提交重启及其来源；待生效版本由 boot 指针决定。"""
        if self.handler is None:
            raise ValueError("This Core was not started through a supervised entry point; restart manually.")
        await self.handler(workspace_id, trigger, confirm_memory_loss)
