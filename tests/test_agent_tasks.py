"""行动任务的持久化、纠正和恢复行为。"""

import asyncio
import json
import os
import time
from collections import deque
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from muika.config import mas_config
from muika.core.agent.task_store import CallRecord, TaskRecord
from muika.core.agent.tasks import AgentTasks
from muika.core.events import AgentTaskEvent
from muika.core.memory import MemoryManager, MemoryQuery, StateUpdate
from muika.core.memory_models import Intention
from muika.core.state import MuikaState
from muika.llm import ModelCompletions, ModelConfig, ModelRequest
from muika.llm._execution import collect_step
from muika.llm._retry import LLMRequestError
from muika.llm._schema import ModelMessage, ModelStreamCompletions, ToolCall, ToolResult
from muika.llm.context import ContextCompactor
from muika.llm.providers.openai import Openai
from muika.plugin.func_call import get_function_calls, get_tool_list, on_function_call


def _done(summary="Done"):
    return ModelCompletions(text=f'<agent_result status="completed">{summary}</agent_result>')


def _calls(*names):
    return ModelCompletions(
        message=ModelMessage(
            role="assistant",
            tool_calls=[
                ToolCall(id=f"call-{index}-{name}", name=name, arguments="{}") for index, name in enumerate(names)
            ],
        ),
        stop_reason="tool_calls",
    )


class StepModel:
    def __init__(self, script):
        self.script = deque(script)
        self.requests = []
        self.config = ModelConfig(provider="_echo", multimodal=False)

    async def step(self, request, messages, *, prepare_context=None):
        if prepare_context is not None:
            request, messages = await prepare_context(request, messages, False)
        self.requests.append((request, [m.model_copy(deep=True) for m in messages]))
        response = self.script.popleft()
        return await response(request, messages) if callable(response) else response


@pytest.fixture
def factory(monkeypatch, db_session, session_ctx_factory):
    monkeypatch.setattr("muika.core.agent.task_store.get_session", lambda: session_ctx_factory(db_session))
    monkeypatch.setattr("muika.plugin.func_call.caller._caller_data", get_function_calls().copy())

    def create(script, queue=None):
        agent = MagicMock()
        agent.action_lock = asyncio.Lock()
        agent.model = StepModel(script)
        agent.memory_reasoner.compactor = ContextCompactor(agent.model)
        agent.build_request = lambda command, state=None: ModelRequest(command, tools=get_tool_list())
        return AgentTasks(agent, MuikaState(), MagicMock(), queue if queue is not None else asyncio.Queue())

    return create


async def _event(manager):
    return await asyncio.wait_for(manager.events.get(), 5)


async def _stop(manager, worker):
    await manager.close()
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


async def test_failed_compaction_waits_for_growth_or_changed_model(factory, monkeypatch):
    manager = factory([])
    task = await manager.submit("inspect", "original")
    task.messages = [ModelMessage(role="user", content="evidence " * 1600)]
    model = manager.agent.model
    model.config = ModelConfig(provider="_echo", context_window=8192, max_tokens=1024)
    compactor = manager.agent.memory_reasoner.compactor
    compact = AsyncMock(side_effect=lambda request, messages, config, **kwargs: (list(messages), 0, ""))
    monkeypatch.setattr(compactor, "compact_messages", compact)
    request = ModelRequest("inspect")

    await manager._prepare_context(task, model, request, task.messages, False)
    task.messages.append(ModelMessage(role="assistant", content="Read another nearby line."))
    await manager._prepare_context(task, model, request, task.messages, False)
    assert compact.await_count == 1

    task.messages.append(ModelMessage(role="user", content="new evidence " * 500))
    await manager._prepare_context(task, model, request, task.messages, False)
    assert compact.await_count == 2
    model.config = model.config.model_copy(update={"context_window": 16000})
    await manager._prepare_context(task, model, request, task.messages, False)
    assert compact.await_count == 3


async def test_action_context_prepares_once_and_forced_retry_saves_before_sending(factory, monkeypatch):
    manager = factory([])
    task = await manager.submit("inspect", "original")
    task.messages = [ModelMessage(role="user", content="evidence " * 1600)]
    model = Openai(
        ModelConfig(provider="openai", model_name="test", api_key="test", context_window=8192, max_tokens=1024)
    )
    compactor = manager.agent.memory_reasoner.compactor
    compacted = [ModelMessage(role="user", content="Saved evidence and exact source reference.")]
    preparations = []
    requests = []

    async def compact(request, messages, config, *, force=False):
        preparations.append(force)
        return (compacted, 1, "Saved evidence") if force else (list(messages), 0, "")

    async def step(request, messages, *, stream):
        requests.append(list(messages))
        if len(requests) == 2:
            raise LLMRequestError("actual service limit", "context_length", 400)
        if len(requests) == 3:
            saved = next(item for item in await manager.store.load() if item.id == task.id)
            assert saved.context_messages == compacted
            assert saved.context_through == len(task.messages)
        yield ModelStreamCompletions(chunk="checked")

    async def prepare(request, messages, force):
        return await manager._prepare_context(task, model, request, messages, force)

    monkeypatch.setattr(compactor, "compact_messages", compact)
    monkeypatch.setattr(model, "request_step", step)
    generic = AsyncMock(side_effect=AssertionError("Duplicate generic preparation"))
    monkeypatch.setattr("muika.llm._execution.prepare_request", generic)
    request = ModelRequest("inspect")
    assert (await collect_step(model, request, task.messages, prepare_context=prepare)).succeed
    task.messages.append(ModelMessage(role="assistant", content="One more detail."))
    assert (await collect_step(model, request, task.messages, prepare_context=prepare)).succeed
    assert preparations == [False, True]
    assert len(requests) == 3 and requests[-1] == compacted
    assert len(task.messages) == 2
    generic.assert_not_awaited()


async def test_tasks_execute_fifo_and_emit_one_versioned_result(factory):
    manager = factory([_done("one"), _done("two")])
    first = await manager.submit("first", "original first")
    second = await manager.submit("second", "original second")
    worker = asyncio.create_task(manager.run())
    try:
        events = [await _event(manager), await _event(manager)]
        assert [event.task_id for event in events] == [first.id, second.id]
        await manager._notify(first)
        assert manager.events.empty()
        await manager.delivered(events[0])
        assert not manager.is_current_event(events[0])
        assert "original first" in manager.agent.model.requests[0][0].prompt
    finally:
        await _stop(manager, worker)


async def test_result_save_failure_leaves_pending_evidence_without_repeating_action(factory, monkeypatch):
    performed = []

    @on_function_call("Write once")
    async def write_once():
        performed.append("written")
        return "written"

    manager = factory([_calls("write_once")])
    task = await manager.submit("write", "original")
    save = manager.store.save

    async def fail_result(record, call=None):
        if call is not None and call.status == "completed":
            raise OSError("result checkpoint unavailable")
        await save(record, call)

    monkeypatch.setattr(manager.store, "save", fail_result)
    worker = asyncio.create_task(manager.run())
    try:
        event = await _event(manager)
        assert event.status == "failed"
        assert performed == ["written"]
        assert (await manager.store.calls(task.id))[0].status == "pending"
        recovered = factory([ModelCompletions(text='{"resolved":false,"evidence":"cannot confirm"}')])
        await recovered.initialize()
        assert recovered.tasks[task.id].status == "recovering"
        await recovered._run_task(recovered.tasks[task.id])
        assert recovered.tasks[task.id].status == "blocked"
        assert performed == ["written"]
    finally:
        await _stop(manager, worker)


async def test_correction_skips_remaining_old_calls_and_keeps_context(factory):
    entered = asyncio.Event()
    release = asyncio.Event()
    performed = []

    @on_function_call("First action")
    async def first_action():
        performed.append("first")
        entered.set()
        await release.wait()
        return "first finished"

    @on_function_call("Obsolete action")
    async def obsolete_action():
        performed.append("obsolete")
        return "obsolete finished"

    manager = factory([_calls("first_action", "obsolete_action"), _done("Corrected work complete")])
    task = await manager.submit("work", "original")
    worker = asyncio.create_task(manager.run())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await manager.update(task.id, "Do not run the second action")
        release.set()
        event = await _event(manager)
        assert performed == ["first"]
        assert event.revision == 2
        history = manager.agent.model.requests[1][1]
        assert any(m.role == "tool" and "Not executed" in m.content for m in history)
        assert any("Do not run the second action" in m.content for m in history)
    finally:
        release.set()
        await _stop(manager, worker)


async def test_cancel_waits_for_inflight_action_then_stops_batch(factory):
    entered = asyncio.Event()
    release = asyncio.Event()
    performed = []

    @on_function_call("Write once")
    async def write_once():
        entered.set()
        await release.wait()
        performed.append("written")
        return "written"

    manager = factory([_calls("write_once", "write_once")])
    task = await manager.submit("work", "original")
    worker = asyncio.create_task(manager.run())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await manager.update(task.id, "stop", cancel=True)
        release.set()
        event = await _event(manager)
        assert event.status == "cancelled"
        assert performed == ["written"]
        records = await manager.store.calls(task.id)
        assert len(records) == 1 and records[0].status == "completed"
    finally:
        release.set()
        await _stop(manager, worker)


async def test_restart_uses_saved_results_without_replaying_actions(factory):
    manager = factory([])
    task = await manager.submit("finish", "original")
    call = ToolCall(id="written", name="write_file", arguments='{"path":"a","content":"b"}')
    task.status = "running"
    task.messages = [
        ModelMessage(role="assistant", tool_calls=[call]),
        ModelMessage(
            role="tool",
            tool_call_id=call.id,
            name=call.name,
            content="Written successfully",
        ),
    ]
    await manager.store.save(
        task,
        CallRecord(
            task_id=task.id,
            call=call,
            status="completed",
            result=ToolResult(text="Written successfully"),
        ),
    )
    resumed = factory([_done("Verified remaining work")])
    worker = asyncio.create_task(resumed.run())
    try:
        event = await _event(resumed)
        assert event.status == "completed"
        history = resumed.agent.model.requests[0][1]
        assert any(m.tool_call_id == "written" and "Written successfully" in m.content for m in history)
        assert len(await resumed.store.calls(task.id)) == 1
    finally:
        await _stop(resumed, worker)


async def test_uncertain_action_requires_new_read_evidence(factory):
    @on_function_call("Inspect state", read_only=True)
    async def inspect_state():
        return "The requested file already contains the expected change."

    performed = []

    @on_function_call("Mutate state")
    async def mutate_state():
        performed.append("mutated")
        return "done"

    manager = factory([])
    task = await manager.submit("finish", "original")
    call = ToolCall(id="unknown", name="mutate_state", arguments="{}")
    task.status = "running"
    task.messages = [ModelMessage(role="assistant", tool_calls=[call])]
    await manager.store.save(task, CallRecord(task_id=task.id, call=call))
    resumed = factory(
        [
            _calls("mutate_state"),
            _calls("inspect_state"),
            ModelCompletions(
                text=json.dumps(
                    {
                        "resolved": True,
                        "evidence": "Read confirms desired contents",
                        "evidence_call_ids": ["call-0-inspect_state"],
                    }
                )
            ),
            _done("Verified"),
        ]
    )
    worker = asyncio.create_task(resumed.run())
    try:
        event = await _event(resumed)
        assert event.status == "completed"
        assert performed == []
        saved = await resumed.store.calls(task.id)
        assert next(c for c in saved if c.call.id == "unknown").status == "reconciled"
        assert "mutate_state" not in {t["function"]["name"] for t in resumed.agent.model.requests[0][0].tools}
    finally:
        await _stop(resumed, worker)


async def test_failed_checkpoint_prevents_action(factory, monkeypatch):
    performed = []

    @on_function_call("Change a file")
    async def change_file():
        performed.append("changed")
        return "done"

    manager = factory([_calls("change_file")])
    await manager.submit("work", "original")
    save = manager.store.save

    async def failing_save(task, call=None):
        if call is not None:
            raise OSError("disk full")
        await save(task, call)

    monkeypatch.setattr(manager.store, "save", failing_save)
    worker = asyncio.create_task(manager.run())
    try:
        event = await _event(manager)
        assert event.status == "failed"
        assert performed == []
        assert "disk full" in event.report
    finally:
        await _stop(manager, worker)


async def test_report_repair_cannot_execute_more_tools(factory):
    performed = []

    @on_function_call("Do work")
    async def perform_work():
        performed.append("done")
        return "verified"

    manager = factory([_calls("perform_work"), ModelCompletions(text="Done without wrapper"), _calls("perform_work")])
    await manager.submit("work", "original")
    worker = asyncio.create_task(manager.run())
    try:
        event = await _event(manager)
        assert event.status == "blocked"
        assert performed == ["done"]
        assert manager.agent.model.requests[2][0].tools == []
        assert len(manager.agent.model.requests) == 3
    finally:
        await _stop(manager, worker)


async def test_two_acknowledgements_block_without_false_completion(factory):
    manager = factory([ModelCompletions(text="I'll start"), ModelCompletions(text="I will read it")])
    await manager.submit("work", "original")
    worker = asyncio.create_task(manager.run())
    try:
        event = await _event(manager)
        assert event.status == "blocked"
        assert len(manager.agent.model.requests) == 2
    finally:
        await _stop(manager, worker)


async def test_handoff_waits_at_boundary_and_records_persona_action(factory):
    manager = factory([])
    task = await manager.submit("work", "original")
    await manager.handoff()
    assert task.handoff

    @on_function_call("Inspect", read_only=True)
    async def inspect_handoff():
        return "verified"

    await manager.execute_persona_call(ToolCall(id="direct", name="inspect_handoff", arguments="{}"))
    assert len(await manager.store.calls(task.id)) == 1
    await manager.complete_handoff(task.id, '<agent_result status="completed">Verified</agent_result>')
    assert task.status == "completed"


async def test_old_notification_is_ignored_after_followup(factory):
    manager = factory([])
    task = await manager.submit("work", "original")
    task.status = "completed"
    event = AgentTaskEvent(task.id, task.revision, "completed", "done")
    await manager.update(task.id, "one more change")
    assert not manager.is_current_event(event)


@pytest.mark.parametrize("delivered", [False, True])
async def test_review_resume_delivers_each_approval_request(factory, delivered):
    manager = factory(
        [
            ModelCompletions(text='<agent_result status="blocked">Approve source review.</agent_result>'),
            ModelCompletions(text='<agent_result status="blocked">Approve validation review.</agent_result>'),
        ]
    )
    task = await manager.submit("Prepare the proposal", "Please make this change")
    await manager._run_task(task)
    first = await _event(manager)
    if delivered:
        await manager.delivered(first)
    revision = task.revision
    await manager.resume_review(task.id, "source-review")
    assert task.revision == revision
    await manager._run_task(task)
    assert not manager.is_current_event(first)
    second = await _event(manager)
    assert "validation review" in second.report
    assert manager.is_current_event(second)
    await manager.delivered(second)
    await manager.notify_pending()
    assert manager.events.empty()
    restored = factory([])
    await restored.initialize()
    assert restored.events.empty()


async def test_failed_process_arguments_are_not_reclassified_as_unknown_actions(factory):
    manager = factory([_done("No work remains")])
    task = await manager.submit("work", "original")
    task.status = "recovering"
    record = CallRecord(
        task_id=task.id,
        call=ToolCall(id="invalid", name="wait_process", arguments='{"seconds":60}'),
        status="completed",
        result=ToolResult(text="Invalid arguments: seconds exceeds 30", is_error=True),
    )
    await manager.store.save(task, record)
    await manager._run_task(task)
    assert task.status == "completed"
    assert (await manager.store.calls(task.id))[0].status == "completed"


async def test_completed_call_missing_from_memory_is_restored_once(factory, redirect_get_session):
    memory = MemoryManager()
    manager = factory([])
    task = await manager.submit("Read a poem", "Read it")
    call = CallRecord(
        task_id=task.id,
        call=ToolCall(id="poem", name="read_poem", arguments="{}"),
        status="completed",
        result=ToolResult(text="The old poem was read."),
        completed_at=datetime(2026, 9, 1, 4, tzinfo=timezone.utc),
    )
    task.status = "completed"
    await manager.store.save(task, call)
    for _ in range(2):
        restored = factory([])
        restored.state.memory = memory
        await restored.initialize()
    hits = await memory.search(MemoryQuery(terms=["old poem"]))
    assert len(hits) == 1
    assert "2026-09-01" in hits[0].occurred_at
    assert f"task_output:{task.id}:{call.id}" in hits[0].content


async def test_same_intention_does_not_submit_duplicate_task_after_restart(factory, redirect_get_session):
    memory = MemoryManager()
    await memory.update_state(StateUpdate(reason="I am curious", intentions=[Intention(id="poem", description="Read")]))
    manager = factory([])
    manager.state.memory = memory
    task = await manager.submit("Read", "Read", intention_id="poem")
    restored = factory([])
    restored.state.memory = memory
    duplicate = await restored.submit("Read", "Read", intention_id="poem")
    assert duplicate.id == task.id
    assert len(restored.tasks) == 1


def test_describe_includes_progress_summary(factory):
    manager = factory([])
    task = TaskRecord(
        original_request="Write the poem",
        instruction="Write the poem",
        status="running",
        progress_summary="Executing: write_file",
    )
    manager.tasks[task.id] = task
    assert "(progress: Executing: write_file)" in manager.describe()
    task.progress_summary = ""
    assert "(progress:" not in manager.describe()


async def test_cleanup_expired_scratch_keeps_active_task_dir(factory):
    manager = factory([])
    tasks_root = mas_config.scratch_dir / "tasks"
    expired_time = time.time() - (mas_config.scratch_retention_days + 1) * 86400
    stale = tasks_root / "stale-task"
    fresh = tasks_root / "fresh-task"
    active = tasks_root / "active-task"
    for directory in (stale, fresh, active):
        directory.mkdir(parents=True)
    os.utime(stale, (expired_time, expired_time))
    os.utime(active, (expired_time, expired_time))
    manager.tasks["active-task"] = TaskRecord(
        id="active-task", original_request="Long job", instruction="Long job", status="running"
    )

    manager._cleanup_expired_scratch()

    assert not stale.exists()
    assert fresh.exists()
    assert active.exists()
