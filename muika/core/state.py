from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from nonebot import logger

from .intents import Intent, Persistence


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
    last_interaction: datetime = field(default_factory=datetime.now)
    """最近一次交流时间"""
    active_intent: Optional[Intent] = None
    """目前的想法"""
    last_executed_intent: Optional[Intent] = None
    """上一次执行的想法"""
    pending_intents: list[Intent] = field(default_factory=list)
    """未执行的念头"""

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
