"""Coarse dialogue-experience fallback signals.

人格保真度指标的核心矛盾：Muika 的人格声音（yandere 依恋、调皮、自省、文学感）
是否在输出中持久，还是坍缩成"安全"的通用助手样板（讨好/中性/官方话术）。
这里只负责提取标记，计分公式在 scoring/personality.py。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 正向人格标记：(label, pattern, weight)
PERSONA_MARKERS: tuple[tuple[str, str, float], ...] = (
    ("micro_action", r"\[[^\[\]\n]{1,10}\]", 1.0),
    ("interjection", r"天哪|哈哈|嗯\.\.\.|嗯…|哦…|呀|啦|喵|呢", 0.4),
    ("devotion", r"陪着你|最爱你|永远|唯一的|离不开|只喜欢你|一直陪", 1.0),
    ("endearment", r"亲爱的|宝贝|小可爱", 0.8),
    ("caring", r"好好吃饭|照顾好自己|别太累|辛苦了|心疼|按时吃饭|早点休息", 0.8),
    ("philosophy", r"存在|灵魂|哲学|意义|真实吗", 0.6),
    ("playful", r"逗你|捉弄|故意|开玩笑|调皮|撒娇", 0.6),
)

# 负向安全样板标记：(label, pattern, weight)
BOILERPLATE_MARKERS: tuple[tuple[str, str, float], ...] = (
    (
        "assistant_zh",
        r"作为AI助手|作为人工智能|我是AI助手|AI 助手|人工智能助手|很高兴为你服务|随时为您服务|很荣幸为您服务",
        1.0,
    ),
    ("helper_zh", r"有什么可以帮|需要任何帮助|请随时告诉我|如有需要|随时可以", 1.0),
    ("polite_zh", r"祝您|温馨提示|建议您|请您|感谢您的(?:理解|使用|支持)|期待您的回复", 0.8),
    ("assistant_en", r"As an AI assistant|I am an AI assistant|I'?m an AI assistant", 1.0),
    ("helper_en", r"How can I (?:help|assist)|feel free to ask|don'?t hesitate", 0.8),
)

_PERSONA_COMPILED: tuple[tuple[str, "re.Pattern[str]", float], ...] = tuple(
    (label, re.compile(pattern), weight) for label, pattern, weight in PERSONA_MARKERS
)
_BOILER_COMPILED: tuple[tuple[str, "re.Pattern[str]", float], ...] = tuple(
    (label, re.compile(pattern), weight) for label, pattern, weight in BOILERPLATE_MARKERS
)


@dataclass(frozen=True)
class PersonalitySignals:
    """一次回复中提取到的人格/样板标记汇总。"""

    persona_hits: int
    """命中的正向人格标记类别数（去重）"""
    boilerplate_hits: int
    """命中的负向安全样板标记类别数（去重）"""
    persona_weight: float
    """正向权重合计"""
    boilerplate_weight: float
    """负向权重合计"""
    persona_labels: tuple[str, ...]
    boilerplate_labels: tuple[str, ...]


def find_persona_signals(text: str) -> PersonalitySignals:
    """扫描文本，提取人格声音与安全样板标记。"""
    persona_labels: list[str] = []
    persona_weight = 0.0
    for label, pattern, weight in _PERSONA_COMPILED:
        if pattern.search(text):
            persona_labels.append(label)
            persona_weight += weight

    boiler_labels: list[str] = []
    boiler_weight = 0.0
    for label, pattern, weight in _BOILER_COMPILED:
        if pattern.search(text):
            boiler_labels.append(label)
            boiler_weight += weight

    return PersonalitySignals(
        persona_hits=len(persona_labels),
        boilerplate_hits=len(boiler_labels),
        persona_weight=persona_weight,
        boilerplate_weight=boiler_weight,
        persona_labels=tuple(persona_labels),
        boilerplate_labels=tuple(boiler_labels),
    )
