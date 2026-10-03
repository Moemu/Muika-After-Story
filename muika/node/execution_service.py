"""认领设备动作并保存事实结果；未知副作用必须核对后再规划。"""

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muika.database.db import get_session
from muika.database.orm_models import RuntimeExecutionORM

from .execution_protocol import ExecutionRecord, ExecutionSpec, ToolCapability
from .journal import JournalConflict, RuntimeJournal
from .resources import ResourceVault


def signature(spec: ExecutionSpec) -> str:
    try:
        arguments = json.dumps(json.loads(spec.call.arguments), sort_keys=True)
    except ValueError:
        arguments = spec.call.arguments
    return f"{spec.call.name}:{arguments}"


class ExecutionService:
    """状态服务只调度已有能力，不执行设备工具或生成模型回复。"""

    def __init__(self, journal: RuntimeJournal, vault: ResourceVault) -> None:
        self.journal, self.vault = journal, vault

    def record(self, row: RuntimeExecutionORM) -> ExecutionRecord:
        if row.result is not None:
            record = ExecutionRecord.model_validate_json(row.result)
            if row.status == "reconciled":
                record.status = "reconciled"
            return record
        return ExecutionRecord(
            spec=ExecutionSpec.model_validate_json(row.payload),
            epoch=row.epoch,
            status=(
                "running"
                if row.status == "running"
                else "unknown" if row.status == "unknown" else "reconciled" if row.status == "reconciled" else "pending"
            ),
        )

    async def submit(
        self, db: AsyncSession, epoch: int, spec: ExecutionSpec, capability: ToolCapability
    ) -> ExecutionRecord:
        row = await db.get(RuntimeExecutionORM, spec.id)
        if row is not None:
            saved = ExecutionSpec.model_validate_json(row.payload)
            if (
                (saved.node_id != spec.node_id and capability.scope != "core")
                or saved.source_id != spec.source_id
                or saved.task_id != spec.task_id
                or signature(saved) != signature(spec)
            ):
                raise JournalConflict("Execution identity was reused with different arguments or device.")
            if row.status == "running" and row.epoch != epoch:
                row.status = "unknown"
            if row.status in {"pending", "unknown"} and capability.retry in {"read_only", "idempotent"}:
                row.status, row.epoch = "pending", epoch
                row.result = None
                if capability.scope == "core":
                    row.node_id, row.payload = spec.node_id, spec.model_dump_json()
            elif row.status == "pending":
                # 还未开始的动作可以由新任期再次授予原设备。
                row.epoch = epoch
                if capability.scope == "core":
                    row.node_id, row.payload = spec.node_id, spec.model_dump_json()
            return self.record(row)
        previous = list(
            await db.scalars(select(RuntimeExecutionORM).where(RuntimeExecutionORM.status.in_(["running", "unknown"])))
        )
        if capability.retry == "verify" and any(
            ExecutionSpec.model_validate_json(item.payload).source_id == spec.source_id for item in previous
        ):
            raise JournalConflict(
                "An action for this source has an unknown outcome. Verify it before issuing another effect."
            )
        row = RuntimeExecutionORM(
            id=spec.id, node_id=spec.node_id, epoch=epoch, payload=spec.model_dump_json(), status="pending"
        )
        db.add(row)
        return self.record(row)

    async def inspect(self, id: str) -> ExecutionRecord:
        async with get_session() as db:
            row = await db.get(RuntimeExecutionORM, id)
            if row is None:
                raise ValueError("Execution was not submitted.")
            return self.record(row)

    async def poll(self, node_id: str, device_tools: set[str]) -> list[ExecutionRecord]:
        async with get_session() as db:
            rows = await db.scalars(
                select(RuntimeExecutionORM).where(
                    RuntimeExecutionORM.node_id == node_id, RuntimeExecutionORM.status == "pending"
                )
            )
            return [
                self.record(row)
                for row in rows
                if ExecutionSpec.model_validate_json(row.payload).call.name in device_tools
            ]

    async def start(self, node_id: str, id: str, epoch: int, *, validate_only: bool = False) -> ExecutionRecord:
        async with get_session() as db:
            row = await db.get(RuntimeExecutionORM, id)
            if row is None or row.node_id != node_id or row.epoch != epoch:
                raise JournalConflict("Execution does not belong to this device and epoch.")
        async with self.journal.execution_transaction(epoch) as db:
            row = await db.get(RuntimeExecutionORM, id)
            if row is None or row.node_id != node_id or row.epoch != epoch or row.status not in {"pending", "running"}:
                raise JournalConflict("Execution cannot be started or resumed.")
            if not validate_only:
                if row.status != "pending":
                    raise JournalConflict("Execution is already running; inspect its outcome before retrying.")
                row.status = "running"
            return self.record(row)

    async def report(self, node_id: str, record: ExecutionRecord) -> None:
        if record.status not in {"completed", "unknown"}:
            raise ValueError("A device can report only completed or unknown outcomes.")
        async with self.journal.truth_transaction() as db:
            row = await db.get(RuntimeExecutionORM, record.spec.id)
            if (
                row is None
                or row.node_id != node_id
                or row.epoch != record.epoch
                or row.payload != record.spec.model_dump_json()
            ):
                raise JournalConflict("Execution report does not match its saved grant.")
            if row.result is not None and row.status == "completed":
                if row.result != record.model_dump_json():
                    raise JournalConflict("Execution result is immutable once saved.")
                return
            if row.status not in {"running", "unknown"}:
                raise JournalConflict("Execution was not started by its device.")
            if record.result is not None:
                paths = {file.key: self.vault.materialize(file.reference).path for file in record.files}
                for reference in record.result.resources:
                    if reference.path not in paths:
                        raise ValueError("Execution result has no transferred resource copy.")
                if record.completed_at is None:
                    record.completed_at = datetime.now(timezone.utc)
            row.status, row.result = record.status, record.model_dump_json()
