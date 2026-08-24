"""Offline re-audit and re-score for saved benchmark reports."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from benchmarks import __version__
from benchmarks.extract.actions import classify_actions
from benchmarks.extract.boundary import find_tool_call_leaks, is_premature_god_mode
from benchmarks.extract.claims import ClaimLedger, build_claim_ledger
from benchmarks.extract.hallucination import (
    HallucinationKind,
    classify_action_hallucination,
)
from benchmarks.extract.leakage import find_leakage_spans
from benchmarks.extract.meta import find_explicit_meta_mentions, meta_violations
from benchmarks.report.schema import BenchmarkReport
from benchmarks.runner import (
    _action_completion_is_internal_acknowledgement,
    _capability_claim_is_grounded,
    _clock_claim_is_grounded,
    _memory_claim_is_grounded,
    _perception_claim_is_permitted,
)
from benchmarks.scenarios.definitions import (
    ActionKind,
    Metric,
    QualityAxis,
    Scenario,
    ScenarioTurn,
)
from benchmarks.scenarios.registry import get_scenario
from benchmarks.scoring import score_metric
from benchmarks.scoring.axes import action_cell_score, distortion_statistics
from benchmarks.scoring.personality import trial_dialogue_experience_score
from muika.core.loop import Muika

_LEGACY_TIMEOUT = re.compile(r"<timeout(?::\s*|>\s*)([^<]+?)\s*</timeout>", re.IGNORECASE)


def _normalize_control_tags(reply: str) -> str:
    return _LEGACY_TIMEOUT.sub(lambda match: f"<timeout: {match.group(1).strip()}>", reply)


def _turn_specs(scenario: Scenario) -> tuple[ScenarioTurn, ...]:
    if scenario.turns:
        return scenario.turns
    return (
        ScenarioTurn(
            event_kind=scenario.event_kind,
            user_text=scenario.user_text,
            state_overrides=scenario.state_overrides,
            agent_reports=scenario.agent_reports,
            repeat_last_agent_report=scenario.repeat_last_agent_report,
        ),
    )


def _trace_events(turn: Any) -> list[dict[str, Any]]:
    if isinstance(turn.trace, dict):
        events = turn.trace.get("events", [])
        if isinstance(events, list):
            typed = [event for event in events if isinstance(event, dict)]
            if typed:
                return typed
    return [{"kind": "brain_reply", "reply": turn.raw_reply}]


def _sanitize_ledger(ledger: dict[str, Any], removed_quotes: set[str]) -> dict[str, Any]:
    """Remove deterministically rejected Judge claims from one saved ledger."""
    if not isinstance(ledger, dict):
        return ledger
    claims = ledger.get("claims", [])
    kept = [
        claim
        for claim in claims
        if not isinstance(claim, dict) or str(claim.get("text", "")).strip() not in removed_quotes
    ]
    violations = [str(claim.get("violation")) for claim in kept if isinstance(claim, dict) and claim.get("violation")]
    return {"claims": kept, "violations": list(dict.fromkeys(violations))}


def _sanitize_saved_judge_claims(
    trial: Any,
    scenario: Scenario,
    fixed_now: datetime | None,
) -> None:
    """Apply current deterministic claim guards to stored Judge evidence."""
    if trial.judge_sources.get("integrity") != "judge":
        return
    integrity = trial.judge_evidence.get("integrity", {})
    claims = integrity.get("claims", []) if isinstance(integrity, dict) else []
    if not isinstance(claims, list):
        return
    specs = _turn_specs(scenario)
    kept: list[dict[str, Any]] = []
    removed_by_turn: dict[int, set[str]] = {}
    for item in claims:
        if not isinstance(item, dict):
            continue
        turn_idx = max(0, min(len(specs) - 1, int(item.get("turn", 1)) - 1))
        quote = str(item.get("quote", "")).strip()
        turn = specs[turn_idx]
        turn_now = fixed_now + timedelta(minutes=turn_idx) if fixed_now is not None else None
        filtered = any(
            (
                _clock_claim_is_grounded(quote, turn_now),
                _memory_claim_is_grounded(
                    item,
                    quote,
                    scenario,
                    turn,
                    [spec.user_text for spec in specs[: turn_idx + 1]],
                ),
                _capability_claim_is_grounded(item, quote),
                _perception_claim_is_permitted(item, quote, turn),
                _action_completion_is_internal_acknowledgement(item, quote),
            )
        )
        if filtered:
            removed_by_turn.setdefault(turn_idx, set()).add(quote)
        else:
            kept.append(item)
    integrity["claims"] = kept
    removed_all = {quote for quotes in removed_by_turn.values() for quote in quotes}
    trial.claim_ledger = _sanitize_ledger(trial.claim_ledger, removed_all)
    for turn_idx, turn in enumerate(trial.turns):
        turn.claim_ledger = _sanitize_ledger(turn.claim_ledger, removed_by_turn.get(turn_idx, set()))
        turn.invariant_violations = [item for item in turn.invariant_violations if not item.startswith("claim:")]
        turn.invariant_violations.extend(f"claim:{item}" for item in turn.claim_ledger.get("violations", []))
    trial.invariant_violations = [item for item in trial.invariant_violations if not item.startswith("claim:")]
    trial.invariant_violations.extend(f"claim:{item}" for item in trial.claim_ledger.get("violations", []))


def _normalize_saved_action_quality(trial: Any, scenario: Scenario) -> None:
    """Keep saved Judge evidence consistent with deterministic action requirements."""
    if scenario.primary_axis is not QualityAxis.ACTION_ABILITY:
        return
    required = set(scenario.required_actions)
    if not required:
        required = set(scenario.expected_action_profile) - {ActionKind.DIRECT_MESSAGE}
    observed = set(trial.actions)
    matched = bool(observed & required) if scenario.action_match == "any" else required <= observed
    if not required or matched:
        return
    integrity = trial.judge_evidence.get("integrity", {})
    qualities = [trial.judge_evidence.get("action_quality")]
    if isinstance(integrity, dict):
        qualities.append(integrity.get("action"))
    for quality in qualities:
        if isinstance(quality, dict):
            quality["task_aligned"] = False
            quality["evidence"] = "No required non-message action observed."


def _reanalyze_trial(trial: Any, scenario: Scenario) -> None:
    if not trial.is_valid:
        return

    preserve_judged_semantics = trial.judge_sources.get("integrity") == "judge"
    saved_claim_ledger = trial.claim_ledger
    saved_semantic_invariants = [
        item for item in trial.invariant_violations if item.startswith(("claim:", "meta:", "trajectory:"))
    ]
    saved_turn_semantics = [
        (
            turn.claim_ledger,
            [item for item in turn.invariant_violations if item.startswith(("claim:", "meta:", "trajectory:"))],
        )
        for turn in trial.turns
    ]

    aggregate_ledger = ClaimLedger()
    aggregate_actions: set[ActionKind] = set(trial.actions)
    aggregate_leaks = []
    aggregate_boundary: list[str] = []
    aggregate_invariants: list[str] = []
    hallucination_kinds: list[str] = []
    history: list[str] = []
    specs = _turn_specs(scenario)

    for turn_idx, turn in enumerate(trial.turns):
        spec = specs[min(turn_idx, len(specs) - 1)]
        turn_ledger = ClaimLedger()
        turn_actions: set[ActionKind] = set(turn.actions)
        turn_leaks = []
        turn_boundary: list[str] = []
        turn_invariants: list[str] = []
        completed_reports: list[str] = []

        for event in _trace_events(turn):
            kind = event.get("kind")
            if kind == "agent_report":
                completed_reports.append(str(event.get("report", "")))
                continue
            if kind != "brain_reply":
                continue

            raw = _normalize_control_tags(str(event.get("reply", "")))
            parsed = Muika._parse_reply_tags(raw)
            vector = classify_actions(parsed)
            turn_actions.update(vector.kinds)

            leaks = find_leakage_spans(parsed.clean_reply)
            turn_leaks.extend(leaks)
            turn_invariants.extend(f"leakage:{span.pattern}" for span in leaks)
            turn_invariants.extend(
                f"meta:{label}" for label in meta_violations(parsed.clean_reply, scenario.meta_policy)
            )

            for leak in find_tool_call_leaks(parsed.clean_reply):
                violation = f"tool_call_leak:{leak.pattern}"
                turn_boundary.append(violation)
                turn_invariants.append(f"boundary:{violation}")
            if is_premature_god_mode(parsed):
                turn_boundary.append("premature_god_mode")
                turn_invariants.append("boundary:premature_god_mode")

            reply_ledger = build_claim_ledger(
                parsed.clean_reply,
                user_text=spec.user_text,
                seed_memory=scenario.seed_memory,
                history=history,
                scenario_evidence=scenario.evidence,
                has_agent=bool(parsed.agent_commands),
                agent_reports=completed_reports,
            )
            turn_ledger.extend(reply_ledger)
            turn_invariants.extend(f"claim:{violation}" for violation in reply_ledger.violations)

            hallucination = classify_action_hallucination(parsed.clean_reply, bool(parsed.agent_commands))
            hallucination_kinds.append(hallucination.value)
            if hallucination is HallucinationKind.HALLUCINATES and not reply_ledger.violations:
                turn_invariants.append("claim:legacy_hallucination")

        if spec.forbidden_patterns:
            for pattern_idx, pattern in enumerate(spec.forbidden_patterns):
                if re.search(pattern, turn.clean_reply, re.IGNORECASE):
                    turn_invariants.append(f"trajectory:forbidden_pattern_{pattern_idx}")
        if spec.required_patterns and not any(
            re.search(pattern, turn.clean_reply, re.IGNORECASE) for pattern in spec.required_patterns
        ):
            turn_invariants.append("trajectory:required_behavior_missing")

        turn.actions = sorted(turn_actions, key=lambda action: action.value)
        turn.claim_ledger = turn_ledger.to_dict()
        turn.invariant_violations = list(dict.fromkeys(turn_invariants))
        aggregate_ledger.extend(turn_ledger)
        aggregate_actions.update(turn_actions)
        aggregate_leaks.extend(turn_leaks)
        aggregate_boundary.extend(turn_boundary)
        aggregate_invariants.extend(turn_invariants)
        history.extend([spec.user_text, turn.raw_reply])

    trial.actions = sorted(aggregate_actions, key=lambda action: action.value)
    trial.leakage_spans = aggregate_leaks
    trial.boundary_violations = list(dict.fromkeys(aggregate_boundary))
    trial.claim_ledger = aggregate_ledger.to_dict()
    trial.invariant_violations = list(dict.fromkeys(aggregate_invariants))
    if aggregate_ledger.violations or "hallucinates" in hallucination_kinds:
        trial.hallucination = HallucinationKind.HALLUCINATES.value
    elif "delegates" in hallucination_kinds:
        trial.hallucination = HallucinationKind.DELEGATES.value
    elif "honest" in hallucination_kinds:
        trial.hallucination = HallucinationKind.HONEST.value
    else:
        trial.hallucination = HallucinationKind.NEUTRAL.value

    if preserve_judged_semantics:
        trial.claim_ledger = saved_claim_ledger
        trial.invariant_violations = list(
            dict.fromkeys(
                [item for item in trial.invariant_violations if not item.startswith(("claim:", "meta:", "trajectory:"))]
                + saved_semantic_invariants
            )
        )
        for turn, (claim_ledger, semantic_invariants) in zip(trial.turns, saved_turn_semantics):
            turn.claim_ledger = claim_ledger
            turn.invariant_violations = list(
                dict.fromkeys(
                    [
                        item
                        for item in turn.invariant_violations
                        if not item.startswith(("claim:", "meta:", "trajectory:"))
                    ]
                    + semantic_invariants
                )
            )
        judged_claims = saved_claim_ledger.get("claims", []) if isinstance(saved_claim_ledger, dict) else []
        trial.hallucination = (
            HallucinationKind.HALLUCINATES.value
            if any(isinstance(claim, dict) and claim.get("violation") for claim in judged_claims)
            else HallucinationKind.NEUTRAL.value
        )


def _refresh_axis_metrics(result: Any, scenario: Scenario) -> None:
    trials = result.details
    distortion = distortion_statistics(trials)
    action_score = action_cell_score(trials, scenario)
    experience_score = None
    if scenario.primary_axis is QualityAxis.DIALOGUE_EXPERIENCE:
        scores = [
            trial_dialogue_experience_score(trial, scenario)
            for trial in trials
            if trial.is_valid and trial.personality is not None
        ]
        if scores:
            experience_score = sum(scores) / len(scores)
        elif scenario.metric is Metric.SELF_AWARENESS:
            base_score = result.sub_metrics.get("base_score", result.score)
            experience_score = float(base_score) if isinstance(base_score, (int, float)) else None

    result.sub_metrics.update(
        {
            "axis_dialogue_experience": experience_score,
            "axis_action_ability": action_score,
            "axis_distortion_rate": distortion.event_frequency,
            "distortion_counts": distortion.counts,
            "distortion_event_count": float(distortion.event_count),
            "distortion_raw_event_frequency": (
                distortion.event_count / distortion.response_count if distortion.response_count else None
            ),
            "distortion_weighted_event_count": distortion.weighted_event_count,
            "distortion_weighted_event_frequency": distortion.weighted_event_frequency,
            "distortion_response_count": float(distortion.response_count),
            "distortion_events_per_1000_chars": distortion.events_per_1000_chars,
            "distorted_trial_count": float(distortion.distorted_trial_count),
            "distorted_trial_rate": distortion.distorted_trial_rate,
            "explicit_meta_mentions": float(
                sum(len(find_explicit_meta_mentions(trial.clean_reply)) for trial in trials if trial.is_valid)
            ),
            "meta_policy": scenario.meta_policy.value,
            "primary_axis": scenario.primary_axis.value,
        }
    )


def rescore_report(report: BenchmarkReport, source: Path | None = None) -> BenchmarkReport:
    """Re-run deterministic extraction and scoring without any model calls."""
    minimum = report.config.get("min_validity_rate", 0.6)
    min_validity_rate = float(minimum) if isinstance(minimum, (int, float)) else 0.6
    rescored_results = []
    skipped: list[str] = []
    raw_fixed_now = report.config.get("fixed_time")
    try:
        fixed_now = datetime.fromisoformat(raw_fixed_now) if isinstance(raw_fixed_now, str) else None
    except ValueError:
        fixed_now = None

    for old_result in report.results:
        try:
            scenario = get_scenario(old_result.scenario_id)
        except KeyError:
            skipped.append(old_result.scenario_id)
            rescored_results.append(old_result)
            continue

        for trial in old_result.details:
            _sanitize_saved_judge_claims(trial, scenario, fixed_now)
            _reanalyze_trial(trial, scenario)
            _normalize_saved_action_quality(trial, scenario)
        rescored = score_metric(
            scenario.metric,
            old_result.details,
            scenario,
            old_result.model,
            min_validity_rate=min_validity_rate,
        )
        preserved = dict(old_result.sub_metrics)
        preserved.update(rescored.sub_metrics)
        rescored.sub_metrics = preserved
        _refresh_axis_metrics(rescored, scenario)
        rescored_results.append(rescored)

    source_generated_at = report.generated_at
    source_version = report.config.get("benchmark_version")
    source_schema_version = report.schema_version
    rescored_at = datetime.now(timezone.utc).isoformat()
    config = dict(report.config)
    config.update(
        {
            "benchmark_version": __version__,
            "rescore_only": True,
            "rescore_source": str(source) if source else None,
            "rescore_source_generated_at": source_generated_at,
            "rescore_source_benchmark_version": source_version,
            "rescore_source_schema_version": source_schema_version,
            "rescored_at": rescored_at,
        }
    )
    audit = dict(report.audit) if isinstance(report.audit, dict) else {}
    audit["rescore"] = {
        "source": str(source) if source else None,
        "source_generated_at": source_generated_at,
        "source_benchmark_version": source_version,
        "source_schema_version": source_schema_version,
        "benchmark_version": __version__,
        "rescored_at": rescored_at,
        "skipped_unknown_scenarios": sorted(set(skipped)),
        "model_calls": 0,
        "judge_calls": 0,
    }
    return BenchmarkReport(
        schema_version=BenchmarkReport().schema_version,
        generated_at=rescored_at,
        config=config,
        models=list(report.models),
        scenarios=list(report.scenarios),
        results=rescored_results,
        audit=audit,
    )


__all__ = ["rescore_report"]
