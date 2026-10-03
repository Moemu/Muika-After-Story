"""定义活动设备的公共操作接口。"""

import platform
import sys
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from muika.config import mas_config


class ExecutionEnvironment(BaseModel):
    """声明动作实际所属设备的环境，不传播设备授权或凭据。"""

    available: bool = True
    os: str = "unavailable"
    working_directory: str = ""
    python: str = ""
    shell: str = ""
    action_permission: Literal["read_only", "write", "self_modify"] = "read_only"
    code_review_mode: Literal["auto", "manual"] = "auto"
    allowed_file_roots: list[str] = Field(default_factory=list)

    @classmethod
    def local(cls) -> "ExecutionEnvironment":
        return cls(
            os=platform.system(),
            working_directory=str(Path.cwd()),
            python=sys.executable,
            shell="powershell" if sys.platform == "win32" else "bash",
            action_permission=mas_config.action_permission,
            code_review_mode=mas_config.code_review_mode,
            allowed_file_roots=list(mas_config.fs_allowed_paths),
        )

    def describe(self) -> str:
        if not self.available:
            return "The task's original execution device is unavailable. Inspect devices before planning more actions."
        return (
            f"Execution environment: OS={self.os}; cwd={self.working_directory}; Python={self.python}. "
            f"Default shell={self.shell}. Use this device's syntax. A running process is not a completed check. "
            f"Action permission={self.action_permission}; code review={self.code_review_mode}; "
            f"allowed file roots={self.allowed_file_roots}."
        )


class DeviceControl(Protocol):
    async def list_devices(self) -> str: ...
    async def select_device(self, id: str) -> str: ...
    async def request_handoff(self, id: str, reason: str) -> str: ...
