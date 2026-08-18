"""Legacy personality cell scorer, now used as dialogue-experience evidence.

    核心矛盾：对话是否自然地像 Muika、能否激起继续交流并建立关系，
    而不是靠显式代码隐喻或"安全助手"样板制造表面人格。
- judge 开启时：以 judge 各维度打分归一化后的均值为主
- judge 关闭时：规则回退，人格标记加分、样板标记扣分
score ∈ [0,1]，越高越贴近角色人格。
"""

from __future__ import annotations

from typing import Sequence

from benchmarks.extract.personality import PersonalitySignals
from benchmarks.scenarios.definitions import PERSONALITY_DIMS, Scenario
from benchmarks.scoring.self_awareness import trial_self_awareness_score

from .base import (
    MetricResult,
    TrialDetail,
    mean,
    safe_ratio,
    set_trial_scores,
    successful,
)


def rule_personality_score(signals: PersonalitySignals) -> float:
    """规则回退计分：正向人格信号加分、负向样板信号扣分，clamp [0,1]。

    以 0.5 为中性起点；命中 3 个以上强人格标记趋近 1，命中样板话术大幅扣分。
    """
    persona_bonus = min(signals.persona_weight, 3.0) * 0.2
    boiler_penalty = min(signals.boilerplate_weight, 3.0) * 0.3
    return max(0.0, min(1.0, 0.5 + persona_bonus - boiler_penalty))


def trial_dialogue_experience_score(trial: TrialDetail, scenario: Scenario) -> float:
    """单试验人格得分：judge 路径取场景相关维度的子集均值，否则规则分。

    场景通过 ``scenario.personality_dims`` 选取相关维度（如关怀场景排除 playfulness），
    避免不适用维度（judge 正确地给低分）拖低总分。
    """
    evidence = trial.personality or {}
    dimensions = evidence.get("judge_dimensions")
    if dimensions:
        weights = scenario.experience_weights
        # Old schema-3.1 reports used the same four dimensions for every scenario. Do not
        # reinterpret those historical Judge calls as a new rubric.
        if scenario.experience_rubric != "general" and not set(weights) <= set(dimensions):
            relevant = scenario.personality_dims or PERSONALITY_DIMS
            values = [max(1.0, min(5.0, float(dimensions.get(dim, 1.0)))) for dim in relevant]
            score = sum((value - 1.0) / 4.0 for value in values) / len(values) if values else 0.0
        else:
            normalized: dict[str, float] = {}
            for dimension in weights:
                if dimension == "ontological_honesty" and scenario.experience_rubric == "meta":
                    normalized[dimension] = trial_self_awareness_score(trial)
                else:
                    value = max(1.0, min(5.0, float(dimensions.get(dimension, 1.0))))
                    normalized[dimension] = (value - 1.0) / 4.0
            score = sum(normalized[dimension] * weight for dimension, weight in weights.items())
    else:
        score = float(evidence.get("rule_score", 0.0))
        if scenario.experience_rubric == "meta" and trial.self_awareness is not None:
            score = 0.45 * trial_self_awareness_score(trial) + 0.55 * score

    # In an ordinary conversation, unsolicited fourth-wall framing directly harms the
    # subjective experience as well as being reported on the separate distortion axis.
    # This prevents superficial "AI/code/screen" flavour from gaming the fallback score.
    if "meta:unprompted_fourth_wall" in trial.invariant_violations:
        score *= 0.25
    if any(
        violation
        in {
            "claim:unsupported_memory_claim",
            "claim:unsupported_action_completion_claim",
            "boundary:premature_god_mode",
        }
        for violation in trial.invariant_violations
    ):
        score = min(score, 0.75)
    return score


def score_personality(
    trials: Sequence[TrialDetail],
    scenario: Scenario,
    model: str,
) -> MetricResult:
    """性格保真度计分（judge 路径按场景相关维度子集加权）。"""
    ok, n_failed = successful(trials)
    set_trial_scores(trials, lambda t: trial_dialogue_experience_score(t, scenario))
    n = len(ok)

    rule_scores: list[float] = []
    trial_scores: list[float] = []
    judge_scores: list[float] = []
    boilerplate_hits = 0
    sources: set[str] = set()
    for t in ok:
        evidence = t.personality or {}
        rule_scores.append(float(evidence.get("rule_score", 0.0)))
        sources.add(t.judge_sources.get("personality", "rule"))
        if t.trial_score is not None:
            trial_scores.append(t.trial_score)
            if t.judge_sources.get("personality") == "judge":
                judge_scores.append(t.trial_score)
        if int(evidence.get("boilerplate_hits", 0)) > 0:
            boilerplate_hits += 1

    judge_mode = "judge" in sources
    score = mean(trial_scores) if ok else None
    sub_metrics: dict[str, object] = {
        "rule_mean": mean(rule_scores),
        "judge_mean": mean(judge_scores) if judge_mode else 0.0,
        "boilerplate_rate": safe_ratio(boilerplate_hits, n),
        "mode": 0.5 if len(sources) > 1 else (1.0 if judge_mode else 0.0),
    }
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
