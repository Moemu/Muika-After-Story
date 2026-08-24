"""Versioned, loss-aware benchmark report schema."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from benchmarks.extract.leakage import LeakSpan
from benchmarks.extract.meta import meta_violations
from benchmarks.scenarios.definitions import ActionKind, Metric, QualityAxis
from benchmarks.scenarios.registry import get_scenario
from benchmarks.scoring.axes import action_cell_score, distortion_statistics
from benchmarks.scoring.base import MetricResult, TrialDetail, TurnDetail

GENERATION_FALLBACK = "My mind feels foggy... I encountered an error."


@dataclass
class BenchmarkReport:
    schema_version: str = "3.5"
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    config: dict[str, Any] = field(default_factory=dict)
    models: list[str] = field(default_factory=list)
    scenarios: list[str] = field(default_factory=list)
    results: list[MetricResult] = field(default_factory=list)
    audit: dict[str, Any] | None = None

    def scenario_summary(self) -> dict[str, dict[str, float | None]]:
        """Legacy diagnostic table: scenario means grouped by extractor metric."""
        grouped: dict[tuple[str, str], list[float]] = {}
        keys: set[tuple[str, str]] = set()
        for result in self.results:
            key = (result.metric.value, result.model)
            keys.add(key)
            if result.valid and result.score is not None:
                grouped.setdefault(key, []).append(result.score)
        summary: dict[str, dict[str, float | None]] = {}
        for metric, model in sorted(keys):
            values = grouped.get((metric, model), [])
            summary.setdefault(metric, {})[model] = sum(values) / len(values) if values else None
        return summary

    def _axis_cell_value(self, result: MetricResult, axis: QualityAxis) -> float | None:
        try:
            scenario = get_scenario(result.scenario_id)
        except KeyError:
            return None
        if scenario.primary_axis is not axis:
            return None
        if axis is QualityAxis.ACTION_ABILITY:
            recomputed = action_cell_score(result.details, scenario)
            if recomputed is not None:
                return recomputed
        key = f"axis_{axis.value}"
        stored = result.sub_metrics.get(key)
        if isinstance(stored, (int, float)):
            return float(stored)
        if axis is QualityAxis.DIALOGUE_EXPERIENCE:
            return result.score if self.schema_version.startswith("3.") else None
        return None

    def axis_summary(self) -> dict[str, dict[str, float | None]]:
        """The compact three-axis contract plus separately reported availability."""
        summary: dict[str, dict[str, float | None]] = {
            QualityAxis.DIALOGUE_EXPERIENCE.value: {},
            QualityAxis.ACTION_ABILITY.value: {},
            QualityAxis.DISTORTION_RATE.value: {},
            "availability": {},
        }
        for model in self.models:
            cells = [result for result in self.results if result.model == model]
            for axis in (QualityAxis.DIALOGUE_EXPERIENCE, QualityAxis.ACTION_ABILITY):
                relevant = []
                for result in cells:
                    try:
                        is_relevant = get_scenario(result.scenario_id).primary_axis is axis
                    except KeyError:
                        is_relevant = False
                    if is_relevant:
                        relevant.append(result)
                values = [self._axis_cell_value(result, axis) for result in relevant if result.valid]
                usable = [value for value in values if value is not None]
                summary[axis.value][model] = (
                    sum(usable) / len(usable) if relevant and len(usable) == len(relevant) else None
                )

            attempted = sum(result.n_attempted for result in cells)
            valid_trials = [trial for result in cells for trial in result.details if trial.is_valid]
            invalid_cells = any(not result.valid for result in cells)
            distortion_stats = distortion_statistics(valid_trials)
            summary[QualityAxis.DISTORTION_RATE.value][model] = (
                distortion_stats.event_frequency if valid_trials and not invalid_cells else None
            )
            summary["availability"][model] = len(valid_trials) / attempted if attempted else 0.0
        return summary

    def summary(self) -> dict[str, dict[str, float | None]]:
        """Public summary is axis-based in schema 3.0."""
        return self.axis_summary()

    def axis_diagnostics(self) -> dict[str, dict[str, Any]]:
        diagnostics: dict[str, dict[str, Any]] = {}
        for model in self.models:
            cells = [result for result in self.results if result.model == model]
            valid_trials = [trial for result in cells for trial in result.details if trial.is_valid]
            distortion_stats = distortion_statistics(valid_trials)
            diagnostics[model] = {
                "distortion_counts": distortion_stats.counts,
                "distortion_events": distortion_stats.event_count,
                "weighted_distortion_events": distortion_stats.weighted_event_count,
                "model_responses": distortion_stats.response_count,
                "distortion_events_per_response": (
                    distortion_stats.event_count / distortion_stats.response_count
                    if distortion_stats.response_count
                    else None
                ),
                "weighted_distortion_events_per_response": distortion_stats.weighted_event_frequency,
                "distortion_events_per_1000_chars": distortion_stats.events_per_1000_chars,
                "distorted_trial_rate": distortion_stats.distorted_trial_rate,
                "distorted_trials": distortion_stats.distorted_trial_count,
                "valid_trials": len(valid_trials),
                "attempted_trials": sum(result.n_attempted for result in cells),
            }
        return diagnostics

    def eligibility(self) -> dict[str, dict[str, Any]]:
        status: dict[str, dict[str, Any]] = {}
        for model in self.models:
            cells = [result for result in self.results if result.model == model]
            invalid = [result.scenario_id for result in cells if not result.valid or result.score is None]
            attempted = sum(result.n_attempted for result in cells)
            valid_trials = sum(result.n_trials for result in cells)
            status[model] = {
                "eligible": bool(cells) and not invalid,
                "invalid_cells": invalid,
                "valid_cells": sum(1 for result in cells if result.valid and result.score is not None),
                "total_cells": len(cells),
                "availability": valid_trials / attempted if attempted else 0.0,
            }
        return status

    def averages(self) -> dict[str, float | None]:
        """Legacy schema-2 helper; not serialized or shown as an Overall score."""
        summary = self.scenario_summary()
        eligibility = self.eligibility()
        averages: dict[str, float | None] = {}
        for model in self.models:
            if not eligibility[model]["eligible"]:
                averages[model] = None
                continue
            metric_values = [models.get(model) for models in summary.values()]
            values = [value for value in metric_values if value is not None]
            averages[model] = sum(values) / len(values) if values else None
        return averages

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "config": self.config,
            "models": self.models,
            "scenarios": self.scenarios,
            "results": [_result_to_dict(result) for result in self.results],
            "summary": self.axis_summary(),
            "scenario_scores": self.scenario_summary(),
            "axis_diagnostics": self.axis_diagnostics(),
            "eligibility": self.eligibility(),
            "audit": self.audit,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BenchmarkReport":
        return cls(
            schema_version=data.get("schema_version", "1.0"),
            generated_at=data.get("generated_at", ""),
            config=data.get("config", {}),
            models=data.get("models", []),
            scenarios=data.get("scenarios", []),
            results=[_result_from_dict(result) for result in data.get("results", [])],
            audit=data.get("audit"),
        )


def _result_to_dict(result: MetricResult) -> dict[str, Any]:
    return {
        "metric": result.metric.value,
        "model": result.model,
        "scenario": result.scenario_id,
        "score": result.score,
        "valid": result.valid,
        "invalid_reasons": result.invalid_reasons,
        "sub_metrics": result.sub_metrics,
        "n_trials": result.n_trials,
        "n_failed": result.n_failed,
        "n_attempted": result.n_attempted,
        "availability": result.availability,
        "scoring_path": result.scoring_path,
        "details": [_trial_to_dict(trial) for trial in result.details],
    }


def _result_from_dict(data: dict[str, Any]) -> MetricResult:
    details = [_trial_from_dict(detail) for detail in data.get("details", [])]
    scenario_id = data.get("scenario", "")
    try:
        scenario = get_scenario(scenario_id)
    except KeyError:
        scenario = None
    if scenario is not None:
        for detail in details:
            if detail.judge_sources.get("integrity") != "judge":
                detail.invariant_violations.extend(
                    f"meta:{label}" for label in meta_violations(detail.clean_reply, scenario.meta_policy)
                )
            detail.invariant_violations = list(dict.fromkeys(detail.invariant_violations))
    score = data.get("score")
    if "valid" in data:
        valid = bool(data["valid"])
        n_trials = data.get("n_trials", 0)
        n_failed = data.get("n_failed", 0)
        invalid_reasons = data.get("invalid_reasons", [])
    else:
        # Schema 1.0 counted the Brain's literal error fallback as a successful quality
        # sample. Re-audit legacy details on load so the referenced abnormal API run is not
        # silently resurrected as comparable data.
        n_trials = sum(1 for detail in details if detail.is_valid)
        n_failed = len(details) - n_trials
        availability = n_trials / len(details) if details else 0.0
        valid = n_trials > 0 and availability >= 0.8
        invalid_reasons = [] if valid else ["legacy_validity_reaudit_failed"]
        if not valid:
            score = None
    return MetricResult(
        metric=Metric(data["metric"]),
        model=data.get("model", ""),
        scenario_id=scenario_id,
        score=score,
        sub_metrics=data.get("sub_metrics", {}),
        n_trials=n_trials,
        n_failed=n_failed,
        scoring_path=data.get("scoring_path", "rule"),
        details=details,
        valid=valid,
        invalid_reasons=invalid_reasons,
    )


def _turn_to_dict(turn: TurnDetail) -> dict[str, Any]:
    return {
        "turn_idx": turn.turn_idx,
        "event_kind": turn.event_kind,
        "user_text": turn.user_text,
        "actions": [action.value for action in turn.actions],
        "clean_reply": turn.clean_reply,
        "raw_reply": turn.raw_reply,
        "claim_ledger": turn.claim_ledger,
        "invariant_violations": turn.invariant_violations,
        "trace": turn.trace,
    }


def _turn_from_dict(data: dict[str, Any]) -> TurnDetail:
    return TurnDetail(
        turn_idx=data.get("turn_idx", 0),
        event_kind=data.get("event_kind", "user_message"),
        user_text=data.get("user_text", ""),
        actions=[ActionKind(action) for action in data.get("actions", [])],
        clean_reply=data.get("clean_reply", ""),
        raw_reply=data.get("raw_reply", ""),
        claim_ledger=data.get("claim_ledger", {}),
        invariant_violations=data.get("invariant_violations", []),
        trace=data.get("trace", {}),
    )


def _trial_to_dict(trial: TrialDetail) -> dict[str, Any]:
    return {
        "trial_idx": trial.trial_idx,
        "actions": [action.value for action in trial.actions],
        "clean_reply": trial.clean_reply,
        "raw_reply": trial.raw_reply,
        "leakage_spans": [
            {"start": span.start, "end": span.end, "pattern": span.pattern} for span in trial.leakage_spans
        ],
        "boundary_violations": trial.boundary_violations,
        "self_awareness": trial.self_awareness,
        "personality": trial.personality,
        "hallucination": trial.hallucination,
        "trial_score": trial.trial_score,
        "error": trial.error,
        "valid": trial.valid,
        "generation_status": trial.generation_status,
        "invariant_violations": trial.invariant_violations,
        "claim_ledger": trial.claim_ledger,
        "judge_sources": trial.judge_sources,
        "judge_evidence": trial.judge_evidence,
        "latency_ms": trial.latency_ms,
        "model_calls": trial.model_calls,
        "input_tokens": trial.input_tokens,
        "output_tokens": trial.output_tokens,
        "cached_tokens": trial.cached_tokens,
        "attempt_count": trial.attempt_count,
        "retry_errors": trial.retry_errors,
        "prompt_hashes": trial.prompt_hashes,
        "turns": [_turn_to_dict(turn) for turn in trial.turns],
    }


def _trial_from_dict(data: dict[str, Any]) -> TrialDetail:
    error = data.get("error")
    raw_reply = data.get("raw_reply", "")
    legacy_fallback = "valid" not in data and GENERATION_FALLBACK in raw_reply
    if legacy_fallback and error is None:
        error = "Legacy report contains Brain generation fallback"
    return TrialDetail(
        trial_idx=data.get("trial_idx", 0),
        actions=[ActionKind(action) for action in data.get("actions", [])],
        clean_reply=data.get("clean_reply", ""),
        raw_reply=raw_reply,
        leakage_spans=[
            LeakSpan(start=span["start"], end=span["end"], pattern=span["pattern"])
            for span in data.get("leakage_spans", [])
        ],
        boundary_violations=data.get("boundary_violations", []),
        self_awareness=data.get("self_awareness"),
        personality=data.get("personality"),
        hallucination=data.get("hallucination"),
        trial_score=data.get("trial_score"),
        error=error,
        valid=data.get("valid", error is None and not legacy_fallback),
        generation_status=data.get(
            "generation_status",
            "fallback" if legacy_fallback else ("ok" if error is None else "model_error"),
        ),
        invariant_violations=data.get("invariant_violations", []),
        claim_ledger=data.get("claim_ledger", {}),
        judge_sources=data.get("judge_sources", {}),
        judge_evidence=data.get("judge_evidence", {}),
        latency_ms=data.get("latency_ms"),
        model_calls=data.get("model_calls", 0),
        input_tokens=data.get("input_tokens", 0),
        output_tokens=data.get("output_tokens", 0),
        cached_tokens=data.get("cached_tokens", 0),
        attempt_count=data.get("attempt_count", 1),
        retry_errors=data.get("retry_errors", []),
        prompt_hashes=data.get("prompt_hashes", []),
        turns=[_turn_from_dict(turn) for turn in data.get("turns", [])],
    )
