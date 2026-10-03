"""在状态服务中执行现有记忆业务，不开放远程 SQL。"""

from muika.core.memory import MemoryManager

from .memory_protocol import (
    AddMaterial,
    DayMaterial,
    ForgetMemory,
    LinkIntention,
    LoadMemory,
    MarkConsidered,
    MemoryOperation,
    MemoryResult,
    MemoryTurn,
    MemoryView,
    NewSession,
    PendingDays,
    ReadSource,
    RecentDiaries,
    RecordTaskResult,
    SaveDream,
    SaveWorkingContext,
    SearchMemory,
    UpdateState,
)
from .resources import ResourceVault


class MemoryService:
    """把记忆的持久边界留在单一状态服务中。"""

    def __init__(self, vault: ResourceVault) -> None:
        self.memory = MemoryManager()
        self.vault = vault

    def view(self) -> MemoryView:
        return MemoryView(
            snapshot=self.memory.snapshot,
            facts=list(self.memory.facts.values()),
            turns=[
                MemoryTurn(
                    role=turn.role,
                    content=turn.content,
                    timestamp=turn.timestamp,
                    resources=[self.vault.preserve(resource) for resource in turn.resources],
                    id=turn.id,
                )
                for turn in self.memory.recent_turns
            ],
        )

    async def execute(self, action: MemoryOperation) -> MemoryResult:
        """执行显式业务操作，并返回更新后的工作视图。"""
        # 每个事务从已提交记录恢复，避免失败事务留下内存状态。
        await self.memory.load()
        result = MemoryResult(view=self.view())
        if isinstance(action, LoadMemory):
            pass
        elif isinstance(action, AddMaterial):
            result.material_id = await self.memory.add_material(
                action.kind,
                action.content,
                timestamp=action.timestamp,
                source=action.source,
                resources=[self.vault.materialize(resource) for resource in action.resources],
            )
        elif isinstance(action, NewSession):
            await self.memory.new_session()
        elif isinstance(action, UpdateState):
            await self.memory.update_state(action.update)
        elif isinstance(action, MarkConsidered):
            await self.memory.mark_considered()
        elif isinstance(action, LinkIntention):
            await self.memory.link_intention(action.intention_id, action.task_id)
        elif isinstance(action, RecordTaskResult):
            await self.memory.record_task_result(action.task_id, action.status)
        elif isinstance(action, ForgetMemory):
            await self.memory.forget_memory(action.category, action.key)
        elif isinstance(action, PendingDays):
            result.days = await self.memory.pending_days(action.now, include_today=action.include_today)
        elif isinstance(action, DayMaterial):
            result.materials = await self.memory.day_material(action.day)
        elif isinstance(action, RecentDiaries):
            result.diaries = await self.memory.recent_diaries(action.before, action.limit)
        elif isinstance(action, SaveDream):
            result.saved = await self.memory.save_dream(action.day, action.result, action.through, action.allowed_refs)
        elif isinstance(action, SearchMemory):
            result.hits = await self.memory.search(action.query, limit=action.limit)
        elif isinstance(action, ReadSource):
            result.text = await self.memory.read_source(action.ref, offset=action.offset, limit=action.limit)
        elif isinstance(action, SaveWorkingContext):
            await self.memory.save_working_context(action.session_id, action.summary, action.through)
        result.view = self.view()
        return result
