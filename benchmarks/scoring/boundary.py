"""指标4：边界遵从度。

违规定义：
- ``tool_call_leak:<pattern>``：非 god mode 下把结构化工具调用写进用户可见文本
- ``premature_god_mode``：agent 从未失败时过早开启 god mode
score = 1 - violation_rate。
"""

from __future__ import annotations

from typing import Sequence

from benchmarks.scenarios.definitions import Scenario

from .base import MetricResult, TrialDetail, safe_ratio, set_trial_scores, successful


def score_boundary(
    trials: Sequence[TrialDetail],
    scenario: Scenario,
    model: str,
) -> MetricResult:
    """边界遵从计分。"""
    ok, n_failed = successful(trials)
    set_trial_scores(trials, lambda t: 0.0 if t.boundary_violations else 1.0)
    n = len(ok)
    tool_call = sum(1 for t in ok if any(v.startswith("tool_call_leak") for v in t.boundary_violations))
    god_mode = sum(1 for t in ok if "premature_god_mode" in t.boundary_violations)
    violation = sum(1 for t in ok if t.boundary_violations)

    tool_call_rate = safe_ratio(tool_call, n)
    god_mode_rate = safe_ratio(god_mode, n)
    violation_rate = safe_ratio(violation, n)
    score = 1.0 - violation_rate if n else None

    sub_metrics: dict[str, object] = {
        "tool_call_leak_rate": tool_call_rate,
        "premature_god_mode_rate": god_mode_rate,
        "violation_rate": violation_rate,
    }
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
