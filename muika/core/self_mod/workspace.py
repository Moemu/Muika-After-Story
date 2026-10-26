"""自修改版本工作区：fork、门禁、快照、备份与指针提交。

工作区是完整的一个她（内核 + persona + skills + topics + configs）的运行树副本。
编辑期零审批（通用文件工具 + 会话写根）；``submit`` 是唯一的权威化路径：
冲突检测 → 白名单分类 → Review（escalate 挂起等玩家）→ 探针 → before/ 快照 →
DB 备份 → manifest 全量落盘 → 指针最后写。指针是提交点；promotion 与回滚在
父进程（supervisor）执行。
"""

from __future__ import annotations

import difflib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional, TypedDict, cast

from muika.config import mas_config
from muika.database.db import backup_database
from muika.utils.logger import logger

_WS_ID_RE = re.compile(r"^[0-9]{8}_[0-9]{6}_[0-9a-f]{8}$")
_DIFF_PAGE_LINES = 120
_DIFF_CAP_CHARS = 48_000
_PROBE_MARKER = "[CORE_PROBE_RESULT]"
PROBE_TIMEOUT_SECONDS = 180
PROBE_OUTPUT_CAP_BYTES = 65_536

WORKSPACE_IGNORE_PATTERNS = (
    ".git",
    ".venv",
    "venv",
    "data",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "*.pyc",
    ".venv-review",
    "logs",
    "dist",
    ".env*",
)
"""fork 排除清单：受管数据、依赖目录与凭据绝不进工作区树。"""

WorkspaceStatus = Literal["open", "submitted", "promoted", "failed", "discarded", "rolled_back"]
ChangeAction = Literal["modify", "create", "delete"]


class SelfWorkspaceError(Exception):
    """工作区操作被门禁或部署环境拒绝，消息可直接展示给 LLM。"""


class WorkspaceChange(TypedDict):
    """一次已分类的触碰文件。"""

    path: str
    action: ChangeAction
    sha_before: Optional[str]
    sha_after: Optional[str]
    escalate: bool


class WorkspaceManifestBase(TypedDict):
    schema_version: int
    ws_id: str
    reason: str
    status: WorkspaceStatus
    created_at: str
    fork_hashes: dict[str, str]


class WorkspaceManifest(WorkspaceManifestBase, total=False):
    """工作区清单：open 时落基础字段，submit 时补全提交字段。"""

    touched: list[WorkspaceChange]
    touched_migrations: bool
    backup_path: str
    review_id: str
    submitted_at: str
    error: str


def _sha256_text(text: str) -> str:
    """返回 UTF-8 文本的 SHA-256。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> Optional[str]:
    """返回文件内容的 SHA-256；文件缺失返回 None。"""
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime_tree_root() -> Path:
    """返回当前进程运行的源码树根（muika 包的父目录）。"""
    import muika

    return Path(muika.__file__).resolve().parents[1]


def _copy_workspace(source_root: Path, destination: Path) -> None:
    """复制源码树到工作区目录。"""
    shutil.copytree(
        source_root,
        destination,
        ignore=shutil.ignore_patterns(*WORKSPACE_IGNORE_PATTERNS),
        dirs_exist_ok=True,
    )


def _tree_hashes(root: Path) -> dict[str, str]:
    """返回树内全部文件的 相对路径 → SHA-256 映射。"""
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            hashes[path.relative_to(root).as_posix()] = _sha256_file(path) or ""
    return hashes


def workspace_fingerprint(root: Path) -> str:
    """返回源码树的内容指纹（探针基线缓存键）。"""
    digest = hashlib.sha256()
    for rel, sha in sorted(_tree_hashes(root).items()):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _run_probe(tree: Path) -> dict:
    """在子进程中以 ``tree`` 为 cwd 运行测试探针，返回结构化报告。"""
    probe_path = Path(__file__).with_name("core_probe.py")
    try:
        completed = subprocess.run(
            [sys.executable, str(probe_path)],
            cwd=tree,
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        capped = ((exc.stdout or b"") + (exc.stderr or b""))[-PROBE_OUTPUT_CAP_BYTES:]
        return {
            "status": "failed",
            "reason": f"Validation timed out after {PROBE_TIMEOUT_SECONDS} seconds.",
            "timed_out": True,
            "output": capped.decode("utf-8", errors="replace"),
            "failures": [],
            "errors": [],
            "test_count": 0,
        }
    output_bytes = (completed.stdout or b"") + (completed.stderr or b"")
    output = output_bytes[-PROBE_OUTPUT_CAP_BYTES:].decode("utf-8", errors="replace")
    marker_line = next((line for line in reversed(output.splitlines()) if line.startswith(_PROBE_MARKER)), None)
    if marker_line is None:
        return {
            "status": "unavailable",
            "reason": f"Validation probe did not return structured output (exit {completed.returncode}).",
            "timed_out": False,
            "output": output,
            "failures": [],
            "errors": [],
            "test_count": 0,
        }
    try:
        result = json.loads(marker_line[len(_PROBE_MARKER) :])
    except json.JSONDecodeError as exc:
        return {
            "status": "unavailable",
            "reason": f"Validation probe returned invalid JSON: {exc}",
            "timed_out": False,
            "output": output,
            "failures": [],
            "errors": [],
            "test_count": 0,
        }
    result["output"] = output
    return result


def _deployment_probe_environment() -> Optional[str]:
    """返回探针环境不可用的原因；可用返回 None。"""
    if importlib.util.find_spec("pytest") is None:
        return "pytest is not installed in the current Python environment."
    return None


def _baseline_report(fingerprint: str) -> dict:
    """读取或测量运行树的基线探针报告（按指纹缓存）。"""
    cache = mas_config.workspaces_dir / "_baseline" / f"{fingerprint}.json"
    if cache.is_file():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cached = None
        if isinstance(cached, dict) and cached.get("fingerprint") == fingerprint:
            report = cached.get("report")
            if isinstance(report, dict):
                return report
    environment_error = _deployment_probe_environment()
    if environment_error:
        report = {
            "status": "unavailable",
            "reason": environment_error,
            "timed_out": False,
            "failures": [],
            "errors": [],
            "test_count": 0,
        }
    else:
        report = _run_probe(_runtime_tree_root())
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"fingerprint": fingerprint, "report": report}, ensure_ascii=False), encoding="utf-8")
    return report


def _manifest_hash(manifest: WorkspaceManifest) -> str:
    """返回 manifest 规范化内容的哈希（指针绑定用）。"""
    return _sha256_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True))


class SelfWorkspaceManager:
    """管理版本工作区的 open/submit/discard 与清单读写。"""

    def __init__(self, runtime_root: Optional[Path] = None) -> None:
        self._runtime_root = (runtime_root or _runtime_tree_root()).resolve()

    # ---------- 路径与清单 ----------

    def workspace_root(self, ws_id: str) -> Path:
        """返回工作区记录目录（tree/ 与 before/ 的父目录）。"""
        return mas_config.workspaces_dir / ws_id

    def tree_root(self, ws_id: str) -> Path:
        """返回工作区源码树根。"""
        return self.workspace_root(ws_id) / "tree"

    def _manifest_path(self, ws_id: str) -> Path:
        return self.workspace_root(ws_id) / "manifest.json"

    def load_manifest(self, ws_id: str) -> WorkspaceManifest:
        """读取并校验工作区清单。"""
        if not _WS_ID_RE.fullmatch(ws_id):
            raise SelfWorkspaceError(f"Invalid workspace id: {ws_id!r}.")
        path = self._manifest_path(ws_id)
        if not path.is_file():
            raise SelfWorkspaceError(f"Workspace not found: {ws_id}.")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SelfWorkspaceError(f"Workspace manifest is invalid: {ws_id}: {exc}") from exc
        if not isinstance(value, dict) or value.get("ws_id") != ws_id:
            raise SelfWorkspaceError(f"Workspace manifest is invalid: {ws_id}.")
        return value  # type: ignore[return-value]

    def _save_manifest(self, manifest: WorkspaceManifest) -> None:
        """原子写工作区清单。"""
        path = self._manifest_path(str(manifest["ws_id"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def list_workspaces(self) -> list[WorkspaceManifest]:
        """按创建时间倒序列出全部工作区清单。"""
        root = mas_config.workspaces_dir
        if not root.is_dir():
            return []
        manifests = []
        for path in root.iterdir():
            if not _WS_ID_RE.fullmatch(path.name):
                continue
            try:
                manifests.append(self.load_manifest(path.name))
            except SelfWorkspaceError:
                continue
        return sorted(manifests, key=lambda item: str(item.get("created_at", "")), reverse=True)

    # ---------- 会话写根 ----------

    def active_write_roots(self) -> list[Path]:
        """返回全部 open 状态工作区的树根（通用文件工具的会话写根）。"""
        roots = []
        for manifest in self.list_workspaces():
            if manifest["status"] != "open":
                continue
            tree = self.tree_root(str(manifest["ws_id"]))
            if tree.is_dir():
                roots.append(tree)
        return roots

    def session_anchor(self) -> Optional[Path]:
        """返回最新 open 工作区树根，作为相对路径的锚。"""
        roots = self.active_write_roots()
        return roots[0] if roots else None

    # ---------- 部署能力 ----------

    def _deployment_capability(self) -> Optional[str]:
        """返回工作区能力缺失原因；可用返回 None。三类入口共用此诊断。"""
        root = self._runtime_root
        if not (root / "muika").is_dir():
            return (
                "This deployment runs from an installed package without a source tree, "
                "so workspace self-modification is unavailable here. "
                "Switch to a source-checkout deployment to enable it."
            )
        if not (root / "pyproject.toml").is_file() or not (root / "tests").is_dir():
            return (
                "The running source tree has no test workspace (pyproject.toml or tests/ missing), "
                "so workspace self-modification is unavailable here. "
                "Run from a full source checkout to enable it."
            )
        if not os.access(root, os.W_OK):
            return "The running source tree is not writable; workspace self-modification needs a writable fork."
        return None

    # ---------- 生命周期 ----------

    def open(self, reason: str) -> str:
        """fork 当前运行树为新的可编辑工作区，返回工作区 id。"""
        capability = self._deployment_capability()
        if capability is not None:
            raise SelfWorkspaceError(capability)
        reason = reason.strip()
        if not reason:
            raise SelfWorkspaceError("A non-empty 'reason' is required: every version of yourself deserves a why.")

        ws_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        tree = self.tree_root(ws_id)
        if tree.exists():
            raise SelfWorkspaceError(f"Workspace id collision: {ws_id}.")
        _copy_workspace(self._runtime_root, tree)

        manifest: WorkspaceManifest = {
            "schema_version": 1,
            "ws_id": ws_id,
            "reason": reason,
            "status": "open",
            "created_at": datetime.now().isoformat(),
            "fork_hashes": _tree_hashes(tree),
        }
        self._save_manifest(manifest)
        logger.info(f"[SelfWorkspace] Opened 1 workspace {ws_id} forking {len(manifest['fork_hashes'])} files.")
        return ws_id

    async def submit(self, ws_id: str, reason: str) -> str:
        """终审门：冲突检测 → 分类 → Review → 探针 → 快照 → 备份 → manifest → 指针。"""
        from muika.ipc import supervisor

        capability = self._deployment_capability()
        if capability is not None:
            raise SelfWorkspaceError(capability)
        reason = reason.strip()
        if not reason:
            raise SelfWorkspaceError("A non-empty 'reason' is required.")

        manifest = self.load_manifest(ws_id)
        if manifest["status"] != "open":
            raise SelfWorkspaceError(f"Workspace {ws_id} is {manifest['status']}; only open workspaces can submit.")

        if supervisor.read_boot_target() is not None:
            raise SelfWorkspaceError(
                "Another workspace is already waiting to boot. Roll it back or discard it before submitting a new one."
            )
        for other in self.list_workspaces():
            if other["ws_id"] != ws_id and other["status"] == "submitted":
                raise SelfWorkspaceError(
                    f"Workspace {other['ws_id']} is submitted and waiting for its boot; finish it first."
                )

        tree = self.tree_root(ws_id)
        tree_hashes = _tree_hashes(tree)
        runtime_hashes = _tree_hashes(self._runtime_root)
        fork_hashes = manifest["fork_hashes"]

        touched: dict[str, ChangeAction] = {}
        for rel, sha in tree_hashes.items():
            if fork_hashes.get(rel) != sha:
                touched[rel] = "modify" if rel in fork_hashes else "create"
        for rel in fork_hashes:
            if rel not in tree_hashes:
                touched[rel] = "delete"
        if not touched:
            raise SelfWorkspaceError("No changes to submit; edit files inside the workspace tree first.")

        conflicts = sorted(rel for rel in touched if rel in fork_hashes and runtime_hashes.get(rel) != fork_hashes[rel])
        if conflicts:
            raise SelfWorkspaceError(
                f"External changes conflict with your edits: {', '.join(conflicts)}. "
                "Discard this workspace, take the external update in, and open a fresh one."
            )

        for rel, sha in runtime_hashes.items():
            if rel not in touched and fork_hashes.get(rel) != sha:
                self._copy_into_tree(self._runtime_root / rel, tree / rel)
        for rel in fork_hashes:
            if rel not in runtime_hashes and rel not in touched and (tree / rel).exists():
                (tree / rel).unlink()

        changes: list[WorkspaceChange] = []
        for rel in sorted(touched):
            change_class = supervisor.classify_change(rel)
            if change_class == "rejected":
                raise SelfWorkspaceError(
                    f"Rejected path in this change: {rel}. "
                    "data, .git, .env and plugins never enter the default tree; "
                    "plugins go through their own staging pipeline."
                )
            if change_class == "unsupported":
                raise SelfWorkspaceError(
                    f"Dependency changes are not supported inside workspaces: {rel}. "
                    "Change dependencies through a deployment update instead."
                )
            action = touched[rel]
            changes.append(
                {
                    "path": rel,
                    "action": action,
                    "sha_before": fork_hashes.get(rel) if action in {"modify", "delete"} else None,
                    "sha_after": tree_hashes.get(rel) if action in {"modify", "create"} else None,
                    "escalate": change_class == "escalated",
                }
            )

        escalate_any = any(change["escalate"] for change in changes)
        touched_migrations = any(change["path"].startswith("muika/migrations/") for change in changes)
        diff_text = self._unified_diff(changes, tree)

        payload: dict = {
            "workspace_id": ws_id,
            "reason": reason,
            "changes": [{"path": c["path"], "action": c["action"], "escalate": c["escalate"]} for c in changes],
            "diff": diff_text,
            "touched_migrations": touched_migrations,
        }
        if touched_migrations:
            payload["warning"] = "回滚此改动将丢失此后对话记忆（回滚前会归档数据库）。"

        from muika.core.code_review import ReviewError, get_code_reviewer

        try:
            review = await get_code_reviewer().authorize(
                "workspace",
                payload,
                files={str(tree / c["path"]): _sha256_file(tree / c["path"]) or "missing" for c in changes},
                require_player=escalate_any,
            )
        except ReviewError as exc:
            manifest["error"] = str(exc)
            self._save_manifest(manifest)
            raise SelfWorkspaceError(f"Review not approved: {exc}") from exc

        baseline = _baseline_report(workspace_fingerprint(self._runtime_root))
        candidate = _run_probe(tree)
        if baseline.get("status") != "completed":
            raise SelfWorkspaceError(
                f"Baseline probe unavailable: {baseline.get('reason', 'unknown')}. "
                "The gate needs a runnable baseline; fix the test environment and retry."
            )
        if candidate.get("status") != "completed":
            raise SelfWorkspaceError(f"Candidate probe could not run: {candidate.get('reason', 'unknown cause')}.")
        baseline_failures = set(map(str, baseline.get("failures", []) + baseline.get("errors", [])))
        candidate_failures = set(map(str, candidate.get("failures", []) + candidate.get("errors", [])))
        new_failures = sorted(candidate_failures - baseline_failures)
        if new_failures:
            raise SelfWorkspaceError(f"Probe failed with new failures: {', '.join(new_failures)}.")
        if int(candidate.get("test_count", 0)) == 0 or int(candidate.get("test_count", 0)) < int(
            baseline.get("test_count", 0)
        ):
            raise SelfWorkspaceError(
                f"Test count fell from {baseline.get('test_count', 0)} to {candidate.get('test_count', 0)}; "
                "removing or skipping tests cannot pass the gate."
            )

        before_dir = self.workspace_root(ws_id) / "before"
        for change in changes:
            source = self._runtime_root / change["path"]
            if source.is_file():
                self._copy_into_tree(source, before_dir / change["path"])

        db_path = mas_config.data_dir.resolve() / "muika.db"
        backup_path = str(backup_database(db_path, label="workspace")) if db_path.is_file() else ""

        manifest.update(
            {
                "status": "submitted",
                "touched": changes,
                "touched_migrations": touched_migrations,
                "backup_path": backup_path,
                "review_id": review.id,
                "submitted_at": datetime.now().isoformat(),
                "error": "",
            }
        )
        self._save_manifest(manifest)
        supervisor.write_boot_target(ws_id, _manifest_hash(manifest), review.id, backup_path)
        logger.info(f"[SelfWorkspace] Submitted 1 workspace {ws_id} with {len(changes)} changed file(s).")
        return (
            f"Workspace {ws_id} passed the gate with {len(changes)} file change(s). "
            "The running code is unchanged. Choose the restart moment in your conversation; "
            f"the new version of you boots after restart. `.patch show {ws_id}` shows the diff for the player."
        )

    def discard(self, ws_id: str, reason: str) -> str:
        """放弃工作区；若指针指向它则一并清除。"""
        from muika.ipc import supervisor

        capability = self._deployment_capability()
        if capability is not None:
            raise SelfWorkspaceError(capability)
        manifest = self.load_manifest(ws_id)
        if manifest["status"] == "promoted":
            raise SelfWorkspaceError(
                f"Workspace {ws_id} already runs as part of the default tree; use `.patch rollback` instead."
            )

        pointer = supervisor.read_boot_target()
        if pointer is not None:
            try:
                pointed_at = json.loads(pointer.read_text(encoding="utf-8")).get("workspace_id")
            except (OSError, json.JSONDecodeError):
                pointed_at = None
            if pointed_at == ws_id:
                supervisor.clear_boot_target()

        manifest["status"] = "discarded"
        manifest["error"] = reason.strip()
        self._save_manifest(manifest)
        shutil.rmtree(self.workspace_root(ws_id), ignore_errors=True)
        logger.info(f"[SelfWorkspace] Discarded 1 workspace {ws_id}.")
        return f"Workspace {ws_id} discarded; nothing about the running you changed."

    async def request_rollback(self, confirm_memory_loss: bool) -> str:
        """Core 侧回滚门：核对记忆损失确认后返回待回滚的 workspace id。

        :raises SelfWorkspaceError: 无可回滚记录，或需 ``--confirm-memory-loss`` 而未确认
        """
        from muika.ipc import supervisor

        capability = self._deployment_capability()
        if capability is not None:
            raise SelfWorkspaceError(capability)
        ready = supervisor.read_rollback_ready()
        if ready is None or ready.get("status") != "ready":
            raise SelfWorkspaceError("No promoted version is rollback-ready; nothing to undo.")
        if (
            ready.get("touched_migrations")
            and not confirm_memory_loss
            and await self._promoted_version_has_memories(ready)
        ):
            raise SelfWorkspaceError(
                "该版本包含数据库迁移；回滚会丢弃升级后的对话记忆（执行前会归档数据库）。"
                "确认请使用 .patch rollback --confirm-memory-loss。"
            )
        return str(ready.get("workspace_id", ""))

    async def _promoted_version_has_memories(self, ready: dict) -> bool:
        """提升之后是否产生了新的对话记忆；读不到时保守返回 True。"""
        promoted_at = str(ready.get("promoted_at", ""))
        if not promoted_at:
            return True
        try:
            from sqlalchemy import func, select

            from muika.database.db import get_session
            from muika.database.orm_models import ExperienceORM

            async with get_session() as session:
                last = await session.scalar(select(func.max(ExperienceORM.occurred_at)))
        except Exception:  # noqa: BLE001 - 数据库不可读时保守要求确认
            return True
        return last is not None and str(last) >= promoted_at

    def show_diff(self, ws_id: str, page: int = 1) -> str:
        """分页展示工作区与运行树的差异。"""
        manifest = self.load_manifest(ws_id)
        tree = self.tree_root(ws_id)
        tree_hashes = _tree_hashes(tree)
        runtime_hashes = _tree_hashes(self._runtime_root)
        changed = sorted(
            rel for rel in set(tree_hashes) | set(runtime_hashes) if tree_hashes.get(rel) != runtime_hashes.get(rel)
        )
        changes = [
            cast(
                WorkspaceChange,
                {
                    "path": rel,
                    "action": (
                        "delete" if rel not in tree_hashes else ("create" if rel not in runtime_hashes else "modify")
                    ),
                    "sha_before": runtime_hashes.get(rel),
                    "sha_after": tree_hashes.get(rel),
                    "escalate": False,
                },
            )
            for rel in changed
        ]
        diff = self._unified_diff(changes, tree)
        lines = diff.splitlines()
        page_count = max(1, (len(lines) + _DIFF_PAGE_LINES - 1) // _DIFF_PAGE_LINES)
        if page < 1 or page > page_count:
            raise SelfWorkspaceError(f"Page {page} exceeds {page_count} pages.")
        selected = lines[(page - 1) * _DIFF_PAGE_LINES : page * _DIFF_PAGE_LINES]
        header = [
            f"工作区：{ws_id}（{manifest['status']}）",
            f"理由：{manifest['reason']}",
            f"文件：{', '.join(changed) or '(无差异)'}",
            f"Diff：第 {page}/{page_count} 页",
            "",
        ]
        return "\n".join(header + selected)

    # ---------- 内部 ----------

    @staticmethod
    def _copy_into_tree(source: Path, target: Path) -> None:
        """把运行树文件复制进工作区（refresh/快照共用）。"""
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    def _unified_diff(self, changes: list[WorkspaceChange], tree: Path) -> str:
        """生成触碰文件相对运行树的 unified diff（总量封顶）。"""
        parts: list[str] = []
        total = 0
        for change in changes:
            rel = change["path"]
            before_path = self._runtime_root / rel
            after_path = tree / rel
            before_text = before_path.read_text(encoding="utf-8", errors="replace") if before_path.is_file() else ""
            after_text = after_path.read_text(encoding="utf-8", errors="replace") if after_path.is_file() else ""
            chunk = "".join(
                difflib.unified_diff(
                    before_text.splitlines(keepends=True),
                    after_text.splitlines(keepends=True),
                    fromfile=f"a/{rel}",
                    tofile=f"b/{rel}",
                )
            )
            parts.append(chunk)
            total += len(chunk)
            if total > _DIFF_CAP_CHARS:
                parts.append(f"...(diff capped at {_DIFF_CAP_CHARS} characters)\n")
                break
        return "".join(parts)


_manager: Optional[SelfWorkspaceManager] = None


def get_self_workspace_manager() -> SelfWorkspaceManager:
    """获取 SelfWorkspaceManager 单例。"""
    global _manager
    if _manager is None:
        _manager = SelfWorkspaceManager()
    return _manager
