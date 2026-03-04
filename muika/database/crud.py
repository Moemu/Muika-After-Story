from datetime import datetime
from typing import Literal, Optional

from nonebot_plugin_orm import async_scoped_session
from sqlalchemy import func, select

from .orm_models import ArchiveRecordORM, MemoryRecordORM, Usage


class UsageORM:
    @staticmethod
    async def get_usage(
        session: async_scoped_session,
        plugin: Optional[str],
        date: Optional[str],
        type: Optional[Literal["chat", "embedding"]] = None,
    ) -> int:
        """
        获取用量信息

        :param session: 数据库会话
        :param plugin: (可选)插件名称，如果为 None 则返回所有插件的用量
        :param date: (可选)日期(`%Y.%m.%d`)，如果为 None 则返回所有日期的用量
        :param type: (可选)用量类型，默认为 None，表示返回所有类型的用量
        """
        query = select(func.sum(Usage.tokens))
        if plugin:
            query = query.where(Usage.plugin == plugin)
        if date:
            query = query.where(Usage.date.like(date))
        if type:
            query = query.where(Usage.type == type)
        result = await session.execute(query)
        return result.scalar() or 0

    @staticmethod
    async def save_usage(
        session: async_scoped_session, plugin: str, total_tokens: int, type: Literal["chat", "embedding"] = "chat"
    ):
        """
        保存用量信息
        """
        if total_tokens < 0:
            return

        date = datetime.now().strftime("%Y.%m.%d")
        stmt = await session.execute(
            select(Usage).where(Usage.plugin == plugin, Usage.type == type, Usage.date == date).limit(1)
        )
        usage = stmt.scalar_one_or_none()

        if usage is not None:
            usage.tokens += total_tokens
            return

        session.add(Usage(plugin=plugin, type=type, date=date, tokens=total_tokens))


# ─────────────────────────────────────────────────────────────────
# MemoryRecordCRUD  —  CORE / STATE / PREFERENCE 层持久化
# ─────────────────────────────────────────────────────────────────


class MemoryRecordCRUD:
    @staticmethod
    async def upsert(
        session: async_scoped_session,
        layer: str,
        category: str,
        key: str,
        value: str,
        expires_at: Optional[str] = None,
    ) -> MemoryRecordORM:
        """
        插入或覆盖一条记忆记录（upsert by layer + key）。
        updated_at 始终更新为当前时间。
        """
        now = datetime.now().isoformat()
        stmt = await session.execute(
            select(MemoryRecordORM).where(MemoryRecordORM.layer == layer, MemoryRecordORM.key == key).limit(1)
        )
        existing = stmt.scalar_one_or_none()

        if existing:
            existing.category = category
            existing.value = value
            existing.updated_at = now
            existing.expires_at = expires_at
            return existing

        record = MemoryRecordORM(
            layer=layer,
            category=category,
            key=key,
            value=value,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
        )
        session.add(record)
        return record

    @staticmethod
    async def delete(
        session: async_scoped_session,
        layer: str,
        key: str,
    ) -> bool:
        """删除一条记忆记录，返回是否实际删除。"""
        stmt = await session.execute(
            select(MemoryRecordORM).where(MemoryRecordORM.layer == layer, MemoryRecordORM.key == key).limit(1)
        )
        existing = stmt.scalar_one_or_none()
        if existing:
            await session.delete(existing)
            return True
        return False

    @staticmethod
    async def get_all(session: async_scoped_session) -> list[MemoryRecordORM]:
        """返回全部记忆记录（供启动时全量加载）。"""
        result = await session.execute(select(MemoryRecordORM))
        return list(result.scalars().all())


# ─────────────────────────────────────────────────────────────────
# ArchiveCRUD  —  ARCHIVE 层（历史 Session 摘要）持久化
# ─────────────────────────────────────────────────────────────────


class ArchiveCRUD:
    @staticmethod
    async def add(
        session: async_scoped_session,
        session_id: str,
        summary: str,
        period_start: str,
        period_end: str,
    ) -> ArchiveRecordORM:
        """添加一条历史 Session 摘要。"""
        record = ArchiveRecordORM(
            session_id=session_id,
            summary=summary,
            period_start=period_start,
            period_end=period_end,
            created_at=datetime.now().isoformat(),
        )
        session.add(record)
        return record

    @staticmethod
    async def list_all(session: async_scoped_session) -> list[ArchiveRecordORM]:
        """返回全部历史摘要，按 period_start 升序。"""
        result = await session.execute(select(ArchiveRecordORM).order_by(ArchiveRecordORM.period_start))
        return list(result.scalars().all())
