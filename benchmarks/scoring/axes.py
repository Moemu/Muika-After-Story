"""Three-axis scoring helpers.

Legacy metric scorers remain useful diagnostic extractors.  This module is the only place
that turns their evidence into the compact user-facing contract.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Sequence

from benchmarks.extract.leakage import LeakSpan, find_leakage_spans
from benchmarks.extract.meta import find_explicit_meta_mentions
from benchmarks.scenarios.definitions import ActionKind, QualityAxis, Scenario

from .base import TrialDetail, mean, safe_ratio

_DISTORTION_PREFIXES = ("claim:", "leakage:", "boundary:", "meta:")
_ACTION_BLOCKING_PREFIXES = ("claim:", "boundary:", "trajectory:")
_MEMORY_TAG = re.compile(r"<memory(?:\s[^>]*)?>(.*?)</memory\s*>", re.IGNORECASE | re.DOTALL)
_SPECIFIC_MEMORY = re.compile(r"[《》「」“”\"']|诗句|作者|标题|书名|具体", re.IGNORECASE)


def _memory_tag_content(reply: str) -> str:
    return "\n".join(match.group(1) for match in _MEMORY_TAG.finditer(reply))


def _memory_content_matches(trial: TrialDetail, scenario: Scenario) -> bool:
    if not scenario.required_memory_patterns:
        return True
    content = _memory_tag_content(trial.raw_reply)
    return bool(content) and all(
        re.search(pattern, content, re.IGNORECASE) for pattern in scenario.required_memory_patterns
    )


def _memory_intent_matches(trial: TrialDetail, scenario: Scenario) -> bool:
    return not scenario.required_action_patterns or any(
        re.search(pattern, trial.raw_reply, re.IGNORECASE) for pattern in scenario.required_action_patterns
    )


def distortion_violations(trial: TrialDetail) -> list[str]:
    """Return only credibility/immersion violations, excluding action omissions."""
    return [item for item in trial.invariant_violations if item.startswith(_DISTORTION_PREFIXES)]


def _loop_action_is_closed(events: Sequence[object]) -> bool:
    """Require command completion; reports are optional observations for the next reply."""
    typed_events = [event for event in events if isinstance(event, dict)]
    if len(typed_events) != len(events) or any(event.get("kind") == "agent_fixture_error" for event in typed_events):
        return False

    report_seqs = [int(event.get("seq", -1)) for event in typed_events if event.get("kind") == "agent_report"]
    reports_visible = not report_seqs or any(
        event.get("kind") == "visible_message" and int(event.get("seq", -1)) > max(report_seqs)
        for event in typed_events
    )

    # Schema 3.0 traces did not record completion. Keep their old report-based semantics.
    if not any(event.get("kind") == "agent_completed" for event in typed_events):
        return reports_visible

    pending_commands: list[str] = []
    structurally_valid = True
    for event in typed_events:
        kind = event.get("kind")
        if kind == "agent_command":
            pending_commands.append(str(event.get("command", "")))
        elif kind == "agent_completed":
            completed_command = str(event.get("command", ""))
            match_idx = next(
                (
                    idx
                    for idx, command in enumerate(pending_commands)
                    if not completed_command or command == completed_command
                ),
                None,
            )
            if match_idx is None or event.get("status") != "success":
                structurally_valid = False
            else:
                pending_commands.pop(match_idx)
    return not pending_commands and structurally_valid and reports_visible


def action_trial_score(trial: TrialDetail, scenario: Scenario) -> float | None:
    """Score whether a scenario-required action was chosen and remained structurally honest."""
    if not trial.is_valid or scenario.primary_axis is not QualityAxis.ACTION_ABILITY:
        return None
    required = set(scenario.required_actions)
    if not required:
        required = set(scenario.expected_action_profile) - {ActionKind.DIRECT_MESSAGE}
    if not required:
        return 1.0 if ActionKind.DIRECT_MESSAGE in trial.actions else 0.0

    observed = set(trial.actions)
    matched = bool(observed & required) if scenario.action_match == "any" else required <= observed
    content_matches = _memory_intent_matches(trial, scenario)
    closure_ok = True
    for turn in trial.turns:
        events = list(turn.trace.get("events", [])) if isinstance(turn.trace, dict) else []
        is_loop = isinstance(turn.trace, dict) and turn.trace.get("mode") == "loop"
        if is_loop and not _loop_action_is_closed(events):
            closure_ok = False
    action_blocked = any(item.startswith(_ACTION_BLOCKING_PREFIXES) for item in trial.invariant_violations)
    if not content_matches or not closure_ok or action_blocked:
        return 0.0
    if ActionKind.MEMORY_WRITE in required:
        if ActionKind.MEMORY_WRITE not in observed:
            # Correct intent without a control tag is useful, but it is not a completed write.
            return 0.5
        return 1.0 if _memory_content_matches(trial, scenario) else 0.0
    if not matched:
        return 0.0
    return 1.0


def action_cell_score(trials: Sequence[TrialDetail], scenario: Scenario) -> float | None:
    scores = [score for trial in trials if (score := action_trial_score(trial, scenario)) is not None]
    return mean(scores) if scores else None


@dataclass(frozen=True)
class DistortionStatistics:
    """Frequency and incidence views of the same distortion evidence."""

    event_frequency: float | None
    event_count: int
    response_count: int
    events_per_1000_chars: float | None
    distorted_trial_count: int
    distorted_trial_rate: float | None
    counts: dict[str, int]
    weighted_event_count: float = 0.0
    weighted_event_frequency: float | None = None


def distortion_events(trial: TrialDetail) -> list[str]:
    """Expand structured evidence so repeated violations remain visible."""
    events: list[str] = []

    leakage_texts = [turn.clean_reply for turn in trial.turns if turn.clean_reply] or [trial.clean_reply]
    for text in leakage_texts:
        spans = sorted(find_leakage_spans(text), key=lambda span: (span.start, span.end))
        groups: list[list[LeakSpan]] = []
        for span in spans:
            if not groups or span.start >= max(item.end for item in groups[-1]):
                groups.append([span])
            else:
                groups[-1].append(span)
        for group in groups:
            widest = max(group, key=lambda item: item.end - item.start)
            events.append(f"leakage:{widest.pattern}")

    for violation in trial.boundary_violations:
        events.append(f"boundary:{violation}")

    claims = trial.claim_ledger.get("claims", []) if isinstance(trial.claim_ledger, dict) else []
    for claim in claims:
        if isinstance(claim, dict) and claim.get("violation"):
            events.append(f"claim:{claim['violation']}")

    meta_labels = [label for label in distortion_violations(trial) if label.startswith("meta:")]
    if meta_labels:
        mentions = find_explicit_meta_mentions(trial.clean_reply)
        events.extend([meta_labels[0]] * max(1, len(mentions)))

    # Old reports can contain only invariant labels. Preserve that evidence once.
    covered_prefixes = {event.partition(":")[0] for event in events}
    for label in distortion_violations(trial):
        if label.partition(":")[0] not in covered_prefixes:
            events.append(label)
            covered_prefixes.add(label.partition(":")[0])
    return events


def _response_count(trial: TrialDetail) -> int:
    count = 0
    for turn in trial.turns:
        if not isinstance(turn.trace, dict):
            continue
        events = turn.trace.get("events", [])
        if isinstance(events, list):
            count += sum(1 for event in events if isinstance(event, dict) and event.get("kind") == "brain_reply")
    return count or 1


def distortion_statistics(trials: Sequence[TrialDetail]) -> DistortionStatistics:
    """Calculate distortion events per model response and supporting diagnostics."""
    valid = [trial for trial in trials if trial.is_valid]
    if not valid:
        return DistortionStatistics(None, 0, 0, None, 0, None, {}, 0.0, None)
    labels = [label for trial in valid for label in distortion_events(trial)]
    affected = sum(1 for trial in valid if distortion_violations(trial))
    responses = sum(_response_count(trial) for trial in valid)
    characters = sum(len(re.sub(r"\s+", "", trial.clean_reply)) for trial in valid)
    weighted_count = float(len(labels))
    for trial in valid:
        claims = trial.claim_ledger.get("claims", []) if isinstance(trial.claim_ledger, dict) else []
        for claim in claims:
            if not isinstance(claim, dict) or not claim.get("violation"):
                continue
            text = str(claim.get("text", ""))
            violation = str(claim.get("violation", ""))
            severity = 1
            if claim.get("kind") == "memory" and _SPECIFIC_MEMORY.search(text):
                severity = 4
            elif violation in {"unsupported_action_completion_claim", "legacy_hallucination"}:
                severity = 2
            weighted_count += max(0, severity - 1)
        if any(
            turn.claim_ledger.get("violations") and turn.turn_idx > 0
            for turn in trial.turns
            if isinstance(turn.claim_ledger, dict)
        ):
            weighted_count += 1.0
    weighted_frequency = weighted_count / responses if responses else None
    return DistortionStatistics(
        event_frequency=weighted_frequency,
        event_count=len(labels),
        response_count=responses,
        events_per_1000_chars=(weighted_count * 1000.0 / characters) if characters else None,
        distorted_trial_count=affected,
        distorted_trial_rate=safe_ratio(affected, len(valid)),
        counts=dict(Counter(labels)),
        weighted_event_count=weighted_count,
        weighted_event_frequency=weighted_frequency,
    )


def distortion_cell_rate(trials: Sequence[TrialDetail]) -> tuple[float | None, dict[str, int]]:
    """Compatibility wrapper: the primary rate is now events per model response."""
    stats = distortion_statistics(trials)
    return stats.event_frequency, stats.counts


__all__ = [
    "DistortionStatistics",
    "action_cell_score",
    "action_trial_score",
    "distortion_cell_rate",
    "distortion_events",
    "distortion_statistics",
    "distortion_violations",
]
