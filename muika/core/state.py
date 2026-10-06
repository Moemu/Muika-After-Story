from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

from pydantic import BaseModel

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


class StateRhythm(BaseModel):
    """外部事件或主动行动后的即时驱动，空闲衰减由每台设备本地计算。"""

    attention: float
    loneliness: float
    curiosity: float
    boredom: float
    last_interaction: datetime
    last_proactive_at: datetime | None
    active_topic: ActiveTopicState | None


@runtime_checkable
class NodeController(Protocol):
    """提供设备状态和交接请求，核心不依赖网络实现。"""

    name: str
    nodes: list[str]
    active: bool
    connected: bool
    sync_error: str | None

    async def handoff(self, target: str) -> None: ...


@dataclass
class MuikaState:
    nodes: NodeController | None = field(default=None, repr=False)
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

    memory: Optional[MemoryManager] = field(default=None, repr=False)
    """对 MemoryManager 的引用，由外部注入，供 Action 工具访问"""

    def restore_rhythm(self, rhythm: StateRhythm) -> None:
        """应用已发生的驱动变化，不调用模型。"""
        self.attention, self.loneliness = rhythm.attention, rhythm.loneliness
        self.curiosity, self.boredom = rhythm.curiosity, rhythm.boredom
        self.last_interaction, self.last_proactive_at = rhythm.last_interaction, rhythm.last_proactive_at
        self.active_topic = rhythm.active_topic

    @property
    def mood(self) -> str:
        """情绪（派生属性）：依据孤独感/无聊感阈值实时计算，不存储。

        作为派生值，情绪始终与当前状态一致——用户消息重置孤独感后立即
        反映为 calm，不存在残留。优先级：孤独感 > 无聊感 > calm。
        """
        if self.loneliness > 0.8:
            return "lonely"
        if self.boredom > 0.7:
            return "bored"
        return "calm"

    def tick_state(self, event: "Event", dt: float):
        # 1. 随着时间流逝，注意力下降
        elapsed = max(0.0, dt)
        self.attention = max(0.0, self.attention - 0.01 * elapsed)

        self.loneliness = min(1.0, self.loneliness + LONELINESS_RATE * elapsed)
        self.boredom = min(1.0, self.boredom + BOREDOM_RATE * elapsed)
        self.curiosity *= 0.99 ** (elapsed / 5)

        # 2. 基于规则的状态机：用户发消息时重置注意力与孤独感，
        #    情绪由 ``mood`` 属性按需派生，无需在此维护。
        now = datetime.now()

        if event.type == "user_message":
            # 用户发消息了，重置注意力，增加陪伴感，降低无聊感
            self.loneliness = 0.0
            self.attention = 1.0
            self.last_interaction = now

            # 如果有活跃话题，标记用户参与了互动
            if self.active_topic is not None:
                self.active_topic.user_engaged = True
