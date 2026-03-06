from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from ..schema import ActionMode, ActionOutput, BaseAction

if TYPE_CHECKING:
    from muika.core.executor import Executor
    from muika.core.state import MuikaState


class BaseTool(BaseAction):
    """Base class for immediate actions."""

    mode: Literal[ActionMode.IMMEDIATE] = ActionMode.IMMEDIATE

    @classmethod
    def is_enabled(cls) -> bool:
        """Return whether this tool is available to the Butler Agent.

        Subclasses may override this to gate availability on runtime config.
        """
        return True

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        raise NotImplementedError(f"{type(self).__name__} must implement handle()")
