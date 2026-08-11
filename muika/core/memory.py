from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from muika.config import mas_config
from muika.database.crud import ArchiveCRUD
from muika.database.db import get_session
from muika.models import Resource
from muika.utils.logger import logger


class MemoryLayer(str, Enum):
    CORE = "core"
    """CoreIdentity：核心身份记忆。稳定、低变动。永久注入 system prompt。"""

    STATE = "state"
    """RelationshipState：关系状态记忆。时间敏感，可过期。仅 Resume 模式下注入最近 ≤3 条。"""

    PREFERENCE = "preference"
    """PreferenceProfile：长期偏好与事实。不默认注入，由 Butler 预处理层按需检索。"""

    ARCHIVE = "archive"
    """ArchiveMemory：历史 Session 摘要。不默认注入，由管家 Agent 按需提供。"""


class MemoryCategory(str, Enum):
    USER = "user"
    """关于用户的事实"""

    SELF = "self"
    """关于 AI 自身的认知"""

    WORLD = "world"
    """世界 / 环境事实"""

    RELATION = "relation"
    """关系 / 交互状态（STATE 层专用）"""


class MemoryRecord(BaseModel):
    """CORE / STATE / PREFERENCE 层的单条记忆条目。"""

    id: Optional[int] = None
    layer: MemoryLayer
    category: MemoryCategory
    key: str
    """语义唯一标识符，同 key + layer 的记录会被覆盖（upsert 语义）。"""
    value: str
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None
    """过期时间，仅 STATE 层使用，过期后不再注入。"""


class ArchiveEntry(BaseModel):
    """历史 Session 摘要（ARCHIVE 层）。"""

    id: Optional[int] = None
    session_id: str
    summary: str
    period_start: datetime
    period_end: datetime
    created_at: datetime = Field(default_factory=datetime.now)


@dataclass
class SessionTurn:
    """Session 级单条对话记录"""

    role: Literal["user", "muika", "agent"]
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    resources: List[Resource] = field(default_factory=list)


class SessionState(BaseModel):
    """当前 Session 的元信息。"""

    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    started_at: datetime = Field(default_factory=datetime.now)
    is_first_session: bool = True
    """True：系统首次对话。False：Resume 模式（已有历史记忆的新 Session）。"""


class MemoryManager:
    def __init__(self, max_turns: int = mas_config.max_memory_records):
        self.recent_turns: deque[SessionTurn] = deque(maxlen=max_turns)
        """Session 中的对话内容"""

        self.records: dict[str, MemoryRecord] = {}
        """CORE / STATE / PREFERENCE 层。key 格式：'{layer}:{category}:{key}'"""

        self.archives: list[ArchiveEntry] = []
        """ARCHIVE 层 — 历史 Session 摘要。"""

        self.session: SessionState = SessionState()

    async def load(self):  # pragma: no cover
        """从数据库加载全量记忆，若有历史数据则进入 Resume 模式。"""
        from muika.database.crud import ArchiveCRUD, MemoryRecordCRUD
        from muika.database.db import get_session

        try:
            async with get_session() as session:
                db_records = await MemoryRecordCRUD.get_all(session)
                for r in db_records:
                    storage_key = f"{r.layer}:{r.category}:{r.key}"
                    self.records[storage_key] = MemoryRecord(
                        id=r.id,
                        layer=MemoryLayer(r.layer),
                        category=MemoryCategory(r.category),
                        key=r.key,
                        value=r.value,
                        created_at=datetime.fromisoformat(r.created_at),
                        updated_at=datetime.fromisoformat(r.updated_at),
                        expires_at=datetime.fromisoformat(r.expires_at) if r.expires_at else None,
                    )

                db_archives = await ArchiveCRUD.list_all(session)
                for a in db_archives:
                    self.archives.append(
                        ArchiveEntry(
                            id=a.id,
                            session_id=a.session_id,
                            summary=a.summary,
                            period_start=datetime.fromisoformat(a.period_start),
                            period_end=datetime.fromisoformat(a.period_end),
                            created_at=datetime.fromisoformat(a.created_at),
                        )
                    )

                if not self.records and not self.archives:
                    logger.debug("[Memory] No data in DB — starting fresh (first session).")
                    return

                self.session.is_first_session = False

                logger.info(
                    f"[Memory] Loaded from DB — records={len(self.records)} "
                    f"archives={len(self.archives)} "
                    f"mode=resume"
                )
                by_layer: dict[str, int] = {}
                for r in self.records.values():
                    by_layer[r.layer.value] = by_layer.get(r.layer.value, 0) + 1
                logger.debug(f"[Memory] Layer breakdown: {by_layer}")

        except Exception as e:
            logger.error(f"[Memory] Failed to load from DB: {e}")

    def new_session(self):
        """
        创建新的 Session（通常由 bot_connect 事件触发）。
        若已存在历史记忆，自动进入 Resume 模式（is_first_session=False）。
        """
        has_prior = bool(self.records) or bool(self.archives)
        self.session = SessionState(is_first_session=not has_prior)
        self.recent_turns.clear()
        logger.info(
            f"[Memory] New session — id={self.session.session_id[:8]}... "
            f"mode={'resume' if has_prior else 'first'} "
            f"prior_records={len(self.records)} prior_archives={len(self.archives)}"
        )

    def add_context(
        self, role: Literal["user", "muika", "agent"], content: str, resources: Optional[list[Resource]] = None
    ):
        """记录一条 Session 级对话记录。"""
        self.recent_turns.append(SessionTurn(role=role, content=content, resources=resources or []))

    def _record_key(self, layer: MemoryLayer, category: MemoryCategory, key: str) -> str:
        return f"{layer.value}:{category.value}:{key}"

    async def upsert_memory(
        self,
        layer: MemoryLayer,
        category: MemoryCategory,
        key: str,
        value: str,
        expires_at: Optional[datetime] = None,
    ) -> None:
        """插入或覆盖一条记忆（CORE / STATE / PREFERENCE 层）。"""
        storage_key = self._record_key(layer, category, key)
        existing = self.records.get(storage_key)

        if existing:
            logger.warning(f"Memory '{storage_key}' overwritten: {existing.value!r} → {value!r}")
            existing.value = value
            existing.updated_at = datetime.now()
            existing.expires_at = expires_at
        else:
            self.records[storage_key] = MemoryRecord(
                layer=layer,
                category=category,
                key=key,
                value=value,
                expires_at=expires_at,
            )

        # 持久化到 DB
        from muika.database.crud import MemoryRecordCRUD
        from muika.database.db import get_session

        try:
            async with get_session() as db_session:
                await MemoryRecordCRUD.upsert(
                    db_session,
                    layer=layer.value,
                    category=category.value,
                    key=key,
                    value=value,
                    expires_at=expires_at.isoformat() if expires_at else None,
                )
        except Exception as e:
            logger.error(f"[Memory] DB upsert failed for {storage_key!r}: {e}")

        logger.debug(f"[Memory] Upserted: {storage_key} = {value!r}")

    async def forget_memory(
        self,
        layer: MemoryLayer,
        category: MemoryCategory,
        key: str,
    ) -> None:
        """删除一条记忆。"""
        storage_key = self._record_key(layer, category, key)
        if storage_key in self.records:
            del self.records[storage_key]
            logger.debug(f"[Memory] Forgotten: {storage_key}")
            # 持久化删除到 DB
            from muika.database.crud import MemoryRecordCRUD
            from muika.database.db import get_session

            try:
                async with get_session() as db_session:
                    await MemoryRecordCRUD.delete(db_session, layer=layer.value, key=key)
            except Exception as e:
                logger.error(f"[Memory] DB delete failed for {storage_key!r}: {e}")
        else:
            logger.warning(f"[Memory] forget_memory: key not found — {storage_key}")

    async def add_archive(
        self,
        summary: str,
        period_start: datetime,
        period_end: datetime,
    ) -> None:
        """添加一条历史 Session 摘要（ARCHIVE 层）。"""
        entry = ArchiveEntry(
            session_id=self.session.session_id,
            summary=summary,
            period_start=period_start,
            period_end=period_end,
        )
        self.archives.append(entry)
        logger.debug(f"[Memory] Archive added for session {self.session.session_id}")

        try:
            async with get_session() as db_session:
                await ArchiveCRUD.add(
                    db_session,
                    session_id=self.session.session_id,
                    summary=summary,
                    period_start=period_start.isoformat(),
                    period_end=period_end.isoformat(),
                )
        except Exception as e:
            logger.error(f"[Memory] DB archive insert failed: {e}")

    async def update_archive(
        self,
        summary: str,
        period_start: datetime,
        period_end: datetime,
    ) -> None:
        """更新一条历史 Session 摘要（ARCHIVE 层）。"""
        archive_entry = next((a for a in self.archives if a.session_id == self.session.session_id), None)
        if archive_entry:
            archive_entry.summary = summary
            archive_entry.period_start = period_start
            archive_entry.period_end = period_end
            logger.debug(f"[Memory] Archive updated for session {self.session.session_id}")
        else:
            return await self.add_archive(summary, period_start, period_end)

        try:
            async with get_session() as db_session:
                await ArchiveCRUD.updated(
                    db_session,
                    session_id=self.session.session_id,
                    summary=summary,
                    period_start=period_start.isoformat(),
                    period_end=period_end.isoformat(),
                )
        except Exception as e:
            logger.error(f"[Memory] DB archive update failed: {e}")

    # ──────────────────────────── Prompt 构建 ────────────────────────────

    def _iter_layer(self, layer: MemoryLayer):
        """遍历指定层的有效记忆（自动跳过已过期的 STATE 条目）。"""
        now = datetime.now()
        skipped = 0
        for record in self.records.values():
            if record.layer != layer:
                continue
            if record.expires_at and record.expires_at < now:
                skipped += 1
                logger.debug(
                    f"[Memory] Skipping expired STATE record: key={record.key!r} "
                    f"expired={record.expires_at.isoformat()}"
                )
                continue
            yield record
        if skipped:
            logger.debug(f"[Memory] _iter_layer({layer.value}): skipped {skipped} expired record(s)")

    def get_core_prompt(self) -> str:
        """
        返回 CoreIdentity 层的结构化摘要，永久注入 system prompt。
        按 category 分组，每组输出为 Markdown 列表。
        """
        records = list(self._iter_layer(MemoryLayer.CORE))
        if not records:
            logger.debug("[Memory] get_core_prompt: no CORE records, skipping injection.")
            return ""

        label_map = {
            MemoryCategory.USER: "User",
            MemoryCategory.SELF: "Self",
            MemoryCategory.WORLD: "World",
            MemoryCategory.RELATION: "Relation",
        }

        by_category: dict[MemoryCategory, list[MemoryRecord]] = {}
        for r in records:
            by_category.setdefault(r.category, []).append(r)

        parts = []
        for cat, items in by_category.items():
            parts.append(f"## {label_map.get(cat, cat.value.capitalize())} (Core Facts)")
            for item in items:
                parts.append(f"- {item.key}: {item.value}")

        logger.debug(
            "[Memory] get_core_prompt: injecting "
            f"{len(records)} CORE record(s) across "
            f"{len(by_category)} category/categories."
        )
        return "\n".join(parts)

    def get_resume_context(self, max_items: int = 3) -> str:
        """
        返回 RelationshipState 层的最近条目，仅在 Resume 模式下注入。
        按 updated_at 降序，最多返回 max_items 条。
        """
        if self.session.is_first_session:
            logger.debug("[Memory] get_resume_context: first session, skipping STATE injection.")
            return ""

        records = sorted(
            self._iter_layer(MemoryLayer.STATE),
            key=lambda r: r.updated_at,
            reverse=True,
        )[:max_items]

        if not records:
            logger.debug("[Memory] get_resume_context: resume mode but no STATE records found.")
            return ""

        logger.debug(
            f"[Memory] get_resume_context: injecting {len(records)} STATE record(s): {[r.key for r in records]}"
        )
        lines = ["## Recent Relationship State"]
        for r in records:
            lines.append(f"- {r.key}: {r.value}")
        return "\n".join(lines)

    def get_archive_prompt(self, max_items: int = 3) -> str:
        """
        返回 ARCHIVE 层的最近条目，仅在 Resume 模式下注入。
        按 period_end 降序，最多返回 max_items 条。
        """
        if self.session.is_first_session:
            logger.debug("[Memory] get_archive_prompt: first session, skipping ARCHIVE injection.")
            return ""

        records = sorted(
            self.archives,
            key=lambda r: r.period_end,
            reverse=True,
        )[:max_items]

        if not records:
            logger.debug("[Memory] get_archive_prompt: resume mode but no ARCHIVE records found.")
            return ""

        logger.debug(
            f"[Memory] get_archive_prompt: injecting {len(records)} ARCHIVE record(s):"
            f" {[r.session_id for r in records]}"
        )
        lines = ["## Recent Session Archives"]
        for r in records:
            period_end_str = r.period_end.strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"- Session {period_end_str}: {r.summary}")
        return "\n".join(lines)

    def get_preference_records(self) -> list[MemoryRecord]:
        """返回所有 PreferenceProfile 条目，供 Butler 预处理层检索用。"""
        prefs = list(self._iter_layer(MemoryLayer.PREFERENCE))
        logger.debug(f"[Memory] get_preference_records: {len(prefs)} PREFERENCE record(s) available.")
        return prefs

    def get_archives(self) -> list[ArchiveEntry]:
        """返回所有历史摘要，供管家 Agent 按需提供。"""
        return self.archives

    def get_memory_prompt(self) -> str:
        """
        构建注入 system prompt 的完整记忆上下文：
          - CORE 层：永久注入
          - STATE 层：Resume 模式下注入（最多 3 条）
          - PREFERENCE: 不注入，由 Butler 按需检索
          - ARCHIVE: 追加最近 3 条的对话摘要
        """
        parts: list[str] = []

        core = self.get_core_prompt()
        if core:
            parts.append(core)

        resume = self.get_resume_context()
        if resume:
            parts.append(resume)

        archive = self.get_archive_prompt()
        if archive:
            parts.append(archive)

        return "\n".join(parts)
