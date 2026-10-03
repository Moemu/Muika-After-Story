"""在已登记的设备上执行工具，持久保存结果后再向状态服务报告。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from muika.core.devices import ExecutionEnvironment
from muika.core.events import Event
from muika.core.executor import Executor
from muika.core.state import MuikaState
from muika.ipc.node_client import NodeClient, NodeRequestError
from muika.ipc.node_protocol import (
    PollExecutions,
    RegisterNode,
    ReportExecution,
    StartExecution,
    ValidateExecution,
)
from muika.llm._schema import MediaReference, ToolResult
from muika.llm.utils.tools import dispatch_tool, route_tools
from muika.models import Resource
from muika.plugin.func_call import get_function_calls
from muika.plugin.func_call.context import ToolContext, tool_context
from muika.plugin.mcp import get_mcp_list
from muika.utils.logger import logger

from .execution_protocol import ExecutionRecord, ToolCapability
from .resources import ResourceVault
from .task_protocol import TransferFile
from .tool_resources import map_resource_values


def capabilities(*, device_only: bool = False) -> list[ToolCapability]:
    registered = [
        ToolCapability(
            name=name,
            scope=caller.scope,
            retry="read_only" if caller.read_only else "idempotent" if caller.idempotent else "verify",
            tool_schema=caller.data(),
        )
        for name, caller in get_function_calls().items()
        if not device_only or caller.scope == "device"
    ]
    return registered + [
        ToolCapability.model_validate(
            {"name": tool["function"]["name"], "scope": "device", "retry": "verify", "tool_schema": tool}
        )
        for tool in get_mcp_list()
    ]


class ExecutionLedger:
    """设备独占的执行日志，不保存人格，也不与其他设备共享文件。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS execution (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        self.db.commit()

    def read(self, id: str) -> ExecutionRecord | None:
        row = self.db.execute("SELECT payload FROM execution WHERE id=?", (id,)).fetchone()
        return ExecutionRecord.model_validate_json(row[0]) if row else None

    def write(self, record: ExecutionRecord) -> None:
        self.db.execute(
            "INSERT INTO execution VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
            (record.spec.id, record.model_dump_json()),
        )
        self.db.commit()

    def records(self) -> list[ExecutionRecord]:
        return [ExecutionRecord.model_validate_json(row[0]) for row in self.db.execute("SELECT payload FROM execution")]

    def close(self) -> None:
        self.db.close()


class ExecutorWorker:
    """拒绝重跑不确定动作；Core 消失后仍可保存已经发生的事实。"""

    def __init__(self, client: NodeClient, directory: Path) -> None:
        self.client = client
        self.ledger = ExecutionLedger(directory / "executions.db")
        self.vault = ResourceVault(directory / "execution_resources")
        self.state = MuikaState()
        self.executor = Executor(asyncio.Queue[Event](), self.unrouted_message)
        self._running: set[str] = set()
        self._execution_lock = asyncio.Lock()

    async def unrouted_message(
        self, content: str, resources: list[Resource] | None = None, target: str | None = None
    ) -> None:
        raise ValueError("Device tools return observations; use an active Core tool for conversation delivery.")

    async def run(self) -> None:
        for saved in self.ledger.records():
            if saved.status == "running":
                saved.status = "unknown"
                self.ledger.write(saved)
            if saved.status in {"completed", "unknown"}:
                try:
                    await self.report(saved)
                except NodeRequestError as exc:
                    if "saved grant" not in str(exc) and "not started" not in str(exc):
                        raise
                    logger.debug(f"[Node] Local outcome belongs to an older or reconciled grant: {saved.spec.id}")
        while True:
            response = await self.client.request(PollExecutions())
            for record in response.executions:
                await self.execute(record)
            await asyncio.sleep(0.1)

    async def report(self, record: ExecutionRecord) -> None:
        files = []
        if record.result is not None:
            files = [
                TransferFile(
                    key=reference.path, reference=await self.client.upload_resource(reference.to_resource(), self.vault)
                )
                for reference in record.result.resources
            ]
        record.files = files
        self.ledger.write(record)
        await self.client.request(ReportExecution(record=record))

    async def execute(
        self,
        grant: ExecutionRecord,
        context: ToolContext | None = None,
        commit: Callable[[], Awaitable[None]] | None = None,
    ) -> ExecutionRecord:
        async with self._execution_lock:
            return await self.execute_owned(grant, context, commit)

    async def execute_owned(
        self,
        grant: ExecutionRecord,
        context: ToolContext | None = None,
        commit: Callable[[], Awaitable[None]] | None = None,
    ) -> ExecutionRecord:
        saved = self.ledger.read(grant.spec.id)
        if saved is not None:
            if saved.spec != grant.spec:
                raise ValueError("Local execution identity has different arguments.")
            if saved.status == "completed":
                if grant.epoch != saved.epoch:
                    await self.client.request(StartExecution(id=grant.spec.id, epoch=grant.epoch))
                    saved.epoch = grant.epoch
                    self.ledger.write(saved)
                await self.report(saved)
                return saved
            if saved.status in {"unknown", "running"}:
                caller = get_function_calls().get(grant.spec.call.name)
                if caller is None or not (caller.read_only or caller.idempotent):
                    saved.status = "unknown"
                    self.ledger.write(saved)
                    await self.report(saved)
                    return saved
        if grant.spec.id in self._running:
            return grant
        response = await self.client.request(StartExecution(id=grant.spec.id, epoch=grant.epoch))
        if response.execution is None:
            raise ConnectionError("State service omitted the execution grant.")
        record = response.execution
        self.ledger.write(record)
        self._running.add(record.spec.id)
        active = True

        async def guard() -> None:
            nonlocal active
            try:
                while True:
                    await asyncio.sleep(0.2)
                    await self.client.request(ValidateExecution(id=record.spec.id, epoch=record.epoch))
            except BaseException:
                active = False
                raise

        watcher = asyncio.create_task(guard())
        action: asyncio.Task[ToolResult] | None = None
        try:
            input_paths = {}
            for reference in record.spec.inputs:
                resource = await self.client.download_resource(reference, self.vault)
                input_paths[f"resource:{reference.sha256}"] = resource.path
            local_call = record.spec.call.model_copy(
                update={
                    "arguments": json.dumps(
                        map_resource_values(
                            json.loads(record.spec.call.arguments), lambda value: input_paths.get(value, value)
                        )
                    )
                }
            )
            state = context.state if context else self.state
            executor = context.executor if context else self.executor
            versions = dict(context.file_versions if context else record.spec.file_versions)
            with (
                route_tools(None),
                tool_context(
                    state,
                    executor,
                    task_id=record.spec.task_id,
                    file_versions=versions,
                    review_context=record.spec.review_context,
                    execution_id=record.spec.id,
                    input_paths=frozenset(input_paths.values()),
                    is_current=lambda: active
                    and (context is None or context.is_current is None or context.is_current()),
                ) as local_context,
            ):
                action = asyncio.create_task(dispatch_tool(local_call))
                done, _ = await asyncio.wait([action, watcher], return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    await watcher
                    raise ConnectionError("Execution authority is unavailable.")
                result = await action
                resources = [reference.to_resource() for reference in result.resources] + local_context.resources
            result.resources = [
                MediaReference(
                    type=resource.type,
                    path=self.vault.materialize(self.vault.preserve(resource)).path,
                    mimetype=resource.mimetype,
                )
                for resource in resources
            ]
            record.result, record.file_versions = result, versions
            if commit is not None:
                await commit()
            record.status = "unknown" if result.outcome == "unknown" else "completed"
            record.completed_at = datetime.now(timezone.utc)
            self.ledger.write(record)
            await self.report(record)
            return record
        finally:
            if action is not None:
                action.cancel()
            watcher.cancel()
            await asyncio.gather(*(task for task in (action, watcher) if task is not None), return_exceptions=True)
            self._running.discard(record.spec.id)

    async def close(self) -> None:
        await self.executor.scheduler.close()
        self.ledger.close()


class ExecutorNode:
    """独立执行角色只有出站连接，不承载 Bot 或人格推导。"""

    def __init__(self, address: str, token: str, node_id: str, directory: Path, *, ca_file: Path | None = None) -> None:
        self.address, self.token, self.node_id, self.directory = address, token, node_id, directory
        self.ready = asyncio.Event()
        self.ca_file = ca_file

    async def run(self) -> None:
        while True:
            worker = None
            try:
                async with NodeClient(self.address, self.token, ca_file=self.ca_file) as client:
                    await client.request(
                        RegisterNode(tools=capabilities(device_only=True), environment=ExecutionEnvironment.local())
                    )
                    worker = ExecutorWorker(client, self.directory)
                    self.ready.set()
                    await worker.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[Node] Executor {self.node_id} unavailable: {exc}")
                await asyncio.sleep(1)
            finally:
                self.ready.clear()
                if worker is not None:
                    await worker.close()
