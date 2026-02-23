from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Union

from muika.models import Resource


@dataclass
class ActionOutput:
    content: str
    resources: List[Resource] = field(default_factory=list)


ActionHandler = Callable[..., Awaitable[Union[str, ActionOutput]]]
