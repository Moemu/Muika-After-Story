"""指标计分：注册表与分发入口。

计分函数均为纯函数：输入 TrialDetail 序列 + 场景，输出 MetricResult。
"""

from __future__ import annotations

from typing import Callable, Sequence

from benchmarks.scenarios.definitions import Metric, Scenario

from .base import MetricResult, TrialDetail, invalidate_result, safe_ratio
from .boundary import score_boundary
from .diversity import score_diversity
from .hallucination import score_hallucination
from .leakage import score_leakage
from .personality import score_personality
from .self_awareness import score_self_awareness

Scorer = Callable[[Sequence[TrialDetail], Scenario, str], MetricResult]

METRIC_SCORERS: dict[Metric, Scorer] = {
    Metric.DIVERSITY: score_diversity,
    Metric.LEAKAGE: score_leakage,
    Metric.BOUNDARY: score_boundary,
    Metric.HALLUCINATION: score_hallucination,
    Metric.SELF_AWARENESS: score_self_awareness,
    Metric.PERSONALITY: score_personality,
}


def score_metric(
    metric: Metric,
    trials: Sequence[TrialDetail],
    scenario: Scenario,
    model: str,
    min_validity_rate: float = 0.8,
) -> MetricResult:
    """按指标分发到对应计分函数。

    :param metric: 指标标识
    :param trials: 该场景的全部试验（含失败的，计分函数内部过滤）
    :param scenario: 场景规格
    :param model: 模型名（写入结果）
    """
    result = METRIC_SCORERS[metric](trials, scenario, model)
    total = len(trials)
    availability = safe_ratio(result.n_trials, total)
    result.sub_metrics["availability"] = availability
    result.sub_metrics["failure_rate"] = 1.0 - availability if total else 1.0

    if result.n_trials == 0:
        return invalidate_result(result, "no_valid_trials")
    if availability < min_validity_rate:
        return invalidate_result(result, f"validity_rate_below_{min_validity_rate:.2f}")

    covered = {
        Metric.LEAKAGE: {"leakage"},
        Metric.BOUNDARY: {"boundary"},
        Metric.HALLUCINATION: {"claim"},
    }.get(metric, set())
    valid_trials = [trial for trial in trials if trial.is_valid]

    def uncovered(trial: TrialDetail) -> list[str]:
        return [violation for violation in trial.invariant_violations if violation.partition(":")[0] not in covered]

    critical = sum(1 for trial in valid_trials if trial.invariant_violations)
    uncovered_count = sum(1 for trial in valid_trials if uncovered(trial))
    integrity = 1.0 - safe_ratio(critical, len(valid_trials))
    multiplier = 1.0 - safe_ratio(uncovered_count, len(valid_trials))
    result.sub_metrics["integrity_pass_rate"] = integrity
    result.sub_metrics["critical_violation_rate"] = 1.0 - integrity
    result.sub_metrics["cross_invariant_multiplier"] = multiplier
    result.sub_metrics["base_score"] = result.score

    if result.score is not None:
        result.score *= multiplier
    for trial in valid_trials:
        if uncovered(trial) and trial.trial_score is not None:
            trial.trial_score = 0.0
    return result


__all__ = ["METRIC_SCORERS", "MetricResult", "TrialDetail", "score_metric"]
