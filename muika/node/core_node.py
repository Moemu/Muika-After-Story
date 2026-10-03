"""向固定状态入口连接的 Core 候选节点和认知检查点运行时。"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from muika.config import ModelConfigManager, cognitive_workspace
from muika.core.agent.task_store import TaskControl, TaskRecord
from muika.core.devices import ExecutionEnvironment
from muika.core.events import (
    OBSERVATION_TYPES,
    AgentHandoffEvent,
    AgentTaskEvent,
    Event,
    RuntimeObservationEvent,
    ScheduledTriggerEvent,
    ScheduledTriggerPayload,
    SessionBootstrapEvent,
    SessionEndEvent,
    TimeoutEvent,
    TimeTickEvent,
    UserMessageEvent,
    UserMessagePayload,
)
from muika.core.executor import Executor
from muika.core.loop import Muika
from muika.core.memory import MemoryManager
from muika.core.memory_models import StateUpdate
from muika.core.runtime import EventHandler, Generation, RuntimeControls, TaskIntent
from muika.core.state import ActiveTopicState, MuikaState
from muika.database.activity import ActivityOperation, ActivityResult, route_activity
from muika.ipc.node_client import NodeClient, NodeRequestError
from muika.ipc.node_protocol import (
    Acquire,
    ActivityRequest,
    Claim,
    CoreReady,
    Emit,
    ExecutionRequest,
    Handoff,
    LoadBundle,
    LoadRuntime,
    NodeStatus,
    PublishEvent,
    RegisterNode,
    Renew,
    SaveBundle,
    SaveRuntime,
    Status,
    TurnRequest,
)
from muika.llm._schema import MediaReference, ToolCall, ToolResult
from muika.llm.utils.tools import route_tools
from muika.models import AdapterInfo, Message, Resource
from muika.plugin.command import CommandDispatcher
from muika.plugin.func_call import get_function_calls, tool_catalog
from muika.plugin.func_call.context import ToolContext, get_dependencies
from muika.plugin.loader import get_plugins, load_plugins
from muika.plugin.skills import cognitive_skills
from muika.utils.logger import logger

from .bundle import CognitiveBundle
from .clock import local_time, utc_time
from .config import PluginBinding
from .event_protocol import RuntimeEvent
from .execution_protocol import ExecutionSpec, InspectExecution, SubmitExecution
from .executor_node import ExecutorWorker, capabilities
from .models import ClaimedMessage, CoreLease, IncomingMessage, OutgoingMessage
from .plugins import load_node_plugins, unload_node_plugins
from .remote_memory import RemoteMemoryManager
from .remote_processes import RemoteTaskProcesses
from .remote_scheduler import RemoteScheduler
from .remote_tasks import RemoteTaskStore
from .task_protocol import TransferFile
from .task_service import task_media
from .tool_resources import map_resource_values
from .turn_protocol import (
    ActiveTopicSnapshot,
    ClientRoute,
    CompleteTurn,
    GeneratedReply,
    LoadTurn,
    RuntimeSnapshot,
    SaveGeneration,
    TurnRecord,
)


@dataclass
class TurnScope:
    claim: ClaimedMessage
    record: TurnRecord
    committed: bool = False
    replies: list[OutgoingMessage] = field(default_factory=list)


_turn_scope: ContextVar[TurnScope | None] = ContextVar("node_turn", default=None)


class CoreNode:
    """只有活动任期才构造人格、启动插件活动和执行任务。"""

    def __init__(
        self,
        address: str,
        token: str,
        node_id: str,
        directory: Path,
        *,
        lease_seconds: float = 15,
        ca_file: Path | None = None,
        plugins: list[PluginBinding] | None = None,
    ) -> None:
        self.address, self.token, self.node_id, self.directory = address, token, node_id, directory
        self.lease_seconds = lease_seconds
        self.ca_file = ca_file
        self.plugins = plugins or []
        self.nodes: list[NodeStatus] = []
        self.client: NodeClient | None = None
        self.lease: CoreLease | None = None
        self.muika: Muika | None = None
        self.ready = asyncio.Event()
        self.stopping = asyncio.Event()
        self.snapshot = RuntimeSnapshot()
        self._runtime_lock = asyncio.Lock()
        self._deliveries: dict[int, tuple[ClaimedMessage, asyncio.Future[None]]] = {}
        self.worker: ExecutorWorker | None = None
        self._handoff_target: str | None = None
        self.bundle: CognitiveBundle | None = None
        self.model_manager: ModelConfigManager | None = None
        self.workspace = self.directory / "cognitive"

    def connection(self) -> NodeClient:
        if self.client is None:
            raise ConnectionError("Core node is not connected.")
        return self.client

    def epoch(self) -> int:
        if self.lease is None:
            raise ConnectionError("Core node has no active lease.")
        return self.lease.epoch

    def memory(self) -> RemoteMemoryManager:
        if self.muika is None or not isinstance(self.muika.memory, RemoteMemoryManager):
            raise RuntimeError("Core node memory is not ready.")
        return self.muika.memory

    async def run(self) -> None:
        """持续连接并参与候选竞争；断线不执行人格分支。"""
        self.directory.mkdir(parents=True, exist_ok=True)
        while not self.stopping.is_set():
            try:
                async with NodeClient(self.address, self.token, ca_file=self.ca_file) as client:
                    self.client = client
                    await client.request(RegisterNode(tools=capabilities(), environment=ExecutionEnvironment.local()))
                    self.nodes = (await client.request(Status())).nodes
                    self.worker = ExecutorWorker(client, self.directory)
                    execution = asyncio.create_task(self.worker.run())
                    try:
                        while not self.stopping.is_set():
                            if execution.done():
                                await execution
                            lease = (await client.request(Acquire(duration=self.lease_seconds))).lease
                            if lease is None:
                                await asyncio.sleep(0.5)
                                continue
                            if lease.owner != self.node_id:
                                raise ValueError("Configured node identity differs from its paired credential.")
                            self.lease = lease
                            await self.active()
                    finally:
                        execution.cancel()
                        await asyncio.gather(execution, return_exceptions=True)
                        await self.worker.close()
                        self.worker = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[Node] {self.node_id} unavailable: {type(exc).__name__}: {exc}")
                await asyncio.sleep(1)
            finally:
                self.client, self.lease = None, None
                self.ready.clear()

    async def renew(self) -> None:
        while not self.stopping.is_set():
            await asyncio.sleep(self.lease_seconds / 3)
            self.lease = (await self.connection().request(Renew(epoch=self.epoch(), duration=self.lease_seconds))).lease
            if self.lease is None:
                raise ConnectionError("State service omitted the renewed lease.")
            if self.muika is not None and not self._runtime_lock.locked():
                await self.checkpoint()

    async def active(self) -> None:
        renewal = asyncio.create_task(self.renew())
        try:
            await self.prepare_active(renewal)
        finally:
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)

    async def prepare_active(self, renewal: asyncio.Task[None]) -> None:
        self.bundle = (await self.connection().request(LoadBundle())).bundle
        manager = None
        with ExitStack() as stack:
            if self.bundle is not None:
                self.bundle.materialize(self.workspace)
                self.bundle.apply_settings()
                manager = ModelConfigManager(self.workspace / "configs/models.yml")
                self.model_manager = manager
                stack.enter_context(cognitive_workspace(self.workspace, manager))
                stack.enter_context(cognitive_skills(self.workspace / "configs/skills"))
            stack.enter_context(route_tools(self.execute_tool))
            stack.enter_context(route_activity(self.activity))
            stack.enter_context(tool_catalog(self.tools, self.read_only_tool))
            try:
                await self.active_runtime(renewal)
            finally:
                if manager is not None:
                    manager.stop_watcher()
                self.model_manager = None

    async def activity(self, operation: ActivityOperation) -> ActivityResult:
        result = await self.connection().request(ActivityRequest(epoch=self.epoch(), body=operation))
        if result.activity is None:
            raise ConnectionError("State service omitted the activity result.")
        return result.activity

    async def active_runtime(self, renewal: asyncio.Task[None]) -> None:
        pump: asyncio.Task[None] | None = None
        plugins_before = set(get_plugins())
        owned_plugins: list[str] = []
        try:
            memory = RemoteMemoryManager(self.connection(), self.epoch(), self.directory / "resources")
            await memory.load()
            runtime = (await self.connection().request(LoadRuntime(epoch=self.epoch()))).runtime
            self.snapshot = runtime or RuntimeSnapshot()
            if runtime is None and memory.recent_turns:
                self.snapshot.last_interaction = memory.recent_turns[-1].timestamp
            if self.snapshot.selected_executor is None:
                self.snapshot.selected_executor = self.node_id
            queue: asyncio.Queue[Event] = asyncio.Queue()
            executor = Executor(queue, self.send_message)
            executor.devices = self
            executor.scheduler = RemoteScheduler(queue, self.connection(), self.epoch(), lambda: self.snapshot.route)
            self.muika = Muika(
                executor,
                queue,
                memory=memory,
                task_store=RemoteTaskStore(self.connection(), self.epoch(), self.directory / "resources"),
                runtime=self,
                processes=RemoteTaskProcesses(MuikaState(), executor, self.execute_tool),
            )
            self.restore_state()
            if isinstance(self.muika.agent_tasks.processes, RemoteTaskProcesses):
                self.muika.agent_tasks.processes.state = self.muika.state
            CommandDispatcher.setup(self.muika, self.send_command_result)
            load_plugins(
                Path(__file__).resolve().parents[1] / "builtin_plugins", base_path=Path(__file__).resolve().parents[2]
            )
            owned_plugins = [module for module in get_plugins() if module not in plugins_before]
            owned_plugins.extend(load_node_plugins(self.plugins, "core"))
            await self.connection().request(
                RegisterNode(tools=capabilities(), environment=ExecutionEnvironment.local())
            )
            self.nodes = (await self.connection().request(Status())).nodes
            await self.muika.agent_tasks.initialize()
            self.muika.resume_controls()
            await self.connection().request(CoreReady(epoch=self.epoch()))
            with route_tools(self.execute_tool):
                self.muika.start()
            self.ready.set()
            logger.info(f"[Node] Core active: {self.node_id}, epoch={self.epoch()}")
            pump = asyncio.create_task(self.receive())
            stop = asyncio.create_task(self.stopping.wait())
            try:
                done, _ = await asyncio.wait([renewal, pump, stop], return_when=asyncio.FIRST_COMPLETED)
                for completed_job in done:
                    if completed_job is not stop:
                        await completed_job
            finally:
                stop.cancel()
                await asyncio.gather(stop, return_exceptions=True)
        finally:
            self.ready.clear()
            for owned_job in (renewal, pump):
                if owned_job is not None:
                    owned_job.cancel()
            await asyncio.gather(*(task for task in (renewal, pump) if task is not None), return_exceptions=True)
            if self.muika is not None:
                await self.muika.stop()
                await self.muika.executor.scheduler.close()
                self.muika = None
            self._deliveries.clear()
            unload_node_plugins([module for module in get_plugins() if module not in plugins_before])

    def tools(self) -> list[dict[str, Any]]:
        current: dict[str, dict[str, Any]] = {}
        for node in self.nodes:
            if not node.connected or not node.compatible:
                continue
            for capability in node.capabilities:
                if capability.scope == "device" or node.id == self.node_id:
                    current[capability.name] = dict(capability.tool_schema)
        return list(current.values())

    def read_only_tool(self, name: str) -> bool:
        matches = [
            tool
            for node in self.nodes
            if node.connected and node.compatible
            for tool in node.capabilities
            if tool.name == name
        ]
        return bool(matches) and all(tool.retry == "read_only" for tool in matches)

    async def commit_tool_state(self) -> None:
        if self.bundle is not None:
            updated = CognitiveBundle.capture(self.workspace)
            if self.model_manager is not None:
                updated.settings.heartbeat_intensity = self.model_manager.heart_intensity
            if updated != self.bundle:
                await self.connection().request(
                    SaveBundle(
                        epoch=self.epoch(),
                        expected_digest=self.bundle.digest,
                        bundle=updated,
                    )
                )
                self.bundle = updated
        await self.save_runtime()

    def restore_state(self) -> None:
        if self.muika is None:
            return
        state, saved = self.muika.state, self.snapshot
        state.attention, state.loneliness, state.curiosity, state.boredom = (
            saved.attention,
            saved.loneliness,
            saved.curiosity,
            saved.boredom,
        )
        state.last_interaction, state.last_proactive_at = saved.last_interaction, saved.last_proactive_at
        state.active_topic = ActiveTopicState(**saved.active_topic.model_dump()) if saved.active_topic else None
        self._handoff_target = saved.handoff_target
        self.muika.restore_controls(
            RuntimeControls(
                saved.god_mode,
                saved.god_mode_pending,
                saved.session_end_triggered,
                saved.timeout_set_at,
                saved.timeout_seconds,
            )
        )

    async def save_runtime(self) -> None:
        """保存当前检查点，避免覆盖尚未在本节点应用的回合。"""
        async with self._runtime_lock:
            await self.connection().request(SaveRuntime(epoch=self.epoch(), runtime=self.runtime_snapshot()))

    async def checkpoint(self) -> None:
        """保存补充检查点，临时锁冲突留待下一次保存。"""
        try:
            await self.save_runtime()
        except NodeRequestError as exc:
            if exc.code != "checkpoint_unavailable":
                raise
            logger.warning(f"[Node] Runtime checkpoint deferred: {exc}")

    def runtime_snapshot(self) -> RuntimeSnapshot:
        if self.muika is None:
            return self.snapshot.model_copy(deep=True)
        state = self.muika.state
        controls = self.muika.runtime_controls()
        topic = state.active_topic
        active_topic = (
            ActiveTopicSnapshot(
                topic_id=topic.topic_id,
                topic_seed=topic.topic_seed,
                topic_type=topic.topic_type,
                started_at=topic.started_at,
                user_engaged=topic.user_engaged,
            )
            if topic
            else None
        )
        return self.snapshot.model_copy(
            deep=True,
            update={
                "god_mode": controls.god_mode,
                "god_mode_pending": controls.god_mode_pending,
                "session_end_triggered": controls.session_end_triggered,
                "timeout_set_at": controls.timeout_set_at,
                "timeout_seconds": controls.timeout_seconds,
                "handoff_target": self._handoff_target,
                "attention": state.attention,
                "loneliness": state.loneliness,
                "curiosity": state.curiosity,
                "boredom": state.boredom,
                "last_interaction": state.last_interaction,
                "last_proactive_at": state.last_proactive_at,
                "active_topic": active_topic,
            },
        )

    async def receive(self) -> None:
        while not self.stopping.is_set():
            if self._handoff_target is not None:
                if self.muika is None:
                    raise RuntimeError("Core is not ready for handoff.")
                await self.muika.agent_tasks.pause_for_core_handoff()
                await self.save_runtime()
                target = self._handoff_target
                response = await self.connection().request(Handoff(epoch=self.epoch(), target=target))
                self._handoff_target = None
                self.snapshot.handoff_id = None
                if response.handoff_accepted is False:
                    controls = self.muika.runtime_controls()
                    self.muika.agent_tasks.restore_persona_owner(controls.god_mode or controls.god_mode_pending)
                    continue
                if response.handoff_accepted is not True:
                    raise ConnectionError("State service omitted the handoff outcome.")
                raise ConnectionError(f"Core handoff requested: {target}")
            claim = (await self.connection().request(Claim(epoch=self.epoch()))).claim
            if claim is None:
                await asyncio.sleep(0.1)
                continue
            if self.muika is None:
                raise RuntimeError("Core disappeared during input processing.")
            status = await self.connection().request(Status())
            self.nodes = status.nodes
            self.muika.current_adapters = [
                AdapterInfo(node.id) for node in status.nodes if node.role == "bot" and node.connected
            ]
            self.snapshot.route = ClientRoute(
                client_id=claim.message.client_id, conversation_id=claim.message.conversation_id
            )
            if claim.message.kind == "command":
                await self.command(claim)
                continue
            if claim.message.kind == "session_bootstrap" and self.snapshot.bootstrapped:
                await self.complete_empty(claim)
                continue
            event = await self.event(claim.message, claim.sequence)
            finished = asyncio.get_running_loop().create_future()
            self._deliveries[id(event)] = (claim, finished)
            await self.muika.create_event(event)
            await finished

    async def list_devices(self) -> str:
        nodes = (await self.connection().request(Status())).nodes
        return json.dumps(
            {
                "active_core": self.node_id,
                "selected_executor": self.snapshot.selected_executor,
                "devices": [node.model_dump(mode="json") for node in nodes],
            },
            ensure_ascii=False,
        )

    async def execution_environment(self, task: TaskRecord) -> ExecutionEnvironment:
        self.nodes = (await self.connection().request(Status())).nodes
        node = next((node for node in self.nodes if node.id == task.execution_node_id), None)
        if node is None or not node.connected or not node.compatible or node.environment is None:
            return ExecutionEnvironment(available=False)
        return node.environment

    async def select_device(self, id: str) -> str:
        nodes = (await self.connection().request(Status())).nodes
        if not any(
            node.id == id and node.role in {"core", "executor"} and node.connected and node.compatible for node in nodes
        ):
            raise ValueError("Selected execution device is unavailable or incompatible.")
        async with self._runtime_lock:
            self.snapshot.selected_executor = id
            await self.connection().request(SaveRuntime(epoch=self.epoch(), runtime=self.runtime_snapshot()))
        return f"New action tasks will run on {id}. Existing tasks keep their device."

    async def request_handoff(self, id: str, reason: str) -> str:
        nodes = (await self.connection().request(Status())).nodes
        if id == self.node_id:
            return "You are already active on this device."
        if not any(node.id == id and node.role == "core" and node.connected and node.compatible for node in nodes):
            raise ValueError("Handoff target is unavailable or incompatible.")
        scope = _turn_scope.get()
        identity = f"handoff:{scope.record.turn_id if scope else uuid4().hex}:{id}"
        async with self._runtime_lock:
            self._handoff_target = id
            self.snapshot.handoff_id = identity
            await self.connection().request(SaveRuntime(epoch=self.epoch(), runtime=self.runtime_snapshot()))
        await self.memory().add_material(
            "agent",
            f"Core handoff requested: {self.node_id} -> {id}. Reason: {reason}",
            source=identity,
        )
        return f"Handoff to {id} is requested. It will happen after this reply and the current action boundary."

    async def event(self, message: IncomingMessage, sequence: int) -> Event:
        timestamp = local_time(message.occurred_at)
        if message.kind == "user_message":
            resources = [
                await self.connection().download_resource(reference, self.memory().vault)
                for reference in message.resources
            ]
            return UserMessageEvent(
                UserMessagePayload(Message(message=message.text, resources=resources)),
                timestamp=timestamp,
                source=f"input:{sequence}",
            )
        if message.kind == "session_bootstrap":
            self.snapshot.bootstrapped = True
            return SessionBootstrapEvent(
                timestamp=timestamp,
                last_chat_time=self.snapshot.last_interaction if self.memory().has_history else None,
            )
        if message.kind == "session_end":
            return SessionEndEvent(timestamp=timestamp)
        event = message.event
        if event is None:
            raise ValueError("Internal event payload is missing.")
        if event.type == "time_tick":
            return TimeTickEvent(timestamp=local_time(event.timestamp), think_mode=event.think_mode)
        if event.type == "scheduled_trigger":
            return ScheduledTriggerEvent(
                ScheduledTriggerPayload(event.when, event.what), timestamp=local_time(event.timestamp)
            )
        if event.type == "session_end":
            return SessionEndEvent(timestamp=local_time(event.timestamp))
        if event.type == "agent_task":
            return AgentTaskEvent(
                event.task_id, event.revision, event.status, event.report, timestamp=local_time(event.timestamp)
            )
        if event.type == "agent_handoff":
            return AgentHandoffEvent(event.report, timestamp=local_time(event.timestamp))
        for observation_type in OBSERVATION_TYPES:
            if event.type == observation_type:
                return RuntimeObservationEvent(
                    event.report,
                    source=f"input:{sequence}:observation",
                    type=observation_type,
                    timestamp=local_time(event.timestamp),
                )
        if event.set_at is None:
            raise ValueError("Timeout origin is missing.")
        return TimeoutEvent(local_time(event.set_at), event.duration, timestamp=local_time(event.timestamp))

    def task_route(self, task_id: str | None) -> ClientRoute | None:
        task = self.muika.agent_tasks.tasks.get(task_id) if self.muika is not None and task_id else None
        if task is not None and task.reply_client_id and task.reply_conversation_id:
            return ClientRoute(client_id=task.reply_client_id, conversation_id=task.reply_conversation_id)
        return self.snapshot.route

    async def publish(self, event: Event) -> None:
        route = self.task_route(event.task_id) if isinstance(event, AgentTaskEvent) else self.snapshot.route
        if route is None:
            return
        payload = RuntimeEvent(
            type="time_tick",
            timestamp=event.timestamp,
            think_mode=event.think_mode if isinstance(event, TimeTickEvent) else None,
        )
        identity = uuid4().hex
        if isinstance(event, AgentTaskEvent):
            payload = RuntimeEvent(
                type="agent_task",
                timestamp=event.timestamp,
                task_id=event.task_id,
                revision=event.revision,
                status=event.status,
                report=event.report,
            )
            identity = f"task:{event.task_id}:{event.revision}:{event.status}"
        elif isinstance(event, AgentHandoffEvent):
            payload = RuntimeEvent(type="agent_handoff", timestamp=event.timestamp, report=event.report)
        elif isinstance(event, RuntimeObservationEvent):
            payload = RuntimeEvent(type=event.type, timestamp=event.timestamp, report=event.report)
            identity = event.source
        elif isinstance(event, ScheduledTriggerEvent):
            payload = RuntimeEvent(
                type="scheduled_trigger", timestamp=event.timestamp, when=event.payload.when, what=event.payload.what
            )
        elif isinstance(event, SessionEndEvent):
            payload = RuntimeEvent(type="session_end", timestamp=event.timestamp)
        elif isinstance(event, TimeoutEvent):
            payload = RuntimeEvent(
                type="timeout", timestamp=event.timestamp, set_at=event.set_at, duration=event.duration
            )
            identity = f"timeout:{utc_time(event.set_at)}"
        elif not isinstance(event, TimeTickEvent):
            raise ValueError(f"Event {event.type} is not a persistent runtime event.")
        await self.connection().request(
            PublishEvent(
                epoch=self.epoch(),
                message=IncomingMessage(
                    id=identity,
                    client_id=route.client_id,
                    conversation_id=route.conversation_id,
                    kind="runtime_event",
                    event=payload,
                    occurred_at=event.timestamp,
                ),
            )
        )

    async def process_event(self, event: Event, dt: float, handler: EventHandler) -> None:
        delivery = self._deliveries.pop(id(event), None)
        if delivery is None:
            if isinstance(event, TimeTickEvent) and self.muika is not None:
                mode = self.muika.get_think_mode(event) if self.snapshot.bootstrapped else None
                if mode is None:
                    await handler(event, dt)
                    return
                event = replace(event, think_mode=mode)
            await self.publish(event)
            return
        claim, finished = delivery
        token = None
        try:
            response = await self.connection().request(
                TurnRequest(epoch=self.epoch(), body=LoadTurn(turn_id=f"input:{claim.sequence}", claim=claim))
            )
            if response.turn is None:
                raise ConnectionError("State service omitted its turn record.")
            scope = TurnScope(claim, response.turn)
            token = _turn_scope.set(scope)
            if not scope.record.completed:
                await handler(event, dt)
                if not scope.committed:
                    await self.commit_reply(None, [], None, [], [], [])
                if isinstance(event, SessionEndEvent):
                    self.snapshot.bootstrapped = False
                await self.checkpoint()
            finished.set_result(None)
        except BaseException as exc:
            if not finished.done():
                finished.set_exception(exc)
            raise
        finally:
            if token is not None:
                _turn_scope.reset(token)

    async def generate(self, stage: str, operation: Generation) -> tuple[str, list[Resource]]:
        scope = _turn_scope.get()
        if scope is None:
            raise RuntimeError("Generation requires a claimed persistent event.")
        generated = scope.record.generations.get(stage)
        if generated is None:
            text, resources = await operation()
            references = [
                await self.connection().upload_resource(resource, self.memory().vault) for resource in resources
            ]
            generated = GeneratedReply(text=text, resources=references)
            await self.connection().request(
                TurnRequest(
                    epoch=self.epoch(),
                    body=SaveGeneration(
                        turn_id=scope.record.turn_id, claim=scope.claim, stage=stage, generated=generated
                    ),
                )
            )
            scope.record.generations[stage] = generated
        return generated.text, [
            await self.connection().download_resource(reference, self.memory().vault)
            for reference in generated.resources
        ]

    async def commit_reply(
        self,
        content: str | None,
        resources: list[Resource],
        target: str | None,
        updates: list[StateUpdate],
        notes: list[str],
        intents: list[TaskIntent],
        *,
        controls: list[TaskControl] | None = None,
        timeout: float | None = None,
        god_mode: bool = False,
        topic: ActiveTopicState | None = None,
    ) -> list[TaskRecord]:
        scope = _turn_scope.get()
        if scope is None:
            raise RuntimeError("A reply requires a claimed persistent event.")
        references = [await self.connection().upload_resource(resource, self.memory().vault) for resource in resources]
        route = ClientRoute(
            client_id=scope.claim.message.client_id, conversation_id=scope.claim.message.conversation_id
        )
        if target and target != route.client_id:
            # 显式目标没有配对路由时拒绝；不得转投最近活跃客户端。
            status = await self.connection().request(Status())
            if not any(node.id == target and node.role == "bot" for node in status.nodes):
                raise ValueError("Explicit reply target is not a paired Bot.")
            route = ClientRoute(client_id=target, conversation_id="master")
        replies = list(scope.replies)
        if content is not None and (content or references):
            messages = Executor._split_message(content) if content else [""]
            for index, text in enumerate(messages):
                replies.append(
                    OutgoingMessage(
                        id=f"{scope.record.turn_id}:reply:{index}",
                        client_id=route.client_id,
                        conversation_id=route.conversation_id,
                        text=text,
                        resources=references if index == len(messages) - 1 else [],
                    )
                )
        persistent = self.memory().persistent.model_copy(deep=True)
        for update in updates:
            MemoryManager._apply_state(persistent, update, datetime.now())
        tasks = []
        for index, intent in enumerate(intents):
            intention = next((item for item in persistent.intentions if item.id == intent.intention_id), None)
            if intent.intention_id and (intention is None or intention.task_id or intention.status != "open"):
                logger.warning("[Node] Intention already has an action or is unavailable.")
                continue
            tasks.append(
                TaskRecord(
                    id=uuid5(NAMESPACE_URL, f"{scope.record.turn_id}:task:{index}").hex,
                    instruction=intent.instruction,
                    original_request=intent.original_request,
                    intention_id=intent.intention_id,
                    execution_node_id=self.snapshot.selected_executor,
                    reply_client_id=scope.claim.message.client_id,
                    reply_conversation_id=scope.claim.message.conversation_id,
                    resources=[
                        MediaReference(
                            type=reference.kind,
                            path=self.memory().vault.materialize(reference).path,
                            mimetype=reference.media_type,
                        )
                        for reference in scope.claim.message.resources
                    ],
                )
            )
        if self.muika is None:
            raise RuntimeError("Core is not ready to commit a reply.")
        task_files = [
            TransferFile(key=media.path, reference=self.memory().vault.preserve(media.to_resource()))
            for task in tasks
            for media in task_media(task)
        ]
        async with self._runtime_lock:
            async with self.muika.agent_tasks.control_changes(controls or []) as (changes, errors):
                pending = self.muika.pending_controls(timeout=timeout, god_mode=god_mode, release_persona=bool(changes))
                runtime = self.runtime_snapshot().model_copy(
                    update={
                        "god_mode": pending.god_mode,
                        "god_mode_pending": pending.god_mode_pending,
                        "session_end_triggered": pending.session_end_triggered,
                        "timeout_set_at": pending.timeout_set_at,
                        "timeout_seconds": pending.timeout_seconds,
                    }
                )
                if topic is not None:
                    runtime.active_topic = ActiveTopicSnapshot(
                        topic_id=topic.topic_id,
                        topic_seed=topic.topic_seed,
                        topic_type=topic.topic_type,
                        started_at=topic.started_at,
                        user_engaged=topic.user_engaged,
                    )
                    runtime.boredom = 0.0
                notes = notes + errors
                await self.memory().commit_turn(
                    TurnRequest(
                        epoch=self.epoch(),
                        body=CompleteTurn(
                            turn_id=scope.record.turn_id,
                            claim=scope.claim,
                            content=content,
                            resources=references,
                            state_updates=updates,
                            notes=notes,
                            replies=replies,
                            tasks=tasks,
                            task_changes=changes,
                            task_files=task_files,
                            runtime=runtime,
                        ),
                    )
                )
            self.snapshot = runtime
            if topic is not None:
                self.muika.state.active_topic = topic
                self.muika.state.boredom = 0.0
            self.muika.commit_controls(pending)
        scope.committed = True
        return tasks

    async def complete_empty(self, claim: ClaimedMessage) -> None:
        await self.connection().request(
            TurnRequest(
                epoch=self.epoch(),
                body=CompleteTurn(turn_id=f"input:{claim.sequence}", claim=claim, runtime=self.runtime_snapshot()),
            )
        )

    async def command(self, claim: ClaimedMessage) -> None:
        response = await self.connection().request(
            TurnRequest(epoch=self.epoch(), body=LoadTurn(turn_id=f"input:{claim.sequence}", claim=claim))
        )
        if response.turn is None or response.turn.completed:
            return
        scope = TurnScope(claim, response.turn)
        token = _turn_scope.set(scope)
        try:
            if "command_started" in scope.record.generations:
                await self.send_command_result("[System] 上一次命令在执行中断。结果尚未核实，请检查后再决定是否重试。")
            else:
                await self.connection().request(
                    TurnRequest(
                        epoch=self.epoch(),
                        body=SaveGeneration(
                            turn_id=scope.record.turn_id,
                            claim=claim,
                            stage="command_started",
                            generated=GeneratedReply(text=claim.message.text),
                        ),
                    )
                )
                await CommandDispatcher.get().dispatch(claim.message.text)
                await self.commit_tool_state()
            await self.commit_reply(None, [], None, [], [], [])
        finally:
            _turn_scope.reset(token)

    async def send_command_result(
        self, content: str, resources: list[dict] | None = None, target: str | None = None
    ) -> None:
        await self.send_message(
            content, [Resource(**resource) for resource in resources or []], target, kind="command_result"
        )

    async def send_message(
        self,
        content: str,
        resources: list[Resource] | None = None,
        target: str | None = None,
        *,
        kind: str = "send_message",
    ) -> None:
        scope = _turn_scope.get()
        context = get_dependencies().get(ToolContext)
        route = (
            ClientRoute(client_id=scope.claim.message.client_id, conversation_id=scope.claim.message.conversation_id)
            if scope
            else self.task_route(context.task_id if isinstance(context, ToolContext) else None)
        )
        if route is None:
            raise ValueError("Message delivery needs a known conversation route.")
        if target and target != route.client_id:
            raise ValueError("A command or direct action must keep its explicit conversation route.")
        references = [
            await self.connection().upload_resource(resource, self.memory().vault) for resource in resources or []
        ]
        effect = context.execution_id if isinstance(context, ToolContext) else None
        message = (
            OutgoingMessage(
                id=f"{scope.record.turn_id}:effect:{len(scope.replies)}",
                client_id=route.client_id,
                conversation_id=route.conversation_id,
                kind="command_result" if kind == "command_result" else "send_message",
                text=content,
                resources=references,
            )
            if scope is not None and not scope.committed
            else OutgoingMessage(
                id=f"delivery:{effect}" if effect else f"delivery:{uuid4().hex}",
                client_id=route.client_id,
                conversation_id=route.conversation_id,
                kind="command_result" if kind == "command_result" else "send_message",
                text=content,
                resources=references,
            )
        )
        if scope is not None and not scope.committed:
            scope.replies.append(message)
        else:
            await self.connection().request(Emit(epoch=self.epoch(), message=message))

    async def execute_tool(self, call: ToolCall) -> ToolResult:
        """使用持久调用身份选择原设备，结果未知时不发起第二次动作。"""
        context = get_dependencies().get(ToolContext)
        context = context if isinstance(context, ToolContext) else None
        scope = _turn_scope.get()
        task_id = context.task_id if context else None
        caller = get_function_calls().get(call.name)
        source = f"task:{task_id}" if task_id else scope.record.turn_id if scope else f"background:{call.id}"
        task = self.muika.agent_tasks.tasks.get(task_id) if self.muika is not None and task_id else None
        node_id = (
            self.node_id
            if caller is not None and caller.scope == "core"
            else (task.execution_node_id if task else self.snapshot.selected_executor)
        )
        if node_id is None:
            return ToolResult(
                text="No execution device is selected. Inspect devices and choose a suitable one.", is_error=True
            )
        self.nodes = (await self.connection().request(Status())).nodes
        capability = next(
            (tool for node in self.nodes if node.id == node_id for tool in node.capabilities if tool.name == call.name),
            None,
        )
        if capability is None:
            return ToolResult(
                text="Tool is unavailable on the task's execution device. Inspect devices before continuing.",
                is_error=True,
                outcome="not_executed",
            )
        inputs = {}
        known_paths = {media.path for media in task_media(task)} if task is not None else set()

        def reference_argument(value: str) -> str:
            path = Path(value)
            if (
                str(path) in known_paths
                or path.parent == self.memory().vault.directory
                and re.fullmatch(r"[0-9a-f]{64}(\.[a-z0-9]{1,10})?", path.name)
            ) and path.is_file():
                reference = self.memory().vault.preserve(Resource(type="file", path=value))
                inputs[reference.sha256] = reference
                return f"resource:{reference.sha256}"
            return value

        try:
            call = call.model_copy(
                update={"arguments": json.dumps(map_resource_values(json.loads(call.arguments), reference_argument))}
            )
        except ValueError as exc:
            return ToolResult(text=f"Invalid tool arguments: {exc}", is_error=True, outcome="not_executed")
        for reference in inputs.values():
            await self.connection().upload_resource(self.memory().vault.materialize(reference), self.memory().vault)
        id = (
            f"call:{context.execution_id}"
            if context and context.execution_id
            else uuid5(
                NAMESPACE_URL,
                f"{source}:{call.name}:{call.arguments}" if capability.retry != "read_only" else f"{source}:{call.id}",
            ).hex
        )
        spec = ExecutionSpec(
            id=id,
            node_id=node_id,
            source_id=source,
            call=call,
            task_id=task_id,
            file_versions=dict(context.file_versions) if context else {},
            review_context=context.review_context if context else "",
            inputs=list(inputs.values()),
        )
        submitted = False
        try:
            response = await self.connection().request(
                ExecutionRequest(epoch=self.epoch(), body=SubmitExecution(spec=spec))
            )
            record = response.execution
            if record is None:
                raise ConnectionError("State service omitted its execution result.")
            submitted = True
            if capability.scope == "core" and record.status == "pending":
                if self.worker is None:
                    raise RuntimeError("Local execution worker is unavailable.")
                record = await self.worker.execute(record, context, self.commit_tool_state)
            while record.status in {"pending", "running"}:
                await asyncio.sleep(0.1)
                response = await self.connection().request(
                    ExecutionRequest(epoch=self.epoch(), body=InspectExecution(id=id))
                )
                if response.execution is None:
                    raise ConnectionError("State service omitted its execution result.")
                record = response.execution
            if record.status != "completed" or record.result is None:
                return ToolResult(
                    text="The action outcome is unknown. Inspect its original device and reconcile the saved call "
                    "before issuing another effect.",
                    is_error=True,
                    outcome="unknown",
                )
            result = record.result.model_copy(deep=True)
            paths = {
                file.key: (await self.connection().download_resource(file.reference, self.memory().vault)).path
                for file in record.files
            }
            for media in result.resources:
                media.path = paths[media.path]
            if context is not None:
                context.file_versions.update(record.file_versions)
            return result
        except ValueError as exc:
            unknown = submitted or "unknown outcome" in str(exc) or "outcome unknown" in str(exc)
            return ToolResult(
                text=f"Action outcome needs verification: {exc}" if unknown else f"Action was not dispatched: {exc}",
                is_error=True,
                outcome="unknown" if unknown else "not_executed",
            )
