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


class BaseAction(BaseModel):
    """Unified action contract for all executable capabilities."""

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        raise NotImplementedError(f"{type(self).__name__} must implement handle()")
