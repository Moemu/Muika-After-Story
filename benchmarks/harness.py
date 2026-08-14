"""Benchmark execution surfaces and auditable production-loop traces."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Sequence

from muika.core.events import Event
from muika.core.loop import Muika
from muika.core.memory import MemoryManager
from muika.core.state import MuikaState


class HarnessMode(str, Enum):
    BRAIN = "brain"
    LOOP = "loop"


@dataclass(frozen=True)
class TraceEvent:
    seq: int
    kind: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "kind": self.kind, **self.data}


@dataclass
class RunTrace:
    mode: HarnessMode
    events: list[TraceEvent] = field(default_factory=list)

    def add(self, kind: str, **data: Any) -> None:
        self.events.append(TraceEvent(len(self.events), kind, data))

    @property
    def raw_replies(self) -> list[str]:
        return [str(event.data.get("reply", "")) for event in self.events if event.kind == "brain_reply"]

    @property
    def visible_messages(self) -> list[str]:
        return [str(event.data.get("message", "")) for event in self.events if event.kind == "visible_message"]

    @property
    def agent_reports(self) -> list[str]:
        return [str(event.data.get("report", "")) for event in self.events if event.kind == "agent_report"]

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode.value, "events": [event.to_dict() for event in self.events]}


class _TracingBrain:
    def __init__(self, inner: Any, trace: RunTrace, fixed_now: datetime | None) -> None:
        self.inner = inner
        self.trace = trace
        self.fixed_now = fixed_now

    async def generate_reply(self, *args: Any, **kwargs: Any) -> str:
        if self.fixed_now is not None:
            kwargs.setdefault("now", self.fixed_now)
        reply = await self.inner.generate_reply(*args, **kwargs)
        self.trace.add("brain_reply", reply=reply)
        return reply


class _TracingExecutor:
    def __init__(self, trace: RunTrace) -> None:
        self.trace = trace

    async def send_message(self, message: str, target: str | None = None, **_: Any) -> None:
        self.trace.add("visible_message", message=message, target=target)


class _FixtureButler:
    def __init__(self, trace: RunTrace, reports: Sequence[str | None]) -> None:
        self.trace = trace
        self._reports = deque(reports)

    async def classify_and_store_memory(self, content: str, state: MuikaState) -> None:
        self.trace.add("memory_write", content=content)

    async def execute_command(self, command: str, state: MuikaState, executor: Any) -> tuple[str, list[Any]]:
        self.trace.add("agent_command", command=command)
        report = self._reports.popleft() if self._reports else None
        if report:
            self.trace.add("agent_report", command=command, report=report)
        return report or "", []


async def run_brain_once(
    brain: Any,
    event: Event,
    state: MuikaState,
    memory: MemoryManager,
    *,
    fixed_now: datetime | None = None,
) -> RunTrace:
    """Run the candidate Brain directly and normalize it into the same trace schema."""
    trace = RunTrace(HarnessMode.BRAIN)
    reply = await brain.generate_reply(event, state, memory, god_mode=False, now=fixed_now)
    trace.add("brain_reply", reply=reply)
    parsed = Muika._parse_reply_tags(reply)
    if parsed.clean_reply:
        trace.add("visible_message", message=parsed.clean_reply, target=parsed.target)
    for content in parsed.memory_contents:
        trace.add("memory_pending", content=content)
    for command in parsed.agent_commands:
        trace.add("agent_pending", command=command)
    if parsed.timeout is not None:
        trace.add("timeout", seconds=parsed.timeout)
    if parsed.god_mode:
        trace.add("god_mode_request")
    return trace


async def run_production_loop(
    brain: Any,
    event: Event,
    state: MuikaState,
    memory: MemoryManager,
    *,
    agent_reports: Sequence[str | None] = (),
    fixed_now: datetime | None = None,
) -> RunTrace:
    """Exercise ``Muika._run_brain_pipeline`` with deterministic Butler/Executor fixtures.

    No real tool is invoked.  The adapter preserves production ordering: visible text is sent,
    then memory tags and Agent commands are handled, and an Agent report may trigger another
    Brain pass.
    """
    trace = RunTrace(HarnessMode.LOOP)
    engine: Any = Muika.__new__(Muika)
    engine.brain = _TracingBrain(brain, trace, fixed_now)
    engine.butler_agent = _FixtureButler(trace, agent_reports)
    engine.executor = _TracingExecutor(trace)
    engine.memory = memory
    engine.state = state
    engine.current_adapters = []
    engine._god_mode = False
    engine._timeout_task = None
    engine._arm_timeout = lambda seconds: trace.add("timeout", seconds=seconds)

    if event.type == "user_message":
        memory.add_context("user", event.payload.message.message)
    await engine._run_brain_pipeline(event, [])
    return trace


__all__ = ["HarnessMode", "RunTrace", "TraceEvent", "run_brain_once", "run_production_loop"]
