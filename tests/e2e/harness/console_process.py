"""在真实控制台或后台文件输出中检查 Core 启动、重启与日志。"""

import asyncio
import json
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import aiohttp

from muika.ipc.supervisor import stop_tree

if sys.platform == "win32":
    import win32console


async def run(directory: Path, port: int, mode: str) -> None:
    result: dict = {
        "mode": mode,
        "stdout_tty": sys.stdout is not None and sys.stdout.isatty(),
        "stderr_tty": sys.stderr is not None and sys.stderr.isatty(),
    }
    with ExitStack() as stack:
        output = stack.enter_context((directory / "stdout.log").open("w", encoding="utf-8"))
        errors = stack.enter_context((directory / "stderr.log").open("w", encoding="utf-8"))
        creationflags = 0
        if sys.platform == "win32" and mode == "background":
            creationflags = subprocess.CREATE_NO_WINDOW
        process = subprocess.Popen(
            [sys.executable, str(directory / "core_main.py"), "--port", str(port)],
            stdout=output if mode in {"background", "stdout_file"} else None,
            stderr=errors if mode in {"background", "stderr_file"} else None,
            creationflags=creationflags,
        )
        try:
            async with aiohttp.ClientSession() as session:

                async def wait_ready() -> None:
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            raise RuntimeError(f"Core exited before readiness: {process.returncode}")
                        try:
                            async with session.get(f"http://127.0.0.1:{port}/health") as response:
                                if response.status == 200:
                                    return
                        except aiohttp.ClientError:
                            pass
                        await asyncio.sleep(0.1)
                    raise TimeoutError("Core did not become ready")

                await wait_ready()
                result["ready"] = True
                async with session.ws_connect(
                    f"http://127.0.0.1:{port}/ws",
                    headers={"X-Auth-Token": "logging-test", "X-Client-Name": "logging-test"},
                ) as ws:
                    await ws.send_json({"type": "command", "raw": ".restart"})
                    path = directory / "data" / "restart.json"
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        if path.is_file() and json.loads(path.read_text(encoding="utf-8"))["status"] == "started":
                            result["restarted"] = True
                            break
                        if process.poll() is not None:
                            raise RuntimeError(f"Core exited during restart: {process.returncode}")
                        await asyncio.sleep(0.1)
                    else:
                        raise TimeoutError("Core did not finish restarting")
        except Exception as error:
            result["error"] = f"{type(error).__name__}: {error}"
        finally:
            stop_tree(process.pid)
            process.wait(timeout=10)
            if sys.platform == "win32" and mode != "background":
                console = win32console.GetStdHandle(win32console.STD_OUTPUT_HANDLE)
                size = console.GetConsoleScreenBufferInfo()["Size"]
                result["console"] = console.ReadConsoleOutputCharacter(
                    size.X * size.Y, win32console.PyCOORDType(0, 0)
                ).rstrip()
    result["redirected_stdout"] = (directory / "stdout.log").read_text(encoding="utf-8", errors="replace")
    result["redirected_stderr"] = (directory / "stderr.log").read_text(encoding="utf-8", errors="replace")
    result["file_log"] = "\n".join(path.read_text(encoding="utf-8") for path in (directory / "logs").glob("*.log"))
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]))
