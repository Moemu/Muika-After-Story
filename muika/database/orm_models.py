from typing import Optional

from nonebot_plugin_orm import Model
from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column


class Usage(Model):
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    plugin: Mapped[str] = mapped_column(String)
    type: Mapped[str] = mapped_column(String, nullable=False)
    date: Mapped[str] = mapped_column(String, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, nullable=True, default=0)


class MemoryRecordORM(Model):
    """
    持久化 CoreIdentity / RelationshipState / PreferenceProfile 三层记忆。
    layer 字段区分层级，key 与 layer 的组合在应用层保持唯一（upsert 语义）。
    """

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    layer: Mapped[str] = mapped_column(String, index=True)  # MemoryLayer value
    category: Mapped[str] = mapped_column(String)  # MemoryCategory value
    key: Mapped[str] = mapped_column(String, index=True)
    value: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String)  # ISO8601
    updated_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # 仅 STATE 层使用


class ArchiveRecordORM(Model):
    """
    持久化历史 Session 摘要（ARCHIVE 层）。
    与 MemoryRecordORM 分表，因为查询模式完全不同（按 session_id / 时间段检索）。
    """

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String, index=True)
    summary: Mapped[str] = mapped_column(Text)
    period_start: Mapped[str] = mapped_column(String)
    period_end: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)


class TopicHistoryORM(Model):
    """
    话题使用历史，用于冷却周期判断和用户参与度评估。
    topic_id 全局唯一（一行对应一个话题的累计记录）。
    """

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    topic_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    last_used_at: Mapped[str] = mapped_column(String)  # ISO8601
    use_count: Mapped[int] = mapped_column(Integer, default=1)
    engaged_count: Mapped[int] = mapped_column(Integer, default=0)
