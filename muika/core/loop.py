import asyncio
import os
import re
import time
from datetime import datetime
from random import random
from typing import Literal, Optional

from nonebot import logger
from nonebot_plugin_localstore import get_plugin_data_dir

from .brain import MuikaBrain
from .butler.agent import ButlerAgent
from .constants import (  # noqa: F401
    CURIOSITY_THRESHOLD,
    DIGEST_INTERVAL_SECONDS,
    DIGEST_STARTUP_DELAY,
    LONELINESS_PROACTIVE_RELIEF,
    PROACTIVE_COOLDOWN,
    SESSION_IDLE_TIMEOUT,
)
from .digest_agent import DigestAgent
from .events import Event, SessionEndEvent, TimeTickEvent
from .executor import Executor
from .memory import MemoryCategory, MemoryLayer, MemoryManager
from .state import ActiveTopicState, MuikaState
from .topic_manager import TopicManager


class Muika:
    def __init__(self) -> None:
        self.is_alive: bool = False
        self.curiosity_drive: float = 0.0

        self.state = MuikaState()
        self.memory = MemoryManager()
        self.state.memory = self.memory  # 注入 memory 引用，供工具使用
        self.event_queue: asyncio.Queue[Event] = asyncio.Queue()
        self.executor = Executor(self.event_queue)

        # New "Ojou-sama & Butler" Architecture
        self.brain = MuikaBrain()
        self.butler_agent = ButlerAgent()
        self.topic_manager = TopicManager()
        self.digest_agent = DigestAgent(self.topic_manager)

        self._session_end_triggered: bool = False
        self._is_collecting_event: bool = False
        self._last_digest_time: float = 0.0
        self._digest_interval: float = DIGEST_INTERVAL_SECONDS

    async def collect_events(self) -> Event:
        try:
            return await asyncio.wait_for(self.event_queue.get(), timeout=5.0)
        except asyncio.TimeoutError:
            return TimeTickEvent()

    async def create_event(self, event: Event):
        await self.event_queue.put(event)

    def get_think_mode(self, event: Event) -> Optional[Literal["emotional", "topic"]]:
        """
        根据当前 tick 决定走哪条认知管线。

        - "emotional"：孤独感驱动，走主 Brain 管线。
        - "topic"：无聊 / 好奇心驱动，走 TopicManager 旁路管线。
        - None：空闲 tick，仅更新状态，不调用 LLM。

        非 time_tick 事件（user_message、scheduled_trigger 等）始终返回 "emotional"。
        话题活跃期间，所有 time_tick 均返回 None，防止情绪管线打断正在进行的话题。
        """
        if event.type != "time_tick":
            return "emotional"

        # While a topic is active, suppress automatic time_tick thinking.
        # User replies will re-enter via their own user_message event ("emotional" path).
        if self.state.active_topic is not None:
            return None

        if self.state.loneliness > 0.8:
            # 检查主动发言冷却期，避免连续倾诉
            if self.state.last_proactive_at is not None:
                since_last = (datetime.now() - self.state.last_proactive_at).total_seconds()
                if since_last < PROACTIVE_COOLDOWN:
                    return None
            logger.debug("TimeTick: loneliness threshold breached — emotional pipeline.")
            return "emotional"

        if self.state.boredom > 0.6:
            logger.debug("TimeTick: boredom threshold breached — topic pipeline.")
            return "topic"

        if self.curiosity_drive > CURIOSITY_THRESHOLD and random() < 0.3:
            self.curiosity_drive = 0.0
            logger.debug("TimeTick: curiosity drive fired — topic pipeline.")
            return "topic"

        return None

    async def loop(self):
        last_tick_time = time.time()

        while self.is_alive:
            current_time = time.time()
            dt = current_time - last_tick_time
            last_tick_time = current_time

            if not self._is_collecting_event:
                logger.debug("Collecting events...")
                self._is_collecting_event = True

            event = await self.collect_events()
            think_mode = self.get_think_mode(event)

            # ── Idle tick: state update and bookkeeping only ──────────────────────
            if think_mode is None:
                await self._tick_idle(event, dt)
                continue

            # ── Incoming event processing ─────────────────────────────────────────
            self._is_collecting_event = False
            self._log_event(event)
            if event.type == "user_message":
                self.memory.add_context("user", event.payload.message.message)

            self.state.tick_state(event, dt)
            logger.debug(
                f"[State] mood={self.state.mood} "
                f"loneliness={self.state.loneliness:.2f} "
                f"boredom={self.state.boredom:.2f} "
                f"attention={self.state.attention:.2f}"
            )

            # ── Session lifecycle ─────────────────────────────────────────────────
            if event.type == "session_end":
                self._session_end_triggered = False
                await self._handle_session_end()
                continue

            if event.type == "session_bootstrap" and self.memory.session.is_first_session:
                await self._record_first_conversation()

            # ── Topic pipeline (boredom / curiosity) ─────────────────────────────
            if think_mode == "topic":
                await self._run_topic_pipeline()
                continue

            # ── Emotional pipeline (main Brain + Butler) ──────────────────────────
            injected_preferences = await self._fetch_preferences(event)
            await self._run_brain_pipeline(event, injected_preferences)
            self._save_last_connection_time()

    @staticmethod
    def _log_event(event: Event) -> None:
        if event.type == "user_message":
            logger.info(f"[Event] user_message | content: {event.payload.message.message!r}")
        elif event.type == "scheduled_trigger":
            logger.info(f"[Event] scheduled_trigger | what: {event.payload.what!r}")
        else:
            logger.info(f"[Event] {event.type}")

    async def _tick_idle(self, event: Event, dt: float) -> None:
        """处理空闲 time_tick：状态衰减、session 空闲超时检测、后台阅读等。"""
        self.state.tick_state(event, dt)

        # 后台新闻阅读计划，每过一个周期且不是处于话题活跃期，则触发
        current_time = time.time()
        if self._last_digest_time == 0.0:
            # 刚启动时稍微延后一点执行
            self._last_digest_time = current_time - self._digest_interval + DIGEST_STARTUP_DELAY

        if (current_time - self._last_digest_time > self._digest_interval) and (self.state.active_topic is None):
            self._last_digest_time = current_time
            # 放到后台执行，不阻塞主 Loop  tick
            asyncio.create_task(self.digest_agent.fetch_and_digest())

        # session 空闲超时检测
        # 若话题刚发出，以话题开始时间作为活动基准，避免立即触发 session end
        if not self._session_end_triggered and self.memory.recent_turns:
            last_activity = self.state.last_interaction
            if self.state.active_topic is not None:
                last_activity = max(last_activity, self.state.active_topic.started_at)
            idle_seconds = (datetime.now() - last_activity).total_seconds()
            if idle_seconds >= SESSION_IDLE_TIMEOUT:
                logger.info(f"[Loop] Session idle for {idle_seconds / 60:.1f} min — triggering session end.")
                self._session_end_triggered = True
                await self.create_event(SessionEndEvent())

    async def _record_first_conversation(self) -> None:
        """首次 session 时将 first_conversation_time 写入 CoreIdentity 记忆。"""
        first_time = datetime.now().isoformat()
        await self.memory.upsert_memory(
            layer=MemoryLayer.CORE,
            category=MemoryCategory.USER,
            key="first_conversation_time",
            value=first_time,
        )
        logger.info(f"[Memory] Recorded first_conversation_time: {first_time}")

    async def _run_topic_pipeline(self) -> None:
        """boredom / curiosity 驱动的话题管线，完全绕开主 Brain。"""
        topic = await self.topic_manager.get_next_topic(self.state)
        if not topic:
            logger.debug("[Topic] No available seed — skipping topic pipeline this tick.")
            return
        expanded = await self.brain.expand_topic(topic, self.state, self.memory)
        if not expanded:
            return
        await self.executor.send_message(expanded)
        logger.info(f"[Topic] Sent: {expanded!r}")
        self.memory.add_context("muika", expanded)
        self.state.active_topic = ActiveTopicState(
            topic_id=topic.id,
            topic_seed=topic.content,
            topic_type=topic.category,
        )
        self.state.boredom = 0.0
        logger.info(f"[Topic] Initiated: {topic.id!r} (category={topic.category})")

    async def _fetch_preferences(self, event: Event) -> list:
        """通过 Butler 检索当前用户消息相关的 PreferenceProfile 条目。"""
        if event.type != "user_message":
            return []
        all_prefs = self.memory.get_preference_records()
        if not all_prefs:
            logger.debug("[Loop] Butler preprocess skipped — no PREFERENCE records in memory.")
            return []
        return await self.butler_agent.fetch_relevant_preferences(
            user_input=event.payload.message.message,
            preferences=all_prefs,
        )

    async def _run_brain_pipeline(
        self,
        event: Event,
        injected_preferences: list,
    ) -> None:
        """迭代式大小姐 ↔ Butler 管线（情绪驱动路径）。"""
        max_inner_loops = 4
        for loop_idx in range(max_inner_loops):
            logger.debug(f"[Brain] turn {loop_idx + 1}/{max_inner_loops} | history_len={len(self.memory.recent_turns)}")
            reply = await self.brain.generate_reply(
                event=event,
                state=self.state,
                memory=self.memory,
                injected_preferences=injected_preferences or None,
            )

            butler_commands = re.findall(r"<Butler:\s*(.+?)>", reply, re.DOTALL)
            clean_reply = re.sub(r"<Butler:\s*(.+?)>", "", reply, flags=re.DOTALL).strip()

            if butler_commands:
                logger.info(f"[Brain] intercepted {len(butler_commands)} butler command(s)")
            if reply:
                logger.info(f"[Muika → User] {clean_reply!r}")
                await self.executor.send_message(clean_reply)
                self.memory.add_context("muika", reply)
            if not butler_commands:
                logger.debug("[Brain] no butler commands, turn complete.")
                break

            any_observation = False
            for cmd in butler_commands:
                logger.info(f"[Butler ←] {cmd!r}")
                butler_report, cmd_resources = await self.butler_agent.execute_command(cmd, self.state, self.executor)
                if not butler_report:
                    logger.debug(f"[Loop] Butler silent op complete — no report injected for: {cmd[:60]!r}")
                    continue
                logger.info(f"[Butler →] {butler_report!r}")
                self.memory.add_context(
                    content=f"[Butler reports]: {butler_report}",
                    role="agent",
                    resources=cmd_resources,
                )
                any_observation = True

            if not any_observation:
                logger.debug("[Brain] All butler commands were silent — turn complete.")
                break
        else:
            logger.warning(
                f"[Brain] reached max inner loops ({max_inner_loops}) without completing — possible butler loop."
            )

        # 主动发言（孤独驱动）后的情感释放
        # 说出来会好一点，但孤独本身不会因为说了一句话就消失
        if event.type == "time_tick":
            prev = self.state.loneliness
            self.state.loneliness = max(0.0, self.state.loneliness - LONELINESS_PROACTIVE_RELIEF)
            self.state.last_proactive_at = datetime.now()
            logger.debug(
                f"[State] Proactive relief — loneliness {prev:.2f} → {self.state.loneliness:.2f} "
                f"(cooldown {PROACTIVE_COOLDOWN / 60:.0f} min)"
            )

    def start(self):
        logger.info("Muika is waking up...")
        self.is_alive = True
        asyncio.create_task(self.loop())

    def stop(self):
        logger.info("Muika is going to sleep.")
        self.is_alive = False

    async def _handle_session_end(self):
        """
        Session 结束处理流程：归纳摘要 → 写入 ARCHIVE → 记录话题历史 → 重置 Session。
        """
        logger.info("[Loop] Session ending — starting summarization...")
        turns = list(self.memory.recent_turns)

        # 仅在用户实际参与过对话时才归档：纯 Muika 独白无需写入长期记忆
        has_user_turn = any(t.role == "user" for t in turns)

        if turns and has_user_turn:
            period_start = self.memory.session.started_at
            period_end = datetime.now()
            summary = await self.butler_agent.summarize_session(turns)
            await self.memory.add_archive(
                summary=summary,
                period_start=period_start,
                period_end=period_end,
            )
            logger.info(
                f"[Loop] Session archived — "
                f"session_id={self.memory.session.session_id[:8]}... "
                f"summary_len={len(summary)}"
            )
        elif turns:
            logger.info("[Loop] Session had no user turns — skipping archive to avoid fabricated memory.")
        else:
            logger.debug("[Loop] No turns in this session — skipping archive.")

        # 话题使用记录：Session 结束时与 TopicHistory 同步
        if self.state.active_topic is not None:
            await self.topic_manager.record_topic_used(
                self.state.active_topic.topic_id,
                user_engaged=self.state.active_topic.user_engaged,
            )
            logger.info(
                f"[Topic] Recorded topic {self.state.active_topic.topic_id!r} "
                f"at session end (engaged={self.state.active_topic.user_engaged})"
            )
            self.state.active_topic = None

        # 真实对话已完整结束，孤独感归零
        self.state.loneliness = 0.0
        self.state.last_proactive_at = None

        self.memory.new_session()
        logger.info("[Loop] Session reset complete — waiting for next user interaction silently.")

    @staticmethod
    def _save_last_connection_time():
        RECORDS_PATH = get_plugin_data_dir() / "connection_records"
        RECORDS_PATH.mkdir(exist_ok=True, parents=True)

        RECORD_FILE = RECORDS_PATH / (datetime.strftime(datetime.now(), "%Y-%m-%d %H-%M-%S") + ".txt")
        RECORD_FILE.write_text("")

        # 自动删除多余的记录文件
        while len(os.listdir(RECORDS_PATH)) > 3:
            oldest_file = min((p for p in RECORDS_PATH.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime)
            oldest_file.unlink()
            logger.debug(f"Deleted old connection record: {oldest_file.name}")
