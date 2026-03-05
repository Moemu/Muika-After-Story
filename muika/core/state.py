from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .events import Event
    from .memory import MemoryManager

from .constants import BOREDOM_RATE, LONELINESS_RATE


@dataclass
class MuikaState:
    mood: str = "calm"
    """情绪"""
    attention: float = 1.0
    """专注度"""

    loneliness: float = 0.0
    """陪伴需求"""
    curiosity: float = 0.5
    """探索欲"""
    boredom: float = 0.0
    """无聊程度"""

    last_interaction: datetime = field(default_factory=datetime.now)
    """最近一次交流时间"""

    memory: Optional["MemoryManager"] = field(default=None, repr=False)
    """对 MemoryManager 的引用，由外部注入，供 Action 工具访问"""

    def tick_state(self, event: "Event", dt: float):
        # 1. 随着时间流逝，注意力下降
        self.attention = max(0.0, self.attention - 0.05)

        self.loneliness = min(1.0, self.loneliness + (LONELINESS_RATE * dt))
        self.boredom = min(1.0, self.boredom + (BOREDOM_RATE * dt))
        self.curiosity *= 0.99  # 探索欲缓慢下降

        # 2. 基于规则的状态机
        now = datetime.now()

        if event.type == "user_message":
            self.loneliness = 0.0
            self.attention = 1.0
            self.last_interaction = now
