"""先验证 P0 失败边界，再接入生产 Core。

失败场景：收件确认丢失导致重复输入；双候选同时领取控制权；旧任期迟到提交；
回复提交后进程被杀；不同客户端错投或越权确认；状态服务重启复活旧租约；
连续写入及大检查点阻塞续租。实验使用独立服务进程和真实出站 WebSocket。
此实验不验证生产 Core、平台投递、TLS、插件或跨操作系统部署。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
import statistics
import sys
import time
from pathlib import Path

import aiohttp
import pytest

pytestmark = pytest.mark.e2e
PROBE = Path(__file__).resolve().parents[1] / "harness" / "state_probe.py"


async def test_outbound_state_service_recovery(tmp_path, recorder):
    """验证控制权、消息事务和进程中断，并产出测量轨迹。"""
    ready = tmp_path / "ready.json"
    directory = tmp_path / "server"
    directory.mkdir()
    token = secrets.token_urlsafe(24)
    process: asyncio.subprocess.Process | None = None
    timings: list[float] = []

    async def start() -> str:
        nonlocal process
        ready.unlink(missing_ok=True)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(PROBE),
            "--directory",
            str(directory),
            "--ready",
            str(ready),
            "--token",
            token,
            cwd=directory,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        for _ in range(100):
            if ready.exists():
                return f"http://127.0.0.1:{json.loads(ready.read_text(encoding='utf-8'))['port']}/ws"
            if process.returncode is not None:
                assert process.stderr is not None
                raise AssertionError((await process.stderr.read()).decode())
            await asyncio.sleep(0.05)
        raise AssertionError("State probe did not start")

    async def request(ws, **payload):
        started = time.perf_counter()
        await ws.send_json(payload)
        result = await ws.receive_json(timeout=10)
        timings.append((time.perf_counter() - started) * 1000)
        return result

    try:
        address = await start()
        async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {token}"}) as session:
            async with (
                session.ws_connect(address, params={"client": "bot-a", "role": "bot"}) as bot_a,
                session.ws_connect(address, params={"client": "bot-b", "role": "bot"}) as bot_b,
                session.ws_connect(address, params={"client": "pc", "role": "core"}) as pc,
                session.ws_connect(address, params={"client": "linux", "role": "core"}) as linux,
            ):
                grant = await request(pc, op="acquire")
                epoch = grant["epoch"]
                assert (await request(linux, op="acquire"))["error"] == "lease_busy"
                assert (await request(bot_a, op="receive", id="first", body="Remember our conversation"))["received"]
                assert (await request(bot_a, op="receive", id="first", body="Remember our conversation"))["received"]
                assert (await request(bot_b, op="receive", id="first", body="different"))["error"] == "input_conflict"
                assert (await request(pc, op="claim", epoch=epoch))["input"]["id"] == "first"
                await pc.close()
                recorder.record("fault", action="disconnect_core_without_release", epoch=epoch)
                await asyncio.sleep(1.1)
                next_epoch = (await request(linux, op="acquire"))["epoch"]
                assert next_epoch > epoch
                assert (await request(linux, op="claim", epoch=next_epoch))["input"]["id"] == "first"
                checkpoint = json.dumps({"session_id": "continuing", "mood": "curious", "history": "x" * 100_000})
                async with session.ws_connect(address, params={"client": "pc", "role": "core"}) as stale:
                    rejected = await request(
                        stale, op="commit", epoch=epoch, id="first", reply="stale", checkpoint=checkpoint
                    )
                    assert rejected["error"] == "stale_owner"
                commit = dict(op="commit", epoch=next_epoch, id="first", reply="Still here", checkpoint=checkpoint)
                assert (await request(linux, **commit))["processed"] == "first"
                assert (await request(linux, **commit))["processed"] == "first"
                assert (await request(bot_b, op="pending"))["messages"] == []
                await request(bot_b, op="delivered", id="first")
                assert len((await request(bot_a, op="pending"))["messages"]) == 1
                recorder.record("checked", invariant="atomic_reply_and_checkpoint_with_fenced_owner")
                assert process is not None
                process.kill()
                await process.wait()
                recorder.record("fault", action="kill_state_service_after_commit")
            address = await start()
            async with (
                session.ws_connect(address, params={"client": "bot-a", "role": "bot"}) as bot_a,
                session.ws_connect(address, params={"client": "bot-b", "role": "bot"}) as bot_b,
                session.ws_connect(address, params={"client": "linux", "role": "core"}) as linux,
            ):
                assert (await request(linux, op="renew", epoch=next_epoch))["error"] == "stale_owner"
                recovered = (await request(bot_a, op="pending"))["messages"]
                assert recovered == [{"id": "first", "body": "Still here"}]
                await request(bot_a, op="delivered", id="first")
                epoch = (await request(linux, op="acquire"))["epoch"]
                for batch in range(25):
                    await asyncio.gather(
                        request(bot_a, op="receive", id=f"a-{batch}", body="A" * 1024),
                        request(bot_b, op="receive", id=f"b-{batch}", body="B" * 1024),
                        request(linux, op="renew", epoch=epoch),
                    )
                    for _ in range(2):
                        message = (await request(linux, op="claim", epoch=epoch))["input"]
                        result = await request(
                            linux, op="commit", epoch=epoch, id=message["id"], reply="Done", checkpoint=checkpoint
                        )
                        assert "error" not in result
                assert len((await request(bot_a, op="pending"))["messages"]) == 25
                assert len((await request(bot_b, op="pending"))["messages"]) == 25
                recorder.record("checked", invariant="client_routing_and_renewal_under_writes")
        with sqlite3.connect(directory / "probe.sqlite") as db:
            assert db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 51
            assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 51
            assert db.execute("SELECT body FROM checkpoint WHERE id=1").fetchone()[0] == checkpoint
        ordered = sorted(timings)
        recorder.record(
            "measurement",
            requests=len(timings),
            checkpoint_bytes=len(checkpoint),
            p50_ms=round(statistics.median(timings), 3),
            p95_ms=round(ordered[int((len(ordered) - 1) * 0.95)], 3),
            max_ms=round(max(timings), 3),
            topology="loopback, one server process, four WS clients",
            production_core=False,
            tls=False,
        )
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
