"""子进程的等待、输出续读及清理检查。"""

import asyncio
import os
import sys

import psutil
import pytest

from muika.core.processes import ProcessManager


@pytest.fixture
async def processes():
    manager = ProcessManager()
    yield manager
    await manager.close()


def _environment():
    return {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


async def test_wait_returns_running_and_later_reads_all_output(processes, tmp_path):
    process_id = await processes.start(
        [sys.executable, "-u", "-c", "import time; print('first', flush=True); time.sleep(0.4); print('second')"],
        env=_environment(),
        cwd=str(tmp_path),
        owner="task",
        timeout=5,
    )
    first = await processes.wait(process_id, owner="task", seconds=0)
    assert first.status == "running"
    final = await processes.wait(process_id, owner="task", seconds=3)
    assert final.status == "completed"
    assert final.exit_code == 0
    assert "first" in final.stdout and "second" in final.stdout


async def test_output_can_be_read_in_pages(processes, tmp_path):
    process_id = await processes.start(
        [sys.executable, "-u", "-c", "print('abcdefghij', end='')"],
        env=_environment(),
        cwd=str(tmp_path),
        owner=None,
        timeout=5,
    )
    first = await processes.wait(process_id, owner=None, seconds=3, max_bytes=4)
    second = await processes.wait(process_id, owner=None, seconds=0, stdout_offset=first.stdout_offset, max_bytes=4)
    last = await processes.wait(process_id, owner=None, seconds=0, stdout_offset=second.stdout_offset, max_bytes=4)
    assert first.stdout + second.stdout + last.stdout == "abcdefghij"
    assert first.stdout_more and second.stdout_more and not last.stdout_more


def test_output_pages_preserve_utf8_characters(tmp_path):
    output = tmp_path / "output.log"
    output.write_text("你好world", encoding="utf-8")
    offset = 0
    chunks = []
    while True:
        content, following, more = ProcessManager._read(output, offset, 1)
        assert following > offset
        chunks.append(content)
        offset = following
        if not more:
            break
    assert "".join(chunks) == "你好world"


async def test_stopping_execution_terminates_descendants(processes, tmp_path):
    code = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "print(child.pid,flush=True); time.sleep(60)"
    )
    process_id = await processes.start(
        [sys.executable, "-u", "-c", code],
        env=_environment(),
        cwd=str(tmp_path),
        owner="task",
        timeout=10,
    )
    result = await processes.wait(process_id, owner="task", seconds=1)
    child_pid = int(result.stdout.strip())
    assert psutil.pid_exists(child_pid)
    stopped = await processes.stop(process_id, owner="task")
    assert stopped.status == "stopped"
    for _ in range(20):
        if not psutil.pid_exists(child_pid):
            break
        await asyncio.sleep(0.05)
    assert not psutil.pid_exists(child_pid)


async def test_timeout_and_unknown_runtime_id_are_explicit(processes, tmp_path):
    process_id = await processes.start(
        [sys.executable, "-u", "-c", "import time; time.sleep(60)"],
        env=_environment(),
        cwd=str(tmp_path),
        owner="task",
        timeout=0.1,
    )
    result = await processes.wait(process_id, owner="task", seconds=3)
    assert result.status == "timeout" and result.exit_code is not None
    with pytest.raises(ValueError, match="another task"):
        await processes.wait(process_id, owner="other", seconds=0)
    with pytest.raises(ValueError, match="runtime"):
        await ProcessManager().wait(process_id, owner="task", seconds=0)
    saved = ProcessManager().read_record(process_id, owner="task")
    assert saved["status"] == "timeout" and not saved["controllable"]
    with pytest.raises(ValueError, match="another task"):
        ProcessManager().read_record(process_id, owner="other")
