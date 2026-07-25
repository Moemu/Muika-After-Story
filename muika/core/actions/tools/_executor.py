from __future__ import annotations

import sys

from pydantic import BaseModel, Field

from muika.config import mas_config
from muika.plugin.func_call import on_function_call
from muika.utils.logger import logger

# Sensitive env-var prefixes stripped before passing env to subprocess
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

_MAX_TIMEOUT = 30.0
_DEFAULT_TIMEOUT = 10.0
_OUTPUT_MAX_CHARS = 4000


def _sanitize_env() -> dict[str, str]:
    """Return a copy of os.environ with sensitive keys removed."""
    import os

    return {
        k: v for k, v in os.environ.items() if not any(k.upper().startswith(prefix) for prefix in _SENSITIVE_PREFIXES)
    }


class ExecutePythonParams(BaseModel):
    code: str = Field(
        ...,
        description=(
            "Python source code to execute. "
            "print() to stdout to produce output. "
            "The subprocess has access to installed packages but no special globals."
        ),
    )
    timeout: float = Field(
        _DEFAULT_TIMEOUT,
        description=f"Execution timeout in seconds (max {_MAX_TIMEOUT}s). "
        "The process is forcefully terminated after this time.",
    )


@on_function_call(
    "Execute a Python code snippet in an isolated subprocess and return stdout/stderr. "
    "Requires ENABLE_CODE_EXECUTION=true.",
    params=ExecutePythonParams,
)
async def execute_python(code: str, timeout: float = _DEFAULT_TIMEOUT):
    if not mas_config.enable_code_execution:
        return "Code execution is disabled. Set ENABLE_CODE_EXECUTION=true to enable."

    import asyncio

    timeout = min(max(timeout, 0.5), _MAX_TIMEOUT)

    env = _sanitize_env()
    proc: asyncio.subprocess.Process | None = None

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
            returncode = proc.returncode
        except asyncio.TimeoutError:
            try:
                proc.terminate()
                await asyncio.sleep(0.5)
                proc.kill()
            except ProcessLookupError:
                pass
            logger.warning(f"[ExecutePython] Process timed out after {timeout}s")
            return f"Execution timed out after {timeout}s."

    except Exception as e:
        logger.error(f"[ExecutePython] Failed to launch subprocess: {e}")
        return f"Failed to launch process: {e}"

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")

    parts: list[str] = []
    if stdout:
        truncated = stdout[:_OUTPUT_MAX_CHARS]
        suffix = (
            f"\n...(stdout truncated, {len(stdout) - _OUTPUT_MAX_CHARS:,} chars omitted)"
            if len(stdout) > _OUTPUT_MAX_CHARS
            else ""
        )
        parts.append(f"[stdout]\n{truncated}{suffix}")
    if stderr:
        truncated = stderr[:_OUTPUT_MAX_CHARS]
        suffix = (
            f"\n...(stderr truncated, {len(stderr) - _OUTPUT_MAX_CHARS:,} chars omitted)"
            if len(stderr) > _OUTPUT_MAX_CHARS
            else ""
        )
        parts.append(f"[stderr]\n{truncated}{suffix}")
    if not parts:
        parts.append("(no output)")

    parts.append(f"[exit code: {returncode}]")

    logger.info(f"[ExecutePython] Completed with exit code {returncode}")
    return "\n\n".join(parts)
