"""在临时副本中验证标准入口、应用重启、启动失败回退和进程清理。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import aiohttp
import psutil

ROOT = Path(__file__).resolve().parents[1]


async def wait_health(port: int, process: psutil.Process) -> None:
    """等到测试实例就绪，失败时保留明确错误。"""
    async with aiohttp.ClientSession() as session:
        for _ in range(300):
            if not process.is_running():
                raise RuntimeError("Supervisor exited before readiness.")
            try:
                async with session.get(f"http://127.0.0.1:{port}/health") as response:
                    if response.status == 200:
                        return
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.1)
    raise TimeoutError("Core did not become ready.")


async def command(port: int, raw: str) -> str:
    """通过真实 IPC 命令入口驱动测试实例。"""
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            f"http://127.0.0.1:{port}/ws",
            headers={"X-Auth-Token": "smoke-secret", "X-Client-Name": "restart-smoke"},
        ) as ws:
            await ws.send_json({"type": "command", "raw": raw})
            for _ in range(30):
                message = await ws.receive(timeout=45)
                if message.type == aiohttp.WSMsgType.TEXT:
                    body = json.loads(message.data)
                    if body.get("type") == "command_result":
                        return body["content"]
                elif message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}:
                    return "Connection closed for restart."
    raise RuntimeError("Command returned no result.")


def create_proposal(repo: Path, env: dict[str, str], fail_startup: bool) -> str:
    """准备一次可控测试变更，交由真实命令进行审批和验证。"""
    change = (
        {
            "action": "modify",
            "path": "muika/core/state.py",
            "replacements": [
                {
                    "old_text": "from __future__ import annotations",
                    "new_text": "from __future__ import annotations\nraise RuntimeError('smoke startup failure')",
                }
            ],
        }
        if fail_startup
        else {"action": "create", "path": "muika/core/restart_smoke_marker.py", "content": "READY = True\n"}
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,sys; from muika.core.self_mod.proposals import CoreProposalManager; "
            "print(CoreProposalManager().create([json.load(sys.stdin)], 'Restart integration fixture.'))",
        ],
        cwd=repo,
        env=env,
        input=json.dumps(change),
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip().splitlines()[-1]


async def plain_restart(
    repo: Path, port: int, supervisor: psutil.Process, *, broken: bool = False, autonomous: bool = False
) -> None:
    """经真实命令验证普通重启，不借用旧提案的审批或回滚快照。"""
    path = repo / "data/restart.json"
    previous = json.loads(path.read_text(encoding="utf-8"))["id"] if path.is_file() else None
    children = supervisor.children(recursive=True)
    worker_pid = max(children, key=lambda p: len(p.parents())).pid
    await command(port, ".restart_probe" if autonomous else ".restart")
    for _ in range(600):
        if path.is_file():
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("id") != previous and record["status"] in {"started", "failed"}:
                break
        await asyncio.sleep(0.1)
    else:
        raise TimeoutError("No plain restart outcome.")
    assert record["status"] == ("failed" if broken else "started"), record
    assert record["patch_id"] is None and record["proposal_file"] is None
    assert record["trigger"].startswith("time_tick: ") if autonomous else record["trigger"] == ".restart"
    if broken:
        supervisor.wait(timeout=10)
        assert record["error"]
    else:
        await wait_health(port, supervisor)
        assert worker_pid not in {p.pid for p in supervisor.children(recursive=True)}
    print(
        f"PASS: plain restart {'failed without reverting manual edits' if broken else 'loaded current files'}",
        flush=True,
    )
    if autonomous:
        print("PASS: background persona decision restarted Core without a consent call", flush=True)


async def check(launcher: Path | None, module: bool, plain_failure: bool) -> None:
    """每次检查使用独立源码、配置、测试与数据库，不访问玩家数据。"""
    with tempfile.TemporaryDirectory(prefix="mas-restart-check-") as temporary:
        root = Path(temporary)
        instance = root / "instances/smoke"
        repo = instance / "repo"
        repo.mkdir(parents=True)
        (instance / "runtime").mkdir()
        shutil.copytree(ROOT / "muika", repo / "muika", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (repo / "muika/builtin_plugins/restart_probe.py").write_text(
            """from unittest.mock import AsyncMock, patch
from arclet.alconna import Alconna
from muika.core.events import TimeTickEvent
from muika.core.loop import Muika
from muika.core.memory import RecallResult
from muika.plugin.command import on_alconna

probe = on_alconna(Alconna("restart_probe"))

@probe.handle()
async def restart_probe(muika: Muika):
    with patch.object(muika.brain, "generate_reply", AsyncMock(return_value="Restart integration fixture.<restart>")):
        await muika._run_brain_pipeline(TimeTickEvent(), RecallResult())
    return "Autonomous restart requested."
""",
            encoding="utf-8",
        )
        for name in ("core_main.py", "alembic.ini", "pyproject.toml"):
            shutil.copy2(ROOT / name, repo / name)
        (repo / "tests").mkdir()
        (repo / "tests/test_smoke.py").write_text(
            "def test_runtime_fixture():\n    assert 2 + 2 == 4\n", encoding="utf-8"
        )
        (repo / "configs").mkdir()
        (repo / "configs/models.yml").write_text("default:\n  provider: _echo\n  default: true\n", encoding="utf-8")
        env = {
            **os.environ,
            "MASTER_ID": "smoke",
            "IPC_SECRET": "smoke-secret",
            "ACTION_PERMISSION": "self_modify",
            "CODE_REVIEW_MODE": "manual",
            "HEART_INTENSITY": "off",
            "ENABLE_AUTO_REFLECTION": "false",
            "DATA_DIR": "data",
            "MUIKA_HOME": str(root),
            "PYTHONIOENCODING": "utf-8",
        }
        env.pop("MUIKA_LIFECYCLE_DIR", None)
        env.pop("MUIKA_SUPERVISOR_PID", None)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        log = (root / "core.log").open("w", encoding="utf-8")
        popen = None
        supervisor = None
        try:
            if launcher is None:
                entry = ["-m", "muika.ipc.bootstrap"] if module else ["core_main.py"]
                popen = subprocess.Popen(
                    [sys.executable, *entry, "--port", str(port)], cwd=repo, env=env, stdout=log, stderr=log
                )
                supervisor = psutil.Process(popen.pid)
            else:
                # 测试副本使用合成协议，不代玩家接受真实协议。
                agreement = {"title": "Integration fixture", "text": "Synthetic test data.", "updated": "2000-01-01"}
                (repo / "muika/user_agreement.json").write_text(json.dumps(agreement), encoding="utf-8")
                (repo / "data").mkdir()
                (repo / "data/user_agreement.json").write_text(
                    json.dumps(
                        {
                            "has_agreed": True,
                            "timestamp": "2000-01-01T00:00:00",
                            "version": "2000-01-01",
                        }
                    ),
                    encoding="utf-8",
                )
                scripts = repo / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
                scripts.mkdir(parents=True)
                shutil.copy2(sys.executable, scripts / ("python.exe" if sys.platform == "win32" else "python"))
                shutil.copy2(Path(sys.prefix) / "pyvenv.cfg", repo / ".venv/pyvenv.cfg")
                packages = (
                    repo
                    / ".venv"
                    / (
                        "Lib/site-packages"
                        if sys.platform == "win32"
                        else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
                    )
                )
                packages.mkdir(parents=True)
                original_packages = next(Path(p) for p in sys.path if p.endswith("site-packages"))
                (packages / "fixture.pth").write_text(
                    f"import site; site.addsitedir({str(original_packages)!r})\n", encoding="utf-8"
                )
                subprocess.run(
                    [
                        str(scripts / ("python.exe" if sys.platform == "win32" else "python")),
                        "-c",
                        "import muika.ipc.bootstrap; print('Fixture interpreter ready')",
                    ],
                    cwd=repo,
                    env=env,
                    check=True,
                    capture_output=True,
                )
                (root / "launcher.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "instances": {
                                "smoke": {
                                    "path": str(instance),
                                    "core_host": "127.0.0.1",
                                    "core_port": port,
                                    "bot": False,
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                subprocess.run([str(launcher), "start", "smoke", "--no-bot"], env=env, check=True, capture_output=True)
                status = subprocess.run(
                    [str(launcher), "status", "smoke", "--json"], env=env, check=True, capture_output=True, text=True
                )
                supervisor = psutil.Process(json.loads(status.stdout)["core_pid"])
            await wait_health(port, supervisor)
            pending_id = create_proposal(repo, env, False)
            pending_path = repo / "data/core_proposals" / pending_id / "proposal.json"
            pending = pending_path.read_bytes()
            manual_path = repo / "muika/core/manual_restart_marker.py"
            manual_path.write_text("PLAYER_EDIT = True\n", encoding="utf-8")
            await plain_restart(repo, port, supervisor)
            await plain_restart(repo, port, supervisor, autonomous=True)
            assert pending_path.read_bytes() == pending
            assert not (repo / "muika/core/restart_smoke_marker.py").exists()
            assert manual_path.read_text(encoding="utf-8") == "PLAYER_EDIT = True\n"
            print("PASS: stale pending proposal was not applied or changed", flush=True)
            for broken in (False, True):
                children = supervisor.children(recursive=True)
                assert children
                worker_pid = max(children, key=lambda p: len(p.parents())).pid
                patch_id = create_proposal(repo, env, broken)
                approved = await command(port, f".patch approve {patch_id}")
                proposal_path = repo / "data/core_proposals" / patch_id / "proposal.json"
                assert json.loads(proposal_path.read_text(encoding="utf-8"))["status"] == "ready", approved
                assert children[0].is_running()
                await wait_health(port, supervisor)
                await command(port, f".patch restart {patch_id}")
                record_path = repo / "data/restart.json"
                for _ in range(600):
                    if record_path.is_file():
                        record = json.loads(record_path.read_text(encoding="utf-8"))
                        if record.get("patch_id") == patch_id and record.get("status") in {
                            "started",
                            "restored",
                            "failed",
                        }:
                            break
                    await asyncio.sleep(0.1)
                else:
                    raise TimeoutError("No terminal restart outcome.")
                assert record["status"] == ("restored" if broken else "started"), record
                await wait_health(port, supervisor)
                assert max(supervisor.children(recursive=True), key=lambda p: len(p.parents())).pid != worker_pid
                assert json.loads(proposal_path.read_text(encoding="utf-8"))["status"] == (
                    "rolled_back" if broken else "approved"
                )
                print(
                    f"PASS: {'startup failure restored old code' if broken else 'ready -> apply -> restart'}",
                    flush=True,
                )
            if plain_failure:
                state_path = repo / "muika/core/state.py"
                manual_edit = state_path.read_text(encoding="utf-8").replace(
                    "from __future__ import annotations",
                    "from __future__ import annotations\nraise RuntimeError('manual startup failure')",
                )
                state_path.write_text(manual_edit, encoding="utf-8")
                await plain_restart(repo, port, supervisor, broken=True)
                assert state_path.read_text(encoding="utf-8") == manual_edit
                assert json.loads(proposal_path.read_text(encoding="utf-8"))["status"] == "rolled_back"
                return
            children = supervisor.children(recursive=True)
            if launcher is not None:
                subprocess.run([str(launcher), "stop", "smoke"], env=env, check=True, capture_output=True)
            else:
                supervisor.kill()
            _, alive = psutil.wait_procs(children, timeout=8)
            assert not [child for child in alive if child.status() != psutil.STATUS_ZOMBIE], alive
            print("PASS: stopping the stable parent cleaned worker processes", flush=True)
        except Exception as exc:
            if isinstance(exc, subprocess.CalledProcessError):
                print(f"Command failed: {exc.stdout!r} {exc.stderr!r}", file=sys.stderr)
            log.flush()
            for path in root.rglob("*.log"):
                if path.is_file():
                    print(
                        f"Log: {path}\n" + path.read_text(encoding="utf-8", errors="replace")[-12000:], file=sys.stderr
                    )
            raise
        finally:
            if supervisor is not None and supervisor.is_running():
                for child in supervisor.children(recursive=True):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                supervisor.kill()
            if popen is not None:
                popen.wait(timeout=10)
            # 启动器可能在写 PID 文件前失败，清理仅属于本次临时副本的进程。
            leftovers = []
            for process in psutil.process_iter(["cwd"]):
                if process.info["cwd"] == str(repo):
                    try:
                        process.kill()
                        leftovers.append(process)
                    except psutil.NoSuchProcess:
                        pass
            psutil.wait_procs(leftovers, timeout=8)
            log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--launcher", type=Path)
    parser.add_argument("--module", action="store_true")
    parser.add_argument("--plain-failure", action="store_true")
    args = parser.parse_args()
    asyncio.run(check(args.launcher.resolve() if args.launcher else None, args.module, args.plain_failure))
