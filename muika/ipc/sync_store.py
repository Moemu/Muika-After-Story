"""保存活动增量，在本地事务中应用历史，不重放模型或外部动作。"""

import asyncio
import json
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import TypeAlias

from pydantic import TypeAdapter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from muika.core.agent.task_store import CallRecord, TaskRecord
from muika.core.events import TimeTickEvent
from muika.core.memory_models import MemorySnapshot
from muika.core.memory_rows import diary_from_row, experience_from_row, fact_from_row
from muika.core.scheduler import Reminder
from muika.core.state import MuikaState, StateRhythm
from muika.database.db import get_session
from muika.database.orm_models import (
    AgentCallORM,
    AgentTaskORM,
    Base,
    DiaryORM,
    ExperienceORM,
    FactORM,
    FactRecallORM,
    MemoryRuntimeORM,
    SyncEventORM,
    SyncReferenceORM,
    SyncStateORM,
)
from muika.llm._schema import ToolResult

from .sync_models import (
    Activity,
    Attachment,
    RecordedExperience,
    RecordedFact,
    RecordedRecall,
    SyncEntry,
)

TemporalData: TypeAlias = dict[str, "TemporalData"] | list["TemporalData"] | str | int | float | bool | date | None


def _dates(value: TemporalData, *, wire: bool) -> TemporalData:
    """跨设备日期带时区，本地认知继续使用本地时间。"""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if wire else value.astimezone().replace(tzinfo=None)
    if isinstance(value, dict):
        return {key: _dates(item, wire=wire) for key, item in value.items()}
    if isinstance(value, list):
        return [_dates(item, wire=wire) for item in value]
    return value


class SyncStore:
    """当前 Core 的活动日志，所有编号映射均留在本地。"""

    def __init__(self, origin: str) -> None:
        self.origin = origin
        self.changed = asyncio.Event()
        self.state: MuikaState | None = None

    async def initialize(self) -> None:
        """首次启用时保存已有记忆基线，后续启动沿用日志。"""
        async with get_session(record_activity=False) as db:
            saved = await db.scalar(select(SyncEventORM.payload).order_by(SyncEventORM.sequence.desc()).limit(1))
            if saved is not None:
                activity = Activity.model_validate(_dates(Activity.model_validate_json(saved).model_dump(), wire=False))
                if self.state is not None and activity.rhythm is not None:
                    self.state.restore_rhythm(activity.rhythm)
                    self.state.tick_state(TimeTickEvent(), (datetime.now() - activity.timestamp).total_seconds())
                return
            rows: list[Base] = []
            for model in (
                ExperienceORM,
                FactORM,
                DiaryORM,
                MemoryRuntimeORM,
                AgentTaskORM,
                AgentCallORM,
                FactRecallORM,
                SyncStateORM,
            ):
                records = list(await db.scalars(select(model)))
                if model is ExperienceORM:
                    for offset in range(0, len(records), 128):
                        await self.record(db, records[offset : offset + 128])
                else:
                    rows.extend(records)
            await self.record(db, rows)

    async def _source(self, db: AsyncSession, kind: str, local_id: int) -> str:
        source = await db.scalar(
            select(SyncReferenceORM.source).where(
                SyncReferenceORM.local_id == local_id, SyncReferenceORM.source.like(f"%:{kind}:%")
            )
        )
        if source is None:
            source = f"{self.origin}:{kind}:{local_id}"
            db.add(SyncReferenceORM(source=source, local_id=local_id))
        return source

    async def record(self, db: AsyncSession, rows: list[Base]) -> Callable[[], None] | None:
        """将业务提交产生的结果与日志共同保存；其他本地表不复制。"""
        activity = Activity(origin=self.origin)
        for row in rows:
            if isinstance(row, MemoryRuntimeORM):
                activity.snapshot = MemorySnapshot.model_validate_json(row.payload)
            elif isinstance(row, ExperienceORM):
                activity.experiences.append(
                    RecordedExperience(
                        **experience_from_row(row).model_dump(),
                        resources=TypeAdapter(list[Attachment]).validate_json(row.resources),
                    )
                )
                await self._source(db, "experience", row.id)
            elif isinstance(row, FactORM):
                activity.facts.append(
                    RecordedFact(
                        **fact_from_row(row).model_dump(),
                        active=row.active,
                        forgotten=row.forgotten,
                        created_at=row.created_at,
                    )
                )
                await self._source(db, "fact", row.id)
            elif isinstance(row, DiaryORM):
                activity.diaries.append(diary_from_row(row))
                await self._source(db, "diary", row.id)
            elif isinstance(row, FactRecallORM):
                activity.recalls.append(RecordedRecall(fact_id=row.fact_id, day=row.day))
            elif isinstance(row, AgentTaskORM):
                activity.tasks.append(TaskRecord.model_validate_json(row.payload))
            elif isinstance(row, AgentCallORM):
                activity.calls.append(CallRecord.model_validate_json(row.payload))
            elif isinstance(row, SyncStateORM) and row.key.startswith("reminder:"):
                activity.reminders.append(Reminder.model_validate_json(row.payload))

        if any(
            (
                activity.snapshot,
                activity.experiences,
                activity.facts,
                activity.diaries,
                activity.tasks,
                activity.calls,
                activity.recalls,
                activity.reminders,
            )
        ):
            used = {ref for fact in activity.facts for ref in fact.source_refs}
            used.update(f"fact:{fact.id}" for fact in activity.facts)
            used.update(f"fact:{recall.fact_id}" for recall in activity.recalls)
            used.update(ref for diary in activity.diaries for ref in diary.source_refs)
            used.update(f"diary:{diary.id}" for diary in activity.diaries)
            used.update(f"experience:{diary.covered_through}" for diary in activity.diaries)
            if activity.snapshot is not None:
                used.add(f"experience:{activity.snapshot.summary_through}")
                used.update(ref for intention in activity.snapshot.state.intentions for ref in intention.source_refs)
            for ref in used:
                kind, separator, value = ref.partition(":")
                if separator and value.isdigit() and int(value):
                    source = await db.scalar(
                        select(SyncReferenceORM.source).where(
                            SyncReferenceORM.local_id == int(value), SyncReferenceORM.source.like(f"%:{kind}:%")
                        )
                    )
                    if source is None:
                        raise ValueError(f"Activity references unrecorded history: {ref}")
                    activity.references[ref] = source
            if self.state is not None:
                activity.rhythm = StateRhythm.model_validate(self.state, from_attributes=True)
            activity = Activity.model_validate(_dates(activity.model_dump(mode="python"), wire=True))
            db.add(SyncEventORM(id=activity.id, origin=self.origin, payload=activity.model_dump_json()))
            return self.changed.set
        return None

    async def capture_snapshot(self) -> None:
        """重连后记录前台节点保留的持续状态。"""
        async with get_session(record_activity=False) as db:
            snapshot = await db.get(MemoryRuntimeORM, 1)
            if snapshot is not None:
                await self.record(db, [snapshot])
        self.changed.set()

    async def cursor(self) -> int:
        async with get_session(record_activity=False) as db:
            return await db.scalar(select(func.max(SyncEventORM.gateway_sequence))) or 0

    async def entries(self, after: int = 0, *, pending_only: bool = False) -> list[SyncEntry]:
        """读取增量或尚未纳入共同日志的本地活动。"""
        async with get_session(record_activity=False) as db:
            query = select(SyncEventORM).where(SyncEventORM.sequence > after).order_by(SyncEventORM.sequence)
            if pending_only:
                query = query.where(SyncEventORM.gateway_sequence.is_(None), SyncEventORM.origin == self.origin)
            return [
                SyncEntry(
                    sequence=row.sequence,
                    gateway_sequence=row.gateway_sequence,
                    activity=Activity.model_validate_json(row.payload),
                )
                for row in await db.scalars(query)
            ]

    async def apply(self, entry: SyncEntry, *, preserve_state: bool = False) -> bool:
        """历史只补齐数据；重复事件不会再次产生经历或副作用。"""
        activity = Activity.model_validate(_dates(entry.activity.model_dump(mode="python"), wire=False))
        async with get_session(record_activity=False) as db:
            existing = await db.scalar(select(SyncEventORM).where(SyncEventORM.id == activity.id))
            if existing is not None:
                if entry.gateway_sequence is not None:
                    existing.gateway_sequence = entry.gateway_sequence
                return False

            async def reference(kind: str, value: int) -> int:
                if not value:
                    return 0
                source = activity.references.get(f"{kind}:{value}", f"{activity.origin}:{kind}:{value}")
                mapped = await db.get(SyncReferenceORM, source)
                if mapped is None:
                    raise ValueError(f"Missing synchronized reference: {activity.origin}:{kind}:{value}")
                return mapped.local_id

            async def refs(values: list[str]) -> list[str]:
                result = []
                for value in values:
                    kind, separator, number = value.partition(":")
                    result.append(
                        f"{kind}:{await reference(kind, int(number))}"
                        if separator and number.isdigit() and kind in {"experience", "fact", "diary"}
                        else value
                    )
                return result

            merged_sessions: set[str] = set()
            for experience in sorted(activity.experiences, key=lambda value: value.id):
                experience_row = (
                    await db.scalar(select(ExperienceORM).where(ExperienceORM.source == experience.source))
                    if experience.source
                    else None
                )
                if experience_row is None:
                    experience_row = ExperienceORM(
                        session_id=experience.session_id,
                        kind=experience.kind,
                        content=experience.content,
                        occurred_at=experience.occurred_at.isoformat(),
                        source=experience.source or f"sync:{activity.origin}:experience:{experience.id}",
                        resources=json.dumps([resource.model_dump() for resource in experience.resources]),
                    )
                    db.add(experience_row)
                elif experience_row.session_id != experience.session_id:
                    merged_sessions.add(experience_row.session_id)
                await db.flush()
                db.add(
                    SyncReferenceORM(source=f"{activity.origin}:experience:{experience.id}", local_id=experience_row.id)
                )
            for fact in activity.facts:
                mapped = await db.get(
                    SyncReferenceORM, activity.references.get(f"fact:{fact.id}", f"{activity.origin}:fact:{fact.id}")
                )
                fact_row = await db.get(FactORM, mapped.local_id) if mapped else None
                if fact_row is None:
                    fact_row = FactORM(category=fact.category.value, key=fact.key, value=fact.value)
                    db.add(fact_row)
                fact_row.category, fact_row.key, fact_row.value = fact.category.value, fact.key, fact.value
                fact_row.active, fact_row.forgotten, fact_row.weight = fact.active, fact.forgotten, fact.weight
                fact_row.weight_at, fact_row.observed_at = fact.weight_at.isoformat(), fact.observed_at.isoformat()
                fact_row.last_recalled_at, fact_row.created_at = (
                    fact.last_recalled_at.isoformat(),
                    fact.created_at.isoformat(),
                )
                fact_row.source_refs = "[]"
                await db.flush()
                if fact.active:
                    alternatives = list(
                        await db.scalars(
                            select(FactORM).where(
                                FactORM.category == fact.category.value,
                                FactORM.key == fact.key,
                                FactORM.active.is_(True),
                                FactORM.id != fact_row.id,
                            )
                        )
                    )
                    if preserve_state and alternatives:
                        fact_row.active = False
                    else:
                        for alternative in alternatives:
                            alternative.active = False
                await db.merge(SyncReferenceORM(source=f"{activity.origin}:fact:{fact.id}", local_id=fact_row.id))
            for diary in activity.diaries:
                mapped = await db.get(
                    SyncReferenceORM,
                    activity.references.get(f"diary:{diary.id}", f"{activity.origin}:diary:{diary.id}"),
                )
                diary_row = await db.get(DiaryORM, mapped.local_id) if mapped else None
                if diary_row is None:
                    source = diary.source
                    if await db.scalar(select(DiaryORM.id).where(DiaryORM.source == source)) is not None:
                        source = f"{activity.origin}:{source}"
                    diary_row = DiaryORM(
                        source=source, day=diary.day.isoformat(), created_at=diary.created_at.isoformat()
                    )
                    db.add(diary_row)
                diary_row.content = diary.content
                diary_row.covered_through = await reference("experience", diary.covered_through)
                diary_row.source_refs = json.dumps(await refs(diary.source_refs))
                await db.flush()
                await db.merge(SyncReferenceORM(source=f"{activity.origin}:diary:{diary.id}", local_id=diary_row.id))
            for fact in activity.facts:
                fact_row = await db.get(FactORM, await reference("fact", fact.id))
                if fact_row is not None:
                    fact_row.source_refs = json.dumps(await refs(fact.source_refs))
            for recall in activity.recalls:
                fact_id = await reference("fact", recall.fact_id)
                if (
                    await db.scalar(
                        select(FactRecallORM.id).where(
                            FactRecallORM.fact_id == fact_id, FactRecallORM.day == recall.day.isoformat()
                        )
                    )
                    is None
                ):
                    db.add(FactRecallORM(fact_id=fact_id, day=recall.day.isoformat()))
            if activity.snapshot is not None and not preserve_state:
                snapshot = activity.snapshot.model_copy(deep=True)
                saved = await db.get(MemoryRuntimeORM, 1)
                if saved is not None:
                    previous = MemorySnapshot.model_validate_json(saved.payload)
                    if previous.session.session_id == snapshot.session.session_id:
                        merged_sessions.update(previous.resume_sessions)
                snapshot.resume_sessions = sorted(set(snapshot.resume_sessions) | merged_sessions)
                snapshot.summary_through = await reference("experience", snapshot.summary_through)
                if (
                    snapshot.summary_through
                    and await db.scalar(
                        select(ExperienceORM.id)
                        .where(
                            ExperienceORM.id <= snapshot.summary_through,
                            ExperienceORM.session_id.in_([snapshot.session.session_id, *snapshot.resume_sessions]),
                            ExperienceORM.kind.in_(["user", "muika", "agent"]),
                            ExperienceORM.id.not_in(
                                select(SyncReferenceORM.local_id).where(
                                    SyncReferenceORM.source.like(f"{activity.origin}:experience:%")
                                )
                            ),
                        )
                        .limit(1)
                    )
                    is not None
                ):
                    snapshot.summary_through, snapshot.working_summary = 0, ""
                for intention in snapshot.state.intentions:
                    intention.source_refs = await refs(intention.source_refs)
                await db.merge(MemoryRuntimeORM(id=1, payload=snapshot.model_dump_json()))
            elif preserve_state and activity.experiences:
                saved = await db.get(MemoryRuntimeORM, 1)
                if saved is not None:
                    foreground = MemorySnapshot.model_validate_json(saved.payload)
                    foreground.resume_sessions = sorted(
                        set(foreground.resume_sessions)
                        | {
                            item.session_id
                            for item in activity.experiences
                            if item.kind in {"user", "muika", "agent"}
                            and item.session_id != foreground.session.session_id
                        }
                    )
                    saved.payload = foreground.model_dump_json()
            for task in activity.tasks:
                task.handoff = False
                if task.status in {"queued", "running", "recovering"}:
                    task.status, task.error = (
                        "failed",
                        f"Execution interrupted on {activity.origin}; inspect its local results.",
                    )
                await db.merge(
                    AgentTaskORM(
                        id=task.id,
                        status=task.status,
                        revision=task.revision,
                        created_at=task.created_at,
                        updated_at=task.updated_at,
                        payload=task.model_dump_json(),
                    )
                )
            for call in activity.calls:
                if call.status == "pending":
                    call.status = "completed"
                    call.completed_at = datetime.now()
                    call.result = ToolResult(
                        text=f"Execution interrupted on {activity.origin}. "
                        "Inspect the original device for side effects.",
                        is_error=True,
                    )
                await db.merge(
                    AgentCallORM(id=call.id, task_id=call.task_id, status=call.status, payload=call.model_dump_json())
                )
            for reminder in activity.reminders:
                await db.merge(SyncStateORM(key="reminder:" + reminder.id, payload=reminder.model_dump_json()))
            db.add(
                SyncEventORM(
                    id=activity.id,
                    origin=activity.origin,
                    gateway_sequence=entry.gateway_sequence,
                    payload=entry.activity.model_dump_json(),
                )
            )
        if self.state is not None and activity.rhythm is not None and not preserve_state:
            self.state.restore_rhythm(activity.rhythm)
            self.state.tick_state(TimeTickEvent(), (datetime.now() - activity.timestamp).total_seconds())
        return True
