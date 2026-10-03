"""复用记忆工作视图和上下文算法，将持久业务提交给状态服务。"""

from collections import deque
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from muika.core.memory import MemoryManager
from muika.core.memory_models import (
    Diary,
    DreamResult,
    Experience,
    MemoryCategory,
    MemoryQuery,
    RecallHit,
    SessionTurn,
    StateUpdate,
)
from muika.ipc.node_client import NodeClient
from muika.ipc.node_protocol import MemoryRequest, NodeResponse, TurnRequest
from muika.models import Resource

from .memory_protocol import (
    AddMaterial,
    DayMaterial,
    ForgetMemory,
    LinkIntention,
    LoadMemory,
    MarkConsidered,
    MemoryOperation,
    MemoryResult,
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


class RemoteMemoryManager(MemoryManager):
    """与单机记忆提供同一业务接口，不在候选节点创建人格数据库。"""

    def __init__(self, client: NodeClient, epoch: int, directory: Path) -> None:
        super().__init__()
        self.client = client
        self.epoch = epoch
        self.vault = ResourceVault(directory)

    async def apply_view(self, view: MemoryView) -> None:
        """仅在资源准备完成后替换本节点的工作视图。"""
        turns = []
        for turn in view.turns:
            resources = [await self.client.download_resource(reference, self.vault) for reference in turn.resources]
            turns.append(SessionTurn(turn.role, turn.content, turn.timestamp, resources, turn.id))
        self.snapshot = view.snapshot
        self.facts = {fact.id: fact for fact in view.facts}
        self.recent_turns = deque(turns)

    async def commit_turn(self, request: TurnRequest) -> NodeResponse:
        """串行替换回复提交后的记忆视图，避免后台整理覆盖新回合。"""
        async with self._lock:
            response = await self.client.request(request)
            if response.memory is not None:
                await self.apply_view(response.memory.view)
            return response

    async def request(self, action: MemoryOperation) -> MemoryResult:
        async with self._lock:
            response = await self.client.request(MemoryRequest(epoch=self.epoch, body=action))
            if response.memory is None:
                raise ConnectionError("State service omitted its memory result.")
            await self.apply_view(response.memory.view)
            return response.memory

    async def load(self) -> None:
        await self.request(LoadMemory())

    async def add_material(
        self,
        kind: Literal["user", "muika", "agent", "note", "state", "legacy"],
        content: str,
        *,
        timestamp: datetime | None = None,
        resources: list[Resource] | None = None,
        source: str | None = None,
    ) -> int:
        references = [await self.client.upload_resource(resource, self.vault) for resource in resources or []]
        result = await self.request(
            AddMaterial(
                kind=kind,
                content=content,
                timestamp=timestamp,
                resources=references,
                source=source,
            )
        )
        if result.material_id is None:
            raise ConnectionError("State service omitted its material identity.")
        return result.material_id

    async def new_session(self) -> None:
        await self.request(NewSession())

    async def update_state(self, update: StateUpdate) -> None:
        await self.request(UpdateState(update=update))

    async def mark_considered(self) -> None:
        await self.request(MarkConsidered())

    async def link_intention(self, intention_id: str, task_id: str) -> None:
        await self.request(LinkIntention(intention_id=intention_id, task_id=task_id))

    async def record_task_result(self, task_id: str, status: str) -> None:
        await self.request(RecordTaskResult(task_id=task_id, status=status))

    async def forget_memory(self, category: MemoryCategory, key: str) -> None:
        await self.request(ForgetMemory(category=category, key=key))

    async def pending_days(self, now: datetime, *, include_today: bool = False) -> list[date]:
        return (await self.request(PendingDays(now=now, include_today=include_today))).days

    async def day_material(self, day: date) -> list[Experience]:
        return (await self.request(DayMaterial(day=day))).materials

    async def recent_diaries(self, before: date, limit: int = 5) -> list[Diary]:
        return (await self.request(RecentDiaries(before=before, limit=limit))).diaries

    async def save_dream(self, day: date, result: DreamResult, through: int, allowed_refs: set[str]) -> bool:
        return (
            await self.request(SaveDream(day=day, result=result, through=through, allowed_refs=allowed_refs))
        ).saved is True

    async def search(self, query: MemoryQuery, *, limit: int = 30) -> list[RecallHit]:
        return (await self.request(SearchMemory(query=query, limit=limit))).hits

    async def read_source(self, ref: str, *, offset: int = 0, limit: int = 6000) -> str:
        return (await self.request(ReadSource(ref=ref, offset=offset, limit=limit))).text

    async def save_working_context(self, session_id: str, summary: str, through: int) -> None:
        await self.request(SaveWorkingContext(session_id=session_id, summary=summary, through=through))
