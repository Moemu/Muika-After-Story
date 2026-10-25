"""用独立父进程监督 Core 重启，启动失败时恢复本次变更。

父进程永远运行默认树代码（锚 = 本模块位置）：读 boot 指针决定子进程跑哪棵树，
ready 校验 nonce 与运行树，就绪后把工作区改动提升（promotion）回默认树；
指针态失败回退默认树，提升后的默认树失败自动回滚一次（有界）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal, TypedDict, cast

import psutil

from muika.utils.logger import logger

RESTART_EXIT_CODE = 75
STARTUP_TIMEOUT = 180
SHUTDOWN_TIMEOUT = 180


class RestartRecordBase(TypedDict):
    id: str
    workspace_id: str | None
    reason: str
    trigger: str
    record_path: str
    status: Literal[
        "preparing",
        "restarting",
        "started",
        "restoring",
        "restored",
        "failed",
        "rolled_back",
        "auto_rolled_back",
    ]


class RestartRecord(RestartRecordBase, total=False):
    """保存重启意图、来源和真实启动结果。"""

    error: str
    confirm_memory_loss: bool


def write_json(path: Path, value: object) -> None:
    """原子保存生命周期状态。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


BOOT_TARGET_FILE = "boot_target.json"
ROLLBACK_READY_FILE = "rollback_ready.json"

PROMOTABLE_FILES = ("bot.py", "core_main.py")
PROMOTABLE_PREFIXES = ("muika/", "muika_bot/", "tests/", "configs/", "templates/")
REJECTED_PREFIXES = ("data/", ".git/", "plugins/")
UNSUPPORTED_FILES = ("pyproject.toml", "uv.lock", "alembic.ini")
"""可提升白名单与拒绝清单（P0-1）：编辑自由留在工作区树上，可提升性收在门上。"""

ESCALATE_FILES = (
    "configs/models.yml",
    "muika/core/code_review.py",
    "muika/core/self_mod/policy.py",
    "muika/core/self_mod/manager.py",
    "muika/core/self_mod/workspace.py",
    "muika/core/self_mod/plugin_deployer.py",
    "muika/core/self_mod/core_probe.py",
    "muika/core/actions/tools/_executor.py",
    "muika/core/actions/tools/_filesystem.py",
    "muika/core/actions/tools/_workspace.py",
    "muika/builtin_plugins/review.py",
    "muika/builtin_plugins/restart.py",
    "muika/builtin_plugins/patch.py",
    "muika/ipc/supervisor.py",
    "muika/ipc/bootstrap.py",
    "muika/core/restart.py",
    "muika/plugin/func_call/context.py",
    "muika/config.py",
    "muika/core/loop.py",
    "core_main.py",
    "tests/test_self_workspace.py",
    "tests/test_supervisor_boot.py",
)
ESCALATE_PREFIXES = ("muika/migrations/", "tests/")
"""升级清单：命中的工作区改动必须由玩家亲自批准（require_player 挂起）。"""

ChangeClass = Literal["promotable", "escalated", "rejected", "unsupported"]


def classify_change(rel_path: str) -> ChangeClass:
    """裁决工作区变更类别：白名单外的路径一律拒绝提升。"""
    normalized = PurePosixPath(rel_path.replace("\\", "/")).as_posix().strip("/")
    if normalized in UNSUPPORTED_FILES:
        return "unsupported"
    if normalized == ".env" or normalized.startswith(".env."):
        return "rejected"
    if any(normalized == prefix.rstrip("/") or normalized.startswith(prefix) for prefix in REJECTED_PREFIXES):
        return "rejected"
    promotable = normalized in PROMOTABLE_FILES or any(normalized.startswith(prefix) for prefix in PROMOTABLE_PREFIXES)
    if not promotable:
        return "rejected"
    if normalized in ESCALATE_FILES or any(normalized.startswith(prefix) for prefix in ESCALATE_PREFIXES):
        return "escalated"
    return "promotable"


def control_data_dir() -> Path:
    """返回 supervisor 与 Core 共用的受管 data 目录（与注入子进程的 DATA_DIR 同源）。"""
    return Path(os.environ.get("DATA_DIR", "data")).resolve()


def boot_target_path() -> Path:
    """返回 boot 指针文件路径。"""
    return control_data_dir() / BOOT_TARGET_FILE


def read_boot_target() -> Path | None:
    """返回 boot 指针文件路径；无指针返回 None。"""
    path = boot_target_path()
    return path if path.is_file() else None


def write_boot_target(workspace_id: str, manifest_hash: str, review_id: str, backup_path: str) -> None:
    """写入 boot 指针（提交点）：下一次重启将运行该工作区树。"""
    write_json(
        boot_target_path(),
        {
            "workspace_id": workspace_id,
            "manifest_hash": manifest_hash,
            "review_id": review_id,
            "backup_path": backup_path,
        },
    )


def clear_boot_target() -> None:
    """清除 boot 指针；下次重启回到默认树。"""
    boot_target_path().unlink(missing_ok=True)


def read_rollback_ready() -> dict | None:
    """读取最近一次提升的回滚记录；不存在返回 None。"""
    path = control_data_dir() / ROLLBACK_READY_FILE
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def write_rollback_ready(record: dict) -> None:
    """写入（覆盖）回滚记录。"""
    write_json(control_data_dir() / ROLLBACK_READY_FILE, record)


def clear_rollback_ready() -> None:
    """删除回滚记录（单级语义：随下次 promotion 顶替或回滚消费）。"""
    (control_data_dir() / ROLLBACK_READY_FILE).unlink(missing_ok=True)


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


def _default_root() -> Path:
    """返回 supervisor 自身运行的默认树根（权威基线锚点）。"""
    return Path(__file__).resolve().parents[2]


def load_workspace_manifest(ws_id: str) -> dict | None:
    """读取工作区清单；不可读返回 None（调用方保守处理）。"""
    path = control_data_dir() / "workspaces" / ws_id / "manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _build_child_env(default_root: Path) -> dict[str, str]:
    """从干净基准构造子进程 env：default 根 .env + 当前环境 + 绝对 DATA_DIR。

    现有环境变量优先于 .env（与 pydantic-settings 语义一致）；workspace 树内
    没有 .env 与 data，受管数据始终落在真实 data 目录。
    """
    env = dict(os.environ)
    env_file = default_root / ".env"
    if env_file.is_file():
        try:
            from dotenv import dotenv_values

            for key, value in dotenv_values(env_file).items():
                if value is not None and key not in os.environ:
                    env[key] = value
        except ImportError:
            pass
    env["DATA_DIR"] = str(control_data_dir())
    env["MUIKA_BOOT_NONCE"] = uuid.uuid4().hex
    return env


def _ready_matches(ready_file: Path, expected_root: Path | None, nonce: str) -> bool:
    """校验 ready 标记：nonce 必须匹配；指针 boot 还须声明正确的运行树。"""
    try:
        payload = json.loads(ready_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict) or payload.get("nonce") != nonce:
        return False
    if expected_root is not None:
        try:
            declared = Path(str(payload.get("running_root", ""))).resolve()
        except OSError:
            return False
        if declared != expected_root.resolve():
            return False
    return True


def _sha256_file(path: Path) -> str | None:
    """返回文件 SHA-256；缺失返回 None。"""
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """临时文件 + 原子替换写入。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def verify_review(review_id: str, ws_id: str, changes: list[dict]) -> None:
    """父进程权威复核：该 diff 确有一次 approved 的 workspace 审查。

    :raises ValueError: 审查记录缺失、未批准或与清单不符
    """
    path = control_data_dir() / "reviews" / f"{review_id}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Workspace review record is missing or unreadable: {review_id}") from exc
    if not isinstance(value, dict) or value.get("status") != "approved":
        raise ValueError(f"Workspace review {review_id} is not approved (status={value.get('status')}).")
    payload = value.get("payload", {})
    if not isinstance(payload, dict) or payload.get("workspace_id") != ws_id:
        raise ValueError("The approved review does not match this workspace.")
    reviewed = {(str(c.get("path")), str(c.get("action"))) for c in payload.get("changes", [])}
    expected = {(str(c.get("path")), str(c.get("action"))) for c in changes}
    if reviewed != expected:
        raise ValueError("The approved review no longer matches the submitted diff.")


def promote_workspace(ws_id: str, default_root: Path) -> dict:
    """就绪后提升：复核 review → rollback_ready(pending) → 逐文件三态写 → 清指针 → 标 ready。

    任一步失败即用 before/ 快照回填已写文件（统一失败路径），再抛出由调用方回退。
    :raises ValueError: 复核失败、快照哈希不符或与外部修改冲突
    """
    manifest = load_workspace_manifest(ws_id)
    if manifest is None:
        raise ValueError(f"Workspace manifest is missing: {ws_id}")
    changes = list(manifest.get("touched", []))
    verify_review(str(manifest.get("review_id", "")), ws_id, changes)

    workspace_root = control_data_dir() / "workspaces" / ws_id
    tree = workspace_root / "tree"
    record: dict = {
        "workspace_id": ws_id,
        "reason": str(manifest.get("reason", "")),
        "touched": changes,
        "touched_migrations": bool(manifest.get("touched_migrations")),
        "backup_path": str(manifest.get("backup_path", "")),
        "review_id": str(manifest.get("review_id", "")),
        "before_dir": str(workspace_root / "before"),
        "status": "pending",
    }
    write_rollback_ready(record)
    try:
        for change in sorted(changes, key=lambda item: str(item.get("path", ""))):
            rel = PurePosixPath(str(change.get("path", ""))).as_posix()
            if classify_change(rel) in {"rejected", "unsupported"}:
                raise ValueError(f"Path is not promotable: {rel}")
            after_hash = change.get("sha_after")
            after = _sha256_file(tree / rel)
            if after != after_hash:
                raise ValueError(f"Workspace snapshot drifted before promotion: {rel}")
            if change.get("sha_before") is not None:
                before_snapshot = workspace_root / "before" / rel
                if _sha256_file(before_snapshot) != change.get("sha_before"):
                    raise ValueError(f"Before snapshot hash mismatch: {rel}")
            target = default_root / rel
            current = _sha256_file(target)
            if after_hash is None:
                if current == change.get("sha_before"):
                    target.unlink(missing_ok=True)
                elif current is not None:
                    raise ValueError(f"Promotion conflict on delete target: {rel}")
            elif current == change.get("sha_before"):
                _atomic_write_bytes(target, (tree / rel).read_bytes())
            elif current == after_hash:
                continue  # 重入幂等：已写过的文件跳过
            else:
                raise ValueError(f"Promotion conflict, default tree drifted: {rel}")
    except Exception:
        pending = read_rollback_ready() or record
        restore_workspace(pending, default_root)
        raise

    clear_boot_target()
    record["status"] = "ready"
    record["promoted_at"] = datetime.now().isoformat()
    write_rollback_ready(record)
    logger.info(f"[Supervisor] Promoted 1 workspace {ws_id} with {len(changes)} change(s) into the default tree.")
    return record


def restore_workspace(record: dict, default_root: Path, *, archive_after_dir: Path | None = None) -> list[str]:
    """把 default 树还原到 before 状态；可选归档将被丢弃的 after 内容。

    :raises ValueError: 回滚资料缺失或哈希不符（还原中止，已还原文件保持 before 态）
    """
    restored: list[str] = []
    before_dir = Path(str(record.get("before_dir", "")))
    for change in reversed(list(record.get("touched", []))):
        rel = PurePosixPath(str(change.get("path", ""))).as_posix()
        target = default_root / rel
        if archive_after_dir is not None and target.is_file():
            archived = archive_after_dir / rel
            archived.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, archived)
        if change.get("sha_before") is None:
            target.unlink(missing_ok=True)
            restored.append(rel)
            continue
        before_snapshot = before_dir / rel
        content = before_snapshot.read_bytes() if before_snapshot.is_file() else None
        if content is None or hashlib.sha256(content).hexdigest() != change.get("sha_before"):
            raise ValueError(f"Rollback material missing or drifted: {rel}")
        _atomic_write_bytes(target, content)
        restored.append(rel)
    if record.get("touched_migrations") and record.get("backup_path"):
        _restore_database(Path(str(record["backup_path"])))
    return restored


def _restore_database(backup: Path) -> None:
    """把备份复制回 muika.db 并清掉 WAL 残留（子进程已停止时调用）。"""
    db = control_data_dir() / "muika.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(backup, db)
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)


def _archive_database(label: str) -> str:
    """在线归档当前数据库，返回归档路径说明；无库返回空串。"""
    db = control_data_dir() / "muika.db"
    if not db.is_file():
        return ""
    archive = control_data_dir() / "backups" / f"{label}-{datetime.now():%Y%m%d-%H%M%S-%f}.db"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as source, sqlite3.connect(archive) as destination:
        source.backup(destination)
    return str(archive)


def _execute_rollback(restart: RestartRecord, default_root: Path) -> None:
    """父进程执行玩家回滚：清指针 → 归档 DB → flag 复核 → before/ 还原 → 条件恢复。"""
    clear_boot_target()
    ready = read_rollback_ready()
    if ready is None:
        restart["status"] = "failed"
        restart["error"] = "No rollback record survived; nothing to roll back."
        write_json(Path(str(restart["record_path"])), restart)
        return
    if ready.get("touched_migrations") and not restart.get("confirm_memory_loss"):
        restart["status"] = "failed"
        restart["error"] = (
            "This rollback discards memories made after the update; " "repeat with --confirm-memory-loss to proceed."
        )
        write_json(Path(str(restart["record_path"])), restart)
        return
    archive_note = _archive_database("rollback-discard")
    try:
        restored = restore_workspace(ready, default_root)
    except Exception as exc:  # noqa: BLE001 - 记录后由默认树重启兜底
        restart["status"] = "failed"
        restart["error"] = f"Rollback failed: {exc}"
        write_json(Path(str(restart["record_path"])), restart)
        logger.critical(f"[Supervisor] {restart['error']}")
        return
    clear_rollback_ready()
    restart["status"] = "rolled_back"
    restart["error"] = f"Rolled back {len(restored)} file(s)." + (
        f" Database archived at {archive_note}." if archive_note else ""
    )
    write_json(Path(str(restart["record_path"])), restart)
    logger.info(f"[Supervisor] Rolled back workspace {ready.get('workspace_id')}: {len(restored)} file(s).")


def _try_auto_rollback(default_root: Path) -> bool:
    """非指针态 default 未 ready 且仍是刚提升状态时自动回滚一次（有界）。

    :return: 是否执行了自动回滚（调用方据此重生一次；consumed 后不再触发）
    """
    ready = read_rollback_ready()
    if not ready or ready.get("status") not in {"pending", "ready"}:
        return False
    if any(
        _sha256_file(default_root / PurePosixPath(str(c.get("path", ""))).as_posix()) != c.get("sha_after")
        for c in ready.get("touched", [])
    ):
        logger.critical(
            "[Supervisor] Default tree drifted from the promoted version; automatic rollback refused. "
            "Manual recovery required."
        )
        return False
    archive_dir = control_data_dir() / "backups" / f"auto-rollback-{datetime.now():%Y%m%d-%H%M%S-%f}"
    archive_note = _archive_database("auto-rollback-discard")
    try:
        restore_workspace(ready, default_root, archive_after_dir=archive_dir)
    except Exception as exc:  # noqa: BLE001 - 拒绝自动回滚，交给人工
        logger.critical(f"[Supervisor] Automatic rollback failed: {exc}; manual recovery required.")
        return False
    ready["status"] = "consumed"
    ready["consumed_at"] = datetime.now().isoformat()
    ready["note"] = "Auto-rolled back after the promoted default failed to start; the discarded version was archived."
    write_rollback_ready(ready)
    record: RestartRecord = {
        "id": uuid.uuid4().hex,
        "workspace_id": str(ready.get("workspace_id", "")),
        "reason": "promoted version failed to start",
        "trigger": "auto_rollback",
        "record_path": str(control_data_dir() / "restart.json"),
        "status": "auto_rolled_back",
    }
    write_json(Path(record["record_path"]), record)
    logger.critical(
        "[Supervisor] Promoted default failed to start; auto-rolled back 1 version. "
        f"Discarded code archived at {archive_dir}; database archived at {archive_note or '(none)'}."
    )
    return True


def supervise(argv: list[str]) -> int:
    """保持父 PID 不变，仅在明确的重启请求后启动新 Core。"""
    stop_signals: list[int] = []

    def request_stop(signum: int, frame: object) -> None:
        stop_signals.append(signum)

    previous_handlers = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        with tempfile.TemporaryDirectory(prefix="muika-lifecycle-") as temporary:
            directory = Path(temporary)
            creationflags = 0
            if sys.platform == "win32" and not any(
                stream is not None and stream.isatty() for stream in (sys.stdout, sys.stderr)
            ):
                creationflags = subprocess.CREATE_NO_WINDOW
            restart: RestartRecord | None = None
            restored = False
            while True:
                if stop_signals:
                    return 0
                ready = directory / "ready.json"
                ready.unlink(missing_ok=True)

                # 逐轮从干净基准重建 env：上一轮指针态的 PYTHONPATH 不带入默认启动
                env = _build_child_env(_default_root())
                env["MUIKA_LIFECYCLE_DIR"] = str(directory)
                env["MUIKA_SUPERVISOR_PID"] = str(os.getpid())
                nonce = env["MUIKA_BOOT_NONCE"]
                boot: dict | None = None
                child_cwd: str | None = None
                expected_root: Path | None = None
                pointer = read_boot_target()
                if pointer is not None:
                    try:
                        candidate = json.loads(pointer.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        candidate = None
                    tree = (
                        control_data_dir() / "workspaces" / str(candidate.get("workspace_id", "")) / "tree"
                        if isinstance(candidate, dict)
                        else None
                    )
                    if tree is None or not tree.is_dir():
                        logger.critical("[Supervisor] Invalid boot pointer; falling back to the default tree.")
                        clear_boot_target()
                    else:
                        boot = candidate
                        child_cwd = str(tree)
                        expected_root = tree
                        env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(tree), os.environ.get("PYTHONPATH"))))
                if boot is not None and restart is None:
                    # 冷启动遇指针：构造隐式记录，失败回退与结果标记有账可查
                    restart = {
                        "id": uuid.uuid4().hex,
                        "workspace_id": str(boot.get("workspace_id", "")),
                        "reason": "boot pointer active",
                        "trigger": "boot",
                        "record_path": str(control_data_dir() / "restart.json"),
                        "status": "preparing",
                    }
                    write_json(Path(restart["record_path"]), restart)

                child = subprocess.Popen(
                    [sys.executable, "-m", "muika.ipc.bootstrap", *argv],
                    env=env,
                    cwd=child_cwd,
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                    creationflags=creationflags,
                )
                started = time.monotonic()
                healthy = False
                pointer_boot = boot is not None
                try:
                    while child.poll() is None and not stop_signals:
                        if not healthy and ready.is_file() and _ready_matches(ready, expected_root, nonce):
                            healthy = True
                            if boot is not None:
                                try:
                                    promote_workspace(str(boot["workspace_id"]), _default_root())
                                    boot = None
                                except Exception as exc:  # noqa: BLE001 - 统一失败路径回退
                                    healthy = False
                                    logger.critical(
                                        f"[Supervisor] Promotion failed: {exc}; restoring the default tree."
                                    )
                                    stop_tree(child.pid)
                                    break
                            if restart is not None:
                                restart["status"] = "restored" if restored else "started"
                                write_json(Path(restart["record_path"]), restart)
                                restart = None
                                restored = False
                        if not healthy and time.monotonic() - started > STARTUP_TIMEOUT:
                            stop_tree(child.pid)
                            break
                        time.sleep(0.1)
                    if stop_signals:
                        logger.info(
                            f"[Supervisor] Requested 1 Core shutdown; waiting up to {SHUTDOWN_TIMEOUT} seconds."
                        )
                        write_json(directory / "stop.json", {})
                        deadline = time.monotonic() + SHUTDOWN_TIMEOUT
                        while child.poll() is None:
                            if len(stop_signals) > 1:
                                logger.warning("[Supervisor] Received a second stop signal; forcing 1 Core shutdown.")
                                break
                            if time.monotonic() >= deadline:
                                logger.warning(
                                    f"[Supervisor] Core exceeded {SHUTDOWN_TIMEOUT} seconds; forcing shutdown."
                                )
                                break
                            time.sleep(0.1)
                        if child.poll() is None:
                            stop_tree(child.pid)
                        child.wait()
                        return 0
                    code = child.wait()
                finally:
                    if child.poll() is None:
                        stop_tree(child.pid)
                        child.wait()
                if stop_signals:
                    return 0
                request_file = directory / "restart.json"
                if code == RESTART_EXIT_CODE and request_file.is_file():
                    restart = cast(RestartRecord, json.loads(request_file.read_text(encoding="utf-8")))
                    request_file.unlink()
                    restored = False
                    if restart.get("trigger") == "rollback":
                        _execute_rollback(restart, _default_root())
                        restart = None
                    continue
                if not healthy:
                    if pointer_boot:
                        # 指针态失败：清指针 + 条件恢复 DB + 默认树重生一次
                        logger.critical(
                            "[Supervisor] Workspace version failed to become ready; restoring the default tree."
                        )
                        manifest = load_workspace_manifest(str((boot or {}).get("workspace_id", "")))
                        clear_boot_target()
                        if manifest and manifest.get("touched_migrations") and manifest.get("backup_path"):
                            try:
                                _restore_database(Path(str(manifest["backup_path"])))
                            except OSError as exc:
                                logger.critical(f"[Supervisor] Database restore after failed boot: {exc}")
                        if restart is not None:
                            restart["status"] = "restoring"
                            restart["error"] = (
                                "The workspace version did not become ready; the default tree was restored."
                            )
                            write_json(Path(restart["record_path"]), restart)
                            restored = True
                        continue
                    if restart is not None:
                        restart["error"] = f"Core did not become ready; exit code {code}. See the Core startup log."
                        restart["status"] = "failed"
                        write_json(Path(restart["record_path"]), restart)
                        restart = None
                    if _try_auto_rollback(_default_root()):
                        continue
                return code if code != 0 or healthy else 1
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def main(argv: list[str] | None = None) -> None:
    """运行受监督的标准启动入口。"""
    if sys.platform == "win32":
        parent = psutil.Process(os.getppid())
        if Path(parent.exe()).resolve() == Path(sys.executable).resolve():
            # Windows venv 启动器持有 Launcher 记录的 PID，实际解释器必须随它退出。
            watch_process(parent.pid)

    raise SystemExit(supervise(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    main()
