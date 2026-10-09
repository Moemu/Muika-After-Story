"""持久保存行动任务、工具调用与完整输出。"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from muika.config import mas_config
from muika.llm._schema import MediaReference, ModelMessage, ToolCall, ToolResult
from muika.models import Resource

from .report import AgentReport

TaskStatus = Literal["queued", "running", "recovering", "blocked", "completed", "failed", "cancelled"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskRecord(BaseModel):
    """保存任务目标、版本和可恢复的模型现场。"""

    id: str = Field(default_factory=lambda: uuid4().hex)
    status: TaskStatus = "queued"
    revision: int = 1
    original_request: str
    instruction: str
    acceptance: str = "Complete the requested work and report what was actually verified."
    corrections: list[str] = Field(default_factory=list)
    messages: list[ModelMessage] = Field(default_factory=list)
    context_messages: list[ModelMessage] = Field(default_factory=list)
    context_through: int = 0
    intention_id: str | None = None
    report: AgentReport | None = None
    report_error: str | None = None
    error: str | None = None
    notified_revision: int = 0
    notified_status: str = ""
    cancel_requested: bool = False
    handoff: bool = False
    acknowledgement_retry: bool = False
    format_retry: bool = False
    resources: list[MediaReference] = Field(default_factory=list)
    file_versions: dict[str, str] = Field(default_factory=dict)
    progress_summary: str = ""
    pending_review_id: str | None = None
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class CallRecord(BaseModel):
    """持久记录一次动作的执行事实。"""

    id: str = Field(default_factory=lambda: uuid4().hex)
    task_id: str
    call: ToolCall
    message_index: int = 0
    status: Literal["pending", "completed", "reconciled"] = "pending"
    result: ToolResult | None = None
    output_path: str | None = None
    recovery_evidence: str | None = None
    completed_at: datetime | None = None


class TaskStore:
    """原子保存文件检查点与执行输出。"""

    def __init__(self) -> None:
        self.directory = mas_config.data_dir.resolve() / "agent_tasks"
        self._write_lock = asyncio.Lock()

    async def load(self) -> list[TaskRecord]:
        tasks = [
            TaskRecord.model_validate(json.loads(path.read_text(encoding="utf-8"))["task"])
            for path in self.directory.glob("*/checkpoint.json")
        ]
        return sorted(tasks, key=lambda task: (task.created_at, task.id))

    async def calls(self, task_id: str) -> list[CallRecord]:
        path = self.directory / task_id / "checkpoint.json"
        if not path.exists():
            return []
        return [CallRecord.model_validate(call) for call in json.loads(path.read_text(encoding="utf-8"))["calls"]]

    async def save(self, task: TaskRecord, call: CallRecord | None = None) -> None:
        """在一个文件中提交任务和动作，失败交由执行层停止。"""
        async with self._write_lock:
            task.updated_at = _now()
            calls = {item.id: item for item in await self.calls(task.id)}
            if call is not None:
                if call.task_id != task.id:
                    raise ValueError("The call belongs to another task")
                calls[call.id] = call
            self.save_output(
                task.id,
                "checkpoint.json",
                json.dumps(
                    {
                        "task": task.model_dump(mode="json"),
                        "calls": [item.model_dump(mode="json") for item in calls.values()],
                    },
                    ensure_ascii=False,
                ),
            )

    def save_output(self, task_id: str, name: str, text: str) -> Path:
        """原子写入检查点或完整输出。"""
        directory = self.directory / task_id
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / name
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(destination)
        return destination

    def archive_result(self, record: CallRecord, result: ToolResult) -> ToolResult:
        output = self.save_output(record.task_id, f"{record.id}.json", result.model_dump_json())
        record.output_path = str(output)
        text = result.text
        if len(text) > 12000:
            text = text[:10000] + f"\n[Output abridged. Full result: {output}]"
        record.result = result.model_copy(update={"text": text})
        return record.result

    def archive_resource(self, task_id: str, resource: Resource) -> MediaReference:
        """复制工具资源，避免后续截图覆盖任务的验证证据。"""
        raw = resource.raw
        if resource.path:
            content = Path(resource.path).read_bytes()
        elif isinstance(raw, bytes):
            content = raw
        elif raw is not None:
            content = raw.getvalue()
        else:
            raise ValueError("The resource has no local content to preserve")
        directory = self.directory / task_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (uuid4().hex + (resource.extension or ".bin"))
        target.write_bytes(content)
        return MediaReference(type=resource.type, path=str(target), mimetype=resource.mimetype)

    def model_messages(self, task: TaskRecord) -> list[ModelMessage]:
        """返回已保存工作视图和后续消息，完整轨迹仍在 messages 中。"""
        return [
            message.model_copy(deep=True) for message in task.context_messages + task.messages[task.context_through :]
        ]
