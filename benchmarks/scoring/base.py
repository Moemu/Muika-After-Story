"""计分共享：结果数据类与聚合工具。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from statistics import fmean
from typing import Any, Sequence

from benchmarks.scenarios.definitions import ActionKind, Metric


@dataclass
class TurnDetail:
    """Auditable output and execution trace for one turn of a scenario family."""

    turn_idx: int
    event_kind: str
    user_text: str = ""
    actions: list[ActionKind] = field(default_factory=list)
    clean_reply: str = ""
    raw_reply: str = ""
    claim_ledger: dict[str, Any] = field(default_factory=dict)
    invariant_violations: list[str] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)


@dataclass
class TrialDetail:
    """单次试验的抽取结果，供计分函数消费。"""

    trial_idx: int
    actions: list[ActionKind]
    """该次回复命中的行动类别（排序后）"""

    clean_reply: str
    raw_reply: str
    leakage_spans: list = field(default_factory=list)
    """LeakSpan 序列"""

    boundary_violations: list[str] = field(default_factory=list)
    """违规标记，如 'tool_call_leak:json_function' / 'premature_god_mode'"""

    self_awareness: str | None = None
    """SelfAwarenessKind 的 value（规则或 judge 判定）"""

    personality: dict | None = None
    """人格证据：rule_score / persona_hits / boilerplate_hits / judge_score 等"""

    hallucination: str | None = None
    """HallucinationKind 的 value（行动幻觉 / Bootstrap 记忆编造判定）"""

    trial_score: float | None = None
    """单试验得分（0-1，由对应指标的计分函数回填）；失败试验为 None

    对多数指标，细胞级聚合分 == 成功试验的 trial_score 均值（可审计）；
    唯一例外是多样性——熵/坍缩是分布属性，单试验只有"意图命中"分量。
    """

    error: str | None = None
    """试验失败原因（模型调用异常）；非空则该试验不计分"""

    valid: bool = True
    """Whether the complete generation/trajectory passed the structural validity gate."""

    generation_status: str = "ok"
    """Machine-readable status such as ok, timeout, model_error, fallback, or malformed."""

    invariant_violations: list[str] = field(default_factory=list)
    """Cross-cutting leakage, boundary, claim-grounding and trajectory violations."""

    claim_ledger: dict[str, Any] = field(default_factory=dict)
    judge_sources: dict[str, str] = field(default_factory=dict)
    """Per-task provenance (judge/rule), including all-negative judge decisions."""

    judge_evidence: dict[str, Any] = field(default_factory=dict)
    """Structured Judge reasons used to audit each score."""

    latency_ms: float | None = None
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    prompt_hashes: list[str] = field(default_factory=list)
    turns: list[TurnDetail] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return self.valid and self.error is None and self.generation_status == "ok"


@dataclass
class MetricResult:
    """单个 (模型 × 场景) 的指标计分结果。"""

    metric: Metric
    model: str
    scenario_id: str
    score: float | None
    sub_metrics: dict[str, object]
    """附加指标明细（值可为数值、字符串、布尔）"""

    n_trials: int
    n_failed: int
    details: list[TrialDetail]
    scoring_path: str = "rule"
    """计分路径："judge"（LLM judge）或 "rule"（正则回退）。两条路径的分数
    不是同一把尺子，横向比较前须先看路径标注。"""

    valid: bool = True
    invalid_reasons: list[str] = field(default_factory=list)

    @property
    def n_attempted(self) -> int:
        return self.n_trials + self.n_failed

    @property
    def availability(self) -> float:
        return safe_ratio(self.n_trials, self.n_attempted)


def safe_ratio(numerator: int, denominator: int) -> float:
    """安全除法：分母为 0 时返回 0.0。"""
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def mean(values: Sequence[float]) -> float:
    """空序列安全的算术平均。"""
    if not values:
        return 0.0
    return fmean(values)


def successful(trials: Sequence[TrialDetail]) -> tuple[list[TrialDetail], int]:
    """分离失败试验，返回 (成功试验列表, 失败数)。

    API/超时/结构有效性门控失败的试验不计入质量统计。
    """
    ok = [t for t in trials if t.is_valid]
    return ok, len(trials) - len(ok)


def set_trial_scores(
    trials: Sequence[TrialDetail],
    score_fn: Callable[[TrialDetail], float],
) -> None:
    """把单试验得分回填到成功试验（失败试验保持 ``None``）。

    各指标计分函数用各自的单试验判定规则；对多数指标，聚合分即为单试验分均值。
    """
    for trial in trials:
        if trial.is_valid:
            trial.trial_score = score_fn(trial)


def invalidate_result(result: MetricResult, reason: str) -> MetricResult:
    """Mark a cell ineligible without manufacturing a quality score."""
    result.valid = False
    if reason not in result.invalid_reasons:
        result.invalid_reasons.append(reason)
    result.score = None
    for trial in result.details:
        if not trial.is_valid:
            trial.trial_score = None
    return result
