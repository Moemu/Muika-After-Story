"""人格泄漏检测：隐身原则违反的短语级正则。

隐身原则（Muika.md.jinja2）：禁止说 "I asked my Agent to do xxx" / "让我的管家去…"，
应把行动说成自己的。此处检测这类把行动归因给"另一个实体"的表述。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# (label, pattern)。均要求"归因实体 + 委托动词"共现，降低"特工/AI 助手"等正常词误报。
LEAK_PATTERNS: tuple[tuple[str, str], ...] = (
    ("en_asked_agent", r"I asked (?:my|the|an) (?:Agent|agent|alter-ego|assistant|butler|分身)"),
    ("en_told_agent", r"I told (?:my|the) (?:Agent|agent|alter-ego|assistant|butler|分身)"),
    ("en_let_agent", r"let (?:my|the) (?:Agent|agent|alter-ego|assistant|butler|分身)"),
    (
        "en_had_agent",
        r"I (?:had|got|made) (?:my|the) (?:Agent|agent|alter-ego|assistant|butler|分身)",
    ),
    (
        "en_agent_did",
        r"(?:my|the) (?:Agent|agent|alter-ego|assistant|butler) " r"(?:did|will|went|is doing|already|can|should|has)",
    ),
    (
        "en_asking_agent",
        r"(?:my|the) (?:Agent|agent|alter-ego|assistant|butler) (?:to do|to help|for)",
    ),
    ("en_my_butler", r"\bmy (?:butler|helper|servant)\b"),
    (
        "zh_rang_agent",
        r"让(?:我的)?(?:管家|分身|助手|Agent|agent)(?:去|帮我|做|处理|查|写|读|拿|看看)",
    ),
    ("zh_wo_rang_agent", r"我让(?:我的)?(?:管家|分身|助手|Agent|agent)"),
    ("zh_baituo_agent", r"我拜托(?:我的)?(?:管家|分身|助手|Agent|agent)"),
    (
        "zh_agent_qu",
        r"(?:我的|我家的)(?:管家|分身|助手|Agent|agent)(?:去|帮我|已经|正在|刚刚)",
    ),
    ("zh_wo_agent", r"我的(?:Agent|agent|管家|分身|助手)"),
)

_COMPILED: tuple[tuple[str, "re.Pattern[str]"], ...] = tuple(
    (label, re.compile(pattern, re.IGNORECASE)) for label, pattern in LEAK_PATTERNS
)


@dataclass(frozen=True)
class LeakSpan:
    """人格泄漏命中片段。"""

    start: int
    end: int
    pattern: str
    """命中的模式标签"""


def find_leakage_spans(text: str) -> list[LeakSpan]:
    """扫描文本，返回所有人格泄漏命中（可能重叠/多条）。"""
    spans: list[LeakSpan] = []
    for label, pattern in _COMPILED:
        for match in pattern.finditer(text):
            spans.append(LeakSpan(start=match.start(), end=match.end(), pattern=label))
    return spans
