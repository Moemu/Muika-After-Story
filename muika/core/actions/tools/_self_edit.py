"""自我编辑工具：Muika 读取/创建/局部修改/回滚她自己（技能文档）的通道。

模板文件由常规文件工具（``read_file`` / ``write_file`` / ``edit_file``）操作，
修改指南内嵌在 ``muika/builtin_skills/muika-self`` 技能文档中。
话题库（``muika/topics/topics.yml``）不走文件级编辑，
由 :mod:`_topics` 的专用结构化工具维护。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from muika.config import mas_config
from muika.core.self_mod import SelfModError, get_self_mod_manager
from muika.core.self_mod.plugin_deployer import get_plugin_deployer
from muika.core.self_mod.policy import allowed_roots, display_path, resolve_self_path
from muika.core.self_mod.validators import validate_content, validate_template
from muika.plugin.func_call import on_function_call
from muika.template.loader import SEARCH_PATH
from muika.utils.logger import logger

from ._filesystem import _apply_edit

_TEMPLATE_NAME_RE = re.compile(r"^[\w.\-]+$")
"""模板文件名只允许字母/数字/下划线/点/连字符，禁止路径分隔符。"""

_READ_LIMIT = 30_000
"""self_read 单次返回的字符上限。"""

_CONTEXT_BEFORE = 10
_CONTEXT_AFTER = 15
"""self_edit 报告中修改点附近的上下文行数。"""

_DISABLED_MSG = "Self-modification is disabled by configuration."

_PENDING_TTL = timedelta(minutes=30)
"""预览暂存的过期时间——超过 30 分钟未确认即失效。"""


@dataclass
class PendingEdit:
    """self_edit 预览后暂存的待写内容，等待 self_edit_confirm 提交。"""

    new_text: str
    reason: str
    timestamp: datetime
    source_sha256: str


_pending_edits: dict[str, PendingEdit] = {}
"""以 display_path 为 key 暂存预览结果，self_edit_confirm 取用后清除。"""


def _disabled() -> bool:
    return not mas_config.enable_self_modification


def _content_sha256(content: str) -> str:
    """计算文本 SHA-256。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _is_plugin_path(path: Path) -> bool:
    """检查路径是否位于插件目录内。"""
    plugins_dir = Path(mas_config.plugins_dir).resolve()
    return path == plugins_dir or plugins_dir in path.parents


def _list_sandbox_files() -> str:
    """列出沙箱内所有可编辑文件，供 self_read 空路径时展示。"""
    lines = ["These are the parts of yourself you can read and edit:"]
    for root in allowed_roots():
        if root.is_file():
            lines.append(f"  [FILE] {display_path(root)}")
        elif root.is_dir():
            found = False
            for p in sorted(root.rglob("*")):
                rel_parts = p.relative_to(root).parts
                if root == Path(mas_config.plugins_dir).resolve() and (
                    len(rel_parts) != 1 or any(part in {"_staging", "_quarantine"} for part in rel_parts)
                ):
                    continue
                if p.is_file() and not p.name.startswith(".") and "__pycache__" not in p.parts:
                    lines.append(f"  [FILE] {display_path(p)}")
                    found = True
            if not found:
                lines.append(f"  [DIR ] {display_path(root)} (empty — create files here)")
        else:
            lines.append(f"  [DIR ] {display_path(root)} (does not exist yet — create it by writing)")

    lines.append(
        "\nPersona templates (templates/*.jinja2) are also in your sandbox. "
        "To customise your persona, use the load_skill('muika-self') skill for guidance."
    )
    lines.append(
        "Topic seeds are NOT edited as a file. " "Use topic_list / topic_add / topic_update / topic_delete instead."
    )
    return "\n".join(lines)


class SelfReadParams(BaseModel):
    path: str = Field(
        "",
        description=(
            "What to read. Empty string: list everything you can edit. "
            "'.history': show your recent self-modification journal. "
            "Otherwise a path such as 'configs/skills/muika-self/SKILL.md'."
        ),
    )


@on_function_call(
    "Read Muika's own editable self: her self-knowledge skills, persona template overrides, "
    "and the journal of her past self-modifications. "
    "This is how Muika looks at herself before deciding to change.",
    params=SelfReadParams,
)
async def self_read(path: str = "") -> str:
    if _disabled():
        return _DISABLED_MSG

    manager = get_self_mod_manager()

    stripped = path.strip()
    if not stripped:
        return _list_sandbox_files()

    if stripped == ".history" or stripped.startswith(".history "):
        target = stripped[len(".history") :].strip()
        try:
            return await manager.history(target, limit=15)
        except SelfModError as e:
            return str(e)

    try:
        resolved = resolve_self_path(stripped)
    except SelfModError as e:
        return str(e)

    if not resolved.exists():
        return f"File not found: {display_path(resolved)}"

    if not resolved.is_file():
        return f"Not a file: {display_path(resolved)}"

    text = resolved.read_text(encoding="utf-8", errors="replace")
    suffix = f"\n...(truncated, {len(text) - _READ_LIMIT:,} chars omitted)" if len(text) > _READ_LIMIT else ""
    return f"File: {display_path(resolved)}\n\n{text[:_READ_LIMIT]}{suffix}"


class SelfWriteParams(BaseModel):
    path: str = Field(
        ...,
        description="Path of the NEW file to create, inside the self-edit sandbox.",
    )
    content: str = Field(
        ...,
        description="Full content of the new file.",
    )
    reason: str = Field(
        ...,
        description="Why Muika wants this change. It is written into her self-modification journal.",
    )


@on_function_call(
    "Muika creates a brand-new file inside herself (a new self-knowledge note or a persona template override). "
    "ONLY for files that do not exist yet — to modify an existing part of herself she must use self_edit, "
    "which makes precise partial changes instead of rewriting the whole file. "
    "Every creation is validated and journaled. Plugin candidates stay in staging until plugin_load activates them.",
    params=SelfWriteParams,
)
async def self_write(path: str, content: str, reason: str) -> str:
    if _disabled():
        return _DISABLED_MSG

    if not reason or not reason.strip():
        return "A non-empty 'reason' is required: every change to yourself must be explained and journaled."

    if not content:
        return "'content' must not be empty: self_write requires the full file content."

    try:
        resolved = resolve_self_path(path, require_write=True)
    except SelfModError as e:
        return str(e)

    if resolved.exists():
        return (
            f"{display_path(resolved)} already exists. self_write only creates new files. "
            "Use self_edit to make a precise partial modification instead of rewriting the whole file."
        )

    try:
        if _is_plugin_path(resolved):
            return await get_plugin_deployer().deploy_new(path, content, reason.strip())
        return await get_self_mod_manager().apply(path, content, reason.strip())
    except SelfModError as e:
        logger.info(f"[SelfEdit] Rejected write to {path!r}: {e}")
        return f"The change was rejected: {e}"
    except Exception as e:
        logger.error(f"[SelfEdit] Unexpected error writing {path!r}: {e}")
        return f"Unexpected error: {e}"


class SelfEditParams(BaseModel):
    path: str = Field(..., description="The existing file to modify, e.g. 'configs/skills/muika-self/SKILL.md'.")
    operation: Literal["replace", "insert", "delete_lines"] = Field(
        ...,
        description=(
            "'replace': replace old_string with new_string (old_string must appear exactly once). "
            "'insert': insert new_string before line_number (1-based). "
            "'delete_lines': delete lines from line_start to line_end inclusive (1-based)."
        ),
    )
    old_string: Optional[str] = Field(None, description="Required for 'replace'. The exact text to find.")
    new_string: Optional[str] = Field(None, description="Required for 'replace' and 'insert'.")
    line_number: Optional[int] = Field(None, description="Required for 'insert'. 1-based line to insert before.")
    line_start: Optional[int] = Field(None, description="Required for 'delete_lines'. First line (inclusive).")
    line_end: Optional[int] = Field(None, description="Required for 'delete_lines'. Last line (inclusive).")
    reason: str = Field(
        ...,
        description="Why Muika wants this change. It is written into her self-modification journal.",
    )


@on_function_call(
    "Muika previews a precise PARTIAL modification to an existing part of herself "
    "(her self-knowledge notes or persona template overrides) — like careful surgery instead of "
    "rewriting her whole self. The edit is computed and validated but NEVER written to disk; "
    "instead the planned change is stored and a confirmation tool (self_edit_confirm) must be "
    "called separately to apply it. The report always shows the file region around the change "
    "so she can verify it before committing.",
    params=SelfEditParams,
)
async def self_edit(
    path: str,
    operation: str,
    old_string: Optional[str] = None,
    new_string: Optional[str] = None,
    line_number: Optional[int] = None,
    line_start: Optional[int] = None,
    line_end: Optional[int] = None,
    reason: str = "",
) -> str:
    if _disabled():
        return _DISABLED_MSG

    if not reason or not reason.strip():
        return "A non-empty 'reason' is required: every change to yourself must be explained and journaled."

    try:
        resolved = resolve_self_path(path, require_write=True)
    except SelfModError as e:
        return str(e)

    if not resolved.exists() or not resolved.is_file():
        return (
            f"File not found: {display_path(resolved)}. "
            "self_write creates new files; self_edit modifies existing ones."
        )

    original = resolved.read_text(encoding="utf-8", errors="replace")

    try:
        new_text = _apply_edit(original, operation, old_string, new_string, line_number, line_start, line_end)
    except ValueError as e:
        return f"The edit was rejected: {e}"

    try:
        validate_content(resolved, new_text)
    except SelfModError as e:
        return f"The edit would break this file, so it was rejected: {e}"

    rel = display_path(resolved)
    change_line = _locate_change_line(original, operation, old_string, line_number, line_start)
    context = _context_around(new_text, change_line)

    _pending_edits[rel] = PendingEdit(
        new_text=new_text,
        reason=reason.strip(),
        timestamp=datetime.now(),
        source_sha256=_content_sha256(original),
    )

    return (
        f"PREVIEW ONLY — nothing has been written yet.\n"
        f"Planned change: {operation} in {rel}\n"
        f"Validation: OK (the resulting file is valid)\n"
        f"--- Region around the change, as the file WOULD look (with line numbers) ---\n"
        f"{context}\n"
        f"---\n"
        f"Read it carefully. If this is exactly what you want, "
        f"call self_edit_confirm(path={rel!r}) to apply it. "
        f"The preview expires in 30 minutes."
    )


class SelfEditConfirmParams(BaseModel):
    path: str = Field(
        ...,
        description="The file whose pending preview should be applied. Must match a prior self_edit call.",
    )
    reason: Optional[str] = Field(
        None,
        description="Optional override for the journal reason. If empty, the reason given at preview time is used.",
    )


@on_function_call(
    "Apply a previously previewed self-edit. Muika must call self_edit first to see the planned change; "
    "this tool commits normal files. Plugin candidates stay in staging until plugin_load activates them. "
    "If no preview is pending for the given path, it returns an error.",
    params=SelfEditConfirmParams,
)
async def self_edit_confirm(path: str, reason: Optional[str] = None) -> str:
    """将 self_edit 预览过的待写内容提交到磁盘。"""
    if _disabled():
        return _DISABLED_MSG

    try:
        resolved = resolve_self_path(path, require_write=True)
    except SelfModError as e:
        return str(e)

    rel = display_path(resolved)
    pending = _pending_edits.get(rel)

    if pending is None:
        return (
            f"No pending edit for {rel}. Call self_edit first to preview the change, "
            f"then call self_edit_confirm to apply it."
        )

    if datetime.now() - pending.timestamp > _PENDING_TTL:
        _pending_edits.pop(rel, None)
        return (
            f"The preview for {rel} has expired (older than 30 minutes). "
            f"Call self_edit again to generate a fresh preview."
        )

    new_text = pending.new_text
    effective_reason = (reason.strip() if reason and reason.strip() else pending.reason) or "(no reason recorded)"

    try:
        if (
            not resolved.is_file()
            or _content_sha256(resolved.read_text(encoding="utf-8", errors="replace")) != pending.source_sha256
        ):
            return f"The file changed after the preview: {rel}. Create a new preview."
        is_plugin = _is_plugin_path(resolved)
        if is_plugin:
            report = await get_plugin_deployer().deploy_if_unchanged(
                path,
                new_text,
                effective_reason,
                pending.source_sha256,
            )
        else:
            report = await get_self_mod_manager().apply(path, new_text, effective_reason)
    except SelfModError as e:
        return f"The change was rejected: {e}"
    except Exception as e:
        logger.error(f"[SelfEdit] Unexpected error confirming edit to {path!r}: {e}")
        return f"Unexpected error: {e}"
    finally:
        _pending_edits.pop(rel, None)

    context = _context_around(new_text, new_text.count("\n") // 2)
    region_label = "staged candidate" if is_plugin else "file now"
    return (
        f"{report}\n"
        f"--- Region around the change, as the {region_label} looks (with line numbers) ---\n"
        f"{context}\n"
        f"---\n"
        f"Verify the region above. If something feels wrong, use self_revert(path={rel!r})."
    )


def _locate_change_line(
    original: str,
    operation: str,
    old_string: Optional[str],
    line_number: Optional[int],
    line_start: Optional[int],
) -> int:
    """估算修改点在新文件中的行号（0-based），用于截取上下文。"""
    if operation == "replace" and old_string:
        idx = original.find(old_string)
        return original.count("\n", 0, idx) if idx >= 0 else 0
    if operation == "insert" and line_number is not None:
        return max(0, line_number - 1)
    if operation == "delete_lines" and line_start is not None:
        return max(0, line_start - 1)
    return 0


def _context_around(text: str, line_idx: int) -> str:
    """截取指定行号附近的带行号上下文。"""
    lines = text.splitlines()
    start = max(0, line_idx - _CONTEXT_BEFORE)
    end = min(len(lines), line_idx + _CONTEXT_AFTER + 1)
    return "\n".join(f"{i + 1:4d} | {lines[i]}" for i in range(start, end))


class SelfRevertParams(BaseModel):
    path: str = Field(..., description="The file to revert, e.g. 'configs/skills/muika-self/SKILL.md'.")
    revision: Optional[int] = Field(
        None,
        description="Journal revision id to revert to (the state BEFORE that revision). "
        "Omit to undo the most recent change.",
    )


@on_function_call(
    "Revert one of Muika's self-modifications to how it was before. "
    "Useful when a change to herself did not feel right.",
    params=SelfRevertParams,
)
async def self_revert(path: str, revision: Optional[int] = None) -> str:
    if _disabled():
        return _DISABLED_MSG

    try:
        resolved = resolve_self_path(path, require_write=True)
        if _is_plugin_path(resolved):
            return await get_plugin_deployer().revert(path, revision_id=revision)
        return await get_self_mod_manager().revert(path, revision_id=revision)
    except SelfModError as e:
        return str(e)
    except Exception as e:
        logger.error(f"[SelfEdit] Unexpected error reverting {path!r}: {e}")
        return f"Unexpected error: {e}"


def _resolve_template(name: str) -> Path:
    """遍历 SEARCH_PATH 定位模板文件。"""
    for search_dir in SEARCH_PATH:
        candidate = Path(str(search_dir)) / name
        if candidate.is_file():
            return candidate

    raise FileNotFoundError(f"Template {name!r} not found in search path: {[str(p) for p in SEARCH_PATH]}")


def _persist_persona_to_env(name: str) -> None:
    """将 persona_template 回写到 .env 文件，使其跨重启持久化。"""
    env_path = Path(".env")
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []

    new_lines: list[str] = []
    found = False
    for line in lines:
        if line.strip().startswith("PERSONA_TEMPLATE"):
            new_lines.append(f"PERSONA_TEMPLATE={name}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"PERSONA_TEMPLATE={name}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


class PersonaSwitchParams(BaseModel):
    template_name: str = Field(
        ...,
        description=(
            "Template filename to switch to, e.g. 'Muika.md.jinja2' or 'Muika' (extension auto-added). "
            "The template must exist in ./templates/ or muika/builtin_templates/. "
            "Use persona_list to see available templates."
        ),
    )


@on_function_call(
    "Switch Muika's active persona template. Validates the template first (Jinja2 syntax + trial render). "
    "Takes effect immediately and is persisted to .env so it survives restarts. "
    "Use persona_list to see available templates.",
    params=PersonaSwitchParams,
)
async def persona_switch(template_name: str) -> str:
    """切换人格模板：校验格式 → 更新配置 → 回写 .env → 立即生效。"""
    name = template_name.strip()
    if not name:
        return "template_name must not be empty."
    if not _TEMPLATE_NAME_RE.match(name):
        return f"Invalid template name {name!r}: only letters, digits, '_', '.', '-' are allowed (no path separators)."
    if not name.endswith((".j2", ".jinja2")):
        name += ".jinja2"

    try:
        source_path = _resolve_template(name)
        content = source_path.read_text(encoding="utf-8")
        validate_template(content)
    except FileNotFoundError as exc:
        return f"Template switch failed: {exc}"
    except OSError as exc:
        return f"Template switch failed: cannot read {name}: {exc}"
    except SelfModError as exc:
        return f"Template validation failed: {exc}"

    old_name = mas_config.persona_template
    mas_config.persona_template = name
    _persist_persona_to_env(name)
    logger.info(f"[Persona] Switched persona template: {old_name} -> {name} (source: {source_path})")

    return "Persona template switched successfully.\n"


@on_function_call(
    "List all available persona templates (override layer + built-in), marking the currently active one. "
    "Use this before persona_switch to see what templates are available.",
)
async def persona_list() -> str:
    """列出所有可用的人格模板文件，标注当前激活项。"""
    current = mas_config.persona_template
    if not current.endswith((".j2", ".jinja2")):
        current += ".jinja2"

    lines = ["Available persona templates:"]
    found_any = False

    override_dir = Path("./templates").resolve()
    if override_dir.is_dir():
        for p in sorted(override_dir.glob("*.jinja2")):
            marker = " ← ACTIVE" if p.name == current else ""
            lines.append(f"  [override] {p.name}{marker}")
            found_any = True

    builtin_dir = Path(str(SEARCH_PATH[-1])) if len(SEARCH_PATH) > 1 else None
    if builtin_dir and builtin_dir.is_dir():
        for p in sorted(builtin_dir.glob("*.jinja2")):
            marker = " ← ACTIVE" if p.name == current else ""
            lines.append(f"  [builtin]  {p.name}{marker}")
            found_any = True

    if not found_any:
        lines.append("  (no templates found)")

    return "\n".join(lines)
