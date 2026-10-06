"""验证 .env 日志阈值、真实控制台、输出重定向、后台运行和重启后的输出。"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from muika.ipc.supervisor import stop_tree

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("mode", "level"),
    [
        ("console", "DEBUG"),
        ("console", "INFO"),
        ("stdout_file", "DEBUG"),
        ("stderr_file", "DEBUG"),
        ("background", "DEBUG"),
    ],
)
async def test_core_console_logging_survives_restart(tmp_path, recorder, mode, level):
    if mode != "background" and sys.platform != "win32":
        pytest.skip("A real Windows console is required")
    shutil.copytree(ROOT / "muika", tmp_path / "muika", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("core_main.py", "alembic.ini"):
        shutil.copy2(ROOT / name, tmp_path / name)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "models.yml").write_text(
        "chat:\n  provider: openai\n  model_name: logging-test\n  api_key: logging-test\n  default: true\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        f"LOG_LEVEL={level}\nENABLE_AUTO_REFLECTION=false\nSELF_CHANGE_AWARENESS_ENABLED=false\n",
        encoding="utf-8",
    )
    environment = {key: value for key, value in os.environ.items() if key.upper() != "LOG_LEVEL"}
    environment.update(MASTER_ID="logging-master", IPC_SECRET="logging-test", PYTHONPATH=str(tmp_path))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    options: dict = {}
    if mode != "background":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        options.update(startupinfo=startup, creationflags=subprocess.CREATE_NEW_CONSOLE)
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "tests/e2e/harness/console_process.py"), str(tmp_path), str(port), mode],
        cwd=tmp_path,
        env=environment,
        **options,
    )
    try:
        await asyncio.wait_for(asyncio.to_thread(process.wait), 55)
    finally:
        if process.poll() is None:
            await asyncio.to_thread(stop_tree, process.pid)
            await asyncio.to_thread(process.wait)
    assert process.returncode == 0
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    recorder.record("core_logging", level=level, **result)
    assert "error" not in result, result
    assert result["ready"] and result["restarted"]
    visible = result["redirected_stdout"] if mode in {"background", "stdout_file"} else result["console"]
    assert visible.count("Muika-After-Story version:") == 2
    assert ("[DEBUG]" in visible) is (level == "DEBUG")
    assert "[DEBUG]" in result["file_log"]
    if mode != "background":
        assert result["stdout_tty"] and result["stderr_tty"]
