"""接管后仍向任务的原设备核对后台进程。"""

import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from muika.core.executor import Executor
from muika.core.processes import ProcessResult
from muika.core.state import MuikaState
from muika.llm._schema import ToolCall, ToolResult
from muika.plugin.func_call.context import tool_context


class RemoteTaskProcesses:
    def __init__(
        self, state: MuikaState, executor: Executor, execute: Callable[[ToolCall], Awaitable[ToolResult]]
    ) -> None:
        self.state, self.executor, self.execute = state, executor, execute

    async def request(self, name: str, owner: str, arguments: dict[str, Any]) -> ToolResult:
        with tool_context(self.state, self.executor, task_id=owner):
            result = await self.execute(ToolCall(id=uuid4().hex, name=name, arguments=json.dumps(arguments)))
        if result.is_error:
            raise ConnectionError(result.text)
        return result

    async def active_for(self, owner: str) -> list[str]:
        result = await self.request("active_task_processes", owner, {})
        value = json.loads(result.text)
        if not isinstance(value, list) or not all(isinstance(id, str) for id in value):
            raise ValueError("Device returned an invalid task process list.")
        return value

    async def read_record(self, id: str, owner: str) -> dict[str, Any]:
        result = await self.request("read_execution_record", owner, {"process_id": id})
        value = json.loads(result.text)
        if not isinstance(value, dict):
            raise ValueError("Device returned an invalid execution record.")
        return value

    async def wait(self, id: str, *, owner: str, seconds: float) -> ProcessResult:
        result = await self.request("wait_process", owner, {"process_id": id, "seconds": seconds})
        return ProcessResult.model_validate_json(result.text)

    async def stop_owner(self, owner: str) -> None:
        await self.request("stop_task_processes", owner, {})

    async def close(self, owners: list[str]) -> None:
        """Core 交接只停止派发；设备上的进程保持原有期限与归属。"""
