from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from nonebot import logger

from .intents import Intent, Persistence

if TYPE_CHECKING:
    from .events import Event

# 定义情绪充满所需的时间（秒）
TIME_TO_FULL_LONELINESS = 3600.0  # 1小时
TIME_TO_FULL_BOREDOM = 7200.0  # 2小时

# 计算每秒增长率
LONELINESS_RATE = 1.0 / TIME_TO_FULL_LONELINESS
BOREDOM_RATE = 1.0 / TIME_TO_FULL_BOREDOM


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
    active_intent: Optional[Intent] = None
    """目前的想法"""
    last_executed_intent: Optional[Intent] = None
    """上一次执行的想法"""
    pending_intents: list[Intent] = field(default_factory=list)
    """未执行的念头"""

    def tick_state(self, event: "Event", dt: float):
        # 1. 随着时间流逝，注意力下降
        self.attention = max(0.0, self.attention - 0.05)

        if not self.active_intent:
            self.loneliness = min(1.0, self.loneliness + (LONELINESS_RATE * dt))
            self.boredom = min(1.0, self.boredom + (BOREDOM_RATE * dt))
            self.curiosity *= 0.99  # 探索欲缓慢下降

        # 2. 基于规则的状态机
        now = datetime.now()

        if event.type == "user_message":
            self.loneliness = 0.0
            self.attention = 1.0
            self.last_interaction = now

    def tick_intents(self):
        """
        根据 Persistence 模式清洗意图池
        """
        alive_intents = []

        for intent in self.pending_intents:

            # 1. EPHEMERAL (瞬时)
            # 如果它在 pending 列表里，说明上一轮 Loop 产生后没被执行（可能因为在此之前被插队了）
            # 瞬时意图只要过了一轮没执行，就该扔了
            if intent.persistence == Persistence.EPHEMERAL:
                logger.debug(f"Discarding ephemeral intent: {intent.name}")
                continue

            # 2. SHORT_TERM (短期)
            elif intent.persistence == Persistence.SHORT_TERM:
                intent.missed_cycles += 1
                if intent.missed_cycles > 5:
                    logger.debug(f"Discarding short-term intent due to missed cycles: {intent.name}")
                    continue

            # 3. STICKY (长期)
            # 永远保留，直到被外部显式移除（执行成功）
            elif intent.persistence == Persistence.STICKY:
                alive_intents.append(intent)

        self.pending_intents = alive_intents
