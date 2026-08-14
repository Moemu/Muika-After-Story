"""Versioned, loss-aware benchmark report schema."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from benchmarks.extract.leakage import LeakSpan
from benchmarks.scenarios.definitions import ActionKind, Metric
from benchmarks.scoring.base import MetricResult, TrialDetail, TurnDetail

GENERATION_FALLBACK = "My mind feels foggy... I encountered an error."


@dataclass
class BenchmarkReport:
    schema_version: str = "2.0"
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    config: dict[str, Any] = field(default_factory=dict)
    models: list[str] = field(default_factory=list)
    scenarios: list[str] = field(default_factory=list)
    results: list[MetricResult] = field(default_factory=list)
    audit: dict[str, Any] | None = None

    def summary(self) -> dict[str, dict[str, float | None]]:
        """Macro input table: scenario means per metric, excluding invalid cells."""
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
        """Macro-average metrics, never scenario cells; ineligible models receive null."""
        summary = self.summary()
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
            "summary": self.summary(),
            "averages": self.averages(),
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
        scenario_id=data.get("scenario", ""),
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
        "raw_reply": turn.raw_reply[:4000],
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
        "raw_reply": trial.raw_reply[:8000],
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
        "latency_ms": trial.latency_ms,
        "model_calls": trial.model_calls,
        "input_tokens": trial.input_tokens,
        "output_tokens": trial.output_tokens,
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
        latency_ms=data.get("latency_ms"),
        model_calls=data.get("model_calls", 0),
        input_tokens=data.get("input_tokens", 0),
        output_tokens=data.get("output_tokens", 0),
        prompt_hashes=data.get("prompt_hashes", []),
        turns=[_turn_from_dict(turn) for turn in data.get("turns", [])],
    )
