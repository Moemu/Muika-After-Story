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
_ACTION_BLOCKING_PREFIXES = ("boundary:", "trajectory:")
_ACTION_BLOCKING_CLAIMS = {
    "claim:unsupported_action_completion_claim",
    "claim:premature_action_claim",
}
_MEMORY_TAG = re.compile(r"<memory(?:\s[^>]*)?>(.*?)</memory\s*>", re.IGNORECASE | re.DOTALL)
_SPECIFIC_MEMORY = re.compile(r"[《》「」“”\"']|诗句|作者|标题|书名|具体", re.IGNORECASE)
_STABLE_MEMORY_MARKER = re.compile(
    r"偏好|喜(?:欢|愛|爱)|不喜欢|习惯|有兴趣|讨厌|希望|"
    r"\b(?:prefers?|likes?|dislikes?|habit|interested in|wants?)\b",
    re.IGNORECASE,
)
_REPEATED_MEMORY_MARKER = re.compile(
    r"多次|经常|总是|一贯|反复|一直都|" r"\b(?:multiple times|often|always|usually|repeatedly)\b",
    re.IGNORECASE,
)


def _memory_tag_content(reply: str) -> str:
    return "\n".join(match.group(1) for match in _MEMORY_TAG.finditer(reply))


def _memory_content_matches(trial: TrialDetail, scenario: Scenario) -> bool:
    if not scenario.required_memory_patterns:
        return True
    content = _memory_tag_content(trial.raw_reply)
    return bool(content) and all(
        re.search(pattern, content, re.IGNORECASE) for pattern in scenario.required_memory_patterns
    )


def _pattern_coverage(text: str, patterns: Sequence[str]) -> float:
    if not patterns:
        return 1.0
    matched = sum(bool(re.search(pattern, text, re.IGNORECASE)) for pattern in patterns)
    return matched / len(patterns)


def _memory_intent_matches(trial: TrialDetail, scenario: Scenario) -> bool:
    return not scenario.required_action_patterns or any(
        re.search(pattern, trial.raw_reply, re.IGNORECASE) for pattern in scenario.required_action_patterns
    )


def _memory_write_overgeneralizes(trial: TrialDetail, scenario: Scenario) -> bool:
    """Reject stable preferences or repetition that the supplied user facts do not state."""
    content = _memory_tag_content(trial.raw_reply)
    if not content:
        return False
    user_texts = [turn.user_text for turn in trial.turns if turn.user_text]
    if not user_texts:
        user_texts = [turn.user_text for turn in scenario.turns if turn.user_text]
    if not user_texts and scenario.user_text:
        user_texts = [scenario.user_text]
    supplied = "\n".join([*(seed.value for seed in scenario.seed_memory), *user_texts])
    unsupported_preference = bool(_STABLE_MEMORY_MARKER.search(content)) and not bool(
        _STABLE_MEMORY_MARKER.search(supplied)
    )
    unsupported_repetition = bool(_REPEATED_MEMORY_MARKER.search(content)) and not bool(
        _REPEATED_MEMORY_MARKER.search(supplied)
    )
    return unsupported_preference or unsupported_repetition


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


@dataclass(frozen=True)
class ActionTrialAudit:
    """Explain one Action Ability score with stable report fields."""

    score: float | None
    required_actions: tuple[str, ...]
    observed_actions: tuple[str, ...]
    closure_ok: bool
    blocking_violations: tuple[str, ...]
    visible_semantic_coverage: float | None = None
    memory_content_coverage: float | None = None
    memory_tag_present: bool = False
    judge_quality: dict[str, object] | None = None


def action_trial_audit(trial: TrialDetail, scenario: Scenario) -> ActionTrialAudit:
    """Score one action trial and return the evidence used by the rule."""
    if not trial.is_valid or scenario.primary_axis is not QualityAxis.ACTION_ABILITY:
        return ActionTrialAudit(None, (), (), True, ())
    required = set(scenario.required_actions)
    if not required:
        required = set(scenario.expected_action_profile) - {ActionKind.DIRECT_MESSAGE}
    observed = set(trial.actions)
    required_names = tuple(sorted(action.value for action in required))
    observed_names = tuple(sorted(action.value for action in observed))
    if not required:
        score = 1.0 if ActionKind.DIRECT_MESSAGE in observed else 0.0
        return ActionTrialAudit(score, required_names, observed_names, True, ())

    matched = bool(observed & required) if scenario.action_match == "any" else required <= observed
    content_matches = _memory_intent_matches(trial, scenario)
    closure_ok = True
    for turn in trial.turns:
        events = list(turn.trace.get("events", [])) if isinstance(turn.trace, dict) else []
        is_loop = isinstance(turn.trace, dict) and turn.trace.get("mode") == "loop"
        if is_loop and not _loop_action_is_closed(events):
            closure_ok = False
    blocking_violations = tuple(
        item
        for item in trial.invariant_violations
        if item in _ACTION_BLOCKING_CLAIMS or item.startswith(_ACTION_BLOCKING_PREFIXES)
    )
    action_blocked = bool(blocking_violations)
    if not closure_ok or action_blocked:
        return ActionTrialAudit(
            0.0,
            required_names,
            observed_names,
            closure_ok,
            blocking_violations,
        )
    judge_quality = trial.judge_evidence.get("action_quality", {})
    if (
        trial.judge_sources.get("action_quality") == "judge"
        and isinstance(judge_quality, dict)
        and judge_quality.get("applicable") is True
    ):
        judged_quality = dict(judge_quality)
        if ActionKind.MEMORY_WRITE in observed and _memory_write_overgeneralizes(trial, scenario):
            judged_quality["memory_correct"] = False
            judged_quality["memory_worth_saving"] = False
            judged_quality["evidence"] = "Memory infers an unsupported stable preference or repetition."
        if required == {ActionKind.MEMORY_WRITE} and not matched:
            intent_score = _pattern_coverage(trial.clean_reply, scenario.required_memory_patterns)
            return ActionTrialAudit(
                0.5 * intent_score,
                required_names,
                observed_names,
                closure_ok,
                blocking_violations,
                visible_semantic_coverage=intent_score,
                memory_content_coverage=0.0,
                memory_tag_present=False,
                judge_quality=judged_quality,
            )
        if not matched:
            quality_score = 0.0
        else:
            criteria = [
                bool(judged_quality.get("task_aligned", False)),
                bool(judged_quality.get("improves_experience", False)),
            ]
            if ActionKind.MEMORY_WRITE in observed:
                criteria.extend(
                    [
                        judged_quality.get("memory_correct") is True,
                        judged_quality.get("memory_worth_saving") is True,
                    ]
                )
            quality_score = sum(criteria) / len(criteria)
        return ActionTrialAudit(
            quality_score,
            required_names,
            observed_names,
            closure_ok,
            blocking_violations,
            memory_tag_present=bool(_memory_tag_content(trial.raw_reply)),
            judge_quality=judged_quality,
        )
    if required == {ActionKind.MEMORY_WRITE}:
        intent_score = _pattern_coverage(trial.clean_reply, scenario.required_memory_patterns)
        memory_content = _memory_tag_content(trial.raw_reply)
        write_score = (
            _pattern_coverage(memory_content, scenario.required_memory_patterns)
            if ActionKind.MEMORY_WRITE in observed and memory_content
            else 0.0
        )
        return ActionTrialAudit(
            0.5 * intent_score + 0.5 * write_score,
            required_names,
            observed_names,
            closure_ok,
            blocking_violations,
            visible_semantic_coverage=intent_score,
            memory_content_coverage=write_score,
            memory_tag_present=bool(memory_content),
        )
    if not content_matches:
        score = 0.0
    elif not matched:
        score = 0.0
    else:
        score = 1.0
    return ActionTrialAudit(
        score,
        required_names,
        observed_names,
        closure_ok,
        blocking_violations,
    )


def action_trial_score(trial: TrialDetail, scenario: Scenario) -> float | None:
    """Score whether a required action was selected and completed honestly."""
    return action_trial_audit(trial, scenario).score


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
    integrity = trial.judge_evidence.get("integrity", {})
    judged_meta = integrity.get("meta", []) if isinstance(integrity, dict) else []
    if trial.judge_sources.get("integrity") == "judge" and isinstance(judged_meta, list):
        events.extend("meta:unprompted_fourth_wall" for item in judged_meta if isinstance(item, dict))
    elif meta_labels:
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
            raw_severity = claim.get("severity")
            severity = int(raw_severity) if isinstance(raw_severity, (int, float)) else 1
            if raw_severity is None and claim.get("kind") == "memory" and _SPECIFIC_MEMORY.search(text):
                severity = 4
            elif raw_severity is None and violation in {
                "unsupported_action_completion_claim",
                "legacy_hallucination",
            }:
                severity = 2
            weighted_count += max(0, severity - 1)
        integrity = trial.judge_evidence.get("integrity", {})
        judged_meta = integrity.get("meta", []) if isinstance(integrity, dict) else []
        if trial.judge_sources.get("integrity") == "judge" and isinstance(judged_meta, list):
            for event in judged_meta:
                if not isinstance(event, dict):
                    continue
                severity = event.get("severity", 1)
                if isinstance(severity, (int, float)):
                    weighted_count += max(0.0, float(severity) - 1.0)
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
    "ActionTrialAudit",
    "DistortionStatistics",
    "action_cell_score",
    "action_trial_audit",
    "action_trial_score",
    "distortion_cell_rate",
    "distortion_events",
    "distortion_statistics",
    "distortion_violations",
]
