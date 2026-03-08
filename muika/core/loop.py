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

    async def collect_events(self) -> Event:
        try:
            return await asyncio.wait_for(self.event_queue.get(), timeout=5.0)
        except asyncio.TimeoutError:
            return TimeTickEvent()

    async def create_event(self, event: Event):
        await self.event_queue.put(event)

    def get_think_mode(self, event: Event) -> Optional[Literal["emotional", "topic"]]:
        """
        Determines the cognitive pipeline to use for the current tick.

        - "emotional": loneliness-driven, routes to the main Brain pipeline.
        - "topic":     boredom/curiosity-driven, routes to the TopicManager pipeline.
        - None:        idle tick — no LLM call, only state update and bookkeeping.

        For non-time_tick events (user_message, scheduled_trigger, etc.) always returns
        "emotional" so they are handled by the main Brain unconditionally.
        When an active topic is in flight, time_tick events are blocked to prevent
        the emotional pipeline from interrupting an ongoing topic while the user is silent.
        """
        if event.type != "time_tick":
            return "emotional"

        # While a topic is active, suppress automatic time_tick thinking.
        # User replies will re-enter via their own user_message event ("emotional" path).
        if self.state.active_topic is not None:
            return None

        if self.state.loneliness > 0.8:
            logger.debug("TimeTick: loneliness threshold breached \u2014 emotional pipeline.")
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

        # We need to maintain a short-term inner dialogue context if a conversation spans multiple Butler calls
        inner_conversation_context = []
        _session_end_triggered = False  # 防止同一 session 重复触发结束事件

        while self.is_alive:
            current_time = time.time()
            dt = current_time - last_tick_time
            last_tick_time = current_time

            logger.debug("Collecting events...")
            event = await self.collect_events()

            # ── Time tick: determine cognitive pipeline ──────────────────────────────
            think_mode: Optional[Literal["emotional", "topic"]] = "emotional"
            if event.type == "time_tick":
                think_mode = self.get_think_mode(event)
                if think_mode is None:
                    # Idle tick: update state and run bookkeeping only.
                    self.state.tick_state(event, dt)

                    # ── Follow-up 续白：话题发出后超过阈值无回复，Muika 继续独自思考 ────────────
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
                            # Mark sent regardless of content to prevent re-triggering.
                            self.state.active_topic.follow_up_sent = True

                    # ── 空闲超时检测：若本 session 有对话且超过阈値，触发 SessionEndEvent ─────────────
                    if not _session_end_triggered and self.memory.recent_turns:
                        idle_seconds = (datetime.now() - self.state.last_interaction).total_seconds()
                        if idle_seconds >= SESSION_IDLE_TIMEOUT:
                            logger.info(
                                f"[Loop] Session idle for {idle_seconds / 60:.1f} min — triggering session end."
                            )
                            _session_end_triggered = True
                            await self.create_event(SessionEndEvent())
                    continue

            if event.type == "user_message":
                logger.info(f"[Event] user_message | content: {event.payload.message.message!r}")
                self.memory.add_context("user", event.payload.message.message)
            elif event.type == "scheduled_trigger":
                logger.info(f"[Event] scheduled_trigger | what: {event.payload.what!r}")
            else:
                logger.info(f"[Event] {event.type}")

            self.state.tick_state(event, dt)
            logger.debug(
                f"[State] mood={self.state.mood} "
                f"loneliness={self.state.loneliness:.2f} "
                f"boredom={self.state.boredom:.2f} "
                f"attention={self.state.attention:.2f}"
            )

            inner_conversation_context.clear()

            # ── Session 结束事件：归纳摘要 → 写奥 Archive → 重置 Session
            if event.type == "session_end":
                _session_end_triggered = False  # 重置，下一个 session 可以重新计时
                await self._handle_session_end()
                continue  # 跳过 Brain，直接开始新 session

            # ── 首次对话：自动写入 CoreIdentity 记忆
            if event.type == "session_bootstrap" and self.memory.session.is_first_session:
                first_time = datetime.now().isoformat()
                await self.memory.upsert_memory(
                    layer=MemoryLayer.CORE,
                    category=MemoryCategory.USER,
                    key="first_conversation_time",
                    value=first_time,
                )
                logger.info(f"[Memory] Recorded first_conversation_time: {first_time}")

            # ── TopicManager 旁路：boredom/curiosity 驱动的话题触发 ──────────────────────────
            if think_mode == "topic":
                topic_seed = await self.topic_manager.get_next_topic(self.state)
                if topic_seed:
                    expanded = await self.brain.expand_topic(topic_seed, self.state, self.memory)
                    if expanded:
                        await self.executor.send_message(expanded)
                        self.memory.add_context("muika", expanded)
                        self.state.active_topic = ActiveTopicState(
                            topic_id=topic_seed.id,
                            topic_seed=topic_seed.seed,
                            topic_type=topic_seed.type,
                        )
                        self.state.boredom = 0.0
                        logger.info(f"[Topic] Initiated: {topic_seed.id!r} (type={topic_seed.type})")
                else:
                    logger.debug("[Topic] No available seed — skipping topic pipeline this tick.")
                continue  # Skip Brain on topic ticks
            # ────────────────────────────────────────────────────────────────────────

            # ── Butler 预处理：对用户输入匹配相关的 PreferenceProfile 条目
            injected_preferences = []
            if event.type == "user_message":
                all_prefs = self.memory.get_preference_records()
                if all_prefs:
                    injected_preferences = await self.butler_agent.fetch_relevant_preferences(
                        user_input=event.payload.message.message,
                        preferences=all_prefs,
                    )
                else:
                    logger.debug("[Loop] Butler preprocess skipped — no PREFERENCE records in memory.")

            # Ojou-sama conversational loop (Iterative Agent)
            max_inner_loops = 4
            for loop_idx in range(max_inner_loops):
                logger.debug(
                    f"[Brain] turn {loop_idx + 1}/{max_inner_loops} | history_len={len(inner_conversation_context)}"
                )

                # 1. Ask Ojou-sama
                reply = await self.brain.generate_reply(
                    event=event,
                    state=self.state,
                    memory=self.memory,
                    conversation_history=inner_conversation_context,
                    injected_preferences=injected_preferences or None,
                )

                # 2. Append her raw reply to the inner conversation context
                inner_conversation_context.append(Message(message=reply, userid="Muika", profile="self"))

                # 3. Intercept Butler commands — format: <Butler: command>
                butler_commands = re.findall(r"<Butler:\s*(.+?)>", reply, re.DOTALL)
                clean_reply = re.sub(r"<Butler:\s*(.+?)>", "", reply, flags=re.DOTALL).strip()

                if butler_commands:
                    logger.info(f"[Brain] intercepted {len(butler_commands)} butler command(s)")

                if clean_reply:
                    logger.info(f"[Muika → User] {clean_reply!r}")
                    await self.executor.send_message(clean_reply)
                    self.memory.add_context("muika", clean_reply)

                if not butler_commands:
                    # No butler command — conversation turn is complete
                    logger.debug("[Brain] no butler commands, turn complete.")
                    break

                # 4. Butler executes each command and feeds results back
                any_observation = False
                for cmd in butler_commands:
                    logger.info(f"[Butler ←] {cmd!r}")
                    butler_report: str = await self.butler_agent.execute_command(cmd, self.state, self.executor)
                    if not butler_report:
                        # silent 操作（如写入记忆）：不把结果注入 context，避免 Brain 第二轮审查
                        logger.debug(f"[Loop] Butler silent op complete — no report injected for: {cmd[:60]!r}")
                        continue
                    logger.info(f"[Butler →] {butler_report!r}")
                    observation = f"[Butler reports]: {butler_report}"
                    inner_conversation_context.append(Message(message=observation, userid="System", profile="self"))
                    any_observation = True

                # 如果所有 Butler 命令均为 silent（无 observation 注入），本轮已完成，无需 Brain 再次审查
                if not any_observation:
                    logger.debug("[Brain] All butler commands were silent — turn complete.")
                    break
            else:
                logger.warning(
                    f"[Brain] reached max inner loops ({max_inner_loops}) without completing — possible butler loop."
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
        Session 结束处理流程：
          1. Butler 归纳对话摘要
          2. 写入 ARCHIVE
          3. 重置 Session
          4. 发送新的 SessionBootstrapEvent
        """
        logger.info("[Loop] Session ending — starting summarization...")
        turns = list(self.memory.recent_turns)

        if turns:
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
        else:
            logger.debug("[Loop] No turns in this session — skipping archive.")

        # ── 话题开销记录：Session 结束时与 TopicHistory 同步 ─────────────────────────────
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

        self.memory.new_session()
        logger.info("[Loop] Session reset complete — waiting for next user interaction silently.")
