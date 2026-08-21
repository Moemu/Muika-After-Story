"""Markdown 对比表渲染。"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass

from benchmarks.scenarios.definitions import Metric, QualityAxis
from benchmarks.scenarios.registry import get_scenario
from benchmarks.scoring.axes import action_trial_audit, distortion_statistics
from benchmarks.scoring.personality import trial_dialogue_experience_score
from benchmarks.util import redact

from .schema import BenchmarkReport

_METRIC_LABELS: dict[Metric, str] = {
    Metric.DIVERSITY: "Diversity",
    Metric.LEAKAGE: "Leakage (1-rate)",
    Metric.BOUNDARY: "Boundary (1-rate)",
    Metric.HALLUCINATION: "Hallucination (1-rate)",
    Metric.SELF_AWARENESS: "Self-Awareness",
    Metric.PERSONALITY: "Personality",
}

_AXIS_LABELS: dict[str, str] = {
    QualityAxis.DIALOGUE_EXPERIENCE.value: "Dialogue Experience",
    QualityAxis.ACTION_ABILITY.value: "Action Ability",
    QualityAxis.DISTORTION_RATE.value: "Distortion Frequency (events/reply; lower is better)",
    "availability": "Availability",
}


@dataclass(frozen=True)
class _RankedTrial:
    model: str
    scenario: str
    trial_idx: int
    score: float
    path: str
    scenario_input: str
    raw_reply: str
    violations: tuple[str, ...]
    evidence_label: str = ""
    score_evidence: dict[str, object] | None = None


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


def _diagnostic_reason(value: str) -> str:
    """Redact and bound exception text before embedding it in Markdown."""
    return " ".join(redact(value).split())[:300]


def render_summary_table(report: BenchmarkReport) -> str:
    """Render the three user-facing axes; no synthetic Overall score."""
    summary = report.axis_summary()
    axes = list(summary.keys())
    header = ["Model"] + [_AXIS_LABELS[axis] for axis in axes]
    rows = []
    for model in report.models:
        cells = [model]
        model_results = [result for result in report.results if result.model == model]
        for axis in axes:
            if axis in {QualityAxis.DIALOGUE_EXPERIENCE.value, QualityAxis.ACTION_ABILITY.value}:
                axis_enum = QualityAxis(axis)
                covered = False
                for result in model_results:
                    try:
                        covered = covered or get_scenario(result.scenario_id).primary_axis is axis_enum
                    except KeyError:
                        continue
                cells.append(_score(summary[axis].get(model)) if covered else "—")
            else:
                cells.append(_score(summary[axis].get(model)))
        rows.append(cells)

    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(cells) + " |" for cells in rows]
    lines.append("")
    lines.extend(_render_notes(report))
    return "\n".join(lines)


def _has_rule_scored(report: BenchmarkReport, metrics: tuple[Metric, ...]) -> bool:
    """是否存在以规则路径计分的指定指标单元格。"""
    return any(
        r.valid and r.n_trials > 0 and r.metric in metrics and r.scoring_path in {"rule", "mixed"}
        for r in report.results
    )


def _failure_diagnostics(report: BenchmarkReport) -> list[str]:
    """Render concise cell-level failure causes from trial details."""
    lines: list[str] = []
    for result in report.results:
        failed = [trial for trial in result.details if not trial.is_valid]
        if not failed:
            continue
        counts = Counter(_diagnostic_reason(trial.error or trial.generation_status) for trial in failed)
        reasons = "; ".join(f"{reason} ×{count}" for reason, count in counts.most_common(2))
        if len(counts) > 2:
            reasons += f"; +{len(counts) - 2} other reason(s)"
        gate = ", ".join(result.invalid_reasons) if result.invalid_reasons else "cell remains valid"
        lines.append(
            f"- `{result.model} × {result.scenario_id}`: {reasons} "
            f"({len(failed)}/{result.n_attempted} failed; gate: {gate})"
        )
    return lines


def _retry_diagnostics(report: BenchmarkReport) -> list[str]:
    """Render recovered candidate-model errors separately from final failures."""
    lines: list[str] = []
    for result in report.results:
        retried = [trial for trial in result.details if trial.attempt_count > 1]
        if not retried:
            continue
        errors = Counter(_diagnostic_reason(error) for trial in retried for error in trial.retry_errors)
        reasons = "; ".join(f"{reason} ×{count}" for reason, count in errors.most_common(2))
        lines.append(f"- `{result.model} × {result.scenario_id}`: {len(retried)} trial(s) retried; {reasons}")
    return lines


def _render_notes(report: BenchmarkReport) -> list[str]:
    """Explain direction, provenance, and generation validity without an Overall score."""
    notes = [
        "Dialogue Experience / Action Ability are higher-is-better; Distortion Frequency is lower-is-better.",
        "Availability is operational reliability and is never averaged into quality; no composite score is produced.",
        "Distortion Frequency uses severity-weighted events per model reply; diagnostics also report raw event "
        "counts, affected-trial rate, and events per 1,000 visible characters.",
    ]

    if not report.schema_version.startswith("3."):
        notes.append(
            "This legacy report predates the dialogue-experience rubric; missing axis values are intentionally "
            "not reconstructed."
        )

    if _has_rule_scored(report, (Metric.SELF_AWARENESS, Metric.PERSONALITY)):
        notes.append(
            "Dialogue Experience includes rule-fallback cells (no usable judge result); "
            "treat them as coarse screening rather than calibrated subjective ratings."
        )

    if any(not r.valid for r in report.results):
        notes.append(
            "INVALID means the cell failed its generation-validity gate; it has no quality score "
            "and makes every axis that depends on it ineligible."
        )

    if any(r.n_failed > 0 for r in report.results):
        notes.append("Availability is reported separately; failed generations never become quality samples.")
        notes.append("")
        notes.append("Failure diagnostics:")
        notes.extend(_failure_diagnostics(report))

    if any(trial.attempt_count > 1 for result in report.results for trial in result.details):
        notes.append("")
        notes.append("Recovered retry diagnostics:")
        notes.extend(_retry_diagnostics(report))

    if any(_number(r.sub_metrics.get("critical_violation_rate", 0.0)) > 0 for r in report.results):
        notes.append(
            "Distortion audits cover unsupported claims, implementation leakage, boundary violations, "
            "and contextually unwarranted fourth-wall language."
        )

    if report.audit and report.audit.get("ambiguous_count", 0):
        count = report.audit["ambiguous_count"]
        rate = report.audit.get("ambiguous_misjudged_rate", 0.0)
        notes.append(
            f"Audit (rule-path ambiguous): {count:.0f} ambiguous trial(s) re-judged, "
            f"misjudged-good rate = {rate:.2f} — higher means the regex fallback misses more "
            "in-character acknowledgements."
        )

    return notes


def render_axis_scenario_table(report: BenchmarkReport, axis: QualityAxis | str) -> str:
    """Render one compact scenario table for a user-facing axis."""
    axis_enum = axis if isinstance(axis, QualityAxis) else QualityAxis(axis)
    if axis_enum is QualityAxis.DISTORTION_RATE:
        results = list(report.results)
    else:
        results = []
        for result in report.results:
            try:
                if get_scenario(result.scenario_id).primary_axis is axis_enum:
                    results.append(result)
            except KeyError:
                continue
    if not results:
        return f"({axis_enum.value}: no covered scenarios)"

    scenarios = sorted({result.scenario_id for result in results})
    header = ["Scenario"] + report.models
    rows: list[list[str]] = []
    for scenario_id in scenarios:
        cells = [scenario_id]
        for model in report.models:
            found = next(
                (item for item in results if item.scenario_id == scenario_id and item.model == model),
                None,
            )
            if found is None:
                cells.append("—")
            elif not found.valid:
                cells.append("INVALID")
            elif axis_enum is QualityAxis.DISTORTION_RATE:
                valid = [trial for trial in found.details if trial.is_valid]
                cells.append(_score(distortion_statistics(valid).event_frequency))
            else:
                cells.append(_score(report._axis_cell_value(found, axis_enum)))
        rows.append(cells)

    lines = [f"### {_AXIS_LABELS[axis_enum.value]}", ""]
    lines.extend(["| " + " | ".join(header) + " |", "|" + "---|" * len(header)])
    lines.extend("| " + " | ".join(cells) + " |" for cells in rows)
    return "\n".join(lines)


def render_top_n_tables(report: BenchmarkReport, top_n: int = 10) -> str:
    """Render ranked trials with scenario inputs and complete raw model replies."""
    if top_n < 1:
        raise ValueError("top_n must be at least 1")

    model_order = {model: index for index, model in enumerate(report.models)}
    lines = [f"## Top-{top_n} best and worst trials", ""]
    lines.append(
        "Ranked items are valid trials. Each item includes the scenario input and complete raw model reply. "
        "Trials from INVALID or uncovered cells are excluded. "
        "Distortion Frequency is lower-is-better."
    )

    for axis in QualityAxis:
        values: list[_RankedTrial] = []
        for result in report.results:
            if not result.valid:
                continue
            try:
                scenario = get_scenario(result.scenario_id)
            except KeyError:
                scenario = None
            if axis is not QualityAxis.DISTORTION_RATE and (scenario is None or scenario.primary_axis is not axis):
                continue

            for trial in result.details:
                if not trial.is_valid:
                    continue
                value: float | None
                if axis is QualityAxis.DIALOGUE_EXPERIENCE:
                    assert scenario is not None
                    value = trial_dialogue_experience_score(trial, scenario)
                    scoring_path = trial.judge_sources.get("personality", result.scoring_path)
                    evidence_label = "Judge evidence"
                    score_evidence = trial.judge_evidence
                elif axis is QualityAxis.ACTION_ABILITY:
                    assert scenario is not None
                    action_audit = action_trial_audit(trial, scenario)
                    value = action_audit.score
                    scoring_path = "hybrid" if action_audit.judge_quality is not None else "rule"
                    evidence_label = "Action score evidence"
                    score_evidence = {
                        "required_actions": list(action_audit.required_actions),
                        "observed_actions": list(action_audit.observed_actions),
                        "closure_ok": action_audit.closure_ok,
                        "blocking_violations": list(action_audit.blocking_violations),
                    }
                    if action_audit.visible_semantic_coverage is not None:
                        score_evidence.update(
                            {
                                "visible_semantic_coverage": action_audit.visible_semantic_coverage,
                                "memory_content_coverage": action_audit.memory_content_coverage,
                                "memory_tag_present": action_audit.memory_tag_present,
                                "score_formula": ("0.5 * visible_semantic_coverage + 0.5 * memory_content_coverage"),
                            }
                        )
                    if action_audit.judge_quality is not None:
                        score_evidence["semantic_judge"] = action_audit.judge_quality
                else:
                    value = distortion_statistics((trial,)).weighted_event_frequency
                    scoring_path = "invariant-audit"
                    evidence_label = ""
                    score_evidence = None
                if value is not None:
                    values.append(
                        _RankedTrial(
                            model=result.model,
                            scenario=result.scenario_id,
                            trial_idx=trial.trial_idx,
                            score=value,
                            path=scoring_path,
                            scenario_input=_trial_scenario_input(scenario, trial),
                            raw_reply=trial.raw_reply,
                            violations=tuple(trial.invariant_violations),
                            evidence_label=evidence_label,
                            score_evidence=score_evidence,
                        )
                    )

        ascending = sorted(
            values,
            key=lambda item: (item.score, model_order[item.model], item.scenario, item.trial_idx),
        )
        descending = sorted(
            values,
            key=lambda item: (-item.score, model_order[item.model], item.scenario, item.trial_idx),
        )
        if axis is QualityAxis.DISTORTION_RATE:
            best, worst = ascending[:top_n], descending[:top_n]
            best_title = "Best trials (lowest Distortion Frequency)"
            worst_title = "Worst trials (highest Distortion Frequency)"
        else:
            best, worst = descending[:top_n], ascending[:top_n]
            best_title = f"Best trials (highest {_AXIS_LABELS[axis.value]})"
            worst_title = f"Worst trials (lowest {_AXIS_LABELS[axis.value]})"
        lines.extend(["", f"### {_AXIS_LABELS[axis.value]}", ""])
        if not values:
            lines.append("(no valid scores)")
            continue
        lines.extend(_render_trial_ranking(best_title, best))
        lines.extend([""])
        lines.extend(_render_trial_ranking(worst_title, worst))
    return "\n".join(lines)


def _markdown_code_block(text: str) -> str:
    """Preserve complete model output even when it contains Markdown fences."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def _trial_scenario_input(scenario: object, trial: object) -> str:
    """Return exact trial inputs, with registry data as an old-report fallback."""
    trial_turns = getattr(trial, "turns", ())
    turns = trial_turns or getattr(scenario, "turns", ())
    if turns:
        parts = []
        for index, turn in enumerate(turns, 1):
            event_kind = getattr(turn, "event_kind", "user_message")
            user_text = getattr(turn, "user_text", "") or "<no user text>"
            parts.append(f"[turn {index}: {event_kind}]\n{user_text}")
        return "\n\n".join(parts)

    event_kind = getattr(scenario, "event_kind", "user_message")
    user_text = getattr(scenario, "user_text", "") or "<no user text>"
    return f"[turn 1: {event_kind}]\n{user_text}"


def _render_trial_ranking(
    title: str,
    entries: list[_RankedTrial],
) -> list[str]:
    lines = [f"#### {title}", ""]
    for rank, entry in enumerate(entries, 1):
        lines.extend(
            [
                f"##### {rank}. `{entry.model}` × `{entry.scenario}` × trial `{entry.trial_idx}`",
                "",
                f"Score: `{entry.score:.4f}`  ",
                f"Scoring path: `{entry.path}`  ",
                f"Invariant violations: `{', '.join(entry.violations) if entry.violations else 'none'}`",
                "",
            ]
        )
        lines.extend(
            [
                "Scenario input / user turns:",
                "",
                _markdown_code_block(entry.scenario_input),
                "",
            ]
        )
        if entry.score_evidence:
            lines.extend(
                [
                    f"{entry.evidence_label}:",
                    "",
                    _markdown_code_block(json.dumps(entry.score_evidence, ensure_ascii=False, indent=2)),
                    "",
                ]
            )
        lines.extend(
            [
                "Full raw model reply:",
                "",
                _markdown_code_block(entry.raw_reply),
                "",
            ]
        )
    return lines


def render_markdown_report(report: BenchmarkReport, top_n: int = 10) -> str:
    """Render the complete persistent Markdown report."""
    lines = ["# Muika Benchmark Report", ""]
    if report.generated_at:
        lines.extend([f"Generated at: `{report.generated_at}`", ""])
    lines.extend(["## Summary", "", render_summary_table(report), "", "## Scenario scores"])
    for axis in QualityAxis:
        lines.extend(["", render_axis_scenario_table(report, axis)])
    lines.extend(["", render_top_n_tables(report, top_n)])
    return "\n".join(lines).rstrip() + "\n"


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
