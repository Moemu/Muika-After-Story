"""Core 只读观察和多文件提案工具。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from muika.core.self_mod.proposals import CoreProposalError, get_core_proposal_manager
from muika.plugin.func_call import on_function_call


class CoreListParams(BaseModel):
    path: str = Field("muika", description="Project-relative Core directory or Python file.")


@on_function_call("List Python files in the approved Core observation scope.", params=CoreListParams)
async def core_list(path: str = "muika") -> str:
    """列出 Core 观察范围内的 Python 文件。"""
    manager = get_core_proposal_manager()
    try:
        manager._require_enabled()
        raw = path.strip() or "muika"
        candidate = manager.resolve_observation_path(raw)
        if candidate.is_file():
            resolved = manager.resolve_core_path(raw)
            return resolved.relative_to(manager.project_root).as_posix()
        if not candidate.is_dir():
            return f"Path not found: {raw}"
        results: list[str] = []
        for item in sorted(candidate.rglob("*.py")):
            try:
                manager.resolve_core_path(item.relative_to(manager.project_root).as_posix())
            except (CoreProposalError, ValueError):
                continue
            results.append(item.relative_to(manager.project_root).as_posix())
        return "\n".join(results) if results else "No Python files found."
    except CoreProposalError as exc:
        return str(exc)


class CoreReadParams(BaseModel):
    path: str = Field(..., description="Project-relative Python file.")
    line_start: int = Field(1, ge=1, description="First line, starting at 1.")
    line_end: int = Field(200, ge=1, description="Last line, inclusive.")


@on_function_call("Read a bounded line range from Core Python code.", params=CoreReadParams)
async def core_read(path: str, line_start: int = 1, line_end: int = 200) -> str:
    """读取 Core 文件的行区间。"""
    manager = get_core_proposal_manager()
    try:
        manager._require_enabled()
        resolved = manager.resolve_core_path(path)
        if not resolved.is_file():
            return f"File not found: {path}"
        if line_end < line_start or line_end - line_start + 1 > 400:
            return "The line range must contain 1 to 400 lines."
        lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[line_start - 1 : line_end]
        return "\n".join(f"{number:5d} | {line}" for number, line in enumerate(selected, line_start))
    except CoreProposalError as exc:
        return str(exc)


class CoreSearchParams(BaseModel):
    query: str = Field(..., min_length=1, description="Exact text to find.")
    path: str = Field("muika", description="Project-relative Core search root.")


@on_function_call("Search exact text in Core Python code and return bounded matches.", params=CoreSearchParams)
async def core_search(query: str, path: str = "muika") -> str:
    """搜索 Core Python 文件。"""
    manager = get_core_proposal_manager()
    try:
        manager._require_enabled()
        candidate = manager.resolve_observation_path(path)
        if not candidate.exists():
            return f"Path not found: {path}"
        files = [manager.resolve_core_path(path)] if candidate.is_file() else sorted(candidate.rglob("*.py"))
        matches: list[str] = []
        for item in files:
            try:
                rel = item.relative_to(manager.project_root).as_posix()
                manager.resolve_core_path(rel)
            except (CoreProposalError, ValueError):
                continue
            for line_number, line in enumerate(item.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if query in line:
                    matches.append(f"{rel}:{line_number}: {line[:300]}")
                    if len(matches) >= 100:
                        return "\n".join(matches) + "\n...(match limit reached)"
        return "\n".join(matches) if matches else "No matches found."
    except (CoreProposalError, OSError) as exc:
        return str(exc)


class CoreReplacement(BaseModel):
    old_text: str = Field(..., min_length=1, description="Exact source text. It must match once.")
    new_text: str = Field(..., description="Replacement text.")


class CoreChange(BaseModel):
    action: Literal["modify", "create", "delete"]
    path: str
    replacements: Optional[list[CoreReplacement]] = None
    content: Optional[str] = None


class ProposeCoreChangeParams(BaseModel):
    changes: list[CoreChange] = Field(..., min_length=1)
    reason: str = Field(..., min_length=1, description="Why this Core change is needed.")


@on_function_call(
    "Create a Core code proposal for human review. This does not change active code.",
    params=ProposeCoreChangeParams,
)
async def propose_core_change(changes: list[CoreChange], reason: str) -> str:
    """创建 Core 多文件提案。"""
    try:
        raw_changes = [change.model_dump() if isinstance(change, CoreChange) else change for change in changes]
        patch_id = get_core_proposal_manager().create(raw_changes, reason)
        return (
            f"Core proposal created: {patch_id}. No active code changed.\n"
            f"Reason: {reason}\n"
            "Tell the user what you want to change and why. The user must review and decide."
        )
    except CoreProposalError as exc:
        return f"Core proposal rejected: {exc}"
