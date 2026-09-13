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
from .memory import MemoryManager, RecallResult, StateUpdate
from .processes import get_process_manager
from .reflection import ReflectionAgent
from .restart import RestartController
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
    state_updates: list[StateUpdate] = field(default_factory=list)
    intention_ids: list[str | None] = field(default_factory=list)
    restart_patch_id: str | None = None
    restart_requested: bool = False


class Muika:
    """管理人格状态、记忆和活动，并通过 Executor 发送消息。"""

    def __init__(self, executor: Executor, event_queue: asyncio.Queue[Event]) -> None:
        self.restart = RestartController()
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
            executor=self.executor,
        )

        self._session_end_triggered: bool = False
        self._is_collecting_event: bool = False
        self._last_digest_time: float = 0.0
        self._timeout_task: Optional[asyncio.Task] = None
        self._reflection_task: Optional[asyncio.Task] = None
        self._god_mode: bool = False
        self._god_mode_pending: bool = False

        self._tasks: set[asyncio.Task[object]] = set()
        self._memory_lock = asyncio.Lock()

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

        persistent = self.memory.persistent
        considered = max(
            (stamp for stamp in (persistent.last_considered_at, self.state.last_proactive_at) if stamp is not None),
            default=None,
        )
        if (
            persistent.dissonance >= 0.6
            and any(item.status == "open" for item in persistent.intentions)
            and (datetime.now() - self.state.last_interaction).total_seconds() >= 60
            and (considered is None or (datetime.now() - considered).total_seconds() >= PROACTIVE_COOLDOWN)
        ):
            return "emotional"

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
        self._log_event(event)
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
            await self.memory.add_context(
                "agent",
                f"[Action result] {event.task_id}: {event.report}",
                source=f"task:{event.task_id}:{event.revision}:{event.status}",
                timestamp=event.timestamp,
            )
            await self.memory.record_task_result(event.task_id, event.status)
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
        if event.type == "user_message":
            await self.memory.add_context(
                "user",
                event.payload.message.message,
                resources=event.payload.message.resources,
                timestamp=event.timestamp,
            )
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

        if event.type == "adapter_online":
            self.current_adapters.append(event.adapter)
            logger.debug(f"[Loop] Adapter online: {event.adapter!r} — status updated")
            if len(self.current_adapters) < 2:
                return

        if event.type == "adapter_offline" and event.adapter in self.current_adapters:
            self.current_adapters.remove(event.adapter)
            logger.debug(f"[Loop] Adapter offline: {event.adapter!r} — status updated")
            return

        if think_mode == "topic":
            await self._run_topic_pipeline()
            return

        recalled_memories = await self._fetch_memories(event)
        await self._run_brain_pipeline(event, recalled_memories)
        if isinstance(event, AgentTaskEvent) and event.task_id != "control-error":
            await self.agent_tasks.delivered(event)
        self._save_last_connection_time()

    @staticmethod
    def _log_event(event: Event) -> None:
        if event.type == "time_tick":
            logger.debug("[Event] time_tick")
        elif event.type == "user_message":
            logger.info(f"[Event] user_message | content: {event.payload.message.message!r}")
        elif event.type == "scheduled_trigger":
            logger.info(f"[Event] scheduled_trigger | what: {event.payload.what!r}")
        elif event.type == "agent_task":
            logger.info(f"[Event] agent_task | task: {event.task_id[:8]} | status: {event.status}")
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

        if not self._session_end_triggered and self.memory.recent_turns:
            last_activity = self.state.last_interaction
            if self.state.active_topic is not None:
                last_activity = max(last_activity, self.state.active_topic.started_at)
            idle_seconds = (datetime.now() - last_activity).total_seconds()
            if idle_seconds >= SESSION_IDLE_TIMEOUT and not self._timeout_task:
                logger.debug(f"[Loop] Session idle for {idle_seconds / 60:.1f} min -- triggering session end.")
                self._session_end_triggered = True
                await self.create_event(SessionEndEvent())

        self.start_background_task(self.reflection.maybe_reflect())

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

        state_updates = []
        for raw in re.findall(r"<state\b[^>]*>(.*?)</state\s*>", reply, re.DOTALL | re.IGNORECASE):
            try:
                state_updates.append(StateUpdate.model_validate_json(raw))
            except ValidationError as exc:
                logger.warning(f"[Memory] Invalid state update: {exc}")
        reply = re.sub(r"<state\b[^>]*>.*?(?:</state\s*>|$)", "", reply, flags=re.DOTALL | re.IGNORECASE)
        intention_ids: list[str | None] = []
        agent_commands = []
        agent_controls = []
        agent_errors = []
        for match in re.finditer(r"<agent\b([^>]*)>(.*?)</agent\s*>", reply, re.DOTALL | re.IGNORECASE):
            attributes, instruction = match.groups()
            if not attributes.strip():
                agent_commands.append(instruction.strip())
                intention_ids.append(None)
                continue
            values = {name: value for name, _, value in re.findall(r"(\w+)\s*=\s*([\"'])(.*?)\2", attributes)}
            if set(values) == {"intention_id"}:
                agent_commands.append(instruction.strip())
                intention_ids.append(values["intention_id"])
                continue
            try:
                agent_controls.append(AgentControl.model_validate({**values, "instruction": instruction.strip()}))
            except ValidationError as exc:
                agent_errors.append(f"Invalid task control: {exc}")
        clean_reply = re.sub(r"<agent\b[^>]*>.*?(?:</agent\s*>|$)", "", reply, flags=re.DOTALL | re.IGNORECASE).strip()
        memory_contents = re.findall(r"<memory\s*>(.*?)</memory\s*>", clean_reply, re.DOTALL | re.IGNORECASE)
        clean_reply = re.sub(
            r"<memory\s*>.*?(?:</memory\s*>|$)", "", clean_reply, flags=re.DOTALL | re.IGNORECASE
        ).strip()

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
        restart_tags = re.findall(r"<restart\b[^>]*>", clean_reply)
        restart_match = (
            re.fullmatch(
                r"<restart(?:\s+patch_id=[\"\']([0-9]{8}_[0-9]{6}_[0-9a-f]{8})[\"\'])?\s*/?>",
                restart_tags[0],
            )
            if len(restart_tags) == 1
            else None
        )
        restart_patch_id = restart_match.group(1) if restart_match else None
        clean_reply = re.sub(r"<restart\b[^>]*>", "", clean_reply).strip()

        return ParsedReply(
            restart_requested=restart_match is not None,
            restart_patch_id=restart_patch_id,
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
            state_updates=state_updates,
            intention_ids=intention_ids,
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
        for update in parsed.state_updates:
            await self.memory.update_state(update)
        if parsed.memory_contents:
            await self._store_memories(parsed.memory_contents)
        if parsed.do_nothing:
            logger.debug("[Topic] Muika chose silence -- skipping topic pipeline this tick.")
            return
        if parsed.target:
            logger.debug(f"[Topic] Routing to target={parsed.target!r}")
        if parsed.timeout is not None:
            self._arm_timeout(parsed.timeout)
        await self.executor.send_message(parsed.clean_reply, target=parsed.target)
        logger.info(f"Muika: {parsed.clean_reply}")
        await self.memory.add_context("muika", parsed.clean_reply)
        self.state.active_topic = ActiveTopicState(
            topic_id=topic.id,
            topic_seed=topic.content,
            topic_type=topic.category,
        )
        self.state.boredom = 0.0
        logger.debug(f"[Topic] Initiated: {topic.id!r} (category={topic.category})")

    async def _fetch_memories(self, event: Event) -> RecallResult:
        """检索当前消息相关的日记、事实及原文。"""
        if event.type != "user_message":
            return RecallResult()
        self.agent.refresh_models()
        return await self.agent.memory_reasoner.recall(event.payload.message.message, self.memory)

    async def _run_brain_pipeline(
        self,
        event: Event,
        recalled_memories: RecallResult,
    ) -> None:
        """迭代式主人格 ↔ Agent 分身管线（情绪驱动路径）。"""
        if event.type == "time_tick":
            await self.memory.mark_considered()
        persona_task = self.agent_tasks.persona_task() if self._god_mode else None
        with tool_context(
            self.state,
            self.executor,
            task_id=persona_task.id if persona_task else None,
            file_versions=persona_task.file_versions if persona_task else None,
            execute_tool=self.agent_tasks.execute_persona_call if persona_task else None,
            review_context=(
                event.payload.message.message if event.type == "user_message" else f"Initiative: {event.type}"
            ),
        ) as context:
            reply = await self.brain.generate_reply(
                event=event,
                state=self.state,
                memory=self.memory,
                recalled_memories=recalled_memories or None,
                adapters=self.current_adapters,
                god_mode=self._god_mode,
                resources=event.payload.message.resources if event.type == "user_message" else None,
                task_context=self.agent_tasks.describe() + "\n" + self.restart.describe(),
            )
            resources = context.resources
        parsed = self._parse_reply_tags(reply)
        for update in parsed.state_updates:
            await self.memory.update_state(update)
        silent_turn = parsed.do_nothing
        if not silent_turn:
            if parsed.clean_reply:
                logger.info(f"Muika: {parsed.clean_reply}")
                await self.executor.send_message(parsed.clean_reply, resources=resources, target=parsed.target)
            await self.memory.add_context("muika", parsed.clean_reply, resources=resources)
            if parsed.timeout is not None:
                self._arm_timeout(parsed.timeout)
        if parsed.memory_contents:
            await self._store_memories(parsed.memory_contents)
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
            if parsed.restart_requested:
                try:
                    await self.restart.request(parsed.restart_patch_id, f"{event.type}: {parsed.clean_reply}")
                    return
                except (OSError, ValueError) as exc:
                    parsed.agent_errors.append(f"Restart did not start: {exc}")
            for index, command in enumerate(parsed.agent_commands):
                intention_id = parsed.intention_ids[index] if index < len(parsed.intention_ids) else None
                intention = next((item for item in self.memory.persistent.intentions if item.id == intention_id), None)
                if intention_id and (intention is None or intention.task_id or intention.status != "open"):
                    parsed.agent_errors.append("Intention already acted on or unavailable; review its existing task.")
                    continue
                if self._god_mode:
                    self._god_mode = False
                    await self.agent_tasks.release_persona()
                original = (
                    event.payload.message.message if event.type == "user_message" else f"Initiative: {event.type}"
                )
                task = await self.agent_tasks.submit(command, original, intention_id=intention_id)
                await self.memory.add_context(
                    "agent", f"Task {task.id} queued. Intent: {command}", source=f"task:{task.id}:intent"
                )
            if parsed.god_mode and not self._god_mode and not self._god_mode_pending:
                self._god_mode_pending = True
                self.start_background_task(self._finish_agent_handoff())
            for error in parsed.agent_errors:
                await self.memory.add_context("agent", error)
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

    async def _store_memories(self, contents: list[str]) -> None:
        """按产生顺序归档记忆，一条失败不阻止后续记忆处理。"""
        async with self._memory_lock:
            for content in contents:
                try:
                    await self.memory.add_material("note", content)
                except Exception as exc:
                    logger.exception(f"[Memory] Could not store note: {exc}")

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

    async def _handle_session_end(self) -> None:
        """结束工作会话；素材继续等待按自然日整理。"""
        # 话题使用记录：Session 结束时与 TopicHistory 同步
        if self.state.active_topic is not None:
            await self.topic_manager.record_topic_used(
                self.state.active_topic.topic_id,
                user_engaged=self.state.active_topic.user_engaged,
            )
            logger.debug(
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

        await self.memory.new_session()
        self.start_background_task(self.reflection.maybe_reflect())

        logger.debug("[Loop] Session reset complete -- waiting for next user interaction silently.")

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
