from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .intents import Intent


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
