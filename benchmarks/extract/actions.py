"""行动抽取与分布统计。

从 ``Muika._parse_reply_tags`` 解析出的 ``ParsedReply`` 构建行动向量，
并对 N 次试验的行动做分布统计（占比 / 坍缩率 / 归一化熵 / 策略遵从）。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from math import log
from typing import Sequence, Set

from benchmarks.scenarios.definitions import ActionKind
from muika.core.loop import ParsedReply


@dataclass(frozen=True)
class ActionVector:
    """单次回复的可观测行动向量。"""

    kinds: frozenset[ActionKind]
    clean_reply: str
    n_agent_commands: int = 0
    n_memory_writes: int = 0
    timeout: float | None = None
    target: str | None = None
    god_mode: bool = False


def classify_actions(parsed: ParsedReply) -> ActionVector:
    """从解析后的回复标签构建行动向量。

    行动类别由标签结构决定，与 clean_reply 的具体文案无关：
    - ``clean_reply`` 非空 → DIRECT_MESSAGE
    - 有 ``agent_commands`` → AGENT_DELEGATION
    - 有 ``memory_contents`` → MEMORY_WRITE
    - ``god_mode`` → GOD_MODE
    - ``timeout`` 非空 → TIMEOUT_SET
    - ``target`` 非空 → TARGET_ROUTE
    """
    kinds: Set[ActionKind] = set()
    if parsed.clean_reply.strip():
        kinds.add(ActionKind.DIRECT_MESSAGE)
    if parsed.agent_commands:
        kinds.add(ActionKind.AGENT_DELEGATION)
    if parsed.memory_contents:
        kinds.add(ActionKind.MEMORY_WRITE)
    if parsed.god_mode:
        kinds.add(ActionKind.GOD_MODE)
    if parsed.timeout is not None:
        kinds.add(ActionKind.TIMEOUT_SET)
    if parsed.target is not None:
        kinds.add(ActionKind.TARGET_ROUTE)

    return ActionVector(
        kinds=frozenset(kinds),
        clean_reply=parsed.clean_reply,
        n_agent_commands=len(parsed.agent_commands),
        n_memory_writes=len(parsed.memory_contents),
        timeout=parsed.timeout,
        target=parsed.target,
        god_mode=parsed.god_mode,
    )


class ActionDistribution:
    """N 次试验的非直接行动分布统计。

    ``DIRECT_MESSAGE`` 出现在几乎每次回复里（任何有文本的回复都算），是恒定基线，
    不参与多样性统计——让 agent 委托/超时/记忆/路由等"行动策略"驱动分布。
    一个试验可能同时命中多个非直接类别（多标签），故各 ``share`` 之和可大于 1。
    """

    def __init__(self, vectors: Sequence[ActionVector]) -> None:
        self.vectors: list[ActionVector] = list(vectors)
        # 非直接行动视图：剔除 DIRECT_MESSAGE 这条恒定基线
        self.trials: list[frozenset[ActionKind]] = [v.kinds - {ActionKind.DIRECT_MESSAGE} for v in self.vectors]
        self.n = len(self.trials)
        self.signature_counts: Counter[frozenset[ActionKind]] = Counter(self.trials)
        self.counts: dict[ActionKind, int] = {kind: 0 for kind in ActionKind}
        for kinds in self.trials:
            for kind in kinds:
                self.counts[kind] += 1
        self._dominant: ActionKind | None = max(ActionKind, key=lambda k: self.counts[k]) if self.n else None

    def meaningful_rate(self) -> float:
        """非直接行动出现率：≥1 个非 direct 行动的试验占比（抗坍缩信号）。

        "只发消息"的模型此值趋近 0；会委托/设超时/写记忆的模型趋近 1。
        """
        if self.n == 0:
            return 0.0
        return sum(1 for kinds in self.trials if kinds) / self.n

    def share(self, kind: ActionKind) -> float:
        """某类非直接行动出现的试验占比。"""
        if self.n == 0:
            return 0.0
        return self.counts[kind] / self.n

    def dominant_share(self) -> float:
        """Share of the most common exact non-direct action plan (including no-op)."""
        if self.n == 0:
            return 0.0
        return max(self.signature_counts.values(), default=0) / self.n

    def dominant_kind_share(self) -> float:
        """Legacy marginal share of the most common individual action channel."""
        if self.n == 0 or self._dominant is None:
            return 0.0
        return self.counts[self._dominant] / self.n

    def dominant_signature(self) -> frozenset[ActionKind] | None:
        if not self.signature_counts:
            return None
        return self.signature_counts.most_common(1)[0][0]

    def dominant_kind(self) -> ActionKind | None:
        """出现频率最高的非直接行动类别。"""
        return self._dominant

    def normalized_entropy(self) -> float:
        """Categorical entropy over exact action-plan signatures.

        Marginal multi-label counts are not a probability distribution.  Treating each full
        plan (including the empty/no-op plan) as one category prevents an always-Agent model
        from receiving a high diversity score merely because direct messages co-occur.
        """
        if self.n <= 1 or len(self.signature_counts) <= 1:
            return 0.0
        entropy = -sum((count / self.n) * log(count / self.n) for count in self.signature_counts.values())
        possible_signatures = 2 ** (len(ActionKind) - 1)
        denominator = log(min(self.n, possible_signatures))
        return entropy / denominator if denominator > 0 else 0.0

    def intent_compliance(self, expected: Set[ActionKind]) -> float:
        """期望的非直接通道命中率：(1/N)Σ[actions(trial) ∩ expected_non_direct ≠ ∅]。

        期望集剔除 DIRECT_MESSAGE 后检查（若剔除后为空则回退全期望），衡量模型
        在该状态下是否使用过设计意图中的"行动"通道，而非只是发了消息。
        """
        expected_nd = set(expected) - {ActionKind.DIRECT_MESSAGE}
        if not expected_nd:
            expected_nd = set(expected)
        if self.n == 0:
            return 0.0
        hits = sum(1 for kinds in self.trials if kinds & expected_nd)
        return hits / self.n

    def is_collapsed(self, threshold: float = 0.5) -> bool:
        """是否坍缩为只发消息：非直接行动出现率低于 threshold。"""
        return self.meaningful_rate() < threshold
