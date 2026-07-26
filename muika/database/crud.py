from datetime import datetime, timedelta
from typing import Literal, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .orm_models import ArchiveRecordORM, MemoryRecordORM, TopicHistoryORM, Usage


class UsageORM:
    @staticmethod
    async def get_usage(
        session: AsyncSession,
        plugin: Optional[str],
        date: Optional[str],
        model: Optional[str],
        type: Optional[Literal["chat", "embedding"]] = None,
    ) -> int:
        """
        获取用量信息

        :param session: 数据库会话
        :param plugin: (可选)插件名称，如果为 None 则返回所有插件的用量
        :param date: (可选)日期(``%Y.%m.%d``)，如果为 None 则返回所有日期的用量
        :param type: (可选)用量类型，默认为 None，表示返回所有类型的用量
        """
        query = select(func.sum(Usage.input_tokens + Usage.output_tokens + Usage.cached_tokens))
        if plugin:
            query = query.where(Usage.plugin == plugin)
        if date:
            query = query.where(Usage.date.like(date))
        if model:
            query = query.where(Usage.model == model)
        if type:
            query = query.where(Usage.type == type)
        result = await session.execute(query)
        return result.scalar() or 0

    @staticmethod
    async def save_usage(
        session: AsyncSession,
        plugin: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        type: Literal["chat", "embedding"] = "chat",
    ):
        """
        保存用量信息（按 plugin + type + date upsert，各字段累加）
        """
        if input_tokens < 0 or output_tokens < 0 or cached_tokens < 0:
            return

        date = datetime.now().strftime("%Y.%m.%d")
        stmt = await session.execute(
            select(Usage)
            .where(Usage.plugin == plugin, Usage.model == model, Usage.type == type, Usage.date == date)
            .limit(1)
        )
        usage = stmt.scalar_one_or_none()

        if usage is not None:
            usage.input_tokens += input_tokens
            usage.output_tokens += output_tokens
            usage.cached_tokens += cached_tokens
            return

        session.add(
            Usage(
                plugin=plugin,
                model=model,
                type=type,
                date=date,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
            )
        )

    @staticmethod
    async def get_usage_records(session: AsyncSession, days: int = 7) -> list[Usage]:
        """返回最近 N 天按日期降序排列的用量明细行。

        :param session: 数据库会话
        :param days: 返回最近多少天的数据
        """
        since = (datetime.now() - timedelta(days=days)).strftime("%Y.%m.%d")
        stmt = await session.execute(
            select(Usage).where(Usage.date >= since).order_by(Usage.date.desc(), Usage.type, Usage.plugin)
        )
        return list(stmt.scalars().all())


# MemoryRecordCRUD：CORE / STATE / PREFERENCE 层持久化
class MemoryRecordCRUD:
    @staticmethod
    async def upsert(
        session: AsyncSession,
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
        session: AsyncSession,
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
    async def get_all(session: AsyncSession) -> list[MemoryRecordORM]:
        """返回全部记忆记录（供启动时全量加载）。"""
        result = await session.execute(select(MemoryRecordORM))
        return list(result.scalars().all())


# ArchiveCRUD：ARCHIVE 层（历史 Session 摘要）持久化
class ArchiveCRUD:
    @staticmethod
    async def add(
        session: AsyncSession,
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
    async def updated(
        session: AsyncSession,
        session_id: str,
        summary: str,
        period_start: str,
        period_end: str,
    ) -> ArchiveRecordORM:
        """更新一条历史 Session 摘要。"""
        stmt = await session.execute(select(ArchiveRecordORM).where(ArchiveRecordORM.session_id == session_id).limit(1))
        existing = stmt.scalar_one_or_none()

        if not existing:
            return await ArchiveCRUD.add(session, session_id, summary, period_start, period_end)

        existing.summary = summary
        existing.period_start = period_start
        existing.period_end = period_end
        return existing

    @staticmethod
    async def list_all(session: AsyncSession) -> list[ArchiveRecordORM]:
        """返回全部历史摘要，按 period_start 升序。"""
        result = await session.execute(select(ArchiveRecordORM).order_by(ArchiveRecordORM.period_start))
        return list(result.scalars().all())


class TopicHistoryCRUD:
    @staticmethod
    async def get_by_topic_id(
        session: AsyncSession,
        topic_id: str,
    ) -> Optional[TopicHistoryORM]:
        """查找话题历史记录，不存在则返回 None。"""
        result = await session.execute(select(TopicHistoryORM).where(TopicHistoryORM.topic_id == topic_id).limit(1))
        return result.scalar_one_or_none()

    @staticmethod
    async def record(
        session: AsyncSession,
        topic_id: str,
        user_engaged: bool,
    ) -> TopicHistoryORM:
        """
        更新或插入一条话题使用记录（upsert by topic_id）。
        每次调用 use_count +1；若用户参与了互动，engaged_count 同时 +1。
        调用方负责 commit。
        """
        now = datetime.now().isoformat()
        stmt = await session.execute(select(TopicHistoryORM).where(TopicHistoryORM.topic_id == topic_id).limit(1))
        existing = stmt.scalar_one_or_none()

        if existing:
            existing.last_used_at = now
            existing.use_count += 1
            if user_engaged:
                existing.engaged_count += 1
            return existing

        entry = TopicHistoryORM(
            topic_id=topic_id,
            last_used_at=now,
            use_count=1,
            engaged_count=1 if user_engaged else 0,
        )
        session.add(entry)
        return entry
