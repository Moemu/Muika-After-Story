import asyncio
import re
import time
from datetime import datetime
from random import random
from typing import Literal, Optional

from nonebot import logger

from muika.models import Message

from .brain import MuikaBrain
from .butler.agent import ButlerAgent
from .constants import (  # noqa: F401
    CURIOSITY_THRESHOLD,
    LONELINESS_PROACTIVE_RELIEF,
    PROACTIVE_COOLDOWN,
    SESSION_IDLE_TIMEOUT,
    TOPIC_FOLLOWUP_TIMEOUT,
)
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

        self._session_end_triggered: bool = False

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
            logger.debug("TimeTick: boredom threshold breached \u2014 topic pipeline.")
            return "topic"

        if self.curiosity_drive > CURIOSITY_THRESHOLD and random() < 0.3:
            self.curiosity_drive = 0.0
            logger.debug("TimeTick: curiosity drive fired \u2014 topic pipeline.")
            return "topic"

        return None

    async def loop(self):
        last_tick_time = time.time()
        inner_conversation_context: list[Message] = []

        while self.is_alive:
            current_time = time.time()
            dt = current_time - last_tick_time
            last_tick_time = current_time

            logger.debug("Collecting events...")
            event = await self.collect_events()
            think_mode = self.get_think_mode(event)

            # ── Idle tick: state update and bookkeeping only ──────────────────────
            if think_mode is None:
                await self._tick_idle(event, dt)
                continue

            # ── Incoming event processing ─────────────────────────────────────────
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
            inner_conversation_context.clear()

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
            await self._run_brain_pipeline(event, inner_conversation_context, injected_preferences)

    @staticmethod
    def _log_event(event: Event) -> None:
        if event.type == "user_message":
            logger.info(f"[Event] user_message | content: {event.payload.message.message!r}")
        elif event.type == "scheduled_trigger":
            logger.info(f"[Event] scheduled_trigger | what: {event.payload.what!r}")
        else:
            logger.info(f"[Event] {event.type}")

    async def _tick_idle(self, event: Event, dt: float) -> None:
        """处理空闲 time_tick：状态衰减、follow-up 续白、session 空闲超时检测。"""
        self.state.tick_state(event, dt)

        # follow-up 续白：话题已发出但用户尚未回复
        if self.state.active_topic is not None and not self.state.active_topic.follow_up_sent:
            since_topic = (datetime.now() - self.state.active_topic.started_at).total_seconds()
            if since_topic > TOPIC_FOLLOWUP_TIMEOUT:
                logger.info(
                    f"[Topic] Follow-up triggered for {self.state.active_topic.topic_id!r}"
                    f" ({since_topic:.0f}s since topic start)"
                )
                followup = await self.brain.expand_topic_followup(
                    seed_text=self.state.active_topic.topic_seed,
                    state=self.state,
                )
                if followup:
                    await self.executor.send_message(followup)
                    self.memory.add_context("muika", followup)
                self.state.active_topic.follow_up_sent = True  # 防止重复触发

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
        topic_seed = await self.topic_manager.get_next_topic(self.state)
        if not topic_seed:
            logger.debug("[Topic] No available seed — skipping topic pipeline this tick.")
            return
        expanded = await self.brain.expand_topic(topic_seed, self.state, self.memory)
        if not expanded:
            return
        await self.executor.send_message(expanded)
        self.memory.add_context("muika", expanded)
        self.state.active_topic = ActiveTopicState(
            topic_id=topic_seed.id,
            topic_seed=topic_seed.seed,
            topic_type=topic_seed.type,
        )
        self.state.boredom = 0.0
        logger.info(f"[Topic] Initiated: {topic_seed.id!r} (type={topic_seed.type})")

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
        inner_conversation_context: list[Message],
        injected_preferences: list,
    ) -> None:
        """迭代式大小姐 ↔ Butler 管线（情绪驱动路径）。"""
        max_inner_loops = 4
        for loop_idx in range(max_inner_loops):
            logger.debug(
                f"[Brain] turn {loop_idx + 1}/{max_inner_loops} | history_len={len(inner_conversation_context)}"
            )
            reply = await self.brain.generate_reply(
                event=event,
                state=self.state,
                memory=self.memory,
                conversation_history=inner_conversation_context,
                injected_preferences=injected_preferences or None,
            )
            inner_conversation_context.append(Message(message=reply, userid="Muika", profile="self"))

            butler_commands = re.findall(r"<Butler:\s*(.+?)>", reply, re.DOTALL)
            clean_reply = re.sub(r"<Butler:\s*(.+?)>", "", reply, flags=re.DOTALL).strip()

            if butler_commands:
                logger.info(f"[Brain] intercepted {len(butler_commands)} butler command(s)")
            if clean_reply:
                logger.info(f"[Muika → User] {clean_reply!r}")
                await self.executor.send_message(clean_reply)
                self.memory.add_context("muika", clean_reply)
            if not butler_commands:
                logger.debug("[Brain] no butler commands, turn complete.")
                break

            any_observation = False
            for cmd in butler_commands:
                logger.info(f"[Butler ←] {cmd!r}")
                butler_report: str = await self.butler_agent.execute_command(cmd, self.state, self.executor)
                if not butler_report:
                    logger.debug(f"[Loop] Butler silent op complete — no report injected for: {cmd[:60]!r}")
                    continue
                logger.info(f"[Butler →] {butler_report!r}")
                inner_conversation_context.append(
                    Message(message=f"[Butler reports]: {butler_report}", userid="System", profile="self")
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
