"""验证真实进程死亡和独立工作目录。

失败边界：关机钩子依赖；服务重启丢队列；接管人格回到初次相遇；
输入重投产生重复回复；私有独白泄露；服务器失联后客户端静默丢输入。
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from muika.ipc.bot_client import DurableBotClient
from muika.node.auth import CredentialStore
from muika.node.config import (
    NodeProfile,
    PluginBinding,
    ServerProfile,
    write_private_json,
)
from muika.node.models import IncomingMessage

pytestmark = pytest.mark.e2e


async def test_supervised_core_reports_initialization_failure(tmp_path, recorder):
    repo = Path(__file__).resolve().parents[3]
    profile = NodeProfile(
        id="failed",
        role="core",
        token="test-only",
        address="ws://127.0.0.1:8766/node/ws",
        directory=tmp_path,
        plugins=[PluginBinding(module="plugins.missing", role="device", requires_tools=["unavailable-tool"])],
    )
    path = tmp_path / "node.json"
    write_private_json(path, profile)
    env = dict(os.environ, PYTHONPATH=str(repo), MASTER_ID="test-master", IPC_SECRET="unused-test-secret")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "muika.node",
        "run",
        str(path),
        "--supervised",
        cwd=tmp_path,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        await asyncio.wait_for(process.wait(), 10)
        output, _ = await process.communicate()
        assert process.returncode != 0 and b"unavailable tools" in output
        assert b"Fatal Python error" not in output
        recorder.record("checked", invariant="supervised_core_failure_exits_without_stdin_shutdown_deadlock")
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def until(predicate):
    async with asyncio.timeout(30):
        while not predicate():
            await asyncio.sleep(0.05)


async def test_kill_pc_and_restart_state_with_real_nodes(tmp_path, recorder):
    repo = Path(__file__).resolve().parents[3]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    address = f"ws://127.0.0.1:{port}/node/ws"
    server_dir = tmp_path / "server"
    server_dir.mkdir()
    (server_dir / "configs").mkdir()
    (server_dir / "configs/models.yml").write_text("default:\n  provider: _echo\n  default: true\n", encoding="utf-8")
    server_profile = ServerProfile(
        directory=server_dir,
        database=server_dir / "state.db",
        public_address=address,
        port=port,
        embedded_core=False,
    )
    write_private_json(server_dir / "server.json", server_profile)
    store = CredentialStore(server_dir / "credentials.db")
    profiles = {}
    for name, role in (("pc", "core"), ("backup", "core"), ("chat", "bot")):
        credential, token = store.redeem(store.issue(name, role))
        directory = tmp_path / name
        profile = NodeProfile(
            id=credential.id, role=role, address=address, token=token, directory=directory, lease_seconds=1
        )
        directory.mkdir()
        write_private_json(directory / "node.json", profile)
        profiles[name] = profile
    (tmp_path / "pc/turns.json").write_text(
        json.dumps(
            [
                '<heart>我想把这句诗藏在心里。</heart>我记住了。<state>{"mood":"期待","reason":"同一首诗。"}</state>',
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "backup/turns.json").write_text(
        json.dumps(["我们继续那首诗。", "刚才的消息还在，我没有忘记。"]), encoding="utf-8"
    )
    env = dict(os.environ)
    env.update(
        PYTHONPATH=os.pathsep.join([str(repo), str(repo / "tests/e2e")]),
        MASTER_ID="test_master",
        IPC_SECRET="test-only-secret",
        ENABLE_AUTO_REFLECTION="false",
    )
    jobs = []
    logs = []

    async def launch(role, name):
        directory = server_dir if role == "state" else tmp_path / name
        profile_path = directory / ("server.json" if role == "state" else "node.json")
        args = [sys.executable, "-m", "harness.node_process", role, str(profile_path)]
        if role == "core":
            args += ["--turns", str(directory / "turns.json")]
        log = (directory / f"process-{len(jobs)}.log").open("wb")
        logs.append(log)
        job = await asyncio.create_subprocess_exec(
            *args,
            cwd=directory,
            env=env,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        jobs.append(job)
        return job

    replies = []
    statuses = []

    async def deliver(message, resources):
        replies.append(message)

    async def status(value):
        statuses.append(value)

    bot = DurableBotClient(address, profiles["chat"].token, "chat", tmp_path / "bot", deliver, on_status=status)
    bot_job = asyncio.create_task(bot.run())
    try:
        state_job = await launch("state", "server")
        await until(lambda: bot.connected.is_set())
        pc_job = await launch("core", "pc")
        await until(lambda: (tmp_path / "pc/ready.json").exists())
        await launch("core", "backup")
        original = IncomingMessage(id="promise", client_id="chat", conversation_id="private", text="记住那首诗。")
        await bot.queue_input(original)
        await until(lambda: any("记住了" in reply.text for reply in replies))
        started = time.monotonic()
        pc_job.kill()
        await pc_job.wait()
        await bot.queue_input(
            IncomingMessage(id="continue", client_id="chat", conversation_id="private", text="我们继续。")
        )
        await until(lambda: any("继续那首诗" in reply.text for reply in replies))
        takeover_seconds = time.monotonic() - started
        assert takeover_seconds < 15
        state_job.kill()
        await state_job.wait()
        await until(lambda: not bot.connected.is_set())
        await bot.queue_input(
            IncomingMessage(id="offline", client_id="chat", conversation_id="private", text="刚才的消息还在吗？")
        )
        await launch("state", "server")
        await until(lambda: any("没有忘记" in reply.text for reply in replies))
        await bot.queue_input(original)
        await asyncio.sleep(0.5)
        assert len([reply for reply in replies if "记住了" in reply.text]) == 1
        assert all(
            reply.conversation_id == "private" and "藏在心里" not in reply.text and "<heart>" not in reply.text
            for reply in replies
        )
        assert "offline" in statuses
        recorder.record("checked", invariant="real_process_kill_and_state_restart", takeover_seconds=takeover_seconds)
    finally:
        bot_job.cancel()
        await asyncio.gather(bot_job, return_exceptions=True)
        await bot.close()
        for job in jobs:
            if job.returncode is None:
                job.kill()
        await asyncio.gather(*(job.wait() for job in jobs))
        for log in logs:
            log.close()
