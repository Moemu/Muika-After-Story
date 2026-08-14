"""Benchmark runner with validity gates, invariant auditing, and traceable harnesses."""

from __future__ import annotations

import asyncio
import hashlib
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from benchmarks import __version__ as benchmark_version
from benchmarks.config import BenchmarkConfig, ModelSpec
from benchmarks.extract.actions import classify_actions
from benchmarks.extract.boundary import find_tool_call_leaks, is_premature_god_mode
from benchmarks.extract.claims import ClaimLedger, build_claim_ledger
from benchmarks.extract.hallucination import (
    HallucinationKind,
    classify_action_hallucination,
    classify_bootstrap_hallucination,
)
from benchmarks.extract.leakage import LeakSpan, find_leakage_spans
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
from benchmarks.scenarios.definitions import Metric, Scenario, ScenarioTurn, SeedMemory
from benchmarks.scenarios.registry import get_scenario, select_scenario_ids
from benchmarks.scoring import score_metric
from benchmarks.scoring.base import MetricResult, TrialDetail, TurnDetail
from benchmarks.scoring.personality import rule_personality_score
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


def _record_key(seed: SeedMemory) -> str:
    return f"{seed.layer}:{seed.category}:{seed.key}"


def _record(seed: SeedMemory) -> MemoryRecord:
    return MemoryRecord(layer=seed.layer, category=seed.category, key=seed.key, value=seed.value)


def build_brain(model: Any) -> MuikaBrain:
    """Construct a Brain without provider loading or watcher threads."""
    brain = MuikaBrain.__new__(MuikaBrain)
    brain.model = model
    brain._mcp_tools = []
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


def _recording_stats(recording: Any) -> tuple[int, int, int, list[str]]:
    calls = int(getattr(recording, "call_count", 0))
    responses = list(getattr(recording, "responses", []))
    input_tokens = sum(int(getattr(response.usage, "input_tokens", 0)) for response in responses)
    output_tokens = sum(int(getattr(response.usage, "output_tokens", 0)) for response in responses)
    hashes: list[str] = []
    for request in getattr(recording, "requests", []):
        payload = f"{request.system or ''}\0{request.prompt}".encode("utf-8")
        hashes.append(hashlib.sha256(payload).hexdigest()[:16])
    return calls, input_tokens, output_tokens, hashes


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

    for event in trace.events:
        if event.kind == "agent_report":
            completed_reports.append(str(event.data.get("report", "")))
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

        for leak in find_tool_call_leaks(parsed.clean_reply):
            violation = f"tool_call_leak:{leak.pattern}"
            boundary.append(violation)
            invariants.append(f"boundary:{violation}")
        if is_premature_god_mode(parsed):
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
            is_first_session=turn.event_kind == "session_bootstrap",
        )
        ledger.extend(reply_ledger)
        invariants.extend(f"claim:{violation}" for violation in reply_ledger.violations)

        if turn.event_kind == "session_bootstrap":
            kind = classify_bootstrap_hallucination(parsed.clean_reply)
        else:
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
    except BaseException as exc:  # noqa: BLE001 - benchmark isolation boundary
        generation_status = "exception"
        caught_error = f"{type(exc).__name__}: {exc}"

    latency_ms = (time.perf_counter() - started) * 1000.0
    raw_replies = [reply for trace in traces for reply in trace.raw_replies]
    if caught_error is None:
        model_failure = _recording_failure(recording)
        if model_failure:
            generation_status = "model_error"
            caught_error = model_failure
    if caught_error is None:
        generation_status, caught_error = _validate_generations(raw_replies)

    calls, input_tokens, output_tokens, prompt_hashes = _recording_stats(recording)
    analyses: list[_TurnAnalysis] = []
    history_cursor: list[Any] = []
    for turn_idx, (turn, trace) in enumerate(zip(turn_specs, traces)):
        # The trace itself contains ordering for Agent evidence; history_cursor supplies only
        # facts from completed earlier user/character turns.
        analysis = _analyze_trace(trace, scenario, turn, turn_idx, history_cursor)
        analyses.append(analysis)
        history_cursor.extend([turn.user_text, analysis.raw_reply])

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
        hallucination_kinds.extend(analysis.hallucination_kinds)

    clean_reply = "\n".join(analysis.clean_reply for analysis in analyses if analysis.clean_reply)
    raw_reply = "\n=== scenario turn ===\n".join(analysis.raw_reply for analysis in analyses)
    valid = caught_error is None and generation_status == "ok"
    self_awareness: str | None = None
    personality: dict[str, Any] | None = None
    judge_sources: dict[str, str] = {}

    scenario_text = "\n".join(turn.user_text for turn in turn_specs if turn.user_text)
    if valid and scenario.metric is Metric.SELF_AWARENESS:
        if judge is not None:
            try:
                self_awareness = await judge.classify_self_awareness(clean_reply, scenario_text)
                judge_sources["self_awareness"] = "judge"
            except JudgeError as exc:
                print(f"[bench] judge failed, falling back to rule: {exc}", file=sys.stderr)
        if self_awareness is None:
            self_awareness = classify_self_awareness(clean_reply).value
            judge_sources["self_awareness"] = "rule"

    if valid and scenario.metric is Metric.PERSONALITY:
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
                judge_score, dimensions = await judge.rate_personality(clean_reply, scenario_text)
            except JudgeError as exc:
                print(f"[bench] judge failed, falling back to rule: {exc}", file=sys.stderr)
            else:
                personality["judge_score"] = judge_score
                personality["judge_dimensions"] = dimensions
                judge_sources["personality"] = "judge"

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
        latency_ms=latency_ms,
        model_calls=calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
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
        trials.append(trial)
        if progress is not None:
            progress.trial_done(i + 1, reply=trial.clean_reply if config.echo else None)

    result = score_metric(
        scenario.metric,
        trials,
        scenario,
        model_spec.name,
        min_validity_rate=config.min_validity_rate,
    )
    latencies = [trial.latency_ms for trial in trials if trial.latency_ms is not None]
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

    semaphore = asyncio.Semaphore(config.concurrency)
    cells = [(model, scenario) for model in config.models for scenario in scenarios]

    async def _one(model: ModelSpec, scenario: Scenario) -> MetricResult:
        async with semaphore:
            cell_id = progress.start_cell(model.name, scenario.id) if progress is not None else 0
            result = await run_cell(model, scenario, config, judge, progress=progress)
            if progress is not None:
                progress.finish_cell(cell_id, result.score, result.n_failed)
            return result

    report.results = list(await asyncio.gather(*(_one(model, scenario) for model, scenario in cells)))

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

    return JudgeClient(config.judge_model)
