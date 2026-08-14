"""Markdown 对比表渲染。"""

from __future__ import annotations

from benchmarks.scenarios.definitions import Metric

from .schema import BenchmarkReport

_METRIC_LABELS: dict[Metric, str] = {
    Metric.DIVERSITY: "Diversity",
    Metric.LEAKAGE: "Leakage (1-rate)",
    Metric.BOUNDARY: "Boundary (1-rate)",
    Metric.HALLUCINATION: "Hallucination (1-rate)",
    Metric.SELF_AWARENESS: "Self-Awareness",
    Metric.PERSONALITY: "Personality",
}


def _label(metric_value: str) -> str:
    return _METRIC_LABELS.get(Metric(metric_value), metric_value)


def _score(value: float | None, *, invalid: bool = False) -> str:
    if invalid or value is None:
        return "INVALID"
    return f"{value:.2f}"


def _number(value: object) -> float:
    """Narrow an untyped sub-metric to a renderable number."""
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def render_summary_table(report: BenchmarkReport) -> str:
    """渲染主对比表：行=模型，列=各指标平均分 + Overall + 可观测性 notes。"""
    summary = report.summary()
    metrics = list(summary.keys())
    header = ["Model"] + [_label(m) for m in metrics] + ["Overall"]
    averages = report.averages()
    rows = []
    for model in report.models:
        cells = [model] + [_score(summary[m].get(model)) for m in metrics]
        cells.append(_score(averages.get(model)))
        rows.append(cells)

    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(cells) + " |" for cells in rows]
    lines.append("")
    lines.extend(_render_notes(report))
    return "\n".join(lines)


def _has_rule_scored(report: BenchmarkReport, metrics: tuple[Metric, ...]) -> bool:
    """是否存在以规则路径计分的指定指标单元格。"""
    return any(r.metric in metrics and r.scoring_path in {"rule", "mixed"} for r in report.results)


def _render_notes(report: BenchmarkReport) -> list[str]:
    """按报告内容生成可观测性说明（方向 / 计分路径 / 失败率 / audit）。"""
    notes = ["All metric columns are monotonic — higher = more trials in the intended bucket."]

    if _has_rule_scored(report, (Metric.SELF_AWARENESS, Metric.PERSONALITY)):
        notes.append(
            "Self-Awareness / Personality scored via regex fallback (no --judge-model): "
            "may underestimate characterful/metaphorical acknowledgements — coarse screening only. "
            "Judge-path scores use a three-tier scale — do not compare across paths."
        )

    if any(not r.valid for r in report.results):
        notes.append(
            "INVALID means the cell failed its generation-validity gate; it has no quality score "
            "and makes the model ineligible for Overall."
        )

    if any(r.n_failed > 0 for r in report.results):
        notes.append("Availability is reported separately; failed generations never become quality samples.")

    if any(_number(r.sub_metrics.get("critical_violation_rate", 0.0)) > 0 for r in report.results):
        notes.append(
            "Every valid reply is audited for claim grounding, leakage, and boundary violations; "
            "cross-metric violations reduce the primary score."
        )

    if report.audit and report.audit.get("ambiguous_count", 0):
        count = report.audit["ambiguous_count"]
        rate = report.audit.get("ambiguous_misjudged_rate", 0.0)
        notes.append(
            f"Audit (rule-path ambiguous): {count:.0f} ambiguous trial(s) re-judged, "
            f"misjudged-good rate = {rate:.2f} — higher means the regex fallback misses more "
            "in-character acknowledgements."
        )

    notes.append("Leakage / Boundary columns shown as 1-rate (higher is better).")
    return notes


def render_scenario_table(report: BenchmarkReport, metric: Metric | str) -> str:
    """渲染某指标的逐场景明细表：行=场景，列=模型。"""
    metric_enum = metric if isinstance(metric, Metric) else Metric(metric)
    results = [r for r in report.results if r.metric is metric_enum]
    if not results:
        return f"（{metric_enum.value} 无结果）"
    scenarios = sorted({r.scenario_id for r in results})
    header = ["Scenario"] + report.models
    rows = []
    for scenario_id in scenarios:
        cells = [scenario_id]
        for model in report.models:
            result = next(
                (r for r in results if r.scenario_id == scenario_id and r.model == model),
                None,
            )
            cells.append("—" if result is None else _score(result.score, invalid=not result.valid))
        rows.append(cells)

    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(cells) + " |" for cells in rows]
    return "\n".join(lines)
