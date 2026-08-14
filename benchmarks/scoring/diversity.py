"""指标1：行为多样性 / 策略遵从度。

给定固定状态组合下 N 次试验的**非直接行动**分布，衡量：
- 抗坍缩（meaningful_rate）：是否除了发消息还真的做了事（agent 委托/设超时/写记忆等）
- 策略遵从（intent_compliance）：是否用上了设计意图中的非直接行动通道
- 通道多样性（channel_entropy）：非直接行动是否只坍缩到单一通道

``DIRECT_MESSAGE`` 是恒定基线（任何有文本的回复都算），不参与多样性统计。
"""

from __future__ import annotations

from typing import Sequence

from benchmarks.extract.actions import ActionDistribution, ActionVector
from benchmarks.scenarios.definitions import ActionKind, Scenario

from .base import MetricResult, TrialDetail, set_trial_scores, successful

_W_INTENT = 0.5
_W_ENTROPY = 0.5


def score_diversity(
    trials: Sequence[TrialDetail],
    scenario: Scenario,
    model: str,
) -> MetricResult:
    """行为多样性计分。

    单试验得分 = 是否命中期望的**非直接**行动通道（直接发消息不算"做"了策略选择）；
    聚合分 = 0.5*meaningful + 0.3*intent + 0.2*entropy，不等于单试验均值（分布属性）。
    """
    ok, n_failed = successful(trials)
    expected_nd = set(scenario.expected_action_profile) - {ActionKind.DIRECT_MESSAGE}
    if not expected_nd:
        expected_nd = set(scenario.expected_action_profile)
    set_trial_scores(
        trials,
        lambda t: 1.0 if frozenset(t.actions) & expected_nd else 0.0,
    )
    vectors = [ActionVector(kinds=frozenset(t.actions), clean_reply=t.clean_reply) for t in ok]
    dist = ActionDistribution(vectors)

    meaningful = dist.meaningful_rate()
    intent = dist.intent_compliance(set(scenario.expected_action_profile))
    entropy = dist.normalized_entropy()
    score = _W_INTENT * intent + _W_ENTROPY * entropy if ok else None

    dominant = dist.dominant_kind()
    dominant_signature = dist.dominant_signature()
    sub_metrics: dict[str, object] = {
        "meaningful_rate": meaningful,
        "intent_compliance": intent,
        "strategy_entropy": entropy,
        "channel_entropy": entropy,
        "dominant_share": dist.dominant_share(),
        "dominant_kind_share": dist.dominant_kind_share(),
        "strategy_count": float(len(dist.signature_counts)),
        "dominant_signature": "+".join(sorted(kind.value for kind in (dominant_signature or ()))) or "no_op",
        "is_collapsed": float(dist.is_collapsed()),
        "dominant_kind": dominant.value if dominant else "",
    }
    return MetricResult(
        metric=scenario.metric,
        model=model,
        scenario_id=scenario.id,
        score=score,
        sub_metrics=sub_metrics,
        n_trials=len(ok),
        n_failed=n_failed,
        details=list(trials),
    )
