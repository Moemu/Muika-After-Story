"""行动任务的持久化、纠正和恢复行为。"""

import asyncio
import json
from collections import deque
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from muika.core.butler.task_store import CallRecord
from muika.core.butler.tasks import AgentTasks
from muika.core.events import AgentTaskEvent
from muika.core.state import MuikaState
from muika.llm import ModelCompletions, ModelRequest
from muika.llm._schema import ModelMessage, ToolCall, ToolResult
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
        self.config = SimpleNamespace(multimodal=False)

    async def step(self, request, messages):
        self.requests.append((request, [m.model_copy(deep=True) for m in messages]))
        response = self.script.popleft()
        return await response(request, messages) if callable(response) else response


@pytest.fixture
def factory(monkeypatch, db_session, session_ctx_factory):
    monkeypatch.setattr("muika.core.butler.task_store.get_session", lambda: session_ctx_factory(db_session))
    monkeypatch.setattr("muika.plugin.func_call.caller._caller_data", get_function_calls().copy())

    def create(script, queue=None):
        agent = MagicMock()
        agent.action_lock = asyncio.Lock()
        agent.model = StepModel(script)
        agent.build_request = lambda command: ModelRequest(command, tools=get_tool_list())
        return AgentTasks(agent, MuikaState(), MagicMock(), queue if queue is not None else asyncio.Queue())

    return create


async def _event(manager):
    return await asyncio.wait_for(manager.events.get(), 5)


async def _stop(manager, worker):
    await manager.close()
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


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
