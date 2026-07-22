from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from .memory import MemoryManager

if TYPE_CHECKING:
    from .events import Event

from .constants import BOREDOM_RATE, LONELINESS_RATE


@dataclass
class ActiveTopicState:
    """当前活跃话题的生命周期追踪，由 TopicManager 写入，Session 结束时清空并评分。"""

    topic_id: str
    topic_seed: str
    topic_type: str
    started_at: datetime = field(default_factory=datetime.now)
    user_engaged: bool = False
    """用户在本话题发出后是否发送过任何消息。"""


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
    last_proactive_at: Optional[datetime] = field(default=None)
    """最近一次由孤独感驱动主动发言的时间，用于冷却期判断。"""

    active_topic: Optional["ActiveTopicState"] = field(default=None)
    """当前活跃话题，由 TopicManager 写入，Session 结束时清空并评分。"""

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

        if not event.type == "user_message":
            return

        # 用户发消息了，重置注意力，增加陪伴感，降低无聊感
        self.loneliness = 0.0
        self.attention = 1.0
        self.last_interaction = now

        # 如果有活跃话题，标记用户参与了互动
        if self.active_topic is not None:
            self.active_topic.user_engaged = True
