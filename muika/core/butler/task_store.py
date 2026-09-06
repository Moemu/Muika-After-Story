"""持久保存行动任务、工具调用与完整输出。"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy import select

from muika.config import mas_config
from muika.database.db import get_session
from muika.database.orm_models import AgentCallORM, AgentTaskORM
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


class TaskStore:
    """协调数据库检查点与持久文件输出。"""

    def __init__(self) -> None:
        self.directory = mas_config.data_dir.resolve() / "agent_tasks"
        self._write_lock = asyncio.Lock()

    async def load(self) -> list[TaskRecord]:
        async with get_session() as session:
            rows = await session.scalars(select(AgentTaskORM).order_by(AgentTaskORM.created_at, AgentTaskORM.id))
            return [TaskRecord.model_validate_json(row.payload) for row in rows]

    async def calls(self, task_id: str) -> list[CallRecord]:
        async with get_session() as session:
            rows = await session.scalars(select(AgentCallORM).where(AgentCallORM.task_id == task_id))
            return [CallRecord.model_validate_json(row.payload) for row in rows]

    async def save(self, task: TaskRecord, call: CallRecord | None = None) -> None:
        """在一个事务中保存任务和本次动作，失败交由执行层停止。"""
        async with self._write_lock:
            task.updated_at = _now()
            async with get_session() as session:
                await session.merge(
                    AgentTaskORM(
                        id=task.id,
                        status=task.status,
                        revision=task.revision,
                        created_at=task.created_at,
                        updated_at=task.updated_at,
                        payload=task.model_dump_json(),
                    )
                )
                if call is not None:
                    await session.merge(
                        AgentCallORM(
                            id=call.id,
                            task_id=task.id,
                            status=call.status,
                            payload=call.model_dump_json(),
                        )
                    )

    def save_output(self, task_id: str, name: str, text: str) -> Path:
        """原子写入完整输出，数据库仅保存可读摘要及索引。"""
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
        """压缩较早的工具正文，完整轨迹和协议字段仍留在持久记录中。"""
        messages = [message.model_copy(deep=True) for message in task.messages]
        budget = mas_config.agent_tool_context_chars
        total = sum(len(m.content) for m in messages if m.role == "tool")
        if total <= budget:
            return messages
        recent = max((i for i, m in enumerate(messages) if m.tool_calls), default=len(messages))
        for index, message in enumerate(messages):
            if index >= recent or total <= budget:
                break
            if message.role != "tool" or len(message.content) < 1800:
                continue
            path = self.save_output(task.id, f"message-{index}.txt", message.content)
            shortened = message.content[:1200] + f"\n[Earlier observation abridged. Full content: {path}]"
            total -= len(message.content) - len(shortened)
            message.content = shortened
        return messages
