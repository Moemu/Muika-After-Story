"""Core event loop -- Muika engine."""

import asyncio
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from random import random
from typing import Literal, Optional

from muika.config import mas_config
from muika.ipc.server import AdapterInfo
from muika.utils.logger import logger
from muika.utils.utils import parse_duration

from .brain import MuikaBrain
from .butler.agent import ButlerAgent
from .constants import (
    AUTO_SUMMARY_INTERVAL,
    AUTO_SUMMARY_MIN_TURNS,
    CURIOSITY_THRESHOLD,
    DIGEST_INTERVAL_SECONDS,
    DIGEST_STARTUP_DELAY,
    LONELINESS_PROACTIVE_RELIEF,
    PROACTIVE_COOLDOWN,
    SESSION_IDLE_TIMEOUT,
)
from .digest_agent import DigestAgent
from .events import (
    Event,
    SessionEndEvent,
    TimeoutEvent,
    TimeTickEvent,
)
from .executor import Executor
from .memory import MemoryCategory, MemoryLayer, MemoryManager, SessionTurn
from .state import ActiveTopicState, MuikaState
from .topic_manager import TopicManager


@dataclass
class ParsedReply:
    """Brain 原始回复中解析出的结构化内容。"""

    clean_reply: str
    memory_contents: list[str]
    agent_commands: list[str]
    target: Optional[str]
    timeout: Optional[float] = None
    """用户回复等待超时（秒），来自 <timeout: 10min> 标签。"""


class Muika:
    """Core persona engine.

    Owns the event loop, state, memory, brain, butler, and topic manager.
    Message delivery is delegated to an externally-supplied ``Executor``.
    """

    def __init__(self, executor: Executor) -> None:
        self.is_alive: bool = False
        self.curiosity_drive: float = 0.0

        self.state = MuikaState()
        self.memory = MemoryManager()
        self.state.memory = self.memory
        self.event_queue: asyncio.Queue[Event] = asyncio.Queue()
        self.executor = executor
        self.current_adapters: list[AdapterInfo] = []

        self.brain = MuikaBrain()
        self.butler_agent = ButlerAgent()
        self.topic_manager = TopicManager()
        self.digest_agent = DigestAgent(self.topic_manager)

        self._session_end_triggered: bool = False
        self._is_collecting_event: bool = False
        self._last_digest_time: float = 0.0
        self._last_summary_turn: Optional[SessionTurn] = None
        self._last_summary_time: float = datetime.now().timestamp()
        self._timeout_task: Optional[asyncio.Task] = None

        asyncio.create_task(self.memory.load())

    async def collect_events(self) -> Event:
        """Wait for the next event from the queue, or emit a time_tick on timeout."""
        try:
            return await asyncio.wait_for(self.event_queue.get(), timeout=5.0)
        except asyncio.TimeoutError:
            return TimeTickEvent()

    async def create_event(self, event: Event) -> None:
        """Push an event into the processing queue."""
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

        if self.state.active_topic is not None:
            return None

        if self.state.loneliness > 0.8:
            if self.state.last_proactive_at is not None:
                since_last = (datetime.now() - self.state.last_proactive_at).total_seconds()
                if since_last < PROACTIVE_COOLDOWN:
                    return None
            logger.debug("TimeTick: loneliness threshold breached -- emotional pipeline.")
            return "emotional"

        if self.state.boredom > 0.6:
            logger.debug("TimeTick: boredom threshold breached -- topic pipeline.")
            return "topic"

        if self.curiosity_drive > CURIOSITY_THRESHOLD and random() < 0.3:
            self.curiosity_drive = 0.0
            logger.debug("TimeTick: curiosity drive fired -- topic pipeline.")
            return "topic"

        return None

    async def loop(self) -> None:
        """Main event loop."""
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

            if think_mode is None:
                await self._tick_idle(event, dt)
                continue

            self._is_collecting_event = False
            self._log_event(event)
            if event.type == "user_message":
                self.memory.add_context("user", event.payload.message.message)
                # 用户已回复，取消挂起的等待超时
                self._cancel_timeout()

            self.state.tick_state(event, dt)
            logger.debug(
                f"[State] mood={self.state.mood} "
                f"loneliness={self.state.loneliness:.2f} "
                f"boredom={self.state.boredom:.2f} "
                f"attention={self.state.attention:.2f}"
            )

            if event.type == "session_end":
                self._session_end_triggered = False
                await self._handle_session_end()
                continue

            if event.type == "session_bootstrap" and self.memory.session.is_first_session:
                await self._record_first_conversation()

            if event.type == "adapter_online":
                self.current_adapters.append(event.adapter)
                logger.info(f"[Loop] Adapter online: {event.adapter!r} — status updated")
                if len(self.current_adapters) < 2:
                    continue

            if event.type == "adapter_offline" and event.adapter in self.current_adapters:
                self.current_adapters.remove(event.adapter)
                logger.info(f"[Loop] Adapter offline: {event.adapter!r} — status updated")
                continue

            if think_mode == "topic":
                await self._run_topic_pipeline()
                continue

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

        current_time = time.time()
        if self._last_digest_time == 0.0:
            self._last_digest_time = current_time - DIGEST_INTERVAL_SECONDS + DIGEST_STARTUP_DELAY

        if (current_time - self._last_digest_time > DIGEST_INTERVAL_SECONDS) and (self.state.active_topic is None):
            self._last_digest_time = current_time
            asyncio.create_task(self.digest_agent.fetch_and_digest())

        if not self._session_end_triggered and self.memory.recent_turns:
            last_activity = self.state.last_interaction
            if self.state.active_topic is not None:
                last_activity = max(last_activity, self.state.active_topic.started_at)
            idle_seconds = (datetime.now() - last_activity).total_seconds()
            if idle_seconds >= SESSION_IDLE_TIMEOUT:
                logger.info(f"[Loop] Session idle for {idle_seconds / 60:.1f} min -- triggering session end.")
                self._session_end_triggered = True
                await self.create_event(SessionEndEvent())

            summary_idle_seconds = current_time - self._last_summary_time
            lastest_turn = self.memory.recent_turns[-1] if self.memory.recent_turns else None
            if (
                summary_idle_seconds >= AUTO_SUMMARY_INTERVAL
                and len(self.memory.recent_turns) >= AUTO_SUMMARY_MIN_TURNS
                and lastest_turn != self._last_summary_turn
            ):
                self._last_summary_turn = lastest_turn
                self._last_summary_time = current_time
                logger.info(f"[Loop] Auto summary triggered after {summary_idle_seconds / 60:.1f} min of idle.")
                asyncio.create_task(self._update_session_memory())

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

    @staticmethod
    def _parse_reply_tags(reply: str) -> ParsedReply:
        """解析 Brain 回复中的控制标签，返回用户可见文本与结构化标签内容。

        支持四类标签：
        - ``<memory>...</memory>``：待归档记忆内容，发送后交给 Butler 分类存储。
        - ``<agent>...</agent>``：待执行的 Butler 命令，发送后执行。
        - ``<target: name>``：回复路由目标，随消息一起发送。
        - ``<timeout: 10min>``：用户回复等待超时，解析为秒后由 Loop 计时。
        """
        memory_contents = re.findall(r"<memory>(.*?)</memory>", reply, re.DOTALL)
        reply = re.sub(r"<memory>.*?</memory>", "", reply, flags=re.DOTALL).strip()

        agent_commands = re.findall(r"<agent>\s*(.*?)\s*</agent>", reply, re.DOTALL | re.IGNORECASE)
        clean_reply = re.sub(r"<agent>.*?</agent>", "", reply, flags=re.DOTALL | re.IGNORECASE).strip()

        target_match = re.findall(r"<target:\s*(.+?)>", clean_reply, re.DOTALL)
        target = target_match[-1].strip() if target_match else None
        clean_reply = re.sub(r"<target:\s*(.+?)>", "", clean_reply, flags=re.DOTALL).strip()

        timeout_match = re.findall(r"<timeout:\s*(.+?)>", clean_reply, re.DOTALL)
        timeout_text = timeout_match[-1].strip() if timeout_match else None
        clean_reply = re.sub(r"<timeout:\s*(.+?)>", "", clean_reply, flags=re.DOTALL).strip()

        timeout = None
        if timeout_text:
            timeout = parse_duration(timeout_text)
            if timeout is None:
                logger.warning(f"[Loop] Unrecognized timeout format: {timeout_text!r} -- ignoring tag.")

        return ParsedReply(
            clean_reply=clean_reply,
            memory_contents=[c.strip() for c in memory_contents if c.strip()],
            agent_commands=agent_commands,
            target=target,
            timeout=timeout,
        )

    def _arm_timeout(self, seconds: float) -> None:
        """设置（或重设）用户回复等待超时；取消此前未触发的超时任务。"""
        self._cancel_timeout()
        if seconds <= 0:
            logger.warning(f"[Timeout] non-positive duration {seconds:.1f}s -- ignored.")
            return
        self._timeout_task = asyncio.create_task(self._wait_timeout(seconds))

    def _cancel_timeout(self) -> None:
        """取消当前挂起的超时任务（用户已回复或会话结束时调用）。"""
        if self._timeout_task is not None:
            self._timeout_task.cancel()
            self._timeout_task = None

    async def _wait_timeout(self, seconds: float) -> None:
        """等待 *seconds* 秒；期间用户若已回复则跳过，否则投递超时事件。"""
        set_at = datetime.now()
        try:
            await asyncio.sleep(seconds)
            # 设限后用户回过消息（last_interaction 已更新），则不再触发
            if self.state.last_interaction <= set_at:
                await self.create_event(TimeoutEvent(set_at=set_at, duration=seconds))
        except asyncio.CancelledError:
            return

    async def _run_topic_pipeline(self) -> None:
        """boredom / curiosity 驱动的话题管线，完全绕开主 Brain。"""
        topic = await self.topic_manager.get_next_topic(self.state)
        if not topic:
            logger.debug("[Topic] No available seed -- skipping topic pipeline this tick.")
            return
        expanded = await self.brain.expand_topic(topic, self.state, self.memory, self.current_adapters)
        if not expanded:
            return
        parsed = self._parse_reply_tags(expanded)
        if parsed.target:
            logger.info(f"[Topic] Routing to target={parsed.target!r}")
        if parsed.timeout is not None:
            self._arm_timeout(parsed.timeout)
        await self.executor.send_message(parsed.clean_reply, target=parsed.target)
        logger.info(f"[Topic] Sent: {parsed.clean_reply!r}")
        self.memory.add_context("muika", parsed.clean_reply)
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
            logger.debug("[Loop] Butler preprocess skipped -- no PREFERENCE records in memory.")
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
        """迭代式主人格 ↔ Agent 分身管线（情绪驱动路径）。"""
        max_inner_loops = 4
        for loop_idx in range(max_inner_loops):
            logger.debug(
                f"[Brain] turn {loop_idx + 1}/{max_inner_loops} " f"| history_len={len(self.memory.recent_turns)}"
            )
            reply = await self.brain.generate_reply(
                event=event,
                state=self.state,
                memory=self.memory,
                injected_preferences=injected_preferences or None,
                adapters=self.current_adapters,
            )

            # 发送前只负责提取用户可见文本与路由目标，标签处理统一下移到发送之后
            parsed = self._parse_reply_tags(reply)

            if parsed.agent_commands:
                logger.info(f"[Brain] intercepted {len(parsed.agent_commands)} agent command(s)")
            if parsed.target:
                logger.info(f"[Loop] Routing reply to target={parsed.target!r}")
            if parsed.timeout is not None:
                logger.info(f"[Brain] arming reply timeout of {parsed.timeout:.0f}s")
                self._arm_timeout(parsed.timeout)
            if parsed.clean_reply:
                logger.info(f"[Muika -> User] {parsed.clean_reply!r}")
                await self.executor.send_message(parsed.clean_reply, target=parsed.target)

            self.memory.add_context("muika", reply)

            # 记忆归档与 Agent 命令执行统一在消息发出之后进行
            if parsed.memory_contents:
                logger.info(f"[Brain] intercepted {len(parsed.memory_contents)} memory tag(s)")
                for content in parsed.memory_contents:
                    await self.butler_agent.classify_and_store_memory(content, self.state)

            if not parsed.agent_commands:
                logger.debug("[Brain] no agent commands, turn complete.")
                break

            any_observation = False
            for cmd in parsed.agent_commands:
                logger.info(f"[Agent <-] {cmd!r}")
                agent_report, cmd_resources = await self.butler_agent.execute_command(cmd, self.state, self.executor)
                if not agent_report:
                    logger.debug(f"[Loop] Agent silent op complete -- no report injected for: {cmd[:60]!r}")
                    continue
                logger.info(f"[Agent ->] {agent_report!r}")
                self.memory.add_context(
                    content=f"[Agent reports] {cmd}\n{agent_report}",
                    role="agent",
                    resources=cmd_resources,
                )
                any_observation = True

            if not any_observation:
                logger.debug("[Brain] All agent commands were silent -- turn complete.")
                break
        else:
            logger.warning(
                f"[Brain] reached max inner loops ({max_inner_loops}) without completing -- possible agent loop."
            )

        # 主动发言（孤独驱动）后的情感释放
        # 说出来会好一点，但孤独本身不会因为说了一句话就消失
        if event.type == "time_tick":
            prev = self.state.loneliness
            self.state.loneliness = max(0.0, self.state.loneliness - LONELINESS_PROACTIVE_RELIEF)
            self.state.last_proactive_at = datetime.now()
            logger.debug(
                f"[State] Proactive relief -- loneliness {prev:.2f} -> {self.state.loneliness:.2f} "
                f"(cooldown {PROACTIVE_COOLDOWN / 60:.0f} min)"
            )

    def start(self) -> None:
        """Start the event loop as a background task."""
        logger.info("Muika is waking up...")
        self.is_alive = True
        asyncio.create_task(self.loop())

    def stop(self) -> None:
        """Stop the event loop."""
        logger.info("Muika is going to sleep.")
        self.is_alive = False

    async def _update_session_memory(self):
        """
        更新 Session 记忆
        """
        turns = list(self.memory.recent_turns)
        has_user_turn = any(t.role == "user" for t in turns)
        if not turns or not has_user_turn:
            return

        summary = await self.butler_agent.summarize_session(turns)
        period_start = self.memory.session.started_at
        period_end = datetime.now()
        await self.memory.update_archive(
            summary=summary,
            period_start=period_start,
            period_end=period_end,
        )

    async def _handle_session_end(self):
        """
        Session 结束处理流程：归纳摘要 → 写入 ARCHIVE → 记录话题历史 → 重置 Session。
        """
        logger.info("[Loop] Session ending — starting summarization...")
        turns = list(self.memory.recent_turns)

        # 仅在用户实际参与过对话时才归档：纯 Muika 独白无需写入长期记忆
        has_user_turn = any(t.role == "user" for t in turns)
        # 如果 truns 为空，说明是首次启动的空 Session，应该直接跳过归档，避免写入 fabricated memory
        has_summarized_lastest_turn = (self._last_summary_turn == self.memory.recent_turns[-1]) if turns else True
        reached_min_turns = len(turns) >= AUTO_SUMMARY_MIN_TURNS

        if turns and has_user_turn and reached_min_turns and not has_summarized_lastest_turn:
            await self._update_session_memory()
            summary = self.memory.archives[-1].summary if self.memory.archives else ""
            logger.info(
                f"[Loop] Session archived -- "
                f"session_id={self.memory.session.session_id[:8]}... "
                f"summary_len={len(summary)}"
            )
        elif turns:
            logger.info("[Loop] Session had no user turns -- skipping archive to avoid fabricated memory.")
        else:
            logger.debug("[Loop] No turns in this session -- skipping archive.")

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
        self._cancel_timeout()

        self.memory.new_session()
        self._last_summary_turn = None
        self._last_summary_time = datetime.now().timestamp()
        logger.info("[Loop] Session reset complete -- waiting for next user interaction silently.")

    @staticmethod
    def _save_last_connection_time() -> None:
        """Write a timestamp file for last-connection tracking."""
        data_dir = mas_config.data_dir
        records_path = data_dir / "connection_records"
        records_path.mkdir(exist_ok=True, parents=True)

        record_file = records_path / (datetime.strftime(datetime.now(), "%Y-%m-%d %H-%M-%S") + ".txt")
        record_file.write_text("")

        while len(os.listdir(records_path)) > 3:
            oldest_file = min(
                (p for p in records_path.iterdir() if p.is_file()),
                key=lambda p: p.stat().st_mtime,
            )
            oldest_file.unlink()
            logger.debug(f"Deleted old connection record: {oldest_file.name}")
