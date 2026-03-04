from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel

from muika.models import Resource

if TYPE_CHECKING:
    from muika.core.executor import Executor
    from muika.core.state import MuikaState


class ActionMode(str, Enum):
    IMMEDIATE = "immediate"
    SCHEDULED = "scheduled"


@dataclass
class ActionOutput:
    content: str
    resources: list[Resource] = field(default_factory=list)
    silent: bool = False
    """
    True 表示此操作为副作用（如写入记忆），结果不需要回报给核心模型。
    Butler 将跳过 Analysis LLM 调用，不将任何内容注入 inner context。
    """


class BaseAction(BaseModel):
    """Unified action contract for all executable capabilities."""

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        raise NotImplementedError(f"{type(self).__name__} must implement handle()")
