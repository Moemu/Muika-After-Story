"""驱动 Muika 的事件处理、对话和后台活动。"""

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from random import random
from typing import Coroutine, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

from muika.config import mas_config
from muika.models import AdapterInfo
from muika.plugin.func_call.context import tool_context
from muika.utils.logger import logger
from muika.utils.utils import parse_duration

from .agent.agent import Agent
from .agent.tasks import AgentTasks
from .brain import MuikaBrain
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
    AgentHandoffEvent,
    AgentTaskEvent,
    Event,
    SessionEndEvent,
    TimeoutEvent,
    TimeTickEvent,
)
from .executor import Executor
from .memory import (
    MemoryCategory,
    MemoryLayer,
    MemoryManager,
    MemoryRecord,
    SessionTurn,
)
from .processes import get_process_manager
from .reflection import ReflectionAgent
from .self_mod.proposals import is_core_maintenance_active
from .state import ActiveTopicState, MuikaState
from .topic_manager import TopicManager

TaskResult = TypeVar("TaskResult")


class AgentControl(BaseModel):
    """主人格对已知行动任务的内部控制。"""

    model_config = ConfigDict(extra="forbid")
    task_id: str
    action: Literal["continue", "cancel", "complete"] = "continue"
    instruction: str = ""


@dataclass
class ParsedReply:
    """Brain 原始回复中解析出的结构化内容。"""

    clean_reply: str
    memory_contents: list[str]
    agent_commands: list[str]
    target: Optional[str]
    timeout: Optional[float] = None
    """用户回复等待超时（秒），来自 <timeout: 10min> 标签。"""
    god_mode: bool = False
    """是否请求接手当前行动任务（<enable_god_mode>）。"""
    heart_cot: Optional[list[str]] = None
    do_nothing: bool = False
    """模型选择本轮沉默（<do_nothing>），不发消息不写 memory。"""
    agent_controls: list[AgentControl] = field(default_factory=list)
    agent_errors: list[str] = field(default_factory=list)


class Muika:
    """管理人格状态、记忆和活动，并通过 Executor 发送消息。"""

    def __init__(self, executor: Executor, event_queue: asyncio.Queue[Event]) -> None:
        self.is_alive: bool = False

        self.state = MuikaState()
        self.memory = MemoryManager()
        self.state.memory = self.memory
        self.event_queue = event_queue
        self.executor = executor
        self.current_adapters: list[AdapterInfo] = []

        self.brain = MuikaBrain()
        self.agent = Agent()
        self.agent_tasks = AgentTasks(self.agent, self.state, self.executor, self.event_queue)
        self.topic_manager = TopicManager()
        self.digest_agent = DigestAgent(self.topic_manager)
        self.reflection = ReflectionAgent(
            agent=self.agent,
            memory=self.memory,
            state=self.state,
            topic_manager=self.topic_manager,
            executor=self.executor,
        )

        self._session_end_triggered: bool = False
        self._is_collecting_event: bool = False
        self._last_digest_time: float = 0.0
        self._last_summary_turn: Optional[SessionTurn] = None
        self._last_summary_time: float = datetime.now().timestamp()
        self._timeout_task: Optional[asyncio.Task] = None
        self._reflection_task: Optional[asyncio.Task] = None
        self._god_mode: bool = False
        self._god_mode_pending: bool = False

        self._tasks: set[asyncio.Task[object]] = set()
        self._summary_task: Optional[asyncio.Task[bool]] = None
        self._summary_lock = asyncio.Lock()
        self._summary_retry_at: float = 0.0

    async def collect_events(self) -> Event:
        """等待队列事件，空闲超时则生成时间事件。"""
        try:
            return await asyncio.wait_for(self.event_queue.get(), timeout=5.0)
        except asyncio.TimeoutError:
            return TimeTickEvent()

    async def create_event(self, event: Event) -> None:
        """将事件放入处理队列。"""
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

        # 好奇心直接读取 state.curiosity（由工具提升、tick 衰减），命中后归零
        if self.state.curiosity > CURIOSITY_THRESHOLD and random() < 0.3:
            self.state.curiosity = 0.0
            logger.debug("TimeTick: curiosity drive fired -- topic pipeline.")
            return "topic"

        return None

    async def loop(self) -> None:
        """顺序处理事件，并隔离单次事件的失败。"""
        last_tick_time = time.time()
        await self.agent_tasks.initialize()

        while self.is_alive:
            current_time = time.time()
            dt = current_time - last_tick_time
            last_tick_time = current_time

            if not self._is_collecting_event:
                logger.debug("Collecting events...")
                self._is_collecting_event = True

            event = await self.collect_events()
            try:
                await self._process_event(event, dt)
            except Exception as exc:
                if isinstance(event, AgentTaskEvent):
                    self.agent_tasks.defer_event(event)
                logger.exception(f"[Loop] Event {event.type} failed: {exc}")

    async def _process_event(self, event: Event, dt: float) -> None:
        """处理一个事件并让单次失败停留在事件边界内。

        :param event: 待处理事件
        :param dt: 距上次循环的秒数
        """
        if is_core_maintenance_active():
            if isinstance(event, AgentTaskEvent):
                self.agent_tasks.defer_event(event)
            logger.debug(f"[Loop] Maintenance mode rejected new {event.type} work.")
            return
        if event.type in {"user_message", "adapter_online"}:
            await self.agent_tasks.notify_pending()
        if isinstance(event, AgentTaskEvent):
            if event.task_id != "control-error" and not self.agent_tasks.is_current_event(event):
                return
            self.memory.add_context("agent", f"[Action result] {event.task_id}: {event.report}")
        elif isinstance(event, AgentHandoffEvent):
            if not self._god_mode_pending:
                return
            self._god_mode = True
            self._god_mode_pending = False
        think_mode = self.get_think_mode(event)

        if think_mode is None:
            await self._tick_idle(event, dt)
            return

        self._is_collecting_event = False
        self._log_event(event)
        if event.type == "user_message":
            self.memory.add_context("user", event.payload.message.message)
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
            return

        if event.type == "session_bootstrap" and self.memory.session.is_first_session:
            await self._record_first_conversation()

        if event.type == "adapter_online":
            self.current_adapters.append(event.adapter)
            logger.info(f"[Loop] Adapter online: {event.adapter!r} — status updated")
            if len(self.current_adapters) < 2:
                return

        if event.type == "adapter_offline" and event.adapter in self.current_adapters:
            self.current_adapters.remove(event.adapter)
            logger.info(f"[Loop] Adapter offline: {event.adapter!r} — status updated")
            return

        if think_mode == "topic":
            await self._run_topic_pipeline()
            return

        injected_preferences = await self._fetch_preferences(event)
        await self._run_brain_pipeline(event, injected_preferences)
        if isinstance(event, AgentTaskEvent) and event.task_id != "control-error":
            await self.agent_tasks.delivered(event)
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
            self.start_background_task(self.digest_agent.fetch_and_digest())

        if not self._session_end_triggered and self.memory.recent_turns and time.monotonic() >= self._summary_retry_at:
            last_activity = self.state.last_interaction
            if self.state.active_topic is not None:
                last_activity = max(last_activity, self.state.active_topic.started_at)
            idle_seconds = (datetime.now() - last_activity).total_seconds()
            if idle_seconds >= SESSION_IDLE_TIMEOUT and not self._timeout_task:
                logger.info(f"[Loop] Session idle for {idle_seconds / 60:.1f} min -- triggering session end.")
                self._session_end_triggered = True
                await self.create_event(SessionEndEvent())

            summary_idle_seconds = current_time - self._last_summary_time
            latest_turn = self.memory.recent_turns[-1] if self.memory.recent_turns else None
            if (
                summary_idle_seconds >= AUTO_SUMMARY_INTERVAL
                and len(self.memory.recent_turns) >= AUTO_SUMMARY_MIN_TURNS
                and latest_turn != self._last_summary_turn
                and (self._summary_task is None or self._summary_task.done())
            ):
                self._last_summary_time = current_time
                logger.info(f"[Loop] Auto summary triggered after {summary_idle_seconds / 60:.1f} min of idle.")
                self._summary_task = self.start_background_task(self.update_session_memory())

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

        支持以下标签（标签的剥离顺序保证 heart 内容不误解析为其他标签）：
        - ``<heart>...</heart>``：私有内心独白，仅从用户可见文本剥离，不入 memory。
        - ``<do_nothing>``：本轮沉默，不发消息。
        - ``<memory>...</memory>``：待归档记忆内容，交给 Agent 分类存储。
        - ``<agent>...</agent>``：待执行的 Agent 命令，发送后执行。
        - ``<target: name>``：目标路由目标，随消息一起发送。
        - ``<timeout: 10min>``：用户回复等待超时，解析为秒后由 Loop 计时。
        - ``<enable_god_mode>``：开启上帝模式，解锁本会话的直接工具调用。
        """
        heart_pattern = r"<heart\s*>(.*?)(?:</heart\s*>|$)"
        heart_cot = re.findall(heart_pattern, reply, re.DOTALL | re.IGNORECASE)
        reply = re.sub(heart_pattern, "", reply, flags=re.DOTALL | re.IGNORECASE).strip()

        do_nothing = bool(re.search(r"<do_nothing\s*/?>", reply, re.IGNORECASE))
        reply = re.sub(r"<do_nothing\s*/?>", "", reply, flags=re.IGNORECASE).strip()

        agent_commands = []
        agent_controls = []
        agent_errors = []
        for match in re.finditer(r"<agent\b([^>]*)>(.*?)</agent\s*>", reply, re.DOTALL | re.IGNORECASE):
            attributes, instruction = match.groups()
            if not attributes.strip():
                agent_commands.append(instruction.strip())
                continue
            values = {name: value for name, _, value in re.findall(r"(\w+)\s*=\s*([\"'])(.*?)\2", attributes)}
            try:
                agent_controls.append(AgentControl.model_validate({**values, "instruction": instruction.strip()}))
            except ValidationError as exc:
                agent_errors.append(f"Invalid task control: {exc}")
        clean_reply = re.sub(r"<agent\b[^>]*>.*?(?:</agent\s*>|$)", "", reply, flags=re.DOTALL | re.IGNORECASE).strip()
        memory_contents = re.findall(r"<memory>(.*?)</memory>", clean_reply, re.DOTALL)
        clean_reply = re.sub(r"<memory>.*?</memory>", "", clean_reply, flags=re.DOTALL).strip()

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

        god_mode = bool(re.search(r"<enable_god_mode\s*/?>", clean_reply, re.IGNORECASE))
        clean_reply = re.sub(r"<enable_god_mode\s*/?>", "", clean_reply, flags=re.IGNORECASE).strip()

        return ParsedReply(
            clean_reply=clean_reply,
            memory_contents=[c.strip() for c in memory_contents if c.strip()],
            agent_commands=agent_commands,
            target=target,
            timeout=timeout,
            god_mode=god_mode,
            heart_cot=heart_cot or None,
            do_nothing=do_nothing,
            agent_controls=agent_controls,
            agent_errors=agent_errors,
        )

    def _arm_timeout(self, seconds: float) -> None:
        """设置（或重设）用户回复等待超时；取消此前未触发的超时任务。"""
        self._cancel_timeout()
        if seconds <= 0:
            logger.warning(f"[Timeout] non-positive duration {seconds:.1f}s -- ignored.")
            return
        self._timeout_task = self.start_background_task(self._wait_timeout(seconds))

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
        if parsed.do_nothing:
            logger.info("[Topic] Muika chose silence -- skipping topic pipeline this tick.")
            return
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

    async def _fetch_preferences(self, event: Event) -> list[MemoryRecord]:
        """通过 Agent 检索当前用户消息相关的 PreferenceProfile 条目。"""
        if event.type != "user_message":
            return []
        all_prefs = self.memory.get_preference_records()
        if not all_prefs:
            logger.debug("[Loop] Agent preprocess skipped -- no PREFERENCE records in memory.")
            return []
        return await self.agent.fetch_relevant_preferences(
            user_input=event.payload.message.message,
            preferences=all_prefs,
        )

    async def _run_brain_pipeline(
        self,
        event: Event,
        injected_preferences: list[MemoryRecord],
    ) -> None:
        """迭代式主人格 ↔ Agent 分身管线（情绪驱动路径）。"""
        persona_task = self.agent_tasks.persona_task() if self._god_mode else None
        with tool_context(
            self.state,
            self.executor,
            task_id=persona_task.id if persona_task else None,
            file_versions=persona_task.file_versions if persona_task else None,
            execute_tool=self.agent_tasks.execute_persona_call if persona_task else None,
        ) as context:
            reply = await self.brain.generate_reply(
                event=event,
                state=self.state,
                memory=self.memory,
                injected_preferences=injected_preferences or None,
                adapters=self.current_adapters,
                god_mode=self._god_mode,
                task_context=self.agent_tasks.describe(),
            )
            resources = context.resources
        parsed = self._parse_reply_tags(reply)
        silent_turn = parsed.do_nothing
        if not silent_turn:
            if parsed.clean_reply:
                logger.info(f"[Muika -> User] {parsed.clean_reply!r}")
                await self.executor.send_message(parsed.clean_reply, resources=resources, target=parsed.target)
            self.memory.add_context("muika", reply, resources=resources)
            if parsed.timeout is not None:
                self._arm_timeout(parsed.timeout)
        for content in parsed.memory_contents:
            await self.agent.classify_and_store_memory(content, self.state)
        if not silent_turn:
            for control in parsed.agent_controls:
                try:
                    if control.action == "complete":
                        await self.agent_tasks.complete_handoff(control.task_id, control.instruction)
                        self._god_mode = False
                    else:
                        await self.agent_tasks.update(
                            control.task_id, control.instruction, cancel=control.action == "cancel"
                        )
                        if control.action == "continue" and self._god_mode:
                            self._god_mode = False
                            await self.agent_tasks.release_persona()
                except (KeyError, ValueError) as exc:
                    parsed.agent_errors.append(f"Task control failed: {exc}")
            for command in parsed.agent_commands:
                if self._god_mode:
                    self._god_mode = False
                    await self.agent_tasks.release_persona()
                original = (
                    event.payload.message.message if event.type == "user_message" else f"Initiative: {event.type}"
                )
                task = await self.agent_tasks.submit(command, original)
                self.memory.add_context("agent", f"Task {task.id} queued. Follow this task for related updates.")
            if parsed.god_mode and not self._god_mode and not self._god_mode_pending:
                self._god_mode_pending = True
                self.start_background_task(self._finish_agent_handoff())
            for error in parsed.agent_errors:
                self.memory.add_context("agent", error)
                logger.warning(f"[AgentTask] {error}")
                if not isinstance(event, AgentTaskEvent) or event.task_id != "control-error":
                    await self.create_event(AgentTaskEvent("control-error", 0, "failed", error))

        # 主动发言（孤独驱动）后的情感释放
        # 说出来会好一点，但孤独本身不会因为说了一句话就消失
        if event.type == "time_tick":
            if not silent_turn:
                prev = self.state.loneliness
                self.state.loneliness = max(0.0, self.state.loneliness - LONELINESS_PROACTIVE_RELIEF)
                logger.debug(
                    f"[State] Proactive relief -- loneliness {prev:.2f} -> {self.state.loneliness:.2f} "
                    f"(cooldown {PROACTIVE_COOLDOWN / 60:.0f} min)"
                )
            # 沉默时仍打 cooldown 戳，避免每个 tick 都连续触发 LLM 调用
            self.state.last_proactive_at = datetime.now()

    def start_background_task(self, coroutine: Coroutine[object, object, TaskResult]) -> asyncio.Task[TaskResult]:
        """启动核心所属的后台任务，并在退出时统一回收。"""
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._finish_background_task)
        return task

    def _finish_background_task(self, task: asyncio.Task[object]) -> None:
        """移除已结束的任务并记录未处理的失败。"""
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error(f"[Loop] Background task failed: {error}")

    async def _finish_agent_handoff(self) -> None:
        snapshot = await self.agent_tasks.handoff()
        if not self._god_mode_pending:
            await self.agent_tasks.release_persona()
            return
        await self.create_event(AgentHandoffEvent(snapshot))

    def start(self) -> None:
        """启动主循环和定期自省任务。"""
        if self.is_alive:
            return
        logger.info("Muika is waking up...")
        self.is_alive = True
        self.start_background_task(self.agent_tasks.run())
        self.start_background_task(self.loop())
        self._reflection_task = self.start_background_task(self.reflection.run_daily())

    async def stop(self) -> None:
        """取消并等待所有核心任务结束。"""
        logger.info("Muika is going to sleep.")
        self.is_alive = False
        await self.agent_tasks.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        await get_process_manager().close()
        self._timeout_task = None
        self._reflection_task = None
        self._summary_task = None

    async def update_session_memory(self) -> bool:
        """尝试归档当前对话，失败后保留会话并等待下一个摘要间隔。

        :return: 对话已归档或无需归档时返回 True，等待重试时返回 False。
        """
        async with self._summary_lock:
            turns = list(self.memory.recent_turns)
            if not turns or not any(turn.role == "user" for turn in turns):
                return True
            if self._last_summary_turn == turns[-1]:
                return True
            if time.monotonic() < self._summary_retry_at:
                return False
            try:
                summary = await self.agent.summarize_session(turns)
                await self.memory.update_archive(
                    summary=summary,
                    period_start=self.memory.session.started_at,
                    period_end=datetime.now(),
                )
            except Exception as error:
                self._summary_retry_at = time.monotonic() + AUTO_SUMMARY_INTERVAL
                logger.warning(
                    f"[Memory] Session archive failed; keeping turns and retrying "
                    f"after {AUTO_SUMMARY_INTERVAL:.0f}s: {error}"
                )
                return False
            self._last_summary_turn = turns[-1]
            self._last_summary_time = datetime.now().timestamp()
            self._summary_retry_at = 0.0
            return True

    async def _handle_session_end(self) -> None:
        """
        Session 结束处理流程：归纳摘要 → 写入 ARCHIVE → 记录话题历史 → 重置 Session。
        """
        logger.info("[Loop] Session ending — starting summarization...")
        turns = list(self.memory.recent_turns)

        # 仅在用户实际参与过对话时才归档：纯 Muika 独白无需写入长期记忆
        has_user_turn = any(t.role == "user" for t in turns)
        has_summarized_latest_turn = (self._last_summary_turn == self.memory.recent_turns[-1]) if turns else True
        reached_min_turns = len(turns) >= AUTO_SUMMARY_MIN_TURNS

        if turns and has_user_turn and reached_min_turns and not has_summarized_latest_turn:
            if not await self.update_session_memory():
                return
            summary = self.memory.archives[-1].summary if self.memory.archives else ""
            logger.info(
                f"[Loop] Session archived -- "
                f"session_id={self.memory.session.session_id[:8]}... "
                f"summary_len={len(summary)}"
            )
        elif turns:
            logger.debug("[Loop] No new summary needed for this session.")
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

        # 真实对话已完整结束，孤独感归零；上帝模式仅限本会话，随会话结束复位
        self.state.loneliness = 0.0
        self.state.last_proactive_at = None
        self._cancel_timeout()
        self._god_mode = False
        self._god_mode_pending = False
        await self.agent_tasks.release_persona()

        self.memory.new_session()
        self._summary_retry_at = 0.0
        self._last_summary_turn = None
        self._last_summary_time = datetime.now().timestamp()

        logger.info("[Loop] Session reset complete -- waiting for next user interaction silently.")

    @staticmethod
    def _save_last_connection_time() -> None:
        """保存最近连接时间，并清理较早的记录。"""
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
