"""将本地记忆记录转换为领域数据，统一日期和来源引用。"""

import json
from datetime import date, datetime

from muika.database.orm_models import DiaryORM, ExperienceORM, FactORM

from .memory_models import Diary, Experience, Fact, MemoryCategory


def local_time(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    return stamp.astimezone().replace(tzinfo=None) if stamp.tzinfo else stamp


def fact_from_row(row: FactORM) -> Fact:
    return Fact(
        id=row.id,
        category=MemoryCategory(row.category),
        key=row.key,
        value=row.value,
        source_refs=json.loads(row.source_refs),
        weight=row.weight,
        weight_at=local_time(row.weight_at),
        observed_at=local_time(row.observed_at),
        last_recalled_at=local_time(row.last_recalled_at),
    )


def experience_from_row(row: ExperienceORM) -> Experience:
    return Experience(
        id=row.id,
        session_id=row.session_id,
        kind=row.kind,
        content=row.content,
        occurred_at=local_time(row.occurred_at),
        source=row.source,
    )


def diary_from_row(row: DiaryORM) -> Diary:
    return Diary(
        id=row.id,
        day=date.fromisoformat(row.day),
        content=row.content,
        source=row.source,
        source_refs=json.loads(row.source_refs),
        covered_through=row.covered_through,
        created_at=local_time(row.created_at),
    )
