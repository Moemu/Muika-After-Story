"""用独立父进程监督 Core 重启，启动失败时恢复本次变更。"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast

import psutil

if TYPE_CHECKING:
    from muika.core.self_mod.proposals import CoreProposal

RESTART_EXIT_CODE = 75
STARTUP_TIMEOUT = 180


class RestartRecordBase(TypedDict):
    id: str
    patch_id: str | None
    reason: str
    trigger: str
    proposal_file: str | None
    record_path: str
    status: Literal["preparing", "restarting", "started", "restoring", "restored", "failed"]


class RestartRecord(RestartRecordBase, total=False):
    """保存重启意图、来源和真实启动结果。"""

    error: str


def write_json(path: Path, value: object) -> None:
    """原子保存生命周期状态。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def lifecycle_directory() -> Path | None:
    """返回仅由父进程提供的控制目录。"""
    value = os.environ.get("MUIKA_LIFECYCLE_DIR")
    return Path(value) if value else None


def stop_tree(pid: int) -> None:
    """终止进程及其后代，容忍进程已退出。"""
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in reversed(children):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        parent.kill()
        psutil.wait_procs([*children, parent], timeout=5)
    except psutil.NoSuchProcess:
        pass


def watch_parent() -> None:
    """父进程被 Launcher 强制终止时清理当前工作进程树。"""
    raw = os.environ.get("MUIKA_SUPERVISOR_PID")
    if not raw:
        return
    watch_process(int(raw))


def watch_process(pid: int) -> None:
    """监视拥有当前进程生命周期的父进程。"""
    parent = psutil.Process(pid)
    started = parent.create_time()

    def watch() -> None:
        while True:
            time.sleep(1)
            try:
                if parent.is_running() and parent.create_time() == started and parent.status() != psutil.STATUS_ZOMBIE:
                    continue
            except psutil.NoSuchProcess:
                pass
            stop_tree(os.getpid())
            return

    threading.Thread(target=watch, name="core-parent-watch", daemon=True).start()


def restore_proposal(proposal_file: Path) -> None:
    """在新 Core 无法导入时依据原有快照恢复文件，不导入候选代码。"""
    proposal = cast("CoreProposal", json.loads(proposal_file.read_text(encoding="utf-8")))
    if proposal["status"] != "approved":
        raise ValueError("Only an applied proposal can be restored after startup failure.")
    root = Path(proposal["source_root"]).resolve()
    snapshot_root = proposal_file.parent.resolve()
    changes: list[tuple[Path, bytes | None]] = []
    for change in proposal["changes"]:
        target = (root / change["path"]).resolve()
        if not target.is_relative_to(root) or target.suffix != ".py":
            raise ValueError("Invalid rollback target.")
        actual = hashlib.sha256(target.read_text(encoding="utf-8").encode()).hexdigest() if target.is_file() else None
        if actual != change["sha256_after"]:
            raise ValueError(f"Rollback target changed: {target}")
        content = None
        if change["before_snapshot"] is not None:
            snapshot = (snapshot_root / change["before_snapshot"]).resolve()
            if not snapshot.is_relative_to(snapshot_root):
                raise ValueError("Invalid rollback snapshot.")
            content = snapshot.read_bytes()
            if hashlib.sha256(snapshot.read_text(encoding="utf-8").encode()).hexdigest() != change["sha256_before"]:
                raise ValueError("Rollback snapshot hash mismatch.")
        changes.append((target, content))
    proposal["status"] = "rolling_back"
    write_json(proposal_file, proposal)
    for target, content in reversed(changes):
        if content is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".restore.tmp")
            temporary.write_bytes(content)
            temporary.replace(target)
    proposal["status"] = "rolled_back"
    proposal["recovery_note"] = "New Core failed to start; supervisor restored the previous version."
    write_json(proposal_file, proposal)


def supervise(argv: list[str]) -> int:
    """保持父 PID 不变，仅在明确的重启请求后启动新 Core。"""
    with tempfile.TemporaryDirectory(prefix="muika-lifecycle-") as temporary:
        directory = Path(temporary)
        env = dict(os.environ, MUIKA_LIFECYCLE_DIR=str(directory), MUIKA_SUPERVISOR_PID=str(os.getpid()))
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW
        restart: RestartRecord | None = None
        restored = False
        while True:
            ready = directory / "ready.json"
            ready.unlink(missing_ok=True)
            child = subprocess.Popen(
                [sys.executable, "-m", "muika.ipc.bootstrap", *argv],
                env=env,
                creationflags=creationflags,
            )
            started = time.monotonic()
            healthy = False
            try:
                while child.poll() is None:
                    if not healthy and ready.is_file():
                        healthy = True
                        if restart is not None:
                            restart["status"] = "restored" if restored else "started"
                            write_json(Path(restart["record_path"]), restart)
                            restart = None
                            restored = False
                    if not healthy and time.monotonic() - started > STARTUP_TIMEOUT:
                        stop_tree(child.pid)
                        break
                    time.sleep(0.1)
                code = child.wait()
            except (KeyboardInterrupt, SystemExit):
                stop_tree(child.pid)
                child.wait()
                return 0
            finally:
                if child.poll() is None:
                    stop_tree(child.pid)
                    child.wait()
            request_file = directory / "restart.json"
            if code == RESTART_EXIT_CODE and request_file.is_file():
                restart = cast(RestartRecord, json.loads(request_file.read_text(encoding="utf-8")))
                request_file.unlink()
                restored = False
                continue
            if restart is not None and not healthy:
                restart["error"] = f"Core did not become ready; exit code {code}. See the Core startup log."
                if not restored and restart["proposal_file"] is not None:
                    try:
                        restore_proposal(Path(restart["proposal_file"]))
                        restored = True
                        restart["status"] = "restoring"
                        write_json(Path(restart["record_path"]), restart)
                        continue
                    except (OSError, ValueError, KeyError) as exc:
                        restart["error"] = str(exc)
                restart["status"] = "failed"
                write_json(Path(restart["record_path"]), restart)
            return code if code != 0 or healthy else 1


def main(argv: list[str] | None = None) -> None:
    """运行受监督的标准启动入口。"""
    if sys.platform == "win32":
        parent = psutil.Process(os.getppid())
        if Path(parent.exe()).resolve() == Path(sys.executable).resolve():
            # Windows venv 启动器持有 Launcher 记录的 PID，实际解释器必须随它退出。
            watch_process(parent.pid)

    def terminate(signum: int, frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)
    raise SystemExit(supervise(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    main()
