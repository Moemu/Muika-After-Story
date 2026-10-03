"""固定 IPC 入口：角色认证与持久消息业务接口。"""

import asyncio
import hashlib
import hmac
import re
import sqlite3
import time
from datetime import datetime
from typing import Literal
from uuid import uuid4

from aiohttp import WSMsgType, web
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from muika.core.agent.task_store import TaskStore
from muika.database.db import database_path, get_session
from muika.database.orm_models import RuntimeStateORM
from muika.node.activity_service import execute_activity
from muika.node.auth import CredentialStore, NodeCredential
from muika.node.bundle import CognitiveBundle
from muika.node.event_protocol import RuntimeEvent
from muika.node.execution_protocol import (
    DATA_REVISION,
    RUNTIME_ABI,
    InspectExecution,
    SubmitExecution,
)
from muika.node.execution_service import ExecutionService
from muika.node.journal import JournalConflict, RuntimeJournal
from muika.node.memory_protocol import MemoryResult
from muika.node.memory_service import MemoryService
from muika.node.resources import MAX_RESOURCE_BYTES, ResourceVault
from muika.node.schedule_service import ScheduleService
from muika.node.service_lock import StateServiceLock
from muika.node.task_service import TaskService
from muika.node.turn_protocol import CompleteTurn
from muika.node.turn_service import TurnService
from muika.utils.logger import logger

from .node_protocol import (
    CANDIDATE_TIMEOUT_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS,
    PROTOCOL_VERSION,
    REQUEST_ADAPTER,
    Acknowledge,
    Acquire,
    ActivityRequest,
    Claim,
    Commit,
    CoreReady,
    Emit,
    ExecutionRequest,
    Handoff,
    LoadBundle,
    LoadRuntime,
    MemoryRequest,
    NodeRequest,
    NodeResponse,
    NodeStatus,
    Pending,
    PollExecutions,
    PublishEvent,
    Receive,
    RegisterNode,
    Release,
    Renew,
    ReportExecution,
    SaveBundle,
    SaveRuntime,
    ScheduleRequest,
    StartExecution,
    Status,
    TaskRequest,
    TurnRequest,
    ValidateExecution,
)


def sqlite_lock_conflict(error: OperationalError) -> bool:
    """识别 SQLite 锁冲突，兼容尚未提供错误码的 Python 3.10。"""
    if not isinstance(error.orig, sqlite3.OperationalError):
        return False
    try:
        return error.orig.sqlite_errorcode & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    except AttributeError:
        return str(error.orig).partition(":")[0] in {"database is locked", "database table is locked"}


class StateServer:
    """承载单一状态服务入口；调用方负责配置监听地址和 TLS。"""

    def __init__(
        self,
        credentials: list[NodeCredential],
        *,
        credential_store: CredentialStore | None = None,
        initial_bundle: CognitiveBundle | None = None,
    ) -> None:
        if len({item.id for item in credentials}) != len(credentials):
            raise ValueError("Node identities must be unique.")
        if len({item.token_sha256 for item in credentials}) != len(credentials):
            raise ValueError("Each node requires an independent token.")
        self._credentials = tuple(credentials)
        self.credential_store = credential_store
        self.initial_bundle = initial_bundle
        self.journal = RuntimeJournal()
        self.vault: ResourceVault | None = None
        self.memory: MemoryService | None = None
        self.tasks = TaskStore()
        self.task_service: TaskService | None = None
        self.turn_service: TurnService | None = None
        self.execution_service: ExecutionService | None = None
        self.registrations: dict[str, RegisterNode] = {}
        self.announced: dict[str, RegisterNode] = {}
        self.observation_tasks: set[asyncio.Task[None]] = set()
        self.scheduler = ScheduleService()
        self._service_lock: StateServiceLock | None = None
        self.connections: dict[str, web.WebSocketResponse] = {}
        self.last_seen: dict[str, float] = {}
        self.app = web.Application()
        self.app.router.add_get("/node/ws", self.handle_connection)
        self.app.router.add_post("/node/pair", self.pair)
        self.app.router.add_get("/node/status", self.status)
        self.app.router.add_put("/node/resources/{sha256}", self.upload_resource)
        self.app.router.add_get("/node/resources/{sha256}", self.download_resource)
        self.app.cleanup_ctx.append(self.lifetime)
        self.app.on_shutdown.append(self.stop)

    @property
    def credentials(self) -> tuple[NodeCredential, ...]:
        return tuple(self.credential_store.credentials()) if self.credential_store else self._credentials

    async def pair(self, request: web.Request) -> web.Response:
        if self.credential_store is None:
            raise web.HTTPNotFound()
        code = await request.text()
        if len(code) > 128:
            raise web.HTTPBadRequest(text="Invalid pairing code.")
        try:
            credential, token = self.credential_store.redeem(code)
        except Exception as exc:
            # 不向未认证连接暴露凭据库内容。
            logger.debug(f"[Node] Pairing refused: {type(exc).__name__}")
            raise web.HTTPForbidden(text="Pairing code is invalid, expired or already used.") from exc
        return web.json_response({"id": credential.id, "role": credential.role, "token": token})

    async def status(self, request: web.Request) -> web.Response:
        result = await self.dispatch(self.authenticate(request), Status())
        return web.json_response(result.model_dump(mode="json"))

    async def lifetime(self, app: web.Application):
        """先独占数据库再恢复控制权，关闭连接后释放所有权。"""
        lock = StateServiceLock(database_path())
        lock.acquire()
        self._service_lock = lock
        try:
            await self.journal.start()
            if self.initial_bundle is not None:
                async with get_session() as db:
                    if await db.get(RuntimeStateORM, 2) is None:
                        db.add(RuntimeStateORM(id=2, payload=self.initial_bundle.model_dump_json()))
            self.vault = ResourceVault(database_path().parent / "node_resources")
            self.memory = MemoryService(self.vault)
            self.task_service = TaskService(self.vault)
            self.tasks = self.task_service.store
            self.turn_service = TurnService(self.memory, self.task_service)
            self.execution_service = ExecutionService(self.journal, self.vault)
            schedules = asyncio.create_task(self.scheduler.run())
            try:
                yield
            finally:
                schedules.cancel()
                await asyncio.gather(schedules, return_exceptions=True)
        finally:
            await asyncio.gather(*self.observation_tasks, return_exceptions=True)
            lock.close()
            self._service_lock = None

    async def stop(self, app: web.Application) -> None:
        """关闭所有连接，不删除持久消息。"""
        for connection in list(self.connections.values()):
            await connection.close(code=1001, message=b"State service stopping")

    def authenticate(self, request: web.Request) -> NodeCredential:
        """从连接凭据确定身份，消息体不能改变角色。"""
        if request.headers.get("X-MAS-Protocol") != str(PROTOCOL_VERSION):
            raise web.HTTPUpgradeRequired(text="This endpoint requires MAS IPC protocol 2. Upgrade the client.")
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise web.HTTPUnauthorized(text="A node token is required.")
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        for credential in self.credentials:
            if hmac.compare_digest(digest, credential.token_sha256):
                return credential
        raise web.HTTPUnauthorized(text="Node token is invalid or revoked.")

    def resource_path(self, request: web.Request):
        self.authenticate(request)
        digest = request.match_info["sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or self.vault is None:
            raise web.HTTPBadRequest(text="Invalid resource reference.")
        return self.vault.directory / digest

    async def upload_resource(self, request: web.Request) -> web.Response:
        """接收有界资源，完整核验后才发布副本。"""
        path = self.resource_path(request)
        temporary = path.parent / (uuid4().hex + ".upload")
        digest, size = hashlib.sha256(), 0
        try:
            with temporary.open("wb") as file:
                async for chunk in request.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > MAX_RESOURCE_BYTES:
                        raise web.HTTPRequestEntityTooLarge(max_size=MAX_RESOURCE_BYTES, actual_size=size)
                    digest.update(chunk)
                    file.write(chunk)
            if digest.hexdigest() != path.name:
                raise web.HTTPBadRequest(text="Resource hash differs from uploaded content.")
            temporary.replace(path)
            return web.Response(status=204)
        finally:
            temporary.unlink(missing_ok=True)

    async def download_resource(self, request: web.Request) -> web.FileResponse:
        path = self.resource_path(request)
        if not path.is_file():
            raise web.HTTPNotFound(text="Resource has no available copy.")
        return web.FileResponse(path)

    async def handle_connection(self, request: web.Request) -> web.WebSocketResponse:
        """接受节点出站连接，并把请求交给类型化业务接口。"""
        credential = self.authenticate(request)
        existing = self.connections.get(credential.id)
        if existing is not None and not existing.closed:
            raise web.HTTPConflict(text="This node already has an active connection.")
        ws = web.WebSocketResponse(heartbeat=HEARTBEAT_INTERVAL_SECONDS, autoping=False, max_msg_size=2 * 1024 * 1024)
        self.connections[credential.id] = ws
        try:
            await ws.prepare(request)
            logger.info(f"[Node] Connected: {credential.id} ({credential.role})")
            async for frame in ws:
                if frame.type in {WSMsgType.PING, WSMsgType.PONG}:
                    self.last_seen[credential.id] = time.monotonic()
                    if frame.type == WSMsgType.PING:
                        await ws.pong(frame.data)
                    continue
                if frame.type != WSMsgType.TEXT:
                    break
                try:
                    command = REQUEST_ADAPTER.validate_json(frame.data)
                except ValidationError:
                    await ws.send_json(NodeResponse(request_id="", error="Invalid protocol 2 request.").model_dump())
                    continue
                try:
                    response = await self.dispatch(credential, command)
                except (JournalConflict, ValueError) as exc:
                    response = NodeResponse(request_id=command.request_id, error=str(exc))
                except Exception as exc:
                    if (
                        isinstance(command, SaveRuntime)
                        and isinstance(exc, OperationalError)
                        and sqlite_lock_conflict(exc)
                    ):
                        logger.warning(f"[Node] Runtime checkpoint deferred: {credential.id}")
                        response = NodeResponse(
                            request_id=command.request_id,
                            error="Runtime checkpoint is temporarily unavailable; keep renewing the lease.",
                            error_code="checkpoint_unavailable",
                        )
                    else:
                        logger.exception(f"[Node] Request failed: {credential.id} {command.operation}")
                        response = NodeResponse(
                            request_id=command.request_id, error="State operation failed; outcome unknown."
                        )
                await ws.send_json(response.model_dump(mode="json", exclude_none=True))
        finally:
            if self.connections.get(credential.id) is ws:
                del self.connections[credential.id]
                registration = self.announced.pop(credential.id, None)
                if registration is not None:
                    observation = asyncio.create_task(
                        self.device_observation(credential, registration, "device_offline")
                    )
                    self.observation_tasks.add(observation)
                    observation.add_done_callback(self.observation_done)
                    # 连接关闭会取消处理器，设备事实由状态服务负责保存。
                    await asyncio.shield(observation)
            logger.info(f"[Node] Disconnected: {credential.id}")
        return ws

    def observation_done(self, task: asyncio.Task[None]) -> None:
        self.observation_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error(f"[Node] Could not persist device observation: {task.exception()}")

    async def device_observation(
        self,
        node: NodeCredential,
        registration: RegisterNode,
        event_type: Literal["device_online", "device_offline", "device_capability_changed"],
    ) -> None:
        report = (
            f"Device {node.id} ({node.role}): {event_type}. "
            f"Compatible: {self.compatible(node.id)}. "
            f"Declared tools: {', '.join(sorted(tool.name for tool in registration.tools)) or 'none'}."
        )
        if registration.environment is not None:
            report += " " + registration.environment.describe()
        if event_type == "device_offline":
            report += " This device is unavailable; its tools cannot run now."
        await self.journal.observe(RuntimeEvent(type=event_type, timestamp=datetime.now(), report=report))

    async def dispatch(self, node: NodeCredential, request: NodeRequest) -> NodeResponse:
        """按连接身份授权，避免客户端在参数中冒充其他节点。"""
        response = NodeResponse(request_id=request.request_id)
        if not any(item == node for item in self.credentials):
            raise ValueError("Node credential has been revoked. Pair this device again.")
        self.last_seen[node.id] = time.monotonic()
        if isinstance(request, Status):
            response.lease = await self.journal.current_lease()
            response.nodes = [
                NodeStatus(
                    id=credential.id,
                    role=credential.role,
                    connected=credential.id in self.connections,
                    compatible=self.compatible(credential.id),
                    tools=(
                        [tool.name for tool in self.registrations[credential.id].tools]
                        if credential.id in self.registrations
                        else []
                    ),
                    capabilities=self.registrations[credential.id].tools if credential.id in self.registrations else [],
                    environment=(
                        self.registrations[credential.id].environment
                        if node.role == "core" and credential.id in self.registrations
                        else None
                    ),
                )
                for credential in self.credentials
            ]
            return response
        if isinstance(request, LoadBundle):
            if node.role != "core" or not self.compatible(node.id):
                raise ValueError("Only compatible Core candidates can read cognitive configuration.")
            async with get_session() as db:
                row = await db.get(RuntimeStateORM, 2)
                response.bundle = CognitiveBundle.model_validate_json(row.payload) if row else None
            return response
        if isinstance(request, RegisterNode):
            if node.role == "executor" and any(tool.scope != "device" for tool in request.tools):
                raise ValueError("Executor nodes can register only device tools.")
            if len({tool.name for tool in request.tools}) != len(request.tools):
                raise ValueError("Tool capability names must be unique.")
            self.registrations[node.id] = request
            if node.role == "bot" or request.environment is not None or node.id in self.announced:
                previous = self.announced.get(node.id)
                if previous is None or previous.model_dump(exclude={"request_id"}) != request.model_dump(
                    exclude={"request_id"}
                ):
                    self.announced[node.id] = request
                    await self.device_observation(
                        node, request, "device_online" if previous is None else "device_capability_changed"
                    )
            return response
        if isinstance(request, (PollExecutions, StartExecution, ValidateExecution, ReportExecution)):
            if node.role not in {"core", "executor"} or self.execution_service is None:
                raise ValueError("This operation requires an execution role.")
            if not self.compatible(node.id):
                raise ValueError("Runtime or data version is incompatible; execution is disabled.")
            if isinstance(request, PollExecutions):
                tools = {tool.name for tool in self.registrations[node.id].tools if tool.scope == "device"}
                response.executions = await self.execution_service.poll(node.id, tools)
            elif isinstance(request, (StartExecution, ValidateExecution)):
                response.execution = await self.execution_service.start(
                    node.id, request.id, request.epoch, validate_only=isinstance(request, ValidateExecution)
                )
            else:
                await self.execution_service.report(node.id, request.record)
            return response
        if isinstance(
            request,
            (
                Acquire,
                Renew,
                Release,
                Handoff,
                CoreReady,
                Claim,
                Commit,
                MemoryRequest,
                TaskRequest,
                TurnRequest,
                LoadRuntime,
                SaveRuntime,
                PublishEvent,
                Emit,
                ExecutionRequest,
                ScheduleRequest,
                ActivityRequest,
                SaveBundle,
            ),
        ):
            if node.role != "core":
                raise ValueError("This operation requires the core role.")
            if not self.compatible(node.id):
                raise ValueError("Runtime or data version is incompatible; Core takeover and writes are disabled.")
            if isinstance(request, Acquire):
                candidates = [
                    item.id
                    for item in sorted(self.credentials, key=lambda item: (item.priority, item.id))
                    if item.role == "core"
                    and item.id in self.connections
                    and not self.connections[item.id].closed
                    and self.compatible(item.id)
                    and time.monotonic() - self.last_seen.get(item.id, 0) < CANDIDATE_TIMEOUT_SECONDS
                ]
                response.lease = await self.journal.acquire(node.id, request.duration, candidates)
            elif isinstance(request, Renew):
                response.lease = await self.journal.renew(node.id, request.epoch, request.duration)
            elif isinstance(request, Release):
                await self.journal.release(node.id, request.epoch)
            elif isinstance(request, Handoff):
                if (
                    request.target == node.id
                    or not any(item.id == request.target and item.role == "core" for item in self.credentials)
                    or request.target not in self.connections
                    or not self.compatible(request.target)
                ):
                    response.handoff_accepted = await self.journal.handoff(
                        node.id, request.epoch, request.target, "The target is unavailable or incompatible."
                    )
                else:
                    response.handoff_accepted = await self.journal.handoff(node.id, request.epoch, request.target)
            elif isinstance(request, CoreReady):
                await self.journal.ready(node.id, request.epoch)
            elif isinstance(request, Claim):
                response.claim = await self.journal.claim(node.id, request.epoch)
            elif isinstance(request, Commit):
                await self.journal.commit(node.id, request.epoch, request.claim, request.replies)
            elif isinstance(request, MemoryRequest):
                if self.memory is None:
                    raise RuntimeError("Memory service is not ready.")
                async with self.journal.transaction(node.id, request.epoch):
                    response.memory = await self.memory.execute(request.body)
            elif isinstance(request, TaskRequest):
                if self.task_service is None:
                    raise RuntimeError("Task service is not ready.")
                async with self.journal.transaction(node.id, request.epoch):
                    response.task = await self.task_service.execute(request.body)
            elif isinstance(request, TurnRequest):
                if self.turn_service is None or self.memory is None:
                    raise RuntimeError("Turn service is not ready.")
                if isinstance(request.body, CompleteTurn):
                    bots = {credential.id for credential in self.credentials if credential.role == "bot"}
                    if any(reply.client_id not in bots for reply in request.body.replies):
                        raise ValueError("Reply target is not a paired Bot client.")
                async with self.journal.transaction(node.id, request.epoch) as db:
                    response.turn = await self.turn_service.execute(db, node.id, request.epoch, request.body)
                    if isinstance(request.body, CompleteTurn):
                        response.memory = MemoryResult(view=self.memory.view())
            elif isinstance(request, LoadRuntime):
                if self.turn_service is None:
                    raise RuntimeError("Turn service is not ready.")
                async with self.journal.transaction(node.id, request.epoch) as db:
                    response.runtime = await self.turn_service.runtime(db)
            elif isinstance(request, SaveRuntime):
                async with self.journal.transaction(node.id, request.epoch) as db:
                    await db.merge(RuntimeStateORM(id=1, payload=request.runtime.model_dump_json()))
            elif isinstance(request, PublishEvent):
                response.sequence = await self.journal.publish(node.id, request.epoch, request.message)
            elif isinstance(request, Emit):
                if not any(item.id == request.message.client_id and item.role == "bot" for item in self.credentials):
                    raise ValueError("Message target is not a paired Bot.")
                if self.memory is None:
                    raise RuntimeError("Memory service is not ready.")
                for reference in request.message.resources:
                    self.memory.vault.materialize(reference)
                async with self.journal.transaction(node.id, request.epoch) as db:
                    await self.journal.emit(db, request.message)
            elif isinstance(request, ExecutionRequest):
                if self.execution_service is None:
                    raise RuntimeError("Execution service is not ready.")
                async with self.journal.transaction(node.id, request.epoch) as db:
                    if isinstance(request.body, SubmitExecution):
                        spec = request.body.spec
                        registration = self.registrations.get(spec.node_id)
                        capability = (
                            next((tool for tool in registration.tools if tool.name == spec.call.name), None)
                            if registration
                            else None
                        )
                        if (
                            capability is None
                            or spec.node_id not in self.connections
                            or not self.compatible(spec.node_id)
                        ):
                            raise ValueError("Selected device capability is unavailable or incompatible.")
                        if capability.scope == "core" and spec.node_id != node.id:
                            raise ValueError("Core tools must execute in the active Core.")
                        response.execution = await self.execution_service.submit(db, request.epoch, spec, capability)
                    elif isinstance(request.body, InspectExecution):
                        response.execution = await self.execution_service.inspect(request.body.id)
            elif isinstance(request, ScheduleRequest):
                async with self.journal.transaction(node.id, request.epoch) as db:
                    response.schedules = await self.scheduler.execute(db, request.body)
            elif isinstance(request, ActivityRequest):
                async with self.journal.transaction(node.id, request.epoch) as db:
                    response.activity = await execute_activity(db, request.body)
            elif isinstance(request, SaveBundle):
                request.bundle.validate_configuration()
                async with self.journal.transaction(node.id, request.epoch) as db:
                    row = await db.get(RuntimeStateORM, 2)
                    if (
                        row is None
                        or CognitiveBundle.model_validate_json(row.payload).digest != request.expected_digest
                    ):
                        raise ValueError("Cognitive configuration changed; reload it before applying this update.")
                    row.payload = request.bundle.model_dump_json()
                    response.bundle = request.bundle
        else:
            if node.role != "bot":
                raise ValueError("This operation requires the bot role.")
            if isinstance(request, Receive):
                if request.message.client_id != node.id:
                    raise ValueError("Input identity differs from the authenticated client.")
                if request.message.kind == "runtime_event" or request.message.event is not None:
                    raise ValueError("Bot clients cannot publish internal runtime events.")
                response.sequence = await self.journal.receive(request.message)
            elif isinstance(request, Pending):
                response.replies = await self.journal.pending(node.id, request.limit)
            elif isinstance(request, Acknowledge):
                await self.journal.acknowledge(node.id, request.message_id)
        return response

    def compatible(self, node_id: str) -> bool:
        registration = self.registrations.get(node_id)
        credential = next((node for node in self.credentials if node.id == node_id), None)
        return (
            registration is not None
            and credential is not None
            and (
                credential.role == "bot"
                or (registration.runtime_abi == RUNTIME_ABI and registration.data_revision == DATA_REVISION)
            )
        )
