"""Core 多文件变更提案、验证、批准和回滚。"""

from __future__ import annotations

import ast
import asyncio
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from muika.config import mas_config
from muika.database.crud import SelfModificationCRUD
from muika.database.db import get_session
from muika.utils.logger import logger

_ALLOWED_DIRS = ("muika", "muika_bot", "tests")
_ALLOWED_FILES = ("bot.py", "core_main.py")
_CONTROL_FILES = (
    "muika/core/self_mod/proposals.py",
    "muika/core/actions/tools/_core_proposal.py",
    "muika/builtin_plugins/patch.py",
    "muika/core/self_mod/core_probe.py",
    "tests/test_core_proposals.py",
)
_CONTROL_PREFIXES = ("muika/migrations/",)
_PATCH_ID_RE = re.compile(r"^[0-9]{8}_[0-9]{6}_[0-9a-f]{8}$")
_PROBE_MARKER = "[CORE_PROBE_RESULT]"
_APPLY_LOCK = asyncio.Lock()


class CoreProposalError(Exception):
    """Core 提案操作被拒绝时抛出。"""


def _sha256_text(text: str) -> str:
    """返回 UTF-8 文本的 SHA-256。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write_text(path: Path, content: str) -> None:
    """原子写入 UTF-8 文本。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".core-proposal.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    """原子写入 JSON 数据。"""
    _atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class CoreProposalManager:
    """管理 Core 多文件提案及其不可变快照。"""

    def __init__(self, project_root: Optional[Path] = None) -> None:
        self.project_root = (project_root or Path.cwd()).resolve()
        self.proposals_root = (self.project_root / mas_config.data_dir / "core_proposals").resolve()

    def _require_enabled(self) -> None:
        """检查 Core 提案的双开关。"""
        if not mas_config.enable_self_modification or not mas_config.enable_core_proposals:
            raise CoreProposalError("Core proposals are disabled by configuration.")

    def resolve_core_path(self, raw_path: str, *, for_write: bool = False) -> Path:
        """解析 Core 路径并检查允许范围。"""
        resolved = self.resolve_observation_path(raw_path)
        rel = resolved.relative_to(self.project_root).as_posix()
        if resolved.suffix != ".py":
            raise CoreProposalError(f"Access denied: {rel} is outside the Core proposal scope.")
        if for_write and (rel in _CONTROL_FILES or any(rel.startswith(prefix) for prefix in _CONTROL_PREFIXES)):
            raise CoreProposalError(f"Access denied: {rel} is protected proposal control code.")
        return resolved

    def resolve_observation_path(self, raw_path: str) -> Path:
        """解析 Core 只读观察路径。"""
        if not raw_path or Path(raw_path).is_absolute():
            raise CoreProposalError("Core paths must be project-relative.")
        lexical = Path(raw_path)
        if any(part in ("", ".", "..") for part in lexical.parts):
            raise CoreProposalError(f"Invalid Core path: {raw_path!r}.")
        candidate = self.project_root / lexical
        if candidate.is_symlink() or any(
            parent.is_symlink() for parent in candidate.parents if parent != self.project_root
        ):
            raise CoreProposalError(f"Access denied: {raw_path} uses a symbolic link.")
        resolved = candidate.resolve()
        try:
            rel = resolved.relative_to(self.project_root).as_posix()
        except ValueError as exc:
            raise CoreProposalError(f"Access denied: {raw_path} is outside the project.") from exc
        allowed = rel in _ALLOWED_FILES or any(rel.startswith(prefix + "/") for prefix in _ALLOWED_DIRS)
        allowed = allowed or rel in _ALLOWED_DIRS
        if not allowed:
            raise CoreProposalError(f"Access denied: {rel} is outside the Core proposal scope.")
        return resolved

    def workspace_fingerprint(self) -> str:
        """返回当前 L3 Python 工作区指纹。"""
        digest = hashlib.sha256()
        paths: list[Path] = []
        for dirname in _ALLOWED_DIRS:
            root = self.project_root / dirname
            if root.is_dir():
                paths.extend(path for path in root.rglob("*.py") if path.is_file() and not path.is_symlink())
        for filename in _ALLOWED_FILES:
            path = self.project_root / filename
            if path.is_file() and not path.is_symlink():
                paths.append(path)
        for path in sorted(set(paths), key=lambda item: item.relative_to(self.project_root).as_posix()):
            rel = path.relative_to(self.project_root).as_posix()
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    def create(self, changes: list[dict[str, Any]], reason: str, source: str = "self") -> str:
        """创建一个多文件 Core 提案。"""
        self._require_enabled()
        reason = reason.strip()
        if not reason:
            raise CoreProposalError("Proposal reason must not be empty.")
        if not changes:
            raise CoreProposalError("Proposal must contain at least one file change.")
        if len(changes) > mas_config.core_proposal_max_files:
            raise CoreProposalError(f"Proposal exceeds the {mas_config.core_proposal_max_files}-file limit.")

        prepared: list[dict[str, Any]] = []
        seen: set[str] = set()
        total_bytes = 0
        for index, change in enumerate(changes):
            item = self._prepare_change(change, index)
            if item["path"] in seen:
                raise CoreProposalError(f"Path appears more than once: {item['path']}.")
            seen.add(item["path"])
            total_bytes += len((item.get("after_text") or "").encode("utf-8"))
            if total_bytes > mas_config.core_proposal_max_total_bytes:
                raise CoreProposalError(f"Proposal content exceeds {mas_config.core_proposal_max_total_bytes} bytes.")
            prepared.append(item)

        patch_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        patch_dir = self.proposals_root / patch_id
        work_dir = self.proposals_root / f".{patch_id}.tmp"
        work_dir.mkdir(parents=True, exist_ok=False)
        before_dir = work_dir / "before"
        after_dir = work_dir / "after"
        diff_parts: list[str] = []
        records: list[dict[str, Any]] = []
        for item in prepared:
            rel = item["path"]
            before_text = item.get("before_text")
            after_text = item.get("after_text")
            before_snapshot = self._write_snapshot(before_dir, rel, before_text)
            after_snapshot = self._write_snapshot(after_dir, rel, after_text)
            diff_parts.extend(
                difflib.unified_diff(
                    (before_text or "").splitlines(keepends=True),
                    (after_text or "").splitlines(keepends=True),
                    fromfile=f"a/{rel}",
                    tofile=f"b/{rel}",
                )
            )
            records.append(
                {
                    "action": item["action"],
                    "path": rel,
                    "sha256_before": _sha256_text(before_text) if before_text is not None else None,
                    "sha256_after": _sha256_text(after_text) if after_text is not None else None,
                    "before_snapshot": before_snapshot,
                    "after_snapshot": after_snapshot,
                }
            )

        proposal = {
            "schema_version": 1,
            "patch_id": patch_id,
            "status": "pending",
            "reason": reason,
            "source": source,
            "created_at": datetime.now().isoformat(),
            "workspace_fingerprint": self.workspace_fingerprint(),
            "changes": records,
            "validation": None,
            "audit_errors": [],
            "warnings": [],
        }
        _atomic_write_text(work_dir / "proposal.diff", "".join(diff_parts))
        _atomic_write_json(work_dir / "proposal.json", proposal)
        os.replace(work_dir, patch_dir)
        return patch_id

    def _prepare_change(self, change: dict[str, Any], index: int) -> dict[str, Any]:
        """检查并展开一个文件变更。"""
        action = str(change.get("action", "")).strip()
        raw_path = str(change.get("path", "")).strip()
        if action not in {"modify", "create", "delete"}:
            raise CoreProposalError(f"Change {index} has invalid action {action!r}.")
        target = self.resolve_core_path(raw_path, for_write=True)
        rel = target.relative_to(self.project_root).as_posix()
        exists = target.is_file()
        if action == "create" and exists:
            raise CoreProposalError(f"Create target already exists: {rel}.")
        if action in {"modify", "delete"} and not exists:
            raise CoreProposalError(f"{action.title()} target does not exist: {rel}.")
        before_text = target.read_text(encoding="utf-8") if exists else None
        after_text: Optional[str]
        if action == "modify":
            after_text = self._apply_replacements(before_text or "", change.get("replacements"), rel)
        elif action == "create":
            content = change.get("content")
            if not isinstance(content, str):
                raise CoreProposalError(f"Create change for {rel} requires string content.")
            after_text = content
        else:
            after_text = None
        if after_text is not None:
            size = len(after_text.encode("utf-8"))
            if size > mas_config.core_proposal_max_file_bytes:
                raise CoreProposalError(f"Candidate {rel} exceeds {mas_config.core_proposal_max_file_bytes} bytes.")
            try:
                ast.parse(after_text, filename=rel)
                compile(after_text, rel, "exec")
            except (SyntaxError, ValueError) as exc:
                raise CoreProposalError(f"Python syntax check failed for {rel}: {exc}") from exc
        return {"action": action, "path": rel, "before_text": before_text, "after_text": after_text}

    @staticmethod
    def _apply_replacements(text: str, replacements: Any, rel: str) -> str:
        """按顺序应用唯一文本替换。"""
        if not isinstance(replacements, list) or not replacements:
            raise CoreProposalError(f"Modify change for {rel} requires replacements.")
        result = text
        for index, replacement in enumerate(replacements):
            if not isinstance(replacement, dict):
                raise CoreProposalError(f"Replacement {index} for {rel} must be an object.")
            old_text = replacement.get("old_text")
            new_text = replacement.get("new_text")
            if not isinstance(old_text, str) or not old_text or not isinstance(new_text, str):
                raise CoreProposalError(f"Replacement {index} for {rel} requires old_text and new_text.")
            count = result.count(old_text)
            if count != 1:
                raise CoreProposalError(
                    f"Replacement {index} for {rel} matched {count} times; expected exactly one match."
                )
            result = result.replace(old_text, new_text, 1)
        return result

    @staticmethod
    def _write_snapshot(root: Path, rel: str, content: Optional[str]) -> Optional[str]:
        """写入提案快照并返回提案目录相对路径。"""
        if content is None:
            return None
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path.relative_to(root.parent).as_posix()

    def load(self, patch_id: str) -> dict[str, Any]:
        """读取一个提案记录。"""
        if not _PATCH_ID_RE.fullmatch(patch_id):
            raise CoreProposalError(f"Invalid patch id: {patch_id!r}.")
        path = self.proposals_root / patch_id / "proposal.json"
        if not path.is_file():
            raise CoreProposalError(f"Proposal not found: {patch_id}.")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CoreProposalError(f"Proposal record is invalid: {patch_id}: {exc}") from exc
        if not isinstance(value, dict) or value.get("patch_id") != patch_id:
            raise CoreProposalError(f"Proposal record is invalid: {patch_id}.")
        return value

    def list_proposals(self, status: str = "") -> list[dict[str, Any]]:
        """按创建时间倒序列出提案。"""
        self._require_enabled()
        if not self.proposals_root.is_dir():
            return []
        proposals: list[dict[str, Any]] = []
        for path in self.proposals_root.iterdir():
            if not path.is_dir() or not _PATCH_ID_RE.fullmatch(path.name):
                continue
            try:
                proposal = self.load(path.name)
            except CoreProposalError:
                continue
            if not status or proposal.get("status") == status:
                proposals.append(proposal)
        return sorted(proposals, key=lambda item: str(item.get("created_at", "")), reverse=True)

    def is_stale(self, proposal: dict[str, Any]) -> bool:
        """判断提案是否与当前工作区发生漂移。"""
        if proposal.get("workspace_fingerprint") != self.workspace_fingerprint():
            return True
        for change in proposal.get("changes", []):
            target = self.resolve_core_path(str(change["path"]), for_write=True)
            current_hash = _sha256_text(target.read_text(encoding="utf-8")) if target.is_file() else None
            if current_hash != change.get("sha256_before"):
                return True
        return False

    def show(self, patch_id: str, page: int = 1) -> str:
        """返回分页 diff 和提案摘要。"""
        self._require_enabled()
        proposal = self.load(patch_id)
        if page < 1:
            raise CoreProposalError("Page must be 1 or greater.")
        diff = (self.proposals_root / patch_id / "proposal.diff").read_text(encoding="utf-8")
        lines = diff.splitlines()
        page_size = mas_config.core_patch_show_page_lines
        page_count = max(1, (len(lines) + page_size - 1) // page_size)
        if page > page_count:
            raise CoreProposalError(f"Page {page} exceeds {page_count} pages.")
        start = (page - 1) * page_size
        selected = lines[start : start + page_size]
        paths = [str(change.get("path", "")) for change in proposal.get("changes", [])]
        warnings: list[str] = []
        if any(path.startswith("tests/") for path in paths):
            warnings.append("警告：该提案修改了测试文件。削弱断言可能使验证结果失去意义。请人工核对。")
        stale = "yes" if self.is_stale(proposal) else "no"
        header = [
            *warnings,
            f"提案：{patch_id}",
            f"状态：{proposal['status']} | 过期：{stale}",
            f"理由：{proposal['reason']}",
            f"文件：{', '.join(paths)}",
            f"Diff：第 {page}/{page_count} 页",
            "",
        ]
        return "\n".join(header + selected)

    def _save(self, proposal: dict[str, Any]) -> None:
        """保存提案记录。"""
        _atomic_write_json(self.proposals_root / proposal["patch_id"] / "proposal.json", proposal)

    def _snapshot_text(self, patch_id: str, relative_snapshot: Optional[str]) -> Optional[str]:
        """读取提案快照。"""
        if relative_snapshot is None:
            return None
        return (self.proposals_root / patch_id / relative_snapshot).read_text(encoding="utf-8")

    def _copy_workspace(self, destination: Path) -> None:
        """复制验证所需的项目文件。"""
        ignored = shutil.ignore_patterns(
            ".git",
            ".venv",
            "venv",
            "data",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            "*.pyc",
        )
        shutil.copytree(self.project_root, destination, ignore=ignored, dirs_exist_ok=True)

    def _apply_candidate_to_copy(self, proposal: dict[str, Any], destination: Path) -> None:
        """把提案 after 快照应用到临时副本。"""
        patch_id = str(proposal["patch_id"])
        for change in proposal["changes"]:
            target = destination / change["path"]
            after_text = self._snapshot_text(patch_id, change.get("after_snapshot"))
            if after_text is None:
                if target.exists():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(after_text, encoding="utf-8")

    def _run_probe(self, workspace: Path) -> dict[str, Any]:
        """在子进程中运行候选测试探针。"""
        command = [sys.executable, "-m", "muika.core.self_mod.core_probe"]
        try:
            completed = subprocess.run(
                command,
                cwd=workspace,
                capture_output=True,
                timeout=mas_config.core_validate_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            timeout_output = ((exc.stdout or b"") + (exc.stderr or b""))[-mas_config.core_validate_output_cap_bytes :]
            return {
                "status": "failed",
                "reason": f"Validation timed out after {mas_config.core_validate_timeout_seconds} seconds.",
                "timed_out": True,
                "output": timeout_output.decode("utf-8", errors="replace"),
                "failures": [],
                "errors": [],
                "test_count": 0,
            }
        output_bytes = (completed.stdout or b"") + (completed.stderr or b"")
        output = output_bytes[-mas_config.core_validate_output_cap_bytes :].decode("utf-8", errors="replace")
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
        if not isinstance(result, dict):
            raise CoreProposalError("Validation probe returned a non-object result.")
        result["output"] = output
        return result

    def _baseline_report(self, fingerprint: str) -> dict[str, Any]:
        """读取或动态测量工作区基线。"""
        cache_path = self.proposals_root / "_baseline" / f"{fingerprint}.json"
        if cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                cached = None
            if isinstance(cached, dict) and cached.get("fingerprint") == fingerprint:
                return cached
        with tempfile.TemporaryDirectory(prefix="muika-core-baseline-") as tmp:
            workspace = Path(tmp) / "workspace"
            self._copy_workspace(workspace)
            report = self._run_probe(workspace)
        report["fingerprint"] = fingerprint
        report["measured_at"] = datetime.now().isoformat()
        _atomic_write_json(cache_path, report)
        return report

    def validate(self, patch_id: str, *, force: bool = False) -> dict[str, Any]:
        """验证一个候选提案并保存结构化报告。"""
        self._require_enabled()
        proposal = self.load(patch_id)
        if proposal["status"] != "pending":
            raise CoreProposalError(f"Only pending proposals can be validated; status is {proposal['status']}.")
        if self.is_stale(proposal):
            raise CoreProposalError("Proposal is stale because the workspace changed.")
        fingerprint = self.workspace_fingerprint()
        current = proposal.get("validation")
        validation_path = self.proposals_root / patch_id / "validation.json"
        if (
            not force
            and isinstance(current, dict)
            and current.get("fingerprint") == fingerprint
            and validation_path.is_file()
        ):
            report = json.loads(validation_path.read_text(encoding="utf-8"))
            if isinstance(report, dict):
                return report

        baseline = self._baseline_report(fingerprint)
        with tempfile.TemporaryDirectory(prefix="muika-core-candidate-") as tmp:
            workspace = Path(tmp) / "workspace"
            self._copy_workspace(workspace)
            self._apply_candidate_to_copy(proposal, workspace)
            candidate = self._run_probe(workspace)

        warnings: list[str] = []
        baseline_status = baseline.get("status")
        candidate_status = candidate.get("status")
        if baseline_status == "unavailable" or candidate_status == "unavailable":
            status = "unavailable"
            reason = candidate.get("reason") or baseline.get("reason") or "The test environment is unavailable."
            new_failures: list[str] = []
            new_errors: list[str] = []
        elif candidate.get("timed_out"):
            status = "failed"
            reason = str(candidate.get("reason", "Candidate validation timed out."))
            new_failures = []
            new_errors = []
        else:
            baseline_failures = set(str(item) for item in baseline.get("failures", []))
            baseline_errors = set(str(item) for item in baseline.get("errors", []))
            new_failures = sorted(set(str(item) for item in candidate.get("failures", [])) - baseline_failures)
            new_errors = sorted(set(str(item) for item in candidate.get("errors", [])) - baseline_errors)
            status = "failed" if new_failures or new_errors else "passed"
            reason = "Candidate adds test failures or collection errors." if status == "failed" else "No new failures."
            if int(candidate.get("test_count", 0)) < int(baseline.get("test_count", 0)):
                warnings.append(
                    f"Test count decreased from {baseline.get('test_count', 0)} to {candidate.get('test_count', 0)}."
                )

        report = {
            "patch_id": patch_id,
            "fingerprint": fingerprint,
            "created_at": datetime.now().isoformat(),
            "status": status,
            "reason": reason,
            "baseline": baseline,
            "candidate": candidate,
            "new_failures": new_failures,
            "new_errors": new_errors,
            "warnings": warnings,
        }
        _atomic_write_json(validation_path, report)
        proposal["validation"] = {
            "status": status,
            "fingerprint": fingerprint,
            "created_at": report["created_at"],
            "warnings": warnings,
        }
        proposal["warnings"] = warnings
        self._save(proposal)
        return report

    async def approve(self, patch_id: str, *, allow_unvalidated: bool = False) -> str:
        """验证并事务式应用一个待批准提案。"""
        self._require_enabled()
        async with _APPLY_LOCK:
            proposal = self.load(patch_id)
            if proposal["status"] != "pending":
                raise CoreProposalError(f"Only pending proposals can be approved; status is {proposal['status']}.")
            if self.is_stale(proposal):
                raise CoreProposalError("Proposal is stale because the workspace changed.")
            report = self.validate(patch_id)
            if report["status"] == "failed":
                raise CoreProposalError(f"Proposal validation failed: {report['reason']}")
            if report["status"] == "unavailable" and not allow_unvalidated:
                raise CoreProposalError(
                    "Proposal validation is unavailable. Review the cause, then use the explicit "
                    "unvalidated approval option."
                )
            if report["status"] not in {"passed", "unavailable"}:
                raise CoreProposalError(f"Proposal validation has invalid status: {report['status']}.")

            proposal = self.load(patch_id)
            if self.is_stale(proposal):
                raise CoreProposalError("Proposal became stale before application.")
            proposal["status"] = "applying"
            proposal["applying_at"] = datetime.now().isoformat()
            proposal["unvalidated_approval"] = report["status"] == "unavailable"
            self._save(proposal)
            try:
                self._apply_formal(proposal)
            except Exception as exc:
                recovery_errors = self._restore_before(proposal)
                proposal["status"] = "failed"
                proposal["failure"] = str(exc)
                proposal["recovery_errors"] = recovery_errors
                self._save(proposal)
                if recovery_errors:
                    raise CoreProposalError(
                        f"Proposal application failed: {exc}. Recovery also failed: {'; '.join(recovery_errors)}"
                    ) from exc
                raise CoreProposalError(f"Proposal application failed and files were restored: {exc}") from exc

            proposal["status"] = "approved"
            proposal["approved_at"] = datetime.now().isoformat()
            self._save(proposal)
            for change in proposal["changes"]:
                audit_error = await self._audit_change(proposal, change, "core_approve")
                if audit_error:
                    proposal["audit_errors"].append(audit_error)
                    self._save(proposal)
            risk = (
                " The tests were unavailable, so this approval used the explicit risk override."
                if allow_unvalidated
                else ""
            )
            return f"Core proposal {patch_id} was approved and applied.{risk} Restart is required."

    def _apply_formal(self, proposal: dict[str, Any]) -> None:
        """应用提案的全部 after 状态。"""
        patch_id = str(proposal["patch_id"])
        deleted_root = self.proposals_root / patch_id / "deleted"
        for change in proposal["changes"]:
            target = self.resolve_core_path(str(change["path"]), for_write=True)
            after_text = self._snapshot_text(patch_id, change.get("after_snapshot"))
            if after_text is None:
                moved = deleted_root / change["path"]
                moved.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, moved)
            else:
                _atomic_write_text(target, after_text)

    def _restore_before(self, proposal: dict[str, Any]) -> list[str]:
        """把全部正式文件恢复到 before 状态。"""
        errors: list[str] = []
        patch_id = str(proposal["patch_id"])
        for change in reversed(proposal["changes"]):
            target = self.resolve_core_path(str(change["path"]), for_write=True)
            before_text = self._snapshot_text(patch_id, change.get("before_snapshot"))
            try:
                if before_text is None:
                    if target.exists():
                        target.unlink()
                else:
                    _atomic_write_text(target, before_text)
            except Exception as exc:
                errors.append(f"{change['path']}: {exc}")
        return errors

    async def _audit_change(
        self,
        proposal: dict[str, Any],
        change: dict[str, Any],
        action: str,
    ) -> Optional[str]:
        """尽力写入一条 Core 文件审计记录。"""
        try:
            async with get_session() as session:
                record = await SelfModificationCRUD.create(
                    session,
                    layer="core",
                    path=str(change["path"]),
                    action=action,
                    reason=str(proposal["reason"]),
                    before_path=change.get("before_snapshot"),
                    after_path=change.get("after_snapshot"),
                    source=f"patch:{proposal['patch_id']}",
                )
                await session.flush()
                logger.info(f"[CoreProposal] Audit #{record.id} recorded for {change['path']}")
                return None
        except Exception as exc:
            message = f"{change['path']}: {exc}"
            logger.critical(f"[CoreProposal] Audit failed after code application: {message}")
            return message


_manager: Optional[CoreProposalManager] = None


def get_core_proposal_manager() -> CoreProposalManager:
    """返回 CoreProposalManager 单例。"""
    global _manager
    if _manager is None:
        _manager = CoreProposalManager()
    return _manager
