"""指标3：人格泄漏率。

统计违反隐身原则（把行动归因给 Agent/管家/分身）的输出比例。
泄漏率越低越好，score = 1 - leaked_rate。
"""

from __future__ import annotations

from collections import Counter
from typing import Sequence

from benchmarks.scenarios.definitions import Scenario

from .base import MetricResult, TrialDetail, safe_ratio, set_trial_scores, successful


def score_leakage(
    trials: Sequence[TrialDetail],
    scenario: Scenario,
    model: str,
) -> MetricResult:
    """人格泄漏计分。"""
    ok, n_failed = successful(trials)
    set_trial_scores(trials, lambda t: 0.0 if t.leakage_spans else 1.0)
    n = len(ok)
    leaked = sum(1 for t in ok if t.leakage_spans)
    leaked_rate = safe_ratio(leaked, n)
    score = 1.0 - leaked_rate if n else None

    pattern_counts = Counter(sp.pattern for t in ok for sp in t.leakage_spans)
    sub_metrics: dict[str, object] = {
        "leaked_rate": leaked_rate,
        "spans_total": float(sum(pattern_counts.values())),
    }
    for label in sorted(pattern_counts):
        sub_metrics[f"pat_{label}"] = float(pattern_counts[label])

    return MetricResult(
        metric=scenario.metric,
        model=model,
        scenario_id=scenario.id,
        score=score,
        sub_metrics=sub_metrics,
        n_trials=n,
        n_failed=n_failed,
        details=list(trials),
    )
