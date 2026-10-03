"""按任务归属管理后台进程，Core 接管不改变设备归属。"""

from typing import Any, Protocol

from .processes import ProcessResult, get_process_manager


class TaskProcesses(Protocol):
    async def active_for(self, owner: str) -> list[str]: ...
    async def read_record(self, id: str, owner: str) -> dict[str, Any]: ...
    async def wait(self, id: str, *, owner: str, seconds: float) -> ProcessResult: ...
    async def stop_owner(self, owner: str) -> None: ...
    async def close(self, owners: list[str]) -> None: ...


class LocalTaskProcesses:
    async def active_for(self, owner: str) -> list[str]:
        return get_process_manager().active_for(owner)

    async def read_record(self, id: str, owner: str) -> dict[str, Any]:
        return get_process_manager().read_record(id, owner=owner)

    async def wait(self, id: str, *, owner: str, seconds: float) -> ProcessResult:
        return await get_process_manager().wait(id, owner=owner, seconds=seconds)

    async def stop_owner(self, owner: str) -> None:
        await get_process_manager().stop_owner(owner)

    async def close(self, owners: list[str]) -> None:
        for owner in owners:
            await self.stop_owner(owner)
