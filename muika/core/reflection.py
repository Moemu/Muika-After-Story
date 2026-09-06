"""自省代理：每日定时回顾或由 ``.reflect`` 命令主动触发。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Optional

from muika.config import mas_config
from muika.core.agent._prompts import REFLECTION_PROMPT
from muika.core.agent.agent import Agent
from muika.core.executor import Executor
from muika.core.memory import MemoryCategory, MemoryLayer, MemoryManager
from muika.core.state import MuikaState
from muika.core.topic_manager import TopicManager
from muika.database.crud import SelfModificationCRUD
from muika.database.db import get_session
from muika.utils.logger import logger

REFLECTION_HOUR = 5

MIN_PENDING_SESSIONS = 5

CHANGE_MEMORY_TTL_DAYS = 7

MAX_SUMMARIES = 8


def seconds_until_reflection(now: datetime) -> float:
    """返回距下一次每日反思的秒数。

    :param now: 当前本地时间
    :return: 非负等待秒数
    """
    target = now.replace(hour=REFLECTION_HOUR, minute=0, second=0, microsecond=0)
    if target < now:
        target += timedelta(days=1)
    return max(0.0, (target - now).total_seconds())


def _extract_outcome(report: str) -> str:
    """返回反思报告，空报告使用中性说明。"""
    if report.strip():
        return report.strip()
    return "I took some time to think about myself. Everything feels okay."


class ReflectionAgent:
    """定时或按命令执行自省。"""

    def __init__(
        self,
        agent: Agent,
        memory: MemoryManager,
        state: MuikaState,
        topic_manager: TopicManager,
        executor: Executor,
    ) -> None:
        self._agent = agent
        self._memory = memory
        self._state = state
        self._topics = topic_manager
        self._executor = executor
        self._running: bool = False

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

    async def _run_reflection(self, notify_user: bool = False) -> None:
        """自省执行主体：审计快照 → Agent 执行 → 审计对比 → 记忆写入 → 用户通知。"""
        if self._running:
            logger.debug("[Reflection] another reflection already running, skipping")
            return
        self._running = True
        try:
            await self._execute(notify_user)
        except Exception as e:
            logger.error(f"[Reflection] reflection failed: {e}", exc_info=True)
        finally:
            self._running = False

    async def _execute(self, notify_user: bool) -> None:
        """_run_reflection 的执行核心，集中在 try/except 内。"""
        last_at = self._last_reflection_at()
        session_summaries = await self._gather_context(last_at)
        topic_stats = await self._topics.get_engagement_stats()

        instruction = REFLECTION_PROMPT.format(
            session_summaries=session_summaries,
            topic_stats=topic_stats,
        )

        async with get_session() as db_session:
            before_records = await SelfModificationCRUD.list_recent(db_session, limit=1)
            before_id = before_records[0].id if before_records else 0

        logger.info("[Reflection] starting reflection")
        report, _ = await self._agent.execute_command(instruction, self._state, self._executor)
        logger.info(f"[Reflection] Agent report ({len(report)} chars)")

        async with get_session() as db_session:
            after_records = await SelfModificationCRUD.list_recent(db_session, limit=1)
            after_record = after_records[0] if after_records else None
            after_id = after_record.id if after_record else 0

        changed = after_id > before_id
        now = datetime.now()

        await self._memory.upsert_memory(
            layer=MemoryLayer.CORE,
            category=MemoryCategory.SELF,
            key="self_reflection_last_at",
            value=now.isoformat(),
        )

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

        if notify_user and outcome:
            await self._executor.send_message(outcome)

    async def maybe_reflect(self) -> None:
        """在开关、冷却和待处理会话满足时执行反思。"""
        if not (mas_config.enable_self_modification and mas_config.enable_auto_reflection):
            logger.debug("[Reflection] skipped: self-mod or auto-reflection disabled")
            return

        now = datetime.now()
        cooldown = timedelta(hours=mas_config.reflection_cooldown_hours)
        last_at = self._last_reflection_at()
        if last_at is not None and (now - last_at) < cooldown:
            logger.debug(f"[Reflection] skipped: cooldown not elapsed (last={last_at})")
            return

        pending = self._pending_session_count(last_at)
        if pending < MIN_PENDING_SESSIONS:
            logger.debug(f"[Reflection] skipped: only {pending}/{MIN_PENDING_SESSIONS} pending sessions")
            return

        await self._run_reflection(notify_user=False)

    async def run_daily(self) -> None:
        """每天本地时间 05:00 尝试一次自动反思。"""
        while True:
            delay = seconds_until_reflection(datetime.now())
            logger.debug(f"[Reflection] next daily check in {delay:.0f} seconds")
            await asyncio.sleep(delay)
            await self.maybe_reflect()

    async def force_reflect(self) -> None:
        """跳过全部门控，供 ``.reflect`` 命令使用。"""
        await self._run_reflection(notify_user=True)
