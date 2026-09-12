"""持久素材、事实账本、日记与持续状态。"""

from __future__ import annotations

import asyncio
import json
import re
import warnings
from collections import deque
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from muika.config import mas_config
from muika.database.db import get_session
from muika.database.orm_models import (
    ArchiveRecordORM,
    DiaryORM,
    ExperienceORM,
    FactORM,
    FactRecallORM,
    MemoryRecordORM,
    MemoryRuntimeORM,
)
from muika.llm._config import ModelConfig
from muika.llm._schema import ModelRequest, ToolResult
from muika.llm.context import (
    ContextCompactor,
    ContextOverflowWarning,
    estimate_tokens,
    input_budget,
    public_text,
    request_tokens,
)
from muika.models import Resource

from .memory_models import (
    Diary,
    DreamResult,
    Experience,
    Fact,
    MemoryCategory,
    MemoryQuery,
    MemorySnapshot,
    PersistentState,
    RecallHit,
    RecallResult,
    SessionState,
    SessionTurn,
    StateUpdate,
)

__all__ = [
    "MemoryManager",
    "MemoryCategory",
    "Fact",
    "Diary",
    "SessionTurn",
    "SessionState",
    "Experience",
    "DreamResult",
    "StateUpdate",
    "PersistentState",
    "RecallHit",
    "RecallResult",
    "MemoryQuery",
]


def _local(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    return stamp.astimezone().replace(tzinfo=None) if stamp.tzinfo else stamp


def _fact(row: FactORM) -> Fact:
    return Fact(
        id=row.id,
        category=MemoryCategory(row.category),
        key=row.key,
        value=row.value,
        source_refs=json.loads(row.source_refs),
        weight=row.weight,
        weight_at=_local(row.weight_at),
        observed_at=_local(row.observed_at),
        last_recalled_at=_local(row.last_recalled_at),
    )


def _experience(row: ExperienceORM) -> Experience:
    return Experience(
        id=row.id,
        session_id=row.session_id,
        kind=row.kind,
        content=row.content,
        occurred_at=_local(row.occurred_at),
        source=row.source,
    )


def _diary(row: DiaryORM) -> Diary:
    return Diary(
        id=row.id,
        day=date.fromisoformat(row.day),
        content=row.content,
        source=row.source,
        source_refs=json.loads(row.source_refs),
        covered_through=row.covered_through,
        created_at=_local(row.created_at),
    )


class MemoryManager:
    """协调记忆事务和有预算的工作视图。"""

    def __init__(self) -> None:
        self.recent_turns: deque[SessionTurn] = deque()
        self.facts: dict[int, Fact] = {}
        self.snapshot = MemorySnapshot()
        self._lock = asyncio.Lock()
        self._context_lock = asyncio.Lock()

    @property
    def session(self) -> SessionState:
        return self.snapshot.session

    @property
    def persistent(self) -> PersistentState:
        return self.snapshot.state

    @property
    def has_history(self) -> bool:
        return self.snapshot.first_interaction_at is not None or bool(self.facts)

    async def _save_snapshot(self, db: AsyncSession, snapshot: MemorySnapshot) -> None:
        await db.merge(MemoryRuntimeORM(id=1, payload=snapshot.model_dump_json()))

    async def load(self) -> None:
        """恢复记忆；数据库失败时不替换当前状态。"""
        async with self._lock:
            async with get_session() as db:
                saved = await db.get(MemoryRuntimeORM, 1)
                snapshot = MemorySnapshot.model_validate_json(saved.payload) if saved else MemorySnapshot()
                if not snapshot.legacy_imported:
                    await self._import_legacy(db, snapshot)
                facts = {
                    row.id: _fact(row) for row in await db.scalars(select(FactORM).where(FactORM.active.is_(True)))
                }
                rows = list(
                    await db.scalars(
                        select(ExperienceORM)
                        .where(
                            ExperienceORM.session_id == snapshot.session.session_id,
                            ExperienceORM.id > snapshot.summary_through,
                            ExperienceORM.kind.in_(["user", "muika", "agent"]),
                        )
                        .order_by(ExperienceORM.id)
                    )
                )
                snapshot.session.is_first_session = snapshot.first_interaction_at is None and not facts
                await self._save_snapshot(db, snapshot)
            self.snapshot, self.facts = snapshot, facts
            self.recent_turns = deque(self._turn(row) for row in rows)

    async def _import_legacy(self, db: AsyncSession, snapshot: MemorySnapshot) -> None:
        now = datetime.now().isoformat()
        for row in await db.scalars(select(MemoryRecordORM).order_by(MemoryRecordORM.updated_at, MemoryRecordORM.id)):
            if row.key in {"first_conversation_time", "self_reflection_last_at"}:
                try:
                    if row.key == "first_conversation_time":
                        snapshot.first_interaction_at = _local(row.value)
                    else:
                        snapshot.last_dream_at = _local(row.value)
                except ValueError:
                    pass
                continue
            material = ExperienceORM(
                session_id="legacy",
                kind="legacy",
                content=row.value,
                occurred_at=_local(row.updated_at).isoformat(),
                source=f"legacy_memory:{row.id}",
                resources="[]",
            )
            db.add(material)
            await db.flush()
            if row.layer not in {"core", "preference"}:
                continue
            existing = await db.scalar(
                select(FactORM).where(
                    FactORM.category == row.category, FactORM.key == row.key, FactORM.active.is_(True)
                )
            )
            if existing is not None:
                existing.active = False
            db.add(
                FactORM(
                    category=row.category,
                    key=row.key,
                    value=row.value,
                    active=True,
                    forgotten=False,
                    source_refs=json.dumps([f"experience:{material.id}"]),
                    weight=1.0,
                    weight_at=now,
                    observed_at=_local(row.updated_at).isoformat(),
                    last_recalled_at=_local(row.updated_at).isoformat(),
                    created_at=row.created_at,
                )
            )
        for row in await db.scalars(select(ArchiveRecordORM).order_by(ArchiveRecordORM.period_start)):
            db.add(
                DiaryORM(
                    source=f"legacy_archive:{row.id}",
                    day=row.period_start[:10],
                    content=row.summary,
                    source_refs="[]",
                    covered_through=0,
                    created_at=row.created_at,
                )
            )
            if snapshot.first_interaction_at is None:
                snapshot.first_interaction_at = _local(row.period_start)
        snapshot.legacy_imported = True

    @staticmethod
    def _turn(row: ExperienceORM) -> SessionTurn:
        role: Literal["user", "muika", "agent"] = (
            "user" if row.kind == "user" else "muika" if row.kind == "muika" else "agent"
        )
        return SessionTurn(
            role, row.content, _local(row.occurred_at), [Resource(**item) for item in json.loads(row.resources)], row.id
        )

    def _preserve_resources(self, resources: list[Resource]) -> list[Resource]:
        saved = []
        for resource in resources:
            directory = mas_config.data_dir.resolve() / "memory_resources"
            directory.mkdir(parents=True, exist_ok=True)
            if resource.path:
                path = Path(resource.path).resolve()
                content = path.read_bytes()
            elif isinstance(resource.raw, bytes):
                content = resource.raw
            elif resource.raw is not None:
                content = resource.raw.getvalue()
            else:
                raise ValueError("A memory resource needs local content before recording")
            target = directory / (uuid4().hex + (resource.extension or ".bin"))
            target.write_bytes(content)
            saved.append(Resource(type=resource.type, path=str(target), mimetype=resource.mimetype))
        return saved

    async def add_context(
        self,
        role: Literal["user", "muika", "agent"],
        content: str,
        resources: list[Resource] | None = None,
        *,
        timestamp: datetime | None = None,
        source: str | None = None,
    ) -> int:
        """落库后添加工作上下文，不按条数删除原文。"""
        return await self.add_material(role, content, timestamp=timestamp, resources=resources, source=source)

    async def add_material(
        self,
        kind: Literal["user", "muika", "agent", "note", "state", "legacy"],
        content: str,
        *,
        timestamp: datetime | None = None,
        resources: list[Resource] | None = None,
        source: str | None = None,
    ) -> int:
        """追加素材；来源标识防止事件重投产生重复记录。"""
        now = _local((timestamp or datetime.now()).isoformat())
        if kind in {"muika", "agent", "note"}:
            content = public_text(content)
        async with self._lock:
            snapshot = self.snapshot.model_copy(deep=True)
            async with get_session() as db:
                if source:
                    existing = await db.scalar(select(ExperienceORM).where(ExperienceORM.source == source))
                    if existing is not None:
                        return existing.id
                refs = self._preserve_resources(resources or [])
                row = ExperienceORM(
                    session_id=self.session.session_id,
                    kind=kind,
                    content=content,
                    occurred_at=now.isoformat(),
                    resources=json.dumps([r.to_dict() for r in refs]),
                    source=source,
                )
                db.add(row)
                await db.flush()
                if kind in {"user", "muika"} and snapshot.first_interaction_at is None:
                    snapshot.first_interaction_at = now
                await self._save_snapshot(db, snapshot)
                turn = self._turn(row) if kind in {"user", "muika", "agent"} else None
                row_id = row.id
            self.snapshot = snapshot
            if turn is not None:
                self.recent_turns.append(turn)
            return row_id

    async def new_session(self) -> None:
        """开始新会话，保留经历和持续状态。"""
        async with self._lock:
            snapshot = self.snapshot.model_copy(deep=True)
            snapshot.session = SessionState(is_first_session=not self.has_history)
            snapshot.working_summary, snapshot.summary_through = "", 0
            async with get_session() as db:
                await self._save_snapshot(db, snapshot)
            self.snapshot = snapshot
            self.recent_turns.clear()

    def get_memory_prompt(self, budget: int = 2048) -> str:
        """按回顾权重选择预算内最多二十条原子事实。"""
        now = datetime.now()
        records = sorted(
            self.facts.values(), key=lambda fact: (fact.score(now), fact.last_recalled_at, -fact.id), reverse=True
        )
        lines: list[str] = []
        for record in records:
            line = record.describe()
            if estimate_tokens("\n".join(lines + [line])) > budget:
                continue
            lines.append(line)
            if len(lines) == 20:
                break
        return "\n".join(lines)

    @staticmethod
    def _apply_state(
        state: PersistentState, update: StateUpdate, now: datetime, cutoff: datetime | None = None
    ) -> None:
        if update.mood is not None and (
            cutoff is None or state.mood_updated_at is None or state.mood_updated_at <= cutoff
        ):
            state.mood, state.reason, state.mood_updated_at = update.mood, update.reason, now
        by_id = {item.id: item for item in state.intentions}
        for change in update.intentions:
            previous = by_id.get(change.id)
            if previous is not None and cutoff is not None and previous.updated_at > cutoff:
                continue
            item = change.model_copy(deep=True)
            item.updated_at = now
            item.task_id = previous.task_id if previous else None
            if item.status in {"acting", "awaiting_feedback"}:
                item.status = previous.status if previous else "open"
            by_id[item.id] = item
        state.intentions = list(by_id.values())

    async def update_state(self, update: StateUpdate) -> None:
        """原子保存白天情绪及素材，保留真实任务关联。"""
        async with self._lock:
            snapshot = self.snapshot.model_copy(deep=True)
            now = datetime.now()
            self._apply_state(snapshot.state, update, now)
            async with get_session() as db:
                db.add(
                    ExperienceORM(
                        session_id=self.session.session_id,
                        kind="state",
                        content=update.model_dump_json(),
                        occurred_at=now.isoformat(),
                        resources="[]",
                    )
                )
                await self._save_snapshot(db, snapshot)
            self.snapshot = snapshot

    async def mark_considered(self) -> None:
        async with self._lock:
            snapshot = self.snapshot.model_copy(deep=True)
            snapshot.state.last_considered_at = datetime.now()
            async with get_session() as db:
                await self._save_snapshot(db, snapshot)
            self.snapshot = snapshot

    async def link_intention(self, intention_id: str, task_id: str) -> None:
        """记录真实任务关联，防止同一意愿再次派发。"""
        async with self._lock:
            snapshot = self.snapshot.model_copy(deep=True)
            item = next((i for i in snapshot.state.intentions if i.id == intention_id), None)
            if item is not None and item.task_id == task_id:
                return
            if item is None or item.task_id is not None or item.status != "open":
                raise ValueError("The intention is missing, resolved or already linked to a task")
            item.task_id, item.status, item.updated_at = task_id, "acting", datetime.now()
            async with get_session() as db:
                await self._save_snapshot(db, snapshot)
            self.snapshot = snapshot

    async def record_task_result(self, task_id: str, status: str) -> None:
        async with self._lock:
            snapshot = self.snapshot.model_copy(deep=True)
            for item in snapshot.state.intentions:
                if item.task_id == task_id and item.status not in {"resolved", "abandoned"}:
                    item.status = "awaiting_feedback" if status == "completed" else "open"
                    item.updated_at = datetime.now()
            async with get_session() as db:
                await self._save_snapshot(db, snapshot)
            self.snapshot = snapshot

    async def forget_memory(self, category: MemoryCategory, key: str) -> None:
        """撤下事实并保留遗忘标记，避免自动重新导入。"""
        async with self._lock:
            async with get_session() as db:
                rows = list(
                    await db.scalars(select(FactORM).where(FactORM.category == category.value, FactORM.key == key))
                )
                for row in rows:
                    row.active, row.forgotten = False, True
                ids = {row.id for row in rows}
            self.facts = {key: fact for key, fact in self.facts.items() if key not in ids}

    async def pending_days(self, now: datetime, *, include_today: bool = False) -> list[date]:
        cutoff = now.date() if include_today or now.hour >= 5 else now.date() - timedelta(days=1)
        async with get_session() as db:
            days = (
                await db.execute(
                    select(func.substr(ExperienceORM.occurred_at, 1, 10), func.max(ExperienceORM.id))
                    .where(ExperienceORM.kind != "legacy")
                    .group_by(func.substr(ExperienceORM.occurred_at, 1, 10))
                )
            ).all()
            diaries = {
                row.day: row.covered_through
                for row in await db.scalars(select(DiaryORM).where(DiaryORM.source.like("dream:%")))
            }
        return sorted(
            date.fromisoformat(day)
            for day, latest in days
            if (date.fromisoformat(day) <= cutoff if include_today else date.fromisoformat(day) < cutoff)
            and latest > diaries.get(day, 0)
        )

    async def day_material(self, day: date) -> list[Experience]:
        async with get_session() as db:
            rows = await db.scalars(
                select(ExperienceORM)
                .where(
                    ExperienceORM.occurred_at >= day.isoformat(),
                    ExperienceORM.occurred_at < (day + timedelta(days=1)).isoformat(),
                )
                .order_by(ExperienceORM.occurred_at, ExperienceORM.id)
            )
            return [_experience(row) for row in rows]

    async def recent_diaries(self, before: date, limit: int = 5) -> list[Diary]:
        async with get_session() as db:
            rows = await db.scalars(
                select(DiaryORM)
                .where(DiaryORM.day <= before.isoformat())
                .order_by(DiaryORM.day.desc(), DiaryORM.id.desc())
                .limit(limit)
            )
            return [_diary(row) for row in rows]

    async def save_dream(self, day: date, result: DreamResult, through: int, allowed_refs: set[str]) -> bool:
        """在同一事务提交日记、事实强化、状态和素材进度。"""
        refs = {ref for fact in result.facts for ref in fact.source_refs} | set(result.tension_source_refs)
        refs |= {f"fact:{fact_id}" for fact_id in result.recalled_fact_ids}
        refs |= {ref for item in result.retractions for ref in item.source_refs}
        refs |= {f"fact:{item.fact_id}" for item in result.retractions}
        refs |= {f"fact:{fact_id}" for item in result.facts for fact_id in item.supersedes}
        if not refs <= allowed_refs:
            raise ValueError("Dream references material that was not supplied")
        if result.dissonance_delta and (not result.tension_source_refs or not result.tension_reason):
            raise ValueError("A tension change requires source material and a reason")
        keys = [(item.category, item.key) for item in result.facts]
        if len(keys) != len(set(keys)):
            raise ValueError("The dream contains conflicting updates for the same fact key")
        async with self._lock:
            snapshot = self.snapshot.model_copy(deep=True)
            now, day_end = datetime.now(), datetime.combine(day, time.max)
            async with get_session() as db:
                diary = await db.scalar(select(DiaryORM).where(DiaryORM.source == f"dream:{day}"))
                if diary is not None and diary.covered_through >= through:
                    return False
                previous_through = diary.covered_through if diary is not None else 0
                if diary is None:
                    diary = DiaryORM(source=f"dream:{day}", day=day.isoformat(), created_at=now.isoformat())
                    db.add(diary)
                diary.content, diary.covered_through = public_text(result.diary), through
                if not diary.content:
                    raise ValueError("The dream contained no diary text")
                diary.source_refs = json.dumps(sorted(allowed_refs))
                evidence = list(
                    await db.scalars(
                        select(ExperienceORM).where(
                            ExperienceORM.id <= through,
                            ExperienceORM.occurred_at >= day.isoformat(),
                            ExperienceORM.occurred_at < (day + timedelta(days=1)).isoformat(),
                        )
                    )
                )
                day_refs = {f"experience:{item.id}" for item in evidence}
                evidence_dates = {f"experience:{item.id}": _local(item.occurred_at) for item in evidence}
                new_refs = {f"experience:{item.id}" for item in evidence if item.id > previous_through}
                if result.dissonance_delta and not set(result.tension_source_refs) & new_refs:
                    raise ValueError("A tension change needs new evidence from this diary day")
                if result.relief == "positive_feedback" and not any(
                    item.kind == "user" and f"experience:{item.id}" in result.tension_source_refs for item in evidence
                ):
                    raise ValueError("Positive feedback requires a user experience reference")
                for item in result.retractions:
                    if not set(item.source_refs) & day_refs:
                        raise ValueError("Fact retraction requires new source evidence")
                    retired = await db.get(FactORM, item.fact_id)
                    observed = max(evidence_dates[ref] for ref in item.source_refs if ref in evidence_dates)
                    if retired is not None and _local(retired.observed_at) <= observed:
                        retired.active = False
                recalled = set(result.recalled_fact_ids)
                for change in result.facts:
                    new_evidence = bool(set(change.source_refs) & day_refs)
                    observed = max(
                        (evidence_dates[ref] for ref in change.source_refs if ref in evidence_dates), default=day_end
                    )
                    if not new_evidence:
                        equivalents = [await db.get(FactORM, fact_id) for fact_id in change.supersedes]
                        if not equivalents or not all(
                            item is not None
                            and item.active
                            and item.category == change.category.value
                            and item.value == change.value
                            and f"fact:{item.id}" in change.source_refs
                            for item in equivalents
                        ):
                            raise ValueError(
                                "Fact updates require new evidence or equivalent source facts to consolidate"
                            )
                    rows = list(
                        await db.scalars(
                            select(FactORM).where(FactORM.category == change.category.value, FactORM.key == change.key)
                        )
                    )
                    if any(row.forgotten for row in rows):
                        continue
                    current = next((row for row in rows if row.active), None)
                    if current is not None and current.value != change.value and _local(current.observed_at) > observed:
                        continue
                    if current is None or current.value != change.value:
                        if current is not None:
                            current.active = False
                        current = FactORM(
                            category=change.category.value,
                            key=change.key,
                            value=change.value,
                            active=True,
                            forgotten=False,
                            source_refs=json.dumps(change.source_refs),
                            weight=0.0 if new_evidence else 1.0,
                            weight_at=now.isoformat(),
                            observed_at=observed.isoformat(),
                            last_recalled_at=day_end.isoformat(),
                            created_at=now.isoformat(),
                        )
                        db.add(current)
                        await db.flush()
                    else:
                        current.source_refs = json.dumps(
                            sorted(set(json.loads(current.source_refs)) | set(change.source_refs))
                        )
                        current.observed_at = max(_local(current.observed_at), observed).isoformat()
                    for fact_id in change.supersedes:
                        retired = await db.get(FactORM, fact_id)
                        if retired is not None and retired.id != current.id and _local(retired.observed_at) <= observed:
                            retired.active = False
                    if new_evidence:
                        recalled.add(current.id)
                for fact_id in recalled:
                    fact = await db.get(FactORM, fact_id)
                    if fact is None or not fact.active:
                        continue
                    seen = await db.scalar(
                        select(FactRecallORM).where(
                            FactRecallORM.fact_id == fact_id, FactRecallORM.day == day.isoformat()
                        )
                    )
                    if seen is not None:
                        continue
                    stamp = max(now, _local(fact.weight_at), day_end)
                    fact.weight = _fact(fact).score(stamp) + 2 ** (
                        -max(0, (stamp - day_end).total_seconds()) / (86400 * 90)
                    )
                    fact.weight_at = stamp.isoformat()
                    fact.last_recalled_at = max(day_end, _local(fact.last_recalled_at)).isoformat()
                    db.add(FactRecallORM(fact_id=fact_id, day=day.isoformat()))
                delta = result.dissonance_delta
                if result.relief == "action_without_feedback":
                    delta = max(-0.05, delta)
                snapshot.state.dissonance = max(0.0, min(1.0, snapshot.state.dissonance + delta))
                if result.state_update is not None:
                    observed = max(evidence_dates.values())
                    self._apply_state(snapshot.state, result.state_update, observed, cutoff=observed)
                snapshot.last_dream_at = now
                snapshot.legacy_consolidated = True
                await self._save_snapshot(db, snapshot)
                await db.flush()
                facts = {
                    row.id: _fact(row) for row in await db.scalars(select(FactORM).where(FactORM.active.is_(True)))
                }
            self.snapshot, self.facts = snapshot, facts
            return True

    async def search(self, query: MemoryQuery, *, limit: int = 30) -> list[RecallHit]:
        """按词和日期读取有限候选，供语义筛选和降级回查。"""
        terms = [term.strip().casefold() for term in query.terms if term.strip()]
        hits: list[RecallHit] = []
        async with get_session() as db:
            for model, content_column, date_column in (
                (ExperienceORM, ExperienceORM.content, ExperienceORM.occurred_at),
                (DiaryORM, DiaryORM.content, DiaryORM.day),
                (FactORM, FactORM.value, FactORM.created_at),
            ):
                statement = select(model)
                if terms:
                    statement = statement.where(
                        or_(*(func.lower(content_column).contains(term, autoescape=True) for term in terms))
                    )
                if query.start:
                    statement = statement.where(date_column >= query.start.isoformat())
                if query.end:
                    statement = statement.where(date_column < (query.end + timedelta(days=1)).isoformat())
                if model is FactORM:
                    statement = statement.where(FactORM.active.is_(True))
                rows = await db.scalars(statement.order_by(date_column.desc()).limit(limit))
                for row in rows:
                    if isinstance(row, ExperienceORM):
                        hit = RecallHit(
                            ref=f"experience:{row.id}", content=public_text(row.content), occurred_at=row.occurred_at
                        )
                    elif isinstance(row, DiaryORM):
                        hit = RecallHit(
                            ref=f"diary:{row.id}",
                            content=("[Legacy session summary] " if row.source.startswith("legacy") else "")
                            + public_text(row.content),
                            occurred_at=row.day,
                            source_refs=json.loads(row.source_refs),
                        )
                    else:
                        hit = RecallHit(
                            ref=f"fact:{row.id}",
                            content=f"{row.category}/{row.key}: {public_text(row.value)}",
                            occurred_at=row.created_at,
                            source_refs=json.loads(row.source_refs),
                        )
                    if len(hit.content) > 2400:
                        position = min(
                            (hit.content.casefold().find(term) for term in terms if term in hit.content.casefold()),
                            default=0,
                        )
                        start = max(0, position - 300)
                        hit.content = (
                            ("…" if start else "")
                            + hit.content[start : start + 2400]
                            + "… [Read source for full context]"
                        )
                    hits.append(hit)
        hits.sort(
            key=lambda item: (sum(term in item.content.casefold() for term in terms), item.occurred_at), reverse=True
        )
        return hits[:limit]

    async def read_source(self, ref: str, *, offset: int = 0, limit: int = 6000) -> str:
        """分页读取来源及相邻素材，不暴露私有思考。"""
        if offset < 0 or not 1 <= limit <= 12000:
            raise ValueError("Use offset >= 0 and limit 1..12000")
        context_ref = re.fullmatch(r"context:([0-9a-f]{64})", ref)
        task_ref = re.fullmatch(r"task_output:([0-9a-f]{32}):([0-9a-f]{32})", ref)
        if context_ref or task_ref:
            if context_ref:
                path = mas_config.data_dir.resolve() / "context_sources" / (context_ref[1] + ".txt")
                content = public_text(path.read_text(encoding="utf-8"))
            else:
                assert task_ref is not None
                path = mas_config.data_dir.resolve() / "agent_tasks" / task_ref[1] / (task_ref[2] + ".json")
                result = ToolResult.model_validate_json(path.read_text(encoding="utf-8"))
                content = (
                    public_text(result.text)
                    + "\nResources: "
                    + json.dumps([item.model_dump() for item in result.resources])
                )
            return self._page(ref, content, offset, limit)
        match = re.fullmatch(r"(experience|diary|fact):(\d+)", ref)
        if match is None or offset < 0 or not 1 <= limit <= 12000:
            raise ValueError("Use experience:N, diary:N or fact:N, offset >= 0 and limit 1..12000")
        kind, raw_id = match.groups()
        async with get_session() as db:
            if kind == "experience":
                row = await db.get(ExperienceORM, int(raw_id))
                if row is None:
                    raise ValueError("Experience not found")
                previous = list(
                    await db.scalars(
                        select(ExperienceORM)
                        .where(ExperienceORM.session_id == row.session_id, ExperienceORM.id < row.id)
                        .order_by(ExperienceORM.id.desc())
                        .limit(2)
                    )
                )
                following = list(
                    await db.scalars(
                        select(ExperienceORM)
                        .where(ExperienceORM.session_id == row.session_id, ExperienceORM.id > row.id)
                        .order_by(ExperienceORM.id)
                        .limit(2)
                    )
                )
                content = "\n".join(
                    public_text(_experience(item).describe()) for item in [*reversed(previous), row, *following]
                )
                content += "\nResources: " + row.resources
                if row.source:
                    content += "\nOrigin: " + row.source
            elif kind == "diary":
                diary = await db.get(DiaryORM, int(raw_id))
                if diary is None:
                    raise ValueError("Diary not found")
                content = f"[{diary.day} | {diary.source}] {public_text(diary.content)}\nSources: {diary.source_refs}"
            else:
                fact = await db.get(FactORM, int(raw_id))
                if fact is None or fact.forgotten:
                    raise ValueError("Fact is missing or forgotten")
                content = "[Superseded or invalid fact] " if not fact.active else ""
                content += public_text(_fact(fact).describe()) + "\nSources: " + fact.source_refs
        return self._page(ref, content, offset, limit)

    @staticmethod
    def _page(ref: str, content: str, offset: int, limit: int) -> str:
        page = content[offset : offset + limit]
        if offset + limit < len(content):
            page += f"\n[Continue {ref} at offset {offset + limit}]"
        return page

    async def prepare_context(
        self, request: ModelRequest, config: ModelConfig, compactor: ContextCompactor, *, force: bool = False
    ) -> ModelRequest:
        """按模型预算整理工作历史，保存摘要与覆盖范围后才替换内存视图。

        先注入已有摘要；输入达到预算的 80% 时，按完整用户回合划分新旧历史，
        将旧摘要与旧回合一起压缩，目标是让整个请求回落到预算的 60%。
        预算不足或摘要不可用时发出警告并保留历史，SQLite 原文始终保留。

        :param request: 含当前输入、人格提示、检索内容及近期历史的请求。
        :param config: 当前目标模型的上下文、输出和思考预算配置。
        :param compactor: 使用摘要模型分块生成工作摘要的组件。
        :param force: 将尝试压缩的触发线从 80% 降为 60%。
        :return: 注入工作摘要并保留近期历史的请求；未能压缩时保留完整历史。
        :raises RuntimeError: 摘要期间切换了会话，不能提交旧会话的覆盖范围。
        """
        async with self._context_lock:
            session_id = self.session.session_id
            base_system = request.system or ""
            if self.snapshot.working_summary:
                request = replace(
                    request, system=base_system + "\n[Working context summary]\n" + self.snapshot.working_summary
                )
            budget = input_budget(config)
            if request_tokens(request) < budget * (0.6 if force else 0.8):
                return request
            history = list(request.history)
            # 保留完整的最近用户回合，不在 assistant 中间划分摘要范围。
            boundaries = [i for i, turn in enumerate(history) if turn.role == "user" and i > 0]
            boundary = next((i for i in reversed(boundaries) if len(history) - i >= 4), 0)
            if not boundary and boundaries:
                boundary = boundaries[-1]
            for candidate in boundaries:
                if candidate < boundary:
                    continue
                retained = replace(request, system=base_system, history=history[candidate:])
                if request_tokens(retained) + 192 < budget * 0.6:
                    boundary = candidate
                    break
            old, recent = history[:boundary], history[boundary:]
            if not old and not self.snapshot.working_summary:
                if request_tokens(request) > budget:
                    warnings.warn(
                        "The current input and persona exceed context_window", ContextOverflowWarning, stacklevel=2
                    )
                return request
            base = replace(request, system=base_system, history=recent)
            allowance = int(budget * 0.6) - request_tokens(base) - 64
            if allowance < 128:
                warnings.warn(
                    "The current input leaves no room for a useful history summary; keeping history",
                    ContextOverflowWarning,
                    stacklevel=2,
                )
                return request
            transcript = (
                self.snapshot.working_summary
                + "\n"
                + "\n".join(
                    f"[experience:{turn.id} | {turn.timestamp.isoformat()} | {turn.role}] {turn.content}"
                    for turn in old
                )
            )
            summary = await compactor.summarize(transcript, min(4096, allowance), available_tokens=allowance)
            if summary is None:
                return request
            through = old[-1].id if old else self.snapshot.summary_through
            async with self._lock:
                if session_id != self.session.session_id:
                    raise RuntimeError("The session changed during context compression")
                snapshot = self.snapshot.model_copy(deep=True)
                snapshot.working_summary, snapshot.summary_through = summary, through
                async with get_session() as db:
                    await self._save_snapshot(db, snapshot)
                self.snapshot = snapshot
                self.recent_turns = deque(turn for turn in self.recent_turns if turn.id > through)
            return replace(base, system=base_system + "\n[Working context summary]\n" + summary)
