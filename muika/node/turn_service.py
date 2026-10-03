"""将已生成结果、人格变化、任务和发件箱提交到同一事务。"""

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muika.core.agent.task_store import TaskRecord
from muika.database.orm_models import (
    AgentTaskORM,
    RuntimeInboxORM,
    RuntimeOutboxORM,
    RuntimeStateORM,
    RuntimeTurnORM,
)

from .journal import JournalConflict
from .memory_service import MemoryService
from .task_service import TaskService, relocate_task
from .turn_protocol import (
    CompleteTurn,
    GeneratedReply,
    LoadTurn,
    RuntimeSnapshot,
    SaveGeneration,
    TurnOperation,
    TurnRecord,
)


class TurnService:
    """调用方在外层校验租约并持有短事务。"""

    def __init__(self, memory: MemoryService, tasks: TaskService) -> None:
        self.memory, self.tasks = memory, tasks

    async def runtime(self, db: AsyncSession) -> RuntimeSnapshot:
        row = await db.get(RuntimeStateORM, 1)
        if row is not None:
            return RuntimeSnapshot.model_validate_json(row.payload)
        await self.memory.memory.load()
        snapshot = RuntimeSnapshot()
        if self.memory.memory.recent_turns:
            snapshot.last_interaction = self.memory.memory.recent_turns[-1].timestamp
        return snapshot

    async def execute(self, db: AsyncSession, owner: str, epoch: int, action: TurnOperation) -> TurnRecord:
        """重用持久结果；重传已完成回合不得产生第二组效果。"""
        claim = action.claim
        inbox = None
        if claim is not None:
            if action.turn_id != f"input:{claim.sequence}":
                raise JournalConflict("Turn identity does not match its input.")
            inbox = await db.get(RuntimeInboxORM, claim.sequence)
            if inbox is None or inbox.owner != owner or inbox.epoch != epoch:
                raise JournalConflict("Input is not claimed by this Core.")
            if claim.owner != owner or claim.epoch != epoch or inbox.payload != claim.message.model_dump_json():
                raise JournalConflict("Claim does not match the saved input.")
        row = await db.get(RuntimeTurnORM, action.turn_id)
        if row is None:
            row = RuntimeTurnORM(id=action.turn_id, generations="{}", completed=False)
            db.add(row)
        generations = {key: GeneratedReply.model_validate(value) for key, value in json.loads(row.generations).items()}
        if isinstance(action, LoadTurn):
            return TurnRecord(turn_id=action.turn_id, generations=generations, completed=row.completed)
        if isinstance(action, SaveGeneration):
            existing = generations.get(action.stage)
            if existing is not None and existing != action.generated:
                raise JournalConflict("Generated result is immutable once saved.")
            if row.completed:
                raise JournalConflict("Turn is already completed.")
            for reference in action.generated.resources:
                self.memory.vault.materialize(reference)
            generations[action.stage] = action.generated
            row.generations = json.dumps({key: value.model_dump(mode="json") for key, value in generations.items()})
        elif isinstance(action, CompleteTurn):
            digest = hashlib.sha256(
                json.dumps(
                    action.model_dump(mode="json", exclude={"claim"}), sort_keys=True, ensure_ascii=False
                ).encode()
            ).hexdigest()
            if row.completed:
                if row.commit_digest != digest:
                    raise JournalConflict("Turn was already committed with different effects.")
                return TurnRecord(turn_id=action.turn_id, generations=generations, completed=True)
            if inbox is not None and inbox.status == "processed":
                raise JournalConflict("Input has already been processed by another turn.")
            if len({reply.id for reply in action.replies}) != len(action.replies):
                raise JournalConflict("Reply IDs must be unique within a turn.")
            await self.memory.memory.load()
            for update in action.state_updates:
                await self.memory.memory.update_state(update)
            if action.content is not None:
                await self.memory.memory.add_context(
                    "muika",
                    action.content,
                    source=f"turn:{action.turn_id}:reply",
                    resources=[self.memory.vault.materialize(reference) for reference in action.resources],
                )
            for index, note in enumerate(action.notes):
                await self.memory.memory.add_material("note", note, source=f"turn:{action.turn_id}:note:{index}")
            for task in action.tasks:
                if await db.get(AgentTaskORM, task.id) is not None:
                    raise JournalConflict("Planned task identity is already in use.")
                paths = {file.key: self.memory.vault.materialize(file.reference).path for file in action.task_files}
                relocate_task(task, paths)
                await self.tasks.store.save(task)
                if task.intention_id:
                    intention = next(
                        (item for item in self.memory.memory.persistent.intentions if item.id == task.intention_id),
                        None,
                    )
                    if intention is None or intention.task_id or intention.status != "open":
                        raise JournalConflict("Intention is unavailable or already has an action.")
                    await self.memory.memory.link_intention(task.intention_id, task.id)
                await self.memory.memory.add_context(
                    "agent", f"Task {task.id} queued. Intent: {task.instruction}", source=f"task:{task.id}:intent"
                )
            for change in action.task_changes:
                existing_task = await db.get(AgentTaskORM, change.task.id)
                if existing_task is None or existing_task.revision != change.expected_revision:
                    raise JournalConflict("Task changed before its control was committed.")
                controlled = TaskRecord.model_validate_json(existing_task.payload)
                controlled.apply_control(change.task)
                await self.tasks.store.save(controlled)
            for reply in action.replies:
                for reference in reply.resources:
                    self.memory.vault.materialize(reference)
                existing = await db.scalar(select(RuntimeOutboxORM).where(RuntimeOutboxORM.message_id == reply.id))
                if existing is not None:
                    raise JournalConflict("Reply identity is already in use.")
                db.add(
                    RuntimeOutboxORM(message_id=reply.id, client_id=reply.client_id, payload=reply.model_dump_json())
                )
            if action.runtime is not None:
                await db.merge(RuntimeStateORM(id=1, payload=action.runtime.model_dump_json()))
            row.completed, row.commit_digest = True, digest
            if inbox is not None:
                inbox.status, inbox.commit_digest = "processed", digest
        return TurnRecord(turn_id=action.turn_id, generations=generations, completed=row.completed)
