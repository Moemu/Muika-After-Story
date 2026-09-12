"""每日空闲整理日记，支持积压补做和手动触发。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from muika.config import mas_config
from muika.core.agent.agent import Agent
from muika.core.executor import Executor
from muika.core.memory import MemoryManager
from muika.core.state import MuikaState
from muika.utils.logger import logger

REFLECTION_HOUR = 5


def seconds_until_reflection(now: datetime) -> float:
    """返回距下一次本地时间 05:00 的秒数。"""
    target = now.replace(hour=REFLECTION_HOUR, minute=0, second=0, microsecond=0)
    if target < now:
        target += timedelta(days=1)
    return max(0.0, (target - now).total_seconds())


class ReflectionAgent:
    """拥有日记整理的串行入口和失败重试间隔。"""

    def __init__(self, agent: Agent, memory: MemoryManager, state: MuikaState, executor: Executor) -> None:
        self._agent, self._memory, self._state, self._executor = agent, memory, state, executor
        self._lock = asyncio.Lock()
        self._retry_after: datetime | None = None

    def _idle(self) -> bool:
        return (
            self._state.active_topic is None and (datetime.now() - self._state.last_interaction).total_seconds() >= 60
        )

    async def maybe_reflect(self) -> None:
        """空闲时补齐到期日期；与自我修改开关无关。"""
        if not mas_config.enable_auto_reflection or not self._idle() or self._lock.locked():
            return
        if self._retry_after is not None and datetime.now() < self._retry_after:
            return
        try:
            await self._run()
            self._retry_after = None
        except Exception as exc:
            self._retry_after = datetime.now() + timedelta(minutes=5)
            logger.exception(f"[Dream] Failed; material remains pending: {exc}")

    async def _run(self, *, manual: bool = False) -> int:
        async with self._lock:
            count = 0
            for day in await self._memory.pending_days(datetime.now(), include_today=manual):
                if not manual and not self._idle():
                    break
                logger.info(f"[Dream] Writing diary for {day}.")
                self._agent.refresh_models()
                if await self._agent.memory_reasoner.dream(day, self._memory):
                    count += 1
                    logger.info(f"Muika saved her diary for {day}.")
            return count

    async def run_daily(self) -> None:
        """启动后检查积压，并在到期后的空闲阶段重试。"""
        while True:
            await self.maybe_reflect()
            await asyncio.sleep(60)

    async def force_reflect(self) -> None:
        """手动调用同一整理事务，可包含今天已经发生的经历。"""
        try:
            count = await self._run(manual=True)
        except Exception:
            await self._executor.send_message("[System] 日记暂未保存，素材仍在，稍后可以重试。")
            raise
        await self._executor.send_message(
            f"[System] 已整理 {count} 天日记。" if count else "[System] 没有新的待整理素材。"
        )
