"""提供可等待、停止和续读输出的代码执行工具。"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from muika.config import mas_config
from muika.core.processes import ProcessResult, get_process_manager
from muika.llm._schema import ToolResult
from muika.llm.utils.tools import ToolError
from muika.plugin.func_call import on_function_call
from muika.plugin.func_call.context import ToolContext, get_dependencies

_SENSITIVE_PREFIXES = (
    "OPENAI_",
    "AZURE_",
    "DASHSCOPE_",
    "GEMINI_",
    "GOOGLE_",
    "ANTHROPIC_",
    "HUGGINGFACE_",
    "API_KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
)
_DEFAULT_TIMEOUT = 1800.0
_DEFAULT_SHELL: Literal["powershell", "bash", "cmd"] = "powershell" if sys.platform == "win32" else "bash"


def _sanitize_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(_SENSITIVE_PREFIXES)}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return env


def _owner() -> str | None:
    context = get_dependencies().get(ToolContext)
    return context.task_id if isinstance(context, ToolContext) else None


def _result(result: ProcessResult) -> ToolResult:
    return ToolResult(
        text=result.model_dump_json(),
        is_error=result.status in {"stopped", "timeout"} or result.exit_code not in {None, 0},
    )


class ExecutionParams(BaseModel):
    timeout: float = Field(
        _DEFAULT_TIMEOUT, gt=0, description="Hard execution deadline in seconds. Default 30 minutes."
    )
    yield_time: float = Field(1.0, ge=0, le=30, description="Seconds to wait for output before returning a process ID.")
    cwd: str | None = Field(None, description="Working directory. Defaults to the current Muika working directory.")


class ExecutePythonParams(ExecutionParams):
    code: str = Field(
        ..., description="Python source code. Uses Muika's Python interpreter; print output for inspection."
    )


async def _start(command: list[str], timeout: float, yield_time: float, cwd: str | None) -> ToolResult:
    manager = get_process_manager()
    try:
        process_id = await manager.start(
            command,
            env=_sanitize_env(),
            cwd=str(Path(cwd).resolve() if cwd else Path.cwd()),
            owner=_owner(),
            timeout=timeout,
        )
        return _result(await manager.wait(process_id, owner=_owner(), seconds=yield_time))
    except (OSError, ValueError) as exc:
        return ToolResult(text=f"Could not start execution: {exc}", is_error=True)


@on_function_call(
    "Run Python in Muika's interpreter with UTF-8 output. A running result is not completion. "
    "Use wait_process to continue waiting. Requires ENABLE_CODE_EXECUTION=true.",
    params=ExecutePythonParams,
)
async def execute_python(code: str, timeout: float = _DEFAULT_TIMEOUT, yield_time: float = 1.0, cwd: str | None = None):
    if not mas_config.enable_code_execution:
        return ToolError("Code execution is disabled. Set ENABLE_CODE_EXECUTION=true to enable.")
    return await _start([sys.executable, "-u", "-c", code], timeout, yield_time, cwd)


class ExecuteShellParams(ExecutionParams):
    command: str = Field(
        ..., description="Command in the chosen shell's syntax. Do not mix PowerShell and Bash syntax."
    )
    shell: Literal["powershell", "bash", "cmd"] = Field(_DEFAULT_SHELL, description="Shell to execute the command.")


@on_function_call(
    "Run a shell command and return a process ID, exit status and output. "
    "Use wait_process for running work. Requires ENABLE_SHELL_EXECUTION=true.",
    params=ExecuteShellParams,
)
async def execute_shell(
    command: str,
    shell: str = _DEFAULT_SHELL,
    timeout: float = _DEFAULT_TIMEOUT,
    yield_time: float = 1.0,
    cwd: str | None = None,
):
    if not mas_config.enable_shell_execution:
        return ToolError("Shell execution is disabled. Set ENABLE_SHELL_EXECUTION=true to enable.")
    if shell == "powershell":
        args = [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); " + command,
        ]
    elif shell == "cmd":
        args = ["cmd.exe", "/d", "/c", "chcp 65001 >nul & " + command]
    elif shell == "bash":
        executable = shutil.which("bash")
        if executable is None:
            return ToolError("Bash is unavailable in PATH. Choose an installed shell.")
        args = [executable, "-c", command]
    else:
        return ToolError(f"Unsupported shell: {shell}")
    return await _start(args, timeout, yield_time, cwd)


class WaitProcessParams(BaseModel):
    process_id: str = Field(..., description="Process ID returned by execute_python or execute_shell in this runtime.")
    seconds: float = Field(1.0, ge=0, le=30, description="Maximum seconds to wait; zero only reads current output.")
    stdout_offset: int = Field(0, ge=0, description="Byte offset from the previous result to continue reading stdout.")
    stderr_offset: int = Field(0, ge=0, description="Byte offset from the previous result to continue reading stderr.")
    max_bytes: int = Field(12000, ge=1, le=100000, description="Maximum bytes per output stream.")


@on_function_call(
    "Wait for an existing process or read another page of its output without restarting it.",
    params=WaitProcessParams,
    read_only=True,
)
async def wait_process(
    process_id: str, seconds: float = 1.0, stdout_offset: int = 0, stderr_offset: int = 0, max_bytes: int = 12000
):
    try:
        return _result(
            await get_process_manager().wait(
                process_id,
                owner=_owner(),
                seconds=seconds,
                stdout_offset=stdout_offset,
                stderr_offset=stderr_offset,
                max_bytes=max_bytes,
            )
        )
    except (ValueError, OSError) as exc:
        return ToolError(str(exc))


class StopProcessParams(BaseModel):
    process_id: str = Field(..., description="Process ID from this runtime to stop, including its descendants.")


@on_function_call("Stop an owned process and its child processes, then return its output.", params=StopProcessParams)
async def stop_process(process_id: str):
    try:
        return _result(await get_process_manager().stop(process_id, owner=_owner()))
    except (ValueError, OSError) as exc:
        return ToolError(str(exc))


class ReadExecutionParams(BaseModel):
    process_id: str
    stdout_offset: int = Field(0, ge=0)
    stderr_offset: int = Field(0, ge=0)
    max_bytes: int = Field(12000, ge=1, le=100000)


@on_function_call(
    "Read saved execution status and output, including after restart. This does not reattach a process.",
    params=ReadExecutionParams,
    read_only=True,
)
async def read_execution_record(
    process_id: str, stdout_offset: int = 0, stderr_offset: int = 0, max_bytes: int = 12000
):
    try:
        return json.dumps(
            get_process_manager().read_record(
                process_id,
                owner=_owner(),
                stdout_offset=stdout_offset,
                stderr_offset=stderr_offset,
                max_bytes=max_bytes,
            ),
            ensure_ascii=False,
        )
    except (ValueError, OSError) as exc:
        return ToolError(str(exc))


class ReadTaskOutputParams(BaseModel):
    path: str = Field(..., description="Full output path from this task's observation index.")
    offset: int = Field(0, ge=0)
    max_chars: int = Field(12000, ge=1, le=100000)


@on_function_call("Read another page of this task's saved tool output.", params=ReadTaskOutputParams, read_only=True)
async def read_task_output(path: str, offset: int = 0, max_chars: int = 12000):
    owner = _owner()
    if owner is None:
        return ToolError("No action task owns this request")
    root = mas_config.data_dir.resolve() / "agent_tasks" / owner
    target = Path(path).resolve()
    if not target.is_relative_to(root) or target.suffix not in {".json", ".txt"}:
        return ToolError("The output must belong to the current task")
    try:
        content = target.read_text(encoding="utf-8")
        return json.dumps(
            {
                "text": content[offset : offset + max_chars],
                "next_offset": offset + max_chars,
                "more": offset + max_chars < len(content),
            },
            ensure_ascii=False,
        )
    except (OSError, UnicodeError) as exc:
        return ToolError(str(exc))
