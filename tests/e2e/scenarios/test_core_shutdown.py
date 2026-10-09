"""验证监督进程等待 Core 清理，并在重复信号或超时后强制退出。"""

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from muika.ipc.supervisor import stop_tree

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[3]
PROGRAM = r"""
import asyncio
import _thread
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from muika.ipc import supervisor

directory = Path.cwd()
mode, signum, transport = sys.argv[1], int(sys.argv[2]), sys.argv[3]
if transport == "console":
    sys.stdout = open("CONOUT$", "w")
    sys.stderr = open("CONOUT$", "w")
if supervisor.lifecycle_directory() is None:
    original_popen = subprocess.Popen
    def launch_worker(command, **kwargs):
        return original_popen([sys.executable, __file__, mode, str(signum), transport], **kwargs)
    supervisor.subprocess.Popen = launch_worker
    if mode == "timeout":
        supervisor.SHUTDOWN_TIMEOUT = 0.3
    def send_signals():
        for number in (1, 2):
            while not (directory / f"signal-{number}").exists():
                time.sleep(0.01)
            if transport == "console":
                import win32api
                win32api.GenerateConsoleCtrlEvent(0, 0)
            elif sys.platform == "win32":
                _thread.interrupt_main(signum)
            else:
                os.kill(os.getpid(), signum)
    threading.Thread(target=send_signals, daemon=True).start()
    supervisor.main([])
else:
    supervisor.watch_parent()
    from muika.ipc import bootstrap
    bootstrap.init_logger = MagicMock()
    bootstrap.init_db = AsyncMock()
    bootstrap.cleanup_servers = AsyncMock()
    bootstrap.load_plugins = MagicMock()
    bootstrap.get_core_proposal_manager = MagicMock(return_value=MagicMock(recover_incomplete=lambda: []))
    bootstrap.mas_config.enable_plugin_hot_reload = False
    instance = MagicMock(stop_requested=asyncio.Event(), restart_requested=True)
    async def start():
        control = supervisor.lifecycle_directory()
        supervisor.write_json(control / "ready.json", {})
        supervisor.write_json(control / "restart.json", {})
        with (directory / "starts").open("a") as stream:
            stream.write("started\n")
        (directory / "ready").touch()
        if mode == "startup":
            await asyncio.sleep(60)
    async def stop():
        (directory / "summary-requested").touch()
        await asyncio.sleep(0.5 if mode in {"graceful", "startup", "initializing"} else 60)
        (directory / "summary-saved").touch()
    if mode == "initializing":
        async def init_db():
            (directory / "starts").write_text("started\n")
            (directory / "ready").touch()
            await asyncio.sleep(60)
        bootstrap.init_db = init_db
        bootstrap.close_db = stop
        bootstrap.stop_plugin_watcher = MagicMock()
        bootstrap.get_plugin_manager = MagicMock()
    instance.start = start
    instance.stop = stop
    bootstrap.CoreBootstrap = MagicMock(
        return_value=instance, watch_stop_request=bootstrap.CoreBootstrap.watch_stop_request
    )
    asyncio.run(bootstrap.run_core())
"""


@pytest.mark.parametrize(
    ("transport", "signum"), [("dispatch", signal.SIGINT), ("dispatch", signal.SIGTERM), ("console", signal.SIGINT)]
)
@pytest.mark.parametrize("mode", ["graceful", "startup", "initializing", "repeat", "timeout"])
async def test_supervisor_waits_for_core_shutdown(tmp_path, recorder, mode, transport, signum):
    if transport == "console" and sys.platform != "win32":
        pytest.skip("A real Windows console is required")
    script = tmp_path / "shutdown.py"
    script.write_text(PROGRAM, encoding="utf-8")
    environment = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    options = {}
    if transport == "console":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        options.update(startupinfo=startup, creationflags=subprocess.CREATE_NEW_CONSOLE)
    with (tmp_path / "output.log").open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [sys.executable, str(script), mode, str(signum), transport],
            cwd=tmp_path,
            env=environment,
            stdout=output,
            stderr=output,
            **options,
        )
        try:
            deadline = time.monotonic() + 25
            while not (tmp_path / "ready").exists():
                assert process.poll() is None, (tmp_path / "output.log").read_text(encoding="utf-8")
                assert time.monotonic() < deadline, "Core startup timed out"
                await asyncio.sleep(0.05)
            (tmp_path / "signal-1").touch()
            while not (tmp_path / "summary-requested").exists():
                assert process.poll() is None, (tmp_path / "output.log").read_text(encoding="utf-8")
                assert time.monotonic() < deadline, "Core did not start shutdown"
                await asyncio.sleep(0.02)
            if mode == "repeat":
                (tmp_path / "signal-2").touch()
            await asyncio.wait_for(asyncio.to_thread(process.wait), 10)
        finally:
            if process.poll() is None:
                stop_tree(process.pid)
                process.wait(timeout=10)
    saved = (tmp_path / "summary-saved").exists()
    starts = (tmp_path / "starts").read_text().splitlines()
    recorder.record(
        "shutdown",
        mode=mode,
        transport=transport,
        signal=signum.name,
        summary_saved=saved,
        starts=len(starts),
        exit_code=process.returncode,
    )
    assert process.returncode == 0
    assert saved is (mode in {"graceful", "startup", "initializing"})
    assert len(starts) == 1
