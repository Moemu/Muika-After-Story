"""自省代理：在夜间 session 结束时偶尔、或由用户用 ``.reflect`` 命令主动驱动 Muika 安静地回顾自己。

复用 Butler 内循环完成至多一处小自我修改——"用户命令改"与"她自己决定改"
走同一机制，只有指令来源不同。后续将演进为"做梦"机制并接入新的记忆系统，
当前保持轻量、模块化。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from muika.config import mas_config
from muika.core.memory import MemoryCategory, MemoryLayer
from muika.utils.logger import logger

if TYPE_CHECKING:
    from muika.core.butler.agent import ButlerAgent
    from muika.core.executor import Executor
    from muika.core.memory import MemoryManager
    from muika.core.state import MuikaState
    from muika.core.topic_manager import TopicManager


# 夜间窗口：22:00 ~ 06:00（本地时间）
NIGHT_START_HOUR = 22
NIGHT_END_HOUR = 6

# 未自省会话数下限（archives 中 period_end > last_reflection_at 的条数）
MIN_PENDING_SESSIONS = 5

# 变更叙事过期天数
CHANGE_MEMORY_TTL_DAYS = 7

# REFLECTION_OUTCOME 行提取正则
_OUTCOME_RE = re.compile(r"\[REFLECTION_OUTCOME\]\s*(.+)")

# 喂给 REFLECTION_PROMPT 的最大摘要数
MAX_SUMMARIES = 8


def _in_night_window(now: datetime) -> bool:
    """判断当前小时是否落在夜间窗口内（跨午夜）。"""
    h = now.hour
    return h >= NIGHT_START_HOUR or h < NIGHT_END_HOUR


def _extract_outcome(report: str) -> str:
    """从 Butler report 中提取 ``[REFLECTION_OUTCOME]`` 行；缺失时返回兜底句。"""
    match = _OUTCOME_RE.search(report)
    if match:
        return match.group(1).strip()
    return "I took some time to think about myself. Everything feels okay."


class ReflectionAgent:
    """自省代理：夜间 session 结束或 ``.reflect`` 命令触发。

    门控按序短路：enable → 夜间窗口 → 24h 冷却 → 未自省会话数 ≥ 5。
    复用 :class:`ButlerAgent` 内循环执行修改，复用既有 self_* 工具。
    """

    def __init__(
        self,
        butler_agent: ButlerAgent,
        memory: MemoryManager,
        state: MuikaState,
        topic_manager: TopicManager,
        executor: Executor,
    ) -> None:
        self._butler = butler_agent
        self._memory = memory
        self._state = state
        self._topics = topic_manager
        self._executor = executor
        self._running: bool = False

    # ------------------------------------------------------------------ public

    async def maybe_reflect(self, trigger: str) -> None:
        """门控按序短路：enable → 夜间窗口 → 冷却 → 未自省会话数。"""
        if not (mas_config.enable_self_modification and mas_config.enable_auto_reflection):
            logger.debug("[Reflection] skipped: self-mod or auto-reflection disabled")
            return

        now = datetime.now()
        if not _in_night_window(now):
            logger.debug(f"[Reflection] skipped: not in night window ({now:%H:%M})")
            return

        cooldown = timedelta(hours=mas_config.reflection_cooldown_hours)
        last_at = self._last_reflection_at()
        if last_at is not None and (now - last_at) < cooldown:
            logger.debug(f"[Reflection] skipped: cooldown not elapsed (last={last_at})")
            return

        pending = self._pending_session_count(last_at)
        if pending < MIN_PENDING_SESSIONS:
            logger.debug(f"[Reflection] skipped: only {pending}/{MIN_PENDING_SESSIONS} pending sessions")
            return

        await self._run_reflection(trigger, notify_user=False)

    async def force_reflect(self, trigger: str) -> None:
        """跳过全部门控，供 ``.reflect`` 命令使用。"""
        await self._run_reflection(trigger, notify_user=True)

    # --------------------------------------------------------------- internals

    def _last_reflection_at(self) -> Optional[datetime]:
        """从 CORE 层 memory 读取上次自省时间。"""
        record = self._memory.records.get("core:self:self_reflection_last_at")
        if record is None:
            return None
        try:
            return datetime.fromisoformat(record.value)
        except (TypeError, ValueError):
            logger.warning("[Reflection] malformed self_reflection_last_at, ignoring")
            return None

    def _pending_session_count(self, last_at: Optional[datetime]) -> int:
        """archives 中 period_end > last_at 的条数（未自省会话数）。"""
        if last_at is None:
            return len(self._memory.archives)
        return sum(1 for a in self._memory.archives if a.period_end > last_at)

    async def _gather_context(self, last_at: Optional[datetime]) -> str:
        """组装 REFLECTION_PROMPT 的 session_summaries 段（最近未自省的摘要，上限 MAX_SUMMARIES）。"""
        if last_at is None:
            recent = self._memory.archives[-MAX_SUMMARIES:]
        else:
            recent = [a for a in self._memory.archives if a.period_end > last_at][-MAX_SUMMARIES:]

        if not recent:
            return "(no recent session summaries available)"

        lines: list[str] = []
        for a in recent:
            lines.append(f"- [{a.period_start:%Y-%m-%d %H:%M} ~ {a.period_end:%H:%M}]\n" f"  {a.summary.strip()}")
        return "\n".join(lines)

    async def _run_reflection(self, trigger: str, notify_user: bool = False) -> None:
        """自省执行主体：审计快照 → Butler 执行 → 审计对比 → 记忆写入 → 用户通知。"""
        if self._running:
            logger.debug("[Reflection] another reflection already running, skipping")
            return
        self._running = True
        try:
            await self._execute(trigger, notify_user)
        except Exception as e:
            logger.error(f"[Reflection] {trigger} reflection failed: {e}", exc_info=True)
        finally:
            self._running = False

    async def _execute(self, trigger: str, notify_user: bool) -> None:
        """_run_reflection 的执行核心，集中在 try/except 内。"""
        from muika.core.butler._prompts import REFLECTION_PROMPT
        from muika.database.crud import SelfModificationCRUD
        from muika.database.db import get_session

        last_at = self._last_reflection_at()
        session_summaries = await self._gather_context(last_at)
        topic_stats = await self._topics.get_engagement_stats()

        instruction = REFLECTION_PROMPT.format(
            session_summaries=session_summaries,
            topic_stats=topic_stats,
        )

        # 审计快照
        async with get_session() as db_session:
            before_records = await SelfModificationCRUD.list_recent(db_session, limit=1)
            before_id = before_records[0].id if before_records else 0

        logger.info(f"[Reflection] starting {trigger} reflection")
        report, _ = await self._butler.execute_command(instruction, self._state, self._executor)
        logger.info(f"[Reflection] Butler report ({len(report)} chars)")

        # 审计对比
        async with get_session() as db_session:
            after_records = await SelfModificationCRUD.list_recent(db_session, limit=1)
            after_record = after_records[0] if after_records else None
            after_id = after_record.id if after_record else 0

        changed = after_id > before_id
        now = datetime.now()

        # 恒写冷却锚点（CORE:SELF 层，天然持久化）
        await self._memory.upsert_memory(
            layer=MemoryLayer.CORE,
            category=MemoryCategory.SELF,
            key="self_reflection_last_at",
            value=now.isoformat(),
        )

        # 有变更 → 写 STATE 层叙事（Resume 时注入，让她自然提起"我最近换了个说法"）
        outcome = _extract_outcome(report)
        if changed and after_record is not None:
            logger.info(
                f"[Reflection] change detected: {after_record.path!r} ({after_record.action}) "
                f"-- reason: {(after_record.reason or '')[:80]!r}"
            )
            await self._memory.upsert_memory(
                layer=MemoryLayer.STATE,
                category=MemoryCategory.RELATION,
                key="recent_self_change",
                value=outcome,
                expires_at=now + timedelta(days=CHANGE_MEMORY_TTL_DAYS),
            )
        else:
            logger.debug("[Reflection] no self-modification in this reflection")

        # 在线触发 → 发送 outcome 句；session_end 触发静默
        if notify_user and outcome:
            await self._executor.send_message(outcome)
