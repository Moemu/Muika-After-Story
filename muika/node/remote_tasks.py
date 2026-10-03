"""在 Core 节点保留执行工作区，通过业务接口保存任务检查点。"""

from pathlib import Path

from muika.core.agent.task_store import CallRecord, TaskRecord, TaskStore
from muika.ipc.node_client import NodeClient
from muika.ipc.node_protocol import TaskRequest
from muika.models import Resource

from .resources import ResourceVault
from .task_protocol import (
    LoadCalls,
    LoadTasks,
    SaveTask,
    TaskOperation,
    TaskResult,
    TransferFile,
)
from .task_service import call_media, relocate_call, relocate_task, task_media


class RemoteTaskStore(TaskStore):
    """让接管节点读取已提交任务和附件，禁止读取原节点的文件路径。"""

    def __init__(self, client: NodeClient, epoch: int, directory: Path) -> None:
        super().__init__()
        self.client, self.epoch = client, epoch
        self.vault = ResourceVault(directory)

    async def request(self, action: TaskOperation) -> TaskResult:
        response = await self.client.request(TaskRequest(epoch=self.epoch, body=action))
        if response.task is None:
            raise ConnectionError("State service omitted its task result.")
        result = response.task
        paths = {}
        for file in result.files:
            paths[file.key] = (await self.client.download_resource(file.reference, self.vault)).path
        for task in result.tasks:
            relocate_task(task, paths)
        for call in result.calls:
            relocate_call(call, paths)
            if call.output_path:
                target = self.directory / call.task_id / (call.id + ".json")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(Path(call.output_path).read_bytes())
                call.output_path = str(target)
        return result

    async def load(self) -> list[TaskRecord]:
        return (await self.request(LoadTasks())).tasks

    async def calls(self, task_id: str) -> list[CallRecord]:
        return (await self.request(LoadCalls(task_id=task_id))).calls

    async def save(self, task: TaskRecord, call: CallRecord | None = None) -> None:
        resources = {reference.path: reference.to_resource() for reference in task_media(task)}
        if call is not None:
            resources.update({reference.path: reference.to_resource() for reference in call_media(call)})
            if call.output_path:
                resources[call.output_path] = Resource(type="file", path=call.output_path)
        files = [
            TransferFile(key=key, reference=await self.client.upload_resource(resource, self.vault))
            for key, resource in resources.items()
        ]
        await self.request(
            SaveTask(task=task.model_copy(deep=True), call=call.model_copy(deep=True) if call else None, files=files)
        )
