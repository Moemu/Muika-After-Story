"""顺序运行可恢复的行动任务，并将结果交回主人格。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

from pydantic import BaseModel, Field, ValidationError

from muika.core.events import AgentTaskEvent, Event
from muika.core.executor import Executor
from muika.core.processes import get_process_manager
from muika.core.state import MuikaState
from muika.llm._execution import dispatch_call, observation_message, result_message
from muika.llm._schema import ModelMessage, ToolCall, ToolResult
from muika.llm.utils.thought_processor import general_processor
from muika.plugin.func_call import get_function_calls
from muika.plugin.func_call.context import tool_context
from muika.utils.logger import logger

from .agent import Agent
from .report import parse_report
from .task_store import CallRecord, TaskRecord, TaskStore


class RecoveryReport(BaseModel):
    """说明是否通过现场证据确认了中断动作的结果。"""

    resolved: bool
    evidence: str
    evidence_call_ids: list[str] = Field(default_factory=list)


class AgentTasks:
    """拥有单一执行任务、等待队列和恢复检查点。"""

    def __init__(self, agent: Agent, state: MuikaState, executor: Executor, events: asyncio.Queue[Event]) -> None:
        self.agent = agent
        self.state = state
        self.executor = executor
        self.events = events
        self.store = TaskStore()
        self.tasks: dict[str, TaskRecord] = {}
        self.active_id: str | None = None
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._boundary = asyncio.Event()
        self._boundary.set()
        self._initialized = False
        self._closing = False
        self._persona_owner = False
        self._storage_error: str | None = None
        self._notifications: set[tuple[str, int, str]] = set()

    async def _save(self, task: TaskRecord, call: CallRecord | None = None) -> None:
        try:
            await self.store.save(task, call)
        except Exception as exc:
            self._storage_error = f"Task checkpoint failed: {exc}"
            raise

    async def initialize(self) -> None:
        """恢复记录并补发尚未交付的结果事件。"""
        async with self._lock:
            if self._initialized:
                return
            records = await self.store.load()
            for task in records:
                self.tasks[task.id] = task
                pending = any(call.status == "pending" for call in await self.store.calls(task.id))
                if task.status in {"running", "recovering"} or task.handoff or pending and task.status != "cancelled":
                    task.status = "recovering"
                    task.handoff = False
                    await self._save(task)
                await self._notify(task)
            self._initialized = True
            self._wake.set()

    async def submit(self, instruction: str, original_request: str) -> TaskRecord:
        """保存新任务，执行工作由后台循环负责。"""
        await self.initialize()
        async with self._lock:
            if self._storage_error:
                raise RuntimeError(self._storage_error)
            task = TaskRecord(instruction=instruction, original_request=original_request)
            await self._save(task)
            self.tasks[task.id] = task
            self._wake.set()
            return task

    async def update(self, task_id: str, instruction: str, *, cancel: bool = False) -> TaskRecord:
        """把纠正加入原任务，并在动作边界生效。"""
        await self.initialize()
        async with self._lock:
            task = self.tasks[task_id]
            task.revision += 1
            task.report = None
            task.report_error = None
            task.error = None
            task.format_retry = False
            task.acknowledgement_retry = False
            task.cancel_requested = cancel
            if instruction:
                task.corrections.append(instruction)
                # 工具批次可能尚未结束，下一次模型请求前再插入纠正消息。
            if self.active_id != task_id:
                task.status = "cancelled" if cancel else "queued"
            await self._save(task)
            await self._notify(task)
            self._wake.set()
            return task

    def describe(self) -> str:
        """提供主人格需要的当前任务状态，不复制工具全文。"""
        active = [t for t in self.tasks.values() if t.status not in {"completed", "cancelled"}]
        recent = [t for t in self.tasks.values() if t.status in {"completed", "cancelled"}][-3:]
        lines = []
        for task in active + recent:
            details = task.report.summary if task.report else task.error or task.instruction[:500]
            lines.append(f"Task {task.id} revision={task.revision} status={task.status}: {details}")
        return "\n".join(lines) or "No action tasks."

    async def run(self) -> None:
        """运行一个任务直到完成、受阻或交回执行权。"""
        await self.initialize()
        while not self._closing:
            await self._wake.wait()
            self._wake.clear()
            if self._persona_owner or self._storage_error:
                continue
            for task in list(self.tasks.values()):
                if self._closing or self._persona_owner or self._storage_error:
                    break
                if task.status not in {"queued", "recovering"} or task.handoff:
                    continue
                self.active_id = task.id
                self._boundary.clear()
                try:
                    async with self.agent.action_lock:
                        await self._run_task(task)
                except asyncio.CancelledError:
                    task.status = "recovering" if not task.cancel_requested else "cancelled"
                    try:
                        await self._save(task)
                    except Exception as exc:
                        logger.error(f"[AgentTask] Could not checkpoint shutdown: {exc}")
                    raise
                except Exception as exc:
                    task.status = "failed"
                    task.error = f"Task execution failed: {type(exc).__name__}: {exc}"
                    logger.exception(f"[AgentTask] {task.id}: {task.error}")
                    try:
                        await self._save(task)
                        await self._notify(task)
                    except Exception as storage_exc:
                        self._storage_error = f"Task storage is unavailable: {storage_exc}"
                        logger.error(f"[AgentTask] {self._storage_error}")
                finally:
                    self.active_id = None
                    self._boundary.set()

    async def _run_task(self, task: TaskRecord) -> None:
        calls = await self.store.calls(task.id)
        for record in calls:
            if (
                record.status != "completed"
                or record.result is None
                or record.result.is_error
                or record.call.name not in {"execute_python", "execute_shell", "wait_process"}
            ):
                continue
            try:
                execution = json.loads(record.result.text)
            except ValueError:
                continue
            if not isinstance(execution, dict) or execution.get("status") != "running":
                continue
            try:
                evidence = get_process_manager().read_record(execution["process_id"], owner=task.id)
                if evidence["status"] == "completed" or evidence["controllable"]:
                    continue
            except (ValueError, KeyError, OSError):
                pass
            record.status = "pending"
            await self._save(task, record)
        uncertain = [call for call in calls if call.status == "pending"]
        resumed = task.status == "recovering"
        self._complete_interrupted_batch(task, calls)
        task.status = "recovering" if uncertain else "running"
        if resumed:
            task.messages.append(
                ModelMessage(
                    role="user",
                    content="Runtime restarted. Re-read affected files and current tool availability. "
                    "Old process IDs and unconfirmed previews are no longer valid. Never replay completed actions.",
                )
            )
        await self._save(task)
        applied_revision = 0
        recovery_reads: set[str] = set()
        while not self._closing:
            if await self._stop_at_boundary(task):
                return
            if applied_revision != task.revision:
                if task.corrections:
                    task.messages.append(
                        ModelMessage(
                            role="user",
                            content="Current corrections, in order:\n" + "\n".join(task.corrections),
                        )
                    )
                applied_revision = task.revision
                await self._save(task)
            request = self.agent.build_request(
                f"{task.instruction}\n\nOriginal request:\n{task.original_request}\n\nAcceptance:\n{task.acceptance}"
            )
            if uncertain:
                safe = {name for name, caller in get_function_calls().items() if caller.read_only}
                request.tools = [tool for tool in request.tools or [] if tool["function"]["name"] in safe]
                request.prompt += (
                    "\n\nRecovery only: these actions have unknown outcomes. Inspect the actual state with read-only "
                    "tools. Do not execute or replay actions. Return JSON with resolved, evidence, evidence_call_ids. "
                    "Set resolved=false when the result cannot be established. Unknown calls:\n"
                    + "\n".join(call.model_dump_json() for call in uncertain)
                )
                request.system = (request.system or "") + (
                    "\nThis step is recovery inspection. "
                    "Return RecoveryReport JSON instead of the normal result wrapper."
                )
                request.format = "json"
                request.json_schema = RecoveryReport
            if task.format_retry:
                request = replace(request, tools=[])
            revision = task.revision
            with tool_context(self.state, self.executor, task_id=task.id):
                completion = await self.agent.model.step(request, self.store.model_messages(task))
            if task.revision != revision or task.cancel_requested or self._persona_owner:
                self.store.save_output(task.id, f"superseded-{len(task.messages)}.txt", completion.text)
                continue
            if not completion.succeed or completion.stop_reason in {"length", "filtered", "error"}:
                task.status = "failed"
                task.error = f"Model step stopped ({completion.stop_reason}): {completion.text}"
                await self._save(task)
                await self._notify(task)
                return
            message = completion.message or ModelMessage(role="assistant", content=completion.text)
            if task.format_retry and message.tool_calls:
                task.report_error = (
                    "The report repair returned tool calls while tools were disabled. Work is preserved."
                )
                task.status = "blocked"
                await self._save(task)
                await self._notify(task)
                return
            message_index = len(task.messages)
            task.messages.append(message)
            for resource in completion.resources:
                task.resources.append(self.store.archive_resource(task.id, resource))
            await self._save(task)
            if message.tool_calls:
                observations = []
                for call in message.tool_calls:
                    if task.revision != revision or task.cancel_requested or self._persona_owner:
                        result = ToolResult(
                            text="Not executed: task instructions or execution ownership changed.", is_error=True
                        )
                        task.messages.append(result_message(call, result))
                        await self._save(task)
                        continue
                    allowed = {tool["function"]["name"] for tool in request.tools or []}
                    current_tool = get_function_calls().get(call.name)
                    if call.name not in allowed or uncertain and (current_tool is None or not current_tool.read_only):
                        result = ToolResult(
                            text="Tool is unavailable for this step. Re-plan with the current tools.", is_error=True
                        )
                        task.messages.append(result_message(call, result))
                        await self._save(task)
                        continue
                    result = await self._record_call(task, call, message_index)
                    if uncertain and not result.is_error:
                        recovery_reads.add(call.id)
                    observations.extend(result.resources)
                if observations:
                    task.messages.append(
                        observation_message(observations, multimodal=self.agent.model.config.multimodal)
                    )
                    await self._save(task)
                continue
            if uncertain:
                _, visible = general_processor(completion.text)
                try:
                    recovery = RecoveryReport.model_validate_json(visible)
                except ValidationError:
                    recovery = RecoveryReport(
                        resolved=False, evidence="Recovery did not return a valid evidence report."
                    )
                if (
                    not recovery.resolved
                    or not recovery.evidence_call_ids
                    or not set(recovery.evidence_call_ids) <= recovery_reads
                ):
                    task.status = "blocked"
                    task.error = f"Interrupted action outcome is still uncertain: {recovery.evidence}"
                    await self._save(task)
                    await self._notify(task)
                    return
                for record in uncertain:
                    record.status = "reconciled"
                    record.recovery_evidence = recovery.evidence
                    await self._save(task, record)
                uncertain = []
                task.status = "running"
                task.messages.append(
                    ModelMessage(role="user", content="Recovery checked. Continue remaining work using these facts.")
                )
                await self._save(task)
                continue
            if await self._finish_report(task, completion.text):
                return

    @staticmethod
    def _complete_interrupted_batch(task: TaskRecord, calls: list[CallRecord]) -> None:
        """为中断批次补齐协议结果，不重新执行工具。"""
        completed = {(record.message_index, record.call.id): record for record in calls}
        rebuilt = []
        for index, message in enumerate(task.messages):
            if message.role == "tool":
                continue
            rebuilt.append(message)
            if not message.tool_calls:
                continue
            following = task.messages[index + 1 : index + 1 + len(message.tool_calls)]
            saved = {m.tool_call_id: m for m in following if m.role == "tool"}
            for call in message.tool_calls:
                record = completed.get((index, call.id))
                if call.id in saved:
                    rebuilt.append(saved[call.id])
                elif record and record.result:
                    rebuilt.append(result_message(call, record.result))
                else:
                    result = ToolResult(
                        text=(
                            "Outcome unknown after interruption. Inspect state before any further action."
                            if record
                            else "Not executed before interruption. Re-read state and re-plan."
                        ),
                        is_error=True,
                    )
                    rebuilt.append(result_message(call, result))
        task.messages = rebuilt

    async def _record_call(self, task: TaskRecord, call: ToolCall, message_index: int) -> ToolResult:
        revision = task.revision
        record = CallRecord(task_id=task.id, call=call, message_index=message_index)
        async with self._lock:
            await self._save(task, record)

        async def action() -> ToolResult:
            if (
                task.revision != revision
                or task.cancel_requested
                or self._closing
                or self._persona_owner
                and not task.handoff
            ):
                return ToolResult(text="Not executed: task instructions or execution ownership changed.", is_error=True)
            with tool_context(self.state, self.executor, task_id=task.id, file_versions=task.file_versions):
                return await dispatch_call(call)

        action_task = asyncio.create_task(action())
        try:
            result = await asyncio.shield(action_task)
        except asyncio.CancelledError:
            action_task.cancel()
            await asyncio.gather(action_task, return_exceptions=True)
            raise
        result.resources = [self.store.archive_resource(task.id, ref.to_resource()) for ref in result.resources]
        result = self.store.archive_result(record, result)
        async with self._lock:
            task.messages.append(result_message(call, result))
            task.resources.extend(ref for ref in result.resources if ref not in task.resources)
            record.status = "completed"
            await self._save(task, record)
        return result

    async def _finish_report(self, task: TaskRecord, text: str) -> bool:
        report = parse_report(text)
        processes = get_process_manager()
        active_processes = processes.active_for(task.id)
        if active_processes:
            revision = task.revision
            while active_processes == processes.active_for(task.id):
                if task.revision != revision or task.cancel_requested or self._persona_owner or self._closing:
                    return False
                await processes.wait(active_processes[0], owner=task.id, seconds=1)
            evidence = [
                (await processes.wait(process_id, owner=task.id, seconds=0)).model_dump_json()
                for process_id in active_processes
            ]
            task.messages.append(
                ModelMessage(
                    role="user",
                    content="A running process was not completion. Execution status changed; inspect these results "
                    "before reporting completion:\n" + "\n".join(evidence),
                )
            )
            await self._save(task)
            return False
        if report:
            task.report = report
            task.status = report.status
            await self._save(task)
            await self._notify(task)
            return True
        has_actions = any(message.role == "tool" for message in task.messages)
        if not has_actions and not task.acknowledgement_retry:
            task.acknowledgement_retry = True
            task.messages.append(
                ModelMessage(
                    role="user",
                    content="Start the requested work now using tools. An acknowledgement is not completion. "
                    "If no action is needed, return the factual result; if blocked, report the exact cause.",
                )
            )
        elif not has_actions and not task.format_retry:
            task.status = "blocked"
            task.error = "The model stopped twice without executing the task or returning a valid result."
            await self._save(task)
            await self._notify(task)
            return True
        elif not task.format_retry:
            task.format_retry = True
            task.messages.append(
                ModelMessage(
                    role="user",
                    content="Repair only your final report. Tools are disabled. Do not repeat any action. "
                    'Return <agent_result status="completed"> or status="blocked" with JSON containing '
                    "summary, completed, verification, remaining. Describe only facts supported by the tool results. "
                    "If work remains, report blocked and list it.",
                )
            )
        else:
            task.status = "blocked"
            task.report_error = "The final report is invalid after one formatting repair. Existing work is preserved."
            await self._save(task)
            await self._notify(task)
            return True
        await self._save(task)
        return False

    async def _stop_at_boundary(self, task: TaskRecord) -> bool:
        if self._storage_error:
            raise RuntimeError(self._storage_error)
        if task.cancel_requested:
            await get_process_manager().stop_owner(task.id)
            task.status = "cancelled"
        elif self._persona_owner:
            task.handoff = True
            task.status = "blocked"
            task.error = "Execution handed to the main personality at a tool boundary."
        else:
            return False
        await self._save(task)
        await self._notify(task)
        return True

    async def _notify(self, task: TaskRecord) -> None:
        if task.handoff:
            return
        if task.status not in {"completed", "blocked", "failed", "cancelled"}:
            return
        if task.notified_revision == task.revision and task.notified_status == task.status:
            return
        key = (task.id, task.revision, task.status)
        if key in self._notifications:
            return
        self._notifications.add(key)
        body = task.report.describe() if task.report else task.error or task.report_error or "Task cancelled."
        await self.events.put(AgentTaskEvent(task.id, task.revision, task.status, body))

    def is_current_event(self, event: AgentTaskEvent) -> bool:
        task = self.tasks.get(event.task_id)
        return bool(
            task
            and task.revision == event.revision
            and task.status == event.status
            and not (task.notified_revision == event.revision and task.notified_status == event.status)
        )

    async def delivered(self, event: AgentTaskEvent) -> None:
        async with self._lock:
            if self.is_current_event(event):
                task = self.tasks[event.task_id]
                task.notified_revision = event.revision
                task.notified_status = event.status
                await self._save(task)

    def defer_event(self, event: AgentTaskEvent) -> None:
        """保留未送达的通知，等待下一次用户活动再尝试。"""
        self._notifications.discard((event.task_id, event.revision, event.status))

    async def notify_pending(self) -> None:
        for task in self.tasks.values():
            await self._notify(task)

    async def handoff(self) -> str:
        """等到工具边界后把全局执行权交给主人格。"""
        candidate = self.tasks.get(self.active_id or "") or next(
            (task for task in reversed(list(self.tasks.values())) if task.status in {"blocked", "failed", "queued"}),
            None,
        )
        self._persona_owner = True
        self._wake.set()
        await self._boundary.wait()
        if candidate and not candidate.handoff:
            candidate.handoff = True
            candidate.status = "blocked"
            candidate.error = "Execution handed to the main personality."
            await self._save(candidate)
        for task in self.tasks.values():
            if task.handoff:
                for process_id in get_process_manager().active_for(task.id):
                    while process_id in get_process_manager().active_for(task.id):
                        await get_process_manager().wait(process_id, owner=task.id, seconds=1)
        handed_task = self.persona_task()
        if handed_task is None:
            return self.describe()
        observations = "\n".join(
            f"{message.name or message.role}: {message.content[-4000:]}"
            for message in handed_task.messages[-12:]
            if message.role == "tool"
        )
        return (
            f"{self.describe()}\nOriginal request: {handed_task.original_request}\n"
            f"Instruction: {handed_task.instruction}\n"
            f"Acceptance: {handed_task.acceptance}\nCorrections: {handed_task.corrections}\n"
            f"Recent execution evidence:\n{observations}\n"
            f"Full outputs: {self.store.directory / handed_task.id}\n"
            "Use read_task_output to inspect saved evidence. Re-read files before making further changes."
        )

    def persona_task(self) -> TaskRecord | None:
        return next((task for task in self.tasks.values() if task.handoff), None)

    async def execute_persona_call(self, call: ToolCall) -> ToolResult:
        """把主人格接手后的动作写入同一个任务记录。"""
        task = self.persona_task()
        if not self._persona_owner or task is None:
            raise ValueError("No task has been handed to the main personality")
        if task.cancel_requested:
            return ToolResult(text="The task was cancelled. Do not start more actions.", is_error=True)
        message_index = len(task.messages)
        task.messages.append(ModelMessage(role="assistant", content="Main personality action.", tool_calls=[call]))
        async with self.agent.action_lock:
            return await self._record_call(task, call, message_index)

    async def release_persona(self) -> None:
        for task in self.tasks.values():
            if task.handoff:
                task.handoff = False
                if task.status == "blocked" and not task.cancel_requested:
                    task.status = "recovering"
                await self._save(task)
        self._persona_owner = False
        self._wake.set()

    async def complete_handoff(self, task_id: str, text: str) -> None:
        task = self.tasks[task_id]
        if not self._persona_owner or not task.handoff:
            raise ValueError("Only the main personality holding this task may complete the handoff")
        report = parse_report(text)
        if report is None:
            raise ValueError("The handoff needs a valid agent_result report with verification evidence")
        if get_process_manager().active_for(task_id):
            raise ValueError("Wait for the task's running processes before completing it")
        task.report = report
        task.status = report.status
        task.handoff = False
        await self._save(task)
        await self._notify(task)
        self._persona_owner = False
        self._wake.set()

    async def close(self) -> None:
        """停止排队派发，并清理本次进程拥有的执行工作。"""
        self._closing = True
        self._wake.set()
        for task in self.tasks.values():
            await get_process_manager().stop_owner(task.id)
