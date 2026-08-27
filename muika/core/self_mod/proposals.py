"""Core 多文件变更提案、验证、批准和回滚。"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from muika.config import mas_config

_ALLOWED_DIRS = ("muika", "muika_bot", "tests")
_ALLOWED_FILES = ("bot.py", "core_main.py")
_CONTROL_FILES = (
    "muika/core/self_mod/proposals.py",
    "muika/core/actions/tools/_core_proposal.py",
    "muika/builtin_plugins/patch.py",
    "tests/test_core_proposals.py",
)
_CONTROL_PREFIXES = ("muika/migrations/",)
_PATCH_ID_RE = re.compile(r"^[0-9]{8}_[0-9]{6}_[0-9a-f]{8}$")


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

    def list(self, status: str = "") -> list[dict[str, Any]]:
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


_manager: Optional[CoreProposalManager] = None


def get_core_proposal_manager() -> CoreProposalManager:
    """返回 CoreProposalManager 单例。"""
    global _manager
    if _manager is None:
        _manager = CoreProposalManager()
    return _manager
