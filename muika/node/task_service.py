"""保存任务业务记录，并把跨节点文件转换为状态服务持有的副本。"""

from muika.core.agent.task_store import CallRecord, TaskRecord, TaskStore
from muika.database.db import get_session
from muika.database.orm_models import AgentTaskORM, RuntimeExecutionORM
from muika.llm._schema import MediaReference
from muika.models import Resource

from .execution_protocol import ExecutionRecord
from .resources import ResourceVault
from .task_protocol import (
    LoadCalls,
    LoadTasks,
    SaveTask,
    TaskOperation,
    TaskResult,
    TransferFile,
)


def task_media(task: TaskRecord) -> list[MediaReference]:
    return task.resources + [ref for message in task.messages + task.context_messages for ref in message.resources]


def call_media(call: CallRecord) -> list[MediaReference]:
    return call.result.resources if call.result else []


def relocate_task(task: TaskRecord, paths: dict[str, str]) -> None:
    for reference in task_media(task):
        if reference.path not in paths:
            raise ValueError("Task resource has no transferred copy.")
        reference.path = paths[reference.path]


def relocate_call(call: CallRecord, paths: dict[str, str]) -> None:
    for reference in call_media(call):
        if reference.path not in paths:
            raise ValueError("Call resource has no transferred copy.")
        reference.path = paths[reference.path]
    if call.output_path:
        if call.output_path not in paths:
            raise ValueError("Call output has no transferred copy.")
        call.output_path = paths[call.output_path]


class TaskService:
    """复用任务恢复记录；工作区和设备进程仍属于执行设备。"""

    def __init__(self, vault: ResourceVault) -> None:
        self.store = TaskStore()
        self.vault = vault

    def references(self, tasks: list[TaskRecord], calls: list[CallRecord]) -> list[TransferFile]:
        media = [reference for task in tasks for reference in task_media(task)]
        media.extend(reference for call in calls for reference in call_media(call))
        resources = {reference.path: reference.to_resource() for reference in media}
        resources.update(
            {call.output_path: Resource(type="file", path=call.output_path) for call in calls if call.output_path}
        )
        return [TransferFile(key=key, reference=self.vault.preserve(resource)) for key, resource in resources.items()]

    async def execute(self, action: TaskOperation) -> TaskResult:
        result = TaskResult()
        if isinstance(action, LoadTasks):
            result.tasks = await self.store.load()
        elif isinstance(action, LoadCalls):
            result.calls = await self.load_calls(action.task_id)
        elif isinstance(action, SaveTask):
            await self.save_task(action)
        result.files = self.references(result.tasks, result.calls)
        return result

    async def load_calls(self, task_id: str) -> list[CallRecord]:
        """读取调用，并从原设备已完成记录恢复尚未归档的结果。"""
        calls = await self.store.calls(task_id)
        task = next((task for task in await self.store.load() if task.id == task_id), None)
        if task is not None:
            async with get_session() as db:
                for call in calls:
                    execution = await db.get(RuntimeExecutionORM, f"call:{call.id}")
                    if (
                        call.status != "pending"
                        or execution is None
                        or execution.status != "completed"
                        or execution.result is None
                    ):
                        continue
                    completed = ExecutionRecord.model_validate_json(execution.result)
                    if completed.result is None:
                        continue
                    output = completed.result.model_copy(deep=True)
                    paths = {file.key: self.vault.materialize(file.reference).path for file in completed.files}
                    for reference in output.resources:
                        reference.path = paths[reference.path]
                    call.result = self.store.archive_result(call, output)
                    call.status, call.completed_at = "completed", completed.completed_at
                    call.execution_node_id = completed.spec.node_id
                    await self.store.save(task, call)
        return calls

    async def save_task(self, action: SaveTask) -> None:
        """校验控制版本和附件，再保存任务及核对记录。"""
        async with get_session() as db:
            existing = await db.get(AgentTaskORM, action.task.id)
            if existing is not None and existing.revision > action.task.revision:
                raise ValueError("Task checkpoint is older than its committed control.")
        paths = {file.key: self.vault.materialize(file.reference).path for file in action.files}
        relocate_task(action.task, paths)
        if action.call is not None:
            if action.call.task_id != action.task.id:
                raise ValueError("Call does not belong to this task.")
            relocate_call(action.call, paths)
        await self.store.save(action.task, action.call)
        if action.call is not None and action.call.status == "reconciled" and action.call.recovery_evidence:
            async with get_session() as db:
                execution = await db.get(RuntimeExecutionORM, f"call:{action.call.id}")
                if execution is not None:
                    execution.status = "reconciled"
