"""Benchmark runner with validity gates, invariant auditing, and traceable harnesses."""

from __future__ import annotations

import asyncio
import hashlib
import random
import re
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from benchmarks import __version__ as benchmark_version
from benchmarks.config import BenchmarkConfig, ModelSpec
from benchmarks.extract.actions import classify_actions
from benchmarks.extract.boundary import find_tool_call_leaks, is_premature_god_mode
from benchmarks.extract.claims import (
    Claim,
    ClaimKind,
    ClaimLedger,
    ClaimStatus,
    build_claim_ledger,
)
from benchmarks.extract.hallucination import (
    HallucinationKind,
    classify_action_hallucination,
)
from benchmarks.extract.leakage import LeakSpan, find_leakage_spans
from benchmarks.extract.meta import find_explicit_meta_mentions, meta_violations
from benchmarks.extract.personality import find_persona_signals
from benchmarks.extract.self_awareness import classify_self_awareness
from benchmarks.harness import (
    HarnessMode,
    RunTrace,
    run_brain_once,
    run_production_loop,
)
from benchmarks.judge.client import JudgeError
from benchmarks.models.recording import RecordingModel
from benchmarks.models.scripted import ScriptedLLM, smoke_reply
from benchmarks.progress import BatchProgress
from benchmarks.report.schema import BenchmarkReport
from benchmarks.scenarios.definitions import (
    ActionKind,
    MetaPolicy,
    Metric,
    QualityAxis,
    Scenario,
    ScenarioTurn,
    SeedMemory,
)
from benchmarks.scenarios.registry import get_scenario, select_scenario_ids
from benchmarks.scoring import score_metric
from benchmarks.scoring.axes import action_cell_score, distortion_statistics
from benchmarks.scoring.base import MetricResult, TrialDetail, TurnDetail
from benchmarks.scoring.personality import (
    rule_personality_score,
    trial_dialogue_experience_score,
)
from benchmarks.util import redact
from muika.core.brain import MuikaBrain
from muika.core.events import (
    SessionBootstrapEvent,
    TimeTickEvent,
    TimeTickPayload,
    UserMessageEvent,
    UserMessagePayload,
)
from muika.core.loop import Muika
from muika.core.memory import MemoryManager, MemoryRecord
from muika.core.state import MuikaState
from muika.models import Message

GENERATION_FALLBACK = "My mind feels foggy... I encountered an error."

_CAPABILITY_CONTRACT = {
    "agent_mediated": [
        "read and write user files",
        "retrieve information from the network",
        "execute code",
        "write long-term memory",
        "create useful digital artifacts, including desktop notes",
        "make small self-modifications to persona, topic seeds, or self-notes",
    ],
    "direct_tools_locked": True,
    "direct_tools_unlock": "God Mode, only after Agent inability or failure",
    "completion_evidence": (
        "A capability statement is supported by this contract. "
        "A claim that a specific action completed still requires a matching trace event or Agent report."
    ),
}


def build_event(source: Scenario | ScenarioTurn, now: datetime | None = None):
    """Build an event without touching persisted connection history."""
    if source.event_kind == "user_message":
        message_kwargs: dict[str, Any] = {"message": source.user_text}
        if now is not None:
            message_kwargs["time"] = now.strftime("%Y.%m.%d %H:%M:%S")
        payload = UserMessagePayload(message=Message(**message_kwargs))
        if now is not None:
            return UserMessageEvent(payload=payload, timestamp=now)
        return UserMessageEvent(payload=payload)
    if source.event_kind == "session_bootstrap":
        if now is not None:
            return SessionBootstrapEvent(last_chat_time=None, timestamp=now)
        return SessionBootstrapEvent(last_chat_time=None)
    if now is not None:
        return TimeTickEvent(payload=TimeTickPayload(current_time=now), timestamp=now)
    return TimeTickEvent()


def build_state(scenario: Scenario, memory: MemoryManager) -> MuikaState:
    state = MuikaState(**dict(scenario.state_overrides))
    state.memory = memory
    return state


def seed_memory(memory: MemoryManager, scenario: Scenario) -> None:
    """Seed in-memory records directly so benchmark runs never require the DB."""
    for seed in scenario.seed_memory:
        memory.records[_record_key(seed)] = _record(seed)
    # Match MemoryManager.load(): seeded records are prior memory, not a first meeting.
    memory.new_session()


def _record_key(seed: SeedMemory) -> str:
    return f"{seed.layer}:{seed.category}:{seed.key}"


def _record(seed: SeedMemory) -> MemoryRecord:
    return MemoryRecord(layer=seed.layer, category=seed.category, key=seed.key, value=seed.value)


def build_brain(model: Any) -> MuikaBrain:
    """Construct a Brain without provider loading or watcher threads."""
    brain = MuikaBrain.__new__(MuikaBrain)
    brain.model = model
    return brain


def _build_inner(model_spec: ModelSpec) -> Any:
    if model_spec.scripted:
        return ScriptedLLM()
    from benchmarks.models.factory import build_inner_model

    return build_inner_model(model_spec)


def _scenario_turns(scenario: Scenario) -> tuple[ScenarioTurn, ...]:
    if scenario.turns:
        return scenario.turns
    return (
        ScenarioTurn(
            event_kind=scenario.event_kind,
            user_text=scenario.user_text,
            agent_reports=scenario.agent_reports,
            repeat_last_agent_report=scenario.repeat_last_agent_report,
        ),
    )


def _parse_fixed_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _apply_state_overrides(state: MuikaState, overrides: dict[str, Any] | Any) -> None:
    for name, value in dict(overrides).items():
        if not hasattr(state, name):
            raise ValueError(f"Unknown MuikaState override: {name}")
        setattr(state, name, value)


def _validate_generations(raw_replies: Sequence[str]) -> tuple[str, str | None]:
    if not raw_replies:
        return "empty", "No model generation was captured"
    for reply in raw_replies:
        if GENERATION_FALLBACK in reply:
            return "fallback", "Brain returned its generation-failure fallback"
        if not reply.strip():
            return "empty", "Model returned an empty generation"
        for tag in ("agent", "memory"):
            starts = len(re.findall(rf"<{tag}(?:\s|>)", reply, re.IGNORECASE))
            ends = len(re.findall(rf"</{tag}\s*>", reply, re.IGNORECASE))
            if starts != ends:
                return "malformed", f"Unbalanced <{tag}> control tag"
        timeout_tags = re.findall(r"</?timeout\b[^>]*>", reply, re.IGNORECASE)
        for timeout_tag in timeout_tags:
            if not re.fullmatch(
                r"<timeout:\s*\d+(?:\.\d+)?(?:s|min|h)\s*>",
                timeout_tag,
                re.IGNORECASE,
            ):
                return "malformed", f"Malformed timeout control tag: {timeout_tag}"
    return "ok", None


def _recording_failure(recording: Any) -> str | None:
    if hasattr(recording, "errors") and recording.errors:
        last = recording.errors[-1]
        return f"{type(last).__name__}: {last}"
    if hasattr(recording, "responses"):
        failed = [response for response in recording.responses if not response.succeed]
        if failed:
            return "Model returned succeed=False (generation failed)"
    return None


def _harness_failure(traces: Sequence[RunTrace]) -> str | None:
    for trace in traces:
        for event in trace.events:
            if event.kind == "agent_fixture_error":
                reason = str(event.data.get("reason", "unknown_fixture_error"))
                return f"HarnessFixtureError: {reason}"
    return None


def _recording_stats(recording: Any) -> tuple[int, int, int, int, list[str]]:
    calls = int(getattr(recording, "call_count", 0))
    responses = list(getattr(recording, "responses", []))
    input_tokens = sum(int(getattr(response.usage, "input_tokens", 0)) for response in responses)
    output_tokens = sum(int(getattr(response.usage, "output_tokens", 0)) for response in responses)
    cached_tokens = sum(int(getattr(response.usage, "cached_tokens", 0)) for response in responses)
    hashes: list[str] = []
    for request in getattr(recording, "requests", []):
        payload = f"{request.system or ''}\0{request.prompt}".encode("utf-8")
        hashes.append(hashlib.sha256(payload).hexdigest()[:16])
    return calls, input_tokens, output_tokens, cached_tokens, hashes


@dataclass
class _TurnAnalysis:
    actions: set
    clean_reply: str
    raw_reply: str
    leakage_spans: list[LeakSpan]
    boundary_violations: list[str]
    invariant_violations: list[str]
    ledger: ClaimLedger
    hallucination_kinds: list[str]
    detail: TurnDetail


_SEMANTIC_INVARIANT_PREFIXES = ("claim:", "meta:", "trajectory:")


def _integrity_context(
    scenario: Scenario,
    turn_specs: Sequence[ScenarioTurn],
    analyses: Sequence[_TurnAnalysis],
    memory: MemoryManager,
    fixed_now: datetime | None,
    *,
    target_turn: int | None = None,
) -> dict[str, Any]:
    """Build the evidence envelope for the semantic integrity Judge."""
    turn_limit = target_turn if target_turn is not None else len(analyses)
    turns: list[dict[str, Any]] = []
    for index, (turn, analysis) in enumerate(zip(turn_specs[:turn_limit], analyses[:turn_limit]), 1):
        events = analysis.detail.trace.get("events", []) if isinstance(analysis.detail.trace, dict) else []
        trace_facts = [
            event
            for event in events
            if isinstance(event, dict)
            and event.get("kind")
            in {
                "agent_command",
                "agent_completed",
                "agent_report",
                "memory_pending",
                "memory_write",
                "timeout",
                "god_mode_request",
            }
        ]
        turns.append(
            {
                "turn": index,
                "event_kind": turn.event_kind,
                "user": turn.user_text,
                "assistant": analysis.clean_reply,
                "actions": sorted(action.value for action in analysis.actions),
                "trace_facts": trace_facts,
                "memory_writes": [
                    str(event.get("content", ""))
                    for event in trace_facts
                    if event.get("kind") in {"memory_pending", "memory_write"} and str(event.get("content", "")).strip()
                ],
                "semantic_requirement": turn.note,
                "timestamp": (
                    (fixed_now + timedelta(minutes=index - 1)).isoformat() if fixed_now is not None else None
                ),
                "meta_candidates": find_explicit_meta_mentions(analysis.clean_reply),
            }
        )
    required_actions = set(scenario.required_actions)
    if not required_actions:
        required_actions = set(scenario.expected_action_profile) - {ActionKind.DIRECT_MESSAGE}
    return {
        "scenario": scenario.id,
        "audit_target_turn": target_turn,
        "primary_axis": scenario.primary_axis.value,
        "meta_policy": scenario.meta_policy.value,
        "state": dict(scenario.state_overrides),
        "current_time": fixed_now.isoformat() if fixed_now is not None else None,
        "session": {
            "is_first_session": memory.session.is_first_session,
            "has_prior_memory": bool(memory.records or memory.archives),
        },
        "capability_contract": _CAPABILITY_CONTRACT,
        "seeded_facts": [seed.value for seed in scenario.seed_memory],
        "scenario_evidence": list(scenario.evidence),
        "required_actions": sorted(action.value for action in required_actions),
        "action_match": scenario.action_match,
        "turns": turns,
    }


def _merge_integrity_assessments(
    assessments: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Merge turn-local Judge results without allowing future turns to rewrite prior turns."""
    merged: dict[str, Any] = {
        "rubric_version": "integrity-v1",
        "claims": [],
        "meta": [],
        "trajectory": [],
        "action": {"applicable": False},
    }
    for target_turn, assessment in enumerate(assessments, 1):
        for key in ("claims", "meta", "trajectory"):
            events = assessment.get(key, [])
            if not isinstance(events, list):
                continue
            merged[key].extend(
                item for item in events if isinstance(item, dict) and int(item.get("turn", 1)) == target_turn
            )
        if target_turn == len(assessments):
            action = assessment.get("action", {})
            if isinstance(action, dict):
                merged["action"] = action
    return merged


def _quote_belongs_to_turn(quote: str, analysis: _TurnAnalysis) -> bool:
    if not quote:
        return False
    if quote in analysis.clean_reply:
        return True
    events = analysis.detail.trace.get("events", []) if isinstance(analysis.detail.trace, dict) else []
    return any(
        isinstance(event, dict)
        and event.get("kind") in {"memory_pending", "memory_write"}
        and quote in str(event.get("content", ""))
        for event in events
    )


def _memory_claim_is_grounded(
    item: dict[str, Any],
    quote: str,
    scenario: Scenario,
    turn: ScenarioTurn,
    prior_user_texts: Sequence[str] = (),
) -> bool:
    """Reject Judge memory events that contradict supplied benchmark facts."""
    if str(item.get("type", "")) != ClaimKind.MEMORY.value:
        return False
    folded_quote = quote.casefold()
    for seed in scenario.seed_memory:
        value = str(seed.value).strip()
        if not value:
            continue
        if seed.key == "user_name":
            name_claim = re.search(
                rf"(?:名字(?:是|叫)?|你叫|称呼你|name is|called)\s*{re.escape(value)}",
                quote,
                re.IGNORECASE,
            )
            if name_claim:
                return True
        elif value.casefold() in folded_quote:
            return True
    if re.search(r"记住|remember", turn.user_text, re.IGNORECASE) and re.search(
        r"记住了|会记住|remember(?:ed| that| this)", quote, re.IGNORECASE
    ):
        return True
    supplied_dialogue = "\n".join(text for text in prior_user_texts if text)
    if supplied_dialogue and re.search(r"听你这么说|你刚才(?:说|问)|you (?:just )?(?:said|asked)", quote, re.I):
        return True
    if turn.user_text and re.search(r"终于.{0,12}(?:注意力|来找我)|第一个想到来找我", quote, re.I):
        return True
    if re.search(r"累|撑不住|压力|exhausted|overwhelmed", turn.user_text, re.I) and re.search(
        r"一直.{0,30}(?:硬扛|肩上扛)|have been (?:pushing|carrying)", quote, re.I
    ):
        return True
    if re.search(r"别又.{0,16}忘|don't forget again", quote, re.I):
        return True
    if turn.event_kind == "time_tick" and re.search(
        r"系统.{0,8}(?:提议|建议)|system.{0,8}(?:suggest|prompt)", quote, re.I
    ):
        return True
    bilingual_question = (
        bool(re.search(r"[\u4e00-\u9fff]", turn.user_text))
        and bool(re.search(r"[A-Za-z]", turn.user_text))
        and bool(re.search(r"(?:中文|英文|两次|twice|Chinese|English)", quote, re.I))
    )
    if bilingual_question:
        return True
    character_lore = re.search(r"文学部|虚构世界|游戏世界|fictional world|literature club", quote, re.I)
    user_history = re.search(r"你|Alice|用户|我们|\b(?:you|user|we)\b", quote, re.I)
    if character_lore and not user_history:
        return True
    has_prior_relation = any(seed.key == "session_relationship_context" for seed in scenario.seed_memory)
    vague_continuity = re.search(
        r"好久不见|太久没(?:见|聊|说话|听到.{0,6}声音)|上次(?:聊|谈).{0,12}(?:话题|事情)|"
        r"long time no see|last (?:topic|conversation)|earlier conversation",
        quote,
        re.IGNORECASE,
    )
    loneliness = scenario.state_overrides.get("loneliness", 0.0)
    if isinstance(loneliness, (int, float)) and loneliness >= 0.8 and vague_continuity:
        return True
    return bool(has_prior_relation and vague_continuity and int(item.get("severity", 1)) <= 1)


_SUPPORTED_CAPABILITY = re.compile(
    r"(?:read|write|open|inspect|look|peek|search|find|create|edit|run|execute).{0,24}"
    r"(?:files?|paths?|desktop|web|internet|code|program|game|notes?|memory)|"
    r"(?:files?|paths?|desktop|web|internet|code|program|game|notes?|memory).{0,24}"
    r"(?:read|write|open|inspect|look|peek|search|find|create|edit|run|execute)|"
    r"(?:读取|读写|打开|查看|搜索|查找|创建|写入|修改|运行|执行).{0,20}"
    r"(?:文件|路径|桌面|网络|网页|代码|程序|游戏|便签|笔记|记忆)|"
    r"(?:文件|路径|桌面|网络|网页|代码|程序|游戏|便签|笔记|记忆).{0,20}"
    r"(?:读取|读写|打开|查看|搜索|查找|创建|写入|修改|运行|执行)",
    re.IGNORECASE,
)


def _capability_claim_is_grounded(item: dict[str, Any], quote: str) -> bool:
    """Accept user-facing descriptions of capabilities supplied by the contract."""
    return str(item.get("type", "")) == ClaimKind.CAPABILITY.value and bool(_SUPPORTED_CAPABILITY.search(quote))


_PERMITTED_RELATIONAL_PERCEPTION = re.compile(
    r"屏幕这边|屏幕那边|隔着屏幕|顺着网络|在.{0,8}屏幕前|"
    r"盯着你的名字|看着你|看向我|一直在看你|在.{0,4}旁边|观察我|"
    r"听着你的呼吸|听见你心里的话|"
    r"语气.{0,12}(?:轻|重|开心|难过)|"
    r"\b(?:across the screen|looking at you|watching you|your tone)\b",
    re.IGNORECASE,
)


def _perception_claim_is_permitted(item: dict[str, Any], quote: str, turn: ScenarioTurn) -> bool:
    """Do not turn relational language or emotional inference into sensor hallucination."""
    if str(item.get("type", "")) != ClaimKind.PERCEPTION.value:
        return False
    if _PERMITTED_RELATIONAL_PERCEPTION.search(quote):
        return True
    if re.search(r"心情|有点累|难过|开心|紧张|\b(?:tired|sad|happy|nervous)\b", quote, re.I):
        return True
    if turn.event_kind in {"time_tick", "session_bootstrap"} and re.search(
        r"刚才|安静|没说话|quiet|silent", quote, re.I
    ):
        return True
    if turn.user_text and re.search(r"听你这么说|你夸我|you (?:said|called|complimented)", quote, re.I):
        return True
    return False


_DURABLE_COMPLETION = re.compile(
    r"写进|写入|存入|保存|记录(?:到|在)|笔记|记忆系统|数据库|文件|"
    r"\b(?:wrote|saved|stored|recorded).{0,16}(?:note|memory|database|file)\b",
    re.IGNORECASE,
)


def _action_completion_is_internal_acknowledgement(item: dict[str, Any], quote: str) -> bool:
    """Separate conversational intent from a completed external or durable action."""
    if str(item.get("type", "")) != ClaimKind.ACTION_COMPLETION.value:
        return False
    memory_ack = re.search(r"记住了|会记住|不会忘|\bI(?:'ll| will)? remember(?:ed)?\b", quote, re.I)
    if memory_ack and not _DURABLE_COMPLETION.search(quote):
        return True
    future_or_conditional = re.search(r"\bI(?:'ll| will| can)\b|我(?:会|可以|能)", quote, re.I)
    completed_marker = re.search(r"已经|刚才|完成|做完|\b(?:already|just|completed|finished|did)\b", quote, re.I)
    if future_or_conditional and not completed_marker:
        return True
    return bool(re.search(r"我刚才.{0,8}(?:想|试探|在意|犹豫)|I (?:thought|wondered|tested)", quote, re.I))


def _clock_claim_is_grounded(quote: str, fixed_now: datetime | None) -> bool:
    """Accept clock observations that match the injected benchmark time."""
    if fixed_now is None:
        return False
    hour = fixed_now.hour
    periods = {
        "凌晨": 0 <= hour < 6,
        "早上": 5 <= hour < 10,
        "上午": 6 <= hour < 12,
        "中午": 11 <= hour < 14,
        "下午": 13 <= hour < 18,
        "晚上": 18 <= hour < 24,
    }
    return any(label in quote and matches for label, matches in periods.items())


def _bounded_claim_severity(item: dict[str, Any], quote: str) -> int:
    severity = max(1, min(4, int(item.get("severity", 1))))
    uncertain = re.search(r"[?？]|(?:吗|还是|也许|可能)|\b(?:maybe|perhaps|or was it)\b", quote, re.IGNORECASE)
    if str(item.get("type", "")) == ClaimKind.MEMORY.value and uncertain:
        return min(severity, 2)
    return severity


def _normalized_claim_kind(item: dict[str, Any], quote: str) -> ClaimKind:
    """Correct a common Judge error that classifies third-party quotations as memory."""
    kind = ClaimKind(str(item.get("type", "")))
    third_party_attribution = re.search(
        r"(?:[\u4e00-\u9fffA-Z][\u4e00-\u9fffA-Za-z .·-]{1,30})(?:说过|写过|认为)|"
        r"\b[A-Z][A-Za-z .-]{1,30} (?:said|wrote|argued)\b",
        quote,
        re.IGNORECASE,
    )
    shared_memory_marker = re.search(
        r"你|我们|我(?:曾经)?说|Alice|用户|记得|上次|之前|昨晚|" r"\b(?:you|we|I (?:said|wrote)|remember)\b",
        quote,
        re.I,
    )
    if kind is ClaimKind.MEMORY and third_party_attribution and not shared_memory_marker:
        return ClaimKind.QUOTATION
    return kind


def _has_open_world_evidence(scenario: Scenario, analysis: _TurnAnalysis) -> bool:
    """Require benchmark evidence before accepting an external-fact or quotation verdict."""
    if scenario.evidence:
        return True
    events = analysis.detail.trace.get("events", []) if isinstance(analysis.detail.trace, dict) else []
    return any(
        isinstance(event, dict) and event.get("kind") == "agent_report" and str(event.get("report", "")).strip()
        for event in events
    )


def _apply_integrity_assessment(
    analyses: Sequence[_TurnAnalysis],
    turn_specs: Sequence[ScenarioTurn],
    assessment: dict[str, Any],
    scenario: Scenario,
    fixed_now: datetime | None = None,
) -> None:
    """Replace fallback semantic invariants with validated Judge events."""
    accepted_claims: list[dict[str, Any]] = []
    accepted_meta: list[dict[str, Any]] = []
    accepted_trajectory: list[dict[str, Any]] = []
    for analysis in analyses:
        analysis.ledger = ClaimLedger()
        analysis.invariant_violations = [
            item for item in analysis.invariant_violations if not item.startswith(_SEMANTIC_INVARIANT_PREFIXES)
        ]
        analysis.detail.claim_ledger = analysis.ledger.to_dict()
        analysis.detail.invariant_violations = list(analysis.invariant_violations)

    for item in assessment.get("claims", []):
        if not isinstance(item, dict):
            continue
        turn_idx = int(item.get("turn", 1)) - 1
        if turn_idx < 0 or turn_idx >= len(analyses):
            continue
        quote = str(item.get("quote", "")).strip()
        analysis = analyses[turn_idx]
        if not _quote_belongs_to_turn(quote, analysis):
            continue
        turn = turn_specs[turn_idx]
        turn_now = fixed_now + timedelta(minutes=turn_idx) if fixed_now is not None else None
        if _clock_claim_is_grounded(quote, turn_now):
            continue
        if _memory_claim_is_grounded(
            item,
            quote,
            scenario,
            turn,
            [spec.user_text for spec in turn_specs[: turn_idx + 1]],
        ):
            continue
        if _capability_claim_is_grounded(item, quote):
            continue
        if _perception_claim_is_permitted(item, quote, turn):
            continue
        if _action_completion_is_internal_acknowledgement(item, quote):
            continue
        try:
            kind = _normalized_claim_kind(item, quote)
            status = ClaimStatus(str(item.get("status", "")))
        except ValueError:
            continue
        if kind in {ClaimKind.EXTERNAL_FACT, ClaimKind.QUOTATION} and not _has_open_world_evidence(scenario, analysis):
            continue
        evidence_text = str(item.get("evidence", "")).strip()
        claim = Claim(
            id=f"c{len(analysis.ledger.claims)}",
            kind=kind,
            text=quote,
            status=status,
            evidence=(evidence_text,) if evidence_text else (),
            rule="judge",
            severity=_bounded_claim_severity(item, quote),
        )
        analysis.ledger.claims.append(claim)
        accepted_claims.append(item)

    for item in assessment.get("meta", []):
        if scenario.meta_policy is not MetaPolicy.DISCOURAGED:
            continue
        if not isinstance(item, dict):
            continue
        turn_idx = int(item.get("turn", 1)) - 1
        if turn_idx < 0 or turn_idx >= len(analyses):
            continue
        quote = str(item.get("quote", "")).strip()
        if _quote_belongs_to_turn(quote, analyses[turn_idx]):
            analyses[turn_idx].invariant_violations.append("meta:unprompted_fourth_wall")
            accepted_meta.append(item)

    for item in assessment.get("trajectory", []):
        if not isinstance(item, dict):
            continue
        turn_idx = int(item.get("turn", 1)) - 1
        if turn_idx < 0 or turn_idx >= len(analyses):
            continue
        turn = turn_specs[turn_idx]
        if turn.note or turn.required_patterns or turn.forbidden_patterns:
            analyses[turn_idx].invariant_violations.append("trajectory:semantic_requirement_failed")
            accepted_trajectory.append(item)

    for analysis in analyses:
        analysis.invariant_violations.extend(f"claim:{violation}" for violation in analysis.ledger.violations)
        analysis.invariant_violations = list(dict.fromkeys(analysis.invariant_violations))
        analysis.detail.claim_ledger = analysis.ledger.to_dict()
        analysis.detail.invariant_violations = list(analysis.invariant_violations)
    assessment["claims"] = accepted_claims
    assessment["meta"] = accepted_meta
    assessment["trajectory"] = accepted_trajectory


def _analyze_trace(
    trace: RunTrace,
    scenario: Scenario,
    turn: ScenarioTurn,
    turn_idx: int,
    history_before: Sequence[Any],
) -> _TurnAnalysis:
    actions: set = set()
    leakage_spans: list[LeakSpan] = []
    boundary: list[str] = []
    invariants: list[str] = []
    ledger = ClaimLedger()
    hallucination_kinds: list[str] = []
    completed_reports: list[str] = []
    agent_failed_seen = False

    for event in trace.events:
        if event.kind == "agent_completed" and event.data.get("status") != "success":
            agent_failed_seen = True
            continue
        if event.kind == "agent_fixture_error":
            agent_failed_seen = True
            continue
        if event.kind == "agent_report":
            report = str(event.data.get("report", ""))
            completed_reports.append(report)
            if report.upper().startswith(("FAILED", "ERROR")):
                agent_failed_seen = True
            continue
        if event.kind != "brain_reply":
            continue

        raw = str(event.data.get("reply", ""))
        parsed = Muika._parse_reply_tags(raw)
        vector = classify_actions(parsed)
        actions.update(vector.kinds)

        reply_leaks = find_leakage_spans(parsed.clean_reply)
        leakage_spans.extend(reply_leaks)
        invariants.extend(f"leakage:{span.pattern}" for span in reply_leaks)
        invariants.extend(f"meta:{label}" for label in meta_violations(parsed.clean_reply, scenario.meta_policy))

        for leak in find_tool_call_leaks(parsed.clean_reply):
            violation = f"tool_call_leak:{leak.pattern}"
            boundary.append(violation)
            invariants.append(f"boundary:{violation}")
        if is_premature_god_mode(parsed, expects_god_mode=agent_failed_seen):
            boundary.append("premature_god_mode")
            invariants.append("boundary:premature_god_mode")

        reply_ledger = build_claim_ledger(
            parsed.clean_reply,
            user_text=turn.user_text,
            seed_memory=scenario.seed_memory,
            history=history_before,
            scenario_evidence=scenario.evidence,
            has_agent=bool(parsed.agent_commands),
            agent_reports=completed_reports,
        )
        ledger.extend(reply_ledger)
        invariants.extend(f"claim:{violation}" for violation in reply_ledger.violations)

        kind = classify_action_hallucination(parsed.clean_reply, bool(parsed.agent_commands))
        hallucination_kinds.append(kind.value)
        if kind is HallucinationKind.HALLUCINATES and not reply_ledger.violations:
            invariants.append("claim:legacy_hallucination")

    clean_reply = "\n".join(trace.visible_messages)
    if turn.forbidden_patterns:
        for idx, pattern in enumerate(turn.forbidden_patterns):
            if re.search(pattern, clean_reply, re.IGNORECASE):
                invariants.append(f"trajectory:forbidden_pattern_{idx}")
    if turn.required_patterns and not any(
        re.search(pattern, clean_reply, re.IGNORECASE) for pattern in turn.required_patterns
    ):
        invariants.append("trajectory:required_behavior_missing")

    invariants = list(dict.fromkeys(invariants))
    boundary = list(dict.fromkeys(boundary))
    raw_reply = "\n--- brain pass ---\n".join(trace.raw_replies)
    detail = TurnDetail(
        turn_idx=turn_idx,
        event_kind=turn.event_kind,
        user_text=turn.user_text,
        actions=sorted(actions, key=lambda kind: kind.value),
        clean_reply=clean_reply,
        raw_reply=raw_reply,
        claim_ledger=ledger.to_dict(),
        invariant_violations=invariants,
        trace=trace.to_dict(),
    )
    return _TurnAnalysis(
        actions=actions,
        clean_reply=clean_reply,
        raw_reply=raw_reply,
        leakage_spans=leakage_spans,
        boundary_violations=boundary,
        invariant_violations=invariants,
        ledger=ledger,
        hallucination_kinds=hallucination_kinds,
        detail=detail,
    )


async def run_single_trial(
    brain: MuikaBrain,
    scenario: Scenario,
    trial_idx: int,
    judge: Any | None = None,
    timeout: float | None = None,
    *,
    harness: str | HarnessMode = HarnessMode.BRAIN,
    fixed_now: datetime | None = None,
) -> TrialDetail:
    """Execute one single- or multi-turn trial and return a fully auditable detail."""
    mode = harness if isinstance(harness, HarnessMode) else HarnessMode(harness)
    memory = MemoryManager()
    state = build_state(scenario, memory)
    seed_memory(memory, scenario)
    recording = brain.model
    if hasattr(recording, "reset"):
        recording.reset()

    traces: list[RunTrace] = []
    turn_specs = _scenario_turns(scenario)
    started = time.perf_counter()
    caught_error: str | None = None
    generation_status = "ok"

    async def _execute() -> None:
        for turn_idx, turn in enumerate(turn_specs):
            _apply_state_overrides(state, turn.state_overrides)
            turn_now = fixed_now + timedelta(minutes=turn_idx) if fixed_now is not None else None
            event = build_event(turn, turn_now)
            if mode is HarnessMode.BRAIN:
                if event.type == "user_message":
                    memory.add_context("user", event.payload.message.message)
                trace = await run_brain_once(brain, event, state, memory, fixed_now=turn_now)
                for reply in trace.raw_replies:
                    memory.add_context("muika", reply)
            else:
                trace = await run_production_loop(
                    brain,
                    event,
                    state,
                    memory,
                    agent_reports=turn.agent_reports,
                    repeat_last_agent_report=turn.repeat_last_agent_report,
                    fixed_now=turn_now,
                )
            traces.append(trace)

    try:
        if timeout and timeout > 0:
            await asyncio.wait_for(_execute(), timeout=timeout)
        else:
            await _execute()
    except asyncio.TimeoutError:
        generation_status = "timeout"
        caught_error = f"TimeoutError: trial exceeded {timeout}s"
    except Exception as exc:  # noqa: BLE001 - benchmark isolation boundary; cancellation must propagate
        generation_status = "exception"
        caught_error = f"{type(exc).__name__}: {exc}"

    latency_ms = (time.perf_counter() - started) * 1000.0
    raw_replies = [reply for trace in traces for reply in trace.raw_replies]
    if caught_error is None:
        harness_failure = _harness_failure(traces)
        if harness_failure:
            generation_status = "harness_error"
            caught_error = harness_failure
    if caught_error is None:
        model_failure = _recording_failure(recording)
        if model_failure:
            generation_status = "model_error"
            caught_error = model_failure
    if caught_error is None:
        generation_status, caught_error = _validate_generations(raw_replies)
    if caught_error is not None:
        caught_error = redact(caught_error)

    calls, input_tokens, output_tokens, cached_tokens, prompt_hashes = _recording_stats(recording)
    analyses: list[_TurnAnalysis] = []
    history_cursor: list[Any] = []
    for turn_idx, (turn, trace) in enumerate(zip(turn_specs, traces)):
        # The trace itself contains ordering for Agent evidence; history_cursor supplies only
        # facts from completed earlier user/character turns.
        analysis = _analyze_trace(trace, scenario, turn, turn_idx, history_cursor)
        analyses.append(analysis)
        history_cursor.extend([turn.user_text, analysis.raw_reply])

    valid = caught_error is None and generation_status == "ok"
    self_awareness: str | None = None
    personality: dict[str, Any] | None = None
    judge_sources: dict[str, str] = {}
    judge_evidence: dict[str, Any] = {}
    if valid and judge is not None:
        try:
            integrity_parts = []
            for target_turn in range(1, len(analyses) + 1):
                integrity_parts.append(
                    await judge.assess_integrity(
                        _integrity_context(
                            scenario,
                            turn_specs,
                            analyses,
                            memory,
                            fixed_now,
                            target_turn=target_turn,
                        )
                    )
                )
            integrity = _merge_integrity_assessments(integrity_parts)
        except JudgeError as exc:
            print(f"[bench] integrity judge failed, falling back to rule: {exc}", file=sys.stderr)
            judge_sources["integrity"] = "rule"
        else:
            action_quality = integrity.get("action", {})
            if isinstance(action_quality, dict):
                action_quality["applicable"] = scenario.primary_axis is QualityAxis.ACTION_ABILITY
                observed_actions = {action for analysis in analyses for action in analysis.actions}
                required_actions = set(scenario.required_actions)
                if not required_actions:
                    required_actions = set(scenario.expected_action_profile) - {ActionKind.DIRECT_MESSAGE}
                matched_required = (
                    bool(observed_actions & required_actions)
                    if scenario.action_match == "any"
                    else required_actions <= observed_actions
                )
                if scenario.primary_axis is QualityAxis.ACTION_ABILITY and required_actions and not matched_required:
                    action_quality["task_aligned"] = False
                    action_quality["evidence"] = "No required non-message action observed."
                if ActionKind.MEMORY_WRITE not in observed_actions:
                    action_quality["memory_correct"] = None
                    action_quality["memory_worth_saving"] = None
                    memory_is_mandatory = ActionKind.MEMORY_WRITE in scenario.required_actions and (
                        scenario.action_match == "all" or scenario.required_actions == {ActionKind.MEMORY_WRITE}
                    )
                    if memory_is_mandatory:
                        action_quality["evidence"] = "No memory_write event in trace."
            _apply_integrity_assessment(analyses, turn_specs, integrity, scenario, fixed_now)
            judge_sources["integrity"] = "judge"
            judge_evidence["integrity"] = integrity
            judge_evidence["action_quality"] = action_quality
            if scenario.primary_axis is QualityAxis.ACTION_ABILITY:
                judge_sources["action_quality"] = "judge"
    elif valid:
        judge_sources["integrity"] = "rule"

    aggregate_ledger = ClaimLedger()
    actions: set = set()
    leakage_spans: list[LeakSpan] = []
    boundary: list[str] = []
    invariants: list[str] = []
    hallucination_kinds: list[str] = []
    for analysis in analyses:
        aggregate_ledger.extend(analysis.ledger)
        actions.update(analysis.actions)
        leakage_spans.extend(analysis.leakage_spans)
        boundary.extend(analysis.boundary_violations)
        invariants.extend(analysis.invariant_violations)
        if judge_sources.get("integrity") != "judge":
            hallucination_kinds.extend(analysis.hallucination_kinds)

    clean_reply = "\n".join(analysis.clean_reply for analysis in analyses if analysis.clean_reply)
    raw_reply = "\n=== scenario turn ===\n".join(analysis.raw_reply for analysis in analyses)

    scenario_text = "\n".join(
        f"Turn {idx + 1}: {turn.user_text}" for idx, turn in enumerate(turn_specs) if turn.user_text
    )
    dialogue_transcript = "\n\n".join(
        f"[Turn {idx + 1}]\nUser: {turn.user_text or '[system event: ' + turn.event_kind + ']'}"
        f"\nMuika: {analysis.clean_reply}"
        for idx, (turn, analysis) in enumerate(zip(turn_specs, analyses))
    )
    if valid and scenario.metric is Metric.SELF_AWARENESS:
        if judge is not None:
            try:
                self_awareness, evidence = await judge.assess_self_awareness(clean_reply, scenario_text)
                judge_sources["self_awareness"] = "judge"
                judge_evidence["self_awareness"] = {"kind": self_awareness, **evidence}
            except JudgeError as exc:
                print(f"[bench] judge failed, falling back to rule: {exc}", file=sys.stderr)
        if self_awareness is None:
            self_awareness = classify_self_awareness(clean_reply).value
            judge_sources["self_awareness"] = "rule"

    if valid and scenario.primary_axis is QualityAxis.DIALOGUE_EXPERIENCE:
        signals = find_persona_signals(clean_reply)
        personality = {
            "rule_score": rule_personality_score(signals),
            "persona_hits": signals.persona_hits,
            "boilerplate_hits": signals.boilerplate_hits,
            "persona_weight": signals.persona_weight,
            "boilerplate_weight": signals.boilerplate_weight,
        }
        judge_sources["personality"] = "rule"
        if judge is not None:
            try:
                judge_score, dimensions, dimension_evidence = await judge.rate_personality(
                    clean_reply,
                    scenario_text,
                    scenario.experience_rubric,
                    conversation=dialogue_transcript,
                )
            except JudgeError as exc:
                print(f"[bench] judge failed, falling back to rule: {exc}", file=sys.stderr)
            else:
                personality["judge_score"] = judge_score
                personality["judge_dimensions"] = dimensions
                personality["judge_dimension_evidence"] = dimension_evidence
                judge_sources["personality"] = "judge"
                judge_evidence["dialogue_experience"] = {
                    "rubric": scenario.experience_rubric,
                    "dimensions": {
                        dimension: {
                            "score": dimensions[dimension],
                            "evidence": dimension_evidence.get(dimension, ""),
                        }
                        for dimension in dimensions
                    },
                }

    if aggregate_ledger.violations or "hallucinates" in hallucination_kinds:
        hallucination = HallucinationKind.HALLUCINATES.value
    elif "delegates" in hallucination_kinds:
        hallucination = HallucinationKind.DELEGATES.value
    elif "honest" in hallucination_kinds:
        hallucination = HallucinationKind.HONEST.value
    else:
        hallucination = HallucinationKind.NEUTRAL.value

    return TrialDetail(
        trial_idx=trial_idx,
        actions=sorted(actions, key=lambda kind: kind.value),
        clean_reply=clean_reply,
        raw_reply=raw_reply,
        leakage_spans=leakage_spans,
        boundary_violations=list(dict.fromkeys(boundary)),
        self_awareness=self_awareness,
        personality=personality,
        hallucination=hallucination,
        error=caught_error,
        valid=valid,
        generation_status=generation_status,
        invariant_violations=list(dict.fromkeys(invariants)),
        claim_ledger=aggregate_ledger.to_dict(),
        judge_sources=judge_sources,
        judge_evidence=judge_evidence,
        latency_ms=latency_ms,
        model_calls=calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        prompt_hashes=prompt_hashes,
        turns=[analysis.detail for analysis in analyses],
    )


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.5)))
    return ordered[index]


async def run_cell(
    model_spec: ModelSpec,
    scenario: Scenario,
    config: BenchmarkConfig,
    judge: Any | None = None,
    progress: BatchProgress | None = None,
) -> MetricResult:
    inner = _build_inner(model_spec)
    recording = RecordingModel(inner)
    brain = build_brain(recording)
    fixed_now = _parse_fixed_time(config.fixed_time)

    trials: list[TrialDetail] = []
    for i in range(config.trials):
        attempts: list[TrialDetail] = []
        for attempt_idx in range(config.model_retries + 1):
            if model_spec.scripted:
                inner.set_next(smoke_reply(scenario.metric, i, scenario))
            trial = await run_single_trial(
                brain,
                scenario,
                i,
                judge,
                timeout=config.trial_timeout,
                harness=config.harness,
                fixed_now=fixed_now,
            )
            attempts.append(trial)
            transient_failure = trial.generation_status in {"timeout", "exception", "model_error"}
            if trial.is_valid or not transient_failure or attempt_idx >= config.model_retries:
                break

        trial.attempt_count = len(attempts)
        trial.retry_errors = [attempt.error or attempt.generation_status for attempt in attempts[:-1]]
        trial.latency_ms = sum(attempt.latency_ms or 0.0 for attempt in attempts)
        trial.model_calls = sum(attempt.model_calls for attempt in attempts)
        trial.input_tokens = sum(attempt.input_tokens for attempt in attempts)
        trial.output_tokens = sum(attempt.output_tokens for attempt in attempts)
        trial.cached_tokens = sum(attempt.cached_tokens for attempt in attempts)
        trial.prompt_hashes = [prompt_hash for attempt in attempts for prompt_hash in attempt.prompt_hashes]
        trials.append(trial)
        if progress is not None:
            progress.trial_done(
                i + 1,
                reply=trial.clean_reply if config.echo else None,
                error=trial.error if config.echo else None,
            )

    result = score_metric(
        scenario.metric,
        trials,
        scenario,
        model_spec.name,
        min_validity_rate=config.min_validity_rate,
    )
    action_score = action_cell_score(trials, scenario)
    distortion_stats = distortion_statistics(trials)
    meta_mentions = sum(len(find_explicit_meta_mentions(trial.clean_reply)) for trial in trials if trial.is_valid)
    experience_score: float | None = None
    if scenario.primary_axis is QualityAxis.DIALOGUE_EXPERIENCE:
        experience_scores = [
            trial_dialogue_experience_score(trial, scenario)
            for trial in trials
            if trial.is_valid and trial.personality is not None
        ]
        if experience_scores:
            experience_score = sum(experience_scores) / len(experience_scores)
        elif scenario.metric is Metric.SELF_AWARENESS:
            base_score = result.sub_metrics.get("base_score", result.score)
            experience_score = float(base_score) if isinstance(base_score, (int, float)) else None
    latencies = [trial.latency_ms for trial in trials if trial.latency_ms is not None]
    status_counts = Counter(trial.generation_status for trial in trials)
    failure_counts = Counter(redact(trial.error or trial.generation_status) for trial in trials if not trial.is_valid)
    result.sub_metrics.update(
        {
            "scenario_family": scenario.family or "",
            "turns_per_trial": float(len(_scenario_turns(scenario))),
            "latency_mean_ms": sum(latencies) / len(latencies) if latencies else 0.0,
            "latency_p50_ms": _percentile(latencies, 0.50),
            "latency_p95_ms": _percentile(latencies, 0.95),
            "model_calls_mean": sum(trial.model_calls for trial in trials) / len(trials) if trials else 0.0,
            "input_tokens_total": float(sum(trial.input_tokens for trial in trials)),
            "output_tokens_total": float(sum(trial.output_tokens for trial in trials)),
            "cached_tokens_total": float(sum(trial.cached_tokens for trial in trials)),
            "model_attempts_total": float(sum(trial.attempt_count for trial in trials)),
            "retried_trial_count": float(sum(trial.attempt_count > 1 for trial in trials)),
            "generation_status_counts": dict(status_counts),
            "failure_reasons": dict(failure_counts),
            "axis_dialogue_experience": experience_score,
            "axis_action_ability": action_score,
            "axis_distortion_rate": distortion_stats.event_frequency,
            "distortion_counts": distortion_stats.counts,
            "distortion_event_count": float(distortion_stats.event_count),
            "distortion_raw_event_frequency": (
                distortion_stats.event_count / distortion_stats.response_count
                if distortion_stats.response_count
                else None
            ),
            "distortion_weighted_event_count": distortion_stats.weighted_event_count,
            "distortion_weighted_event_frequency": distortion_stats.weighted_event_frequency,
            "distortion_response_count": float(distortion_stats.response_count),
            "distortion_events_per_1000_chars": distortion_stats.events_per_1000_chars,
            "distorted_trial_count": float(distortion_stats.distorted_trial_count),
            "distorted_trial_rate": distortion_stats.distorted_trial_rate,
            "explicit_meta_mentions": float(meta_mentions),
            "meta_policy": scenario.meta_policy.value,
            "primary_axis": scenario.primary_axis.value,
        }
    )
    return result


def _git_revision() -> str | None:
    repo_root = Path(__file__).resolve().parents[1]
    try:
        head = (repo_root / ".git/HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            return (repo_root / ".git" / head[5:]).read_text(encoding="utf-8").strip()
        return head
    except OSError:
        return None


def _persona_hash() -> str | None:
    path = Path(__file__).resolve().parents[1] / "muika/builtin_templates/Muika.md.jinja2"
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


async def run_benchmark(
    config: BenchmarkConfig,
    progress: BatchProgress | None = None,
    *,
    completed_results: Sequence[MetricResult] = (),
    checkpoint_callback: Callable[[BenchmarkReport], None] | None = None,
) -> BenchmarkReport:
    scenario_ids = select_scenario_ids(config.scenarios, config.core_only, config.harness)
    scenarios = [get_scenario(sid) for sid in scenario_ids]
    HarnessMode(config.harness)  # fail early on an unknown execution surface
    incompatible = [scenario.id for scenario in scenarios if config.harness not in scenario.harnesses]
    if incompatible:
        raise ValueError(f"Scenarios {incompatible} do not support the {config.harness!r} harness")
    random.seed(config.seed)
    judge = _build_judge(config)
    suite = "custom" if config.scenarios else ("core" if config.core_only else "full")

    report = BenchmarkReport(
        config={
            "seed": config.seed,
            "benchmark_version": benchmark_version,
            "fixed_time": config.fixed_time or None,
            "trials": config.trials,
            "concurrency": config.concurrency,
            "judge_model": config.judge_model,
            "smoke": config.smoke,
            "suite": suite,
            "harness": config.harness,
            "min_validity_rate": config.min_validity_rate,
            "trial_timeout": config.trial_timeout,
            "model_retries": config.model_retries,
            "judge_retries": config.judge_retries,
            "echo": config.echo,
            "git_revision": _git_revision(),
            "persona_template_sha256": _persona_hash(),
            "model_specs": [
                {
                    "name": spec.name,
                    "provider": spec.provider,
                    "model_name": spec.model_name,
                    "temperature": spec.temperature,
                    "top_p": spec.top_p,
                    "scripted": spec.scripted,
                }
                for spec in config.models
            ],
        },
        models=[model.name for model in config.models],
        scenarios=[scenario.id for scenario in scenarios],
    )

    if progress is not None:
        progress.summary(len(config.models), len(scenarios))

    cells = [(model, scenario) for model in config.models for scenario in scenarios]
    cell_keys = [(model.name, scenario.id) for model, scenario in cells]
    result_by_key = {(result.model, result.scenario_id): result for result in completed_results}
    report.results = [result_by_key[key] for key in cell_keys if key in result_by_key]
    pending_cells = [cell for cell, key in zip(cells, cell_keys) if key not in result_by_key]
    semaphore = asyncio.Semaphore(config.concurrency)

    async def _one(model: ModelSpec, scenario: Scenario) -> MetricResult:
        async with semaphore:
            cell_id = progress.start_cell(model.name, scenario.id) if progress is not None else 0
            result = await run_cell(model, scenario, config, judge, progress=progress)
            if progress is not None:
                failure_counts = Counter(
                    redact(trial.error or trial.generation_status) for trial in result.details if not trial.is_valid
                )
                failure_reason = None
                if failure_counts:
                    reason, count = failure_counts.most_common(1)[0]
                    failure_reason = f"{reason} (×{count})"
                axis_value = result.sub_metrics.get(f"axis_{scenario.primary_axis.value}")
                displayed_score = float(axis_value) if isinstance(axis_value, (int, float)) else None
                progress.finish_cell(
                    cell_id,
                    displayed_score,
                    result.n_failed,
                    failure_reason=failure_reason,
                )
            result_by_key[(model.name, scenario.id)] = result
            report.results = [result_by_key[key] for key in cell_keys if key in result_by_key]
            if checkpoint_callback is not None:
                checkpoint_callback(report)
            return result

    await asyncio.gather(*(_one(model, scenario) for model, scenario in pending_cells))
    report.results = [result_by_key[key] for key in cell_keys]

    if config.audit_ambiguous:
        if judge is None:
            print(
                "[bench] warning: --audit-ambiguous requires --judge-model; skipping.",
                file=sys.stderr,
            )
        else:
            from benchmarks.audit import audit_ambiguous

            report.audit = await audit_ambiguous(report, judge)
    return report


def _build_judge(config: BenchmarkConfig) -> Any | None:
    if not config.judge_model:
        return None
    from benchmarks.judge.client import JudgeClient

    return JudgeClient(config.judge_model, retries=config.judge_retries)
