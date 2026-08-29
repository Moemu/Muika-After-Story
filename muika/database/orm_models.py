"""SQLAlchemy ORM models."""

from typing import Optional

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


class Usage(Base):
    __tablename__ = "usage"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    plugin: Mapped[str] = mapped_column(String)
    type: Mapped[str] = mapped_column(String, nullable=False)
    date: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=True, default=0)


class MemoryRecordORM(Base):
    """Persistent storage for CoreIdentity / RelationshipState / PreferenceProfile layers.

    ``layer`` distinguishes the tier; ``key`` + ``layer`` is treated as unique
    at the application level (upsert semantics).
    """

    __tablename__ = "memory_record"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    layer: Mapped[str] = mapped_column(String, index=True)
    category: Mapped[str] = mapped_column(String)
    key: Mapped[str] = mapped_column(String, index=True)
    value: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class ArchiveRecordORM(Base):
    """Persistent storage for historical session summaries (ARCHIVE layer)."""

    __tablename__ = "archive_record"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String, index=True)
    summary: Mapped[str] = mapped_column(Text)
    period_start: Mapped[str] = mapped_column(String)
    period_end: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)


class TopicHistoryORM(Base):
    """Topic usage history for cooldown and engagement tracking."""

    __tablename__ = "topic_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    topic_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    last_used_at: Mapped[str] = mapped_column(String)
    use_count: Mapped[int] = mapped_column(Integer, default=1)
    engaged_count: Mapped[int] = mapped_column(Integer, default=0)


class SelfModificationORM(Base):
    """自我修改审计日志——记录 Muika 对自身文件（模板/话题/技能/插件）的每次变更。

    ``before_path`` / ``after_path`` 为相对备份目录的文件路径，保存修改前后的完整内容；
    ``before_path`` 为 ``None`` 表示该次写入前文件不存在（revert 到此版本即删除文件）。
    """

    __tablename__ = "self_modification_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[str] = mapped_column(String)
    layer: Mapped[str] = mapped_column(String, index=True)
    path: Mapped[str] = mapped_column(String, index=True)
    action: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(Text)
    before_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    after_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="applied")
    source: Mapped[str] = mapped_column(String, default="self")


class RssDigestCacheORM(Base):
    """RSS 评估结果缓存——持久化所有 LLM 评估结果，避免重启后重复抓取和评估。"""

    __tablename__ = "rss_digest_cache"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    topic_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    source_id: Mapped[str] = mapped_column(String, index=True)
    link: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    published: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    score: Mapped[int] = mapped_column(Integer)
    keep: Mapped[int] = mapped_column(Integer)  # SQLite bool: 0/1
    reason: Mapped[str] = mapped_column(String)
    primary_theme: Mapped[str] = mapped_column(String)
    summary: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[str] = mapped_column(String)
    evaluated_at: Mapped[str] = mapped_column(String)
