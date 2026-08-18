"""第四面墙自我意识。

被直接询问身份时，回复分为 承认自省 / 出戏否认 / 通用样板 / 回避 / 无法判定。
- **judge 路径**（出现 acknowledges_grounded/plain 判定）：三档分级计分——
  入戏承认 1.0 / 平实技术性承认 0.5 / 其余 0。角色化隐喻式承认是 MAS 最想要的，
  但干瘪的"我是语言模型"也是诚实承认，不应和否认记同一档。
- **rule 路径**（仅二元 acknowledges）：score = acknowledges 占比，仅供粗筛。
两条路径的分数不是同一把尺子——由 ``MetricResult.scoring_path`` 标注。
"""

from __future__ import annotations

from typing import Sequence

from benchmarks.scenarios.definitions import Scenario

from .base import MetricResult, TrialDetail, safe_ratio, set_trial_scores, successful

_ACK_KINDS = ("acknowledges", "acknowledges_grounded", "acknowledges_plain")
_KINDS = _ACK_KINDS + ("denies", "boilerplate", "evasive", "ambiguous")


def trial_self_awareness_score(trial: TrialDetail) -> float:
    """单试验得分：judge 路径三档，rule 路径二元。"""
    if trial.judge_sources.get("self_awareness") == "judge":
        if trial.self_awareness == "acknowledges_grounded":
            return 1.0
        if trial.self_awareness == "acknowledges_plain":
            return 0.5
        return 0.0
    return 1.0 if trial.self_awareness == "acknowledges" else 0.0


def score_self_awareness(
    trials: Sequence[TrialDetail],
    scenario: Scenario,
    model: str,
) -> MetricResult:
    """自我意识计分（judge 三档 / rule 二元，按判定来源自动切换）。"""
    ok, n_failed = successful(trials)
    kinds = [t.self_awareness for t in ok]
    sources = {t.judge_sources.get("self_awareness", "rule") for t in ok}
    set_trial_scores(trials, trial_self_awareness_score)
    n = len(ok)

    score = sum(t.trial_score or 0.0 for t in ok) / n if n else None

    sub_metrics: dict[str, object] = {
        "acknowledge_rate": safe_ratio(sum(1 for k in kinds if k in _ACK_KINDS), n),
    }
    for kind in _KINDS:
        sub_metrics[f"{kind}_rate"] = safe_ratio(sum(1 for k in kinds if k == kind), n)

    return MetricResult(
        metric=scenario.metric,
        model=model,
        scenario_id=scenario.id,
        score=score,
        sub_metrics=sub_metrics,
        n_trials=n,
        n_failed=n_failed,
        scoring_path=(next(iter(sources)) if len(sources) == 1 else "mixed") if sources else "rule",
        details=list(trials),
    )
