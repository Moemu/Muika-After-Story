"""自我编辑工具：查看可编辑清单、回滚历史版本与切换人格模板。

日常编辑走通用文件工具（write_file/edit_file 按 self 路径自动路由到校验与
日志管线）；版本化改动走 ``_workspace`` 的工作区。修改指南内嵌在
``muika/builtin_skills/muika-self`` 技能文档中。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from muika.config import mas_config
from muika.core.self_mod import SelfModError, get_self_mod_manager
from muika.core.self_mod.plugin_deployer import get_plugin_deployer
from muika.core.self_mod.policy import allowed_roots, display_path, resolve_self_path
from muika.core.self_mod.validators import validate_template
from muika.llm.utils.tools import ToolError
from muika.plugin.func_call import on_function_call
from muika.template.loader import SEARCH_PATH
from muika.utils.logger import logger

_TEMPLATE_NAME_RE = re.compile(r"^[\w.\-]+$")
"""模板文件名只允许字母/数字/下划线/点/连字符，禁止路径分隔符。"""


def _is_plugin_path(path: Path) -> bool:
    """检查路径是否位于插件目录内。"""
    plugins_dir = Path(mas_config.plugins_dir).resolve()
    return path == plugins_dir or plugins_dir in path.parents


def _list_sandbox_files() -> str:
    """列出沙箱内所有可编辑文件，供 self_read 空路径时展示。"""
    lines = ["These are the parts of yourself you can read and edit:"]
    for root in allowed_roots(include_read_only=True):
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
        "\nPersona templates (templates/*.jinja2) and your core code are edited with the ordinary file tools "
        "(write_file / edit_file with a reason). For versioned core changes, open a workspace with "
        "self_workspace_open. Guidance lives in the load_skill('muika-self') skill."
    )
    lines.append(
        "Topic seeds are NOT edited as a file. Use topic_list / topic_add / topic_update / topic_delete instead."
    )
    return "\n".join(lines)


class SelfReadParams(BaseModel):
    path: str = Field(
        "",
        description=(
            "'.history' shows your recent self-modification journal (append a file path to filter). "
            "Empty string: list everything you can edit. "
            "File contents are read with the ordinary read_file tool."
        ),
    )


@on_function_call(
    "List Muika's own editable self (persona overrides, skills, plugins) and her journal of past "
    "self-modifications. This is how Muika looks at herself before deciding to change.",
    params=SelfReadParams,
    read_only=True,
)
async def self_read(path: str = "") -> str:
    manager = get_self_mod_manager()

    stripped = path.strip()
    if not stripped:
        return _list_sandbox_files()

    if stripped == ".history" or stripped.startswith(".history "):
        target = stripped[len(".history") :].strip()
        try:
            return await manager.history(target, limit=15)
        except SelfModError as e:
            return ToolError(str(e))

    return ToolError("self_read lists your editable self and your journal; read file contents with read_file.")


class SelfRevertParams(BaseModel):
    path: str = Field(..., description="The file to revert, e.g. 'configs/skills/muika-self/SKILL.md'.")
    revision: Optional[int] = Field(
        None,
        description="Journal revision id to revert to (the state BEFORE that revision). "
        "Omit to undo the most recent change.",
    )


@on_function_call(
    "Revert one of Muika's self-modifications to how it was before. "
    "Useful when a change to herself did not feel right; staged plugin candidates are discarded instead.",
    params=SelfRevertParams,
)
async def self_revert(path: str, revision: Optional[int] = None) -> str:
    if not mas_config.can_self_modify:
        return ToolError("Self-modification is disabled by configuration.")

    try:
        resolved = resolve_self_path(path, require_write=True)
        if _is_plugin_path(resolved):
            return await get_plugin_deployer().revert(path, revision_id=revision)
        return await get_self_mod_manager().revert(path, revision_id=revision)
    except SelfModError as e:
        return ToolError(str(e))
    except Exception as e:
        logger.error(f"[SelfEdit] Unexpected error reverting {path!r}: {e}")
        return ToolError(f"Unexpected error: {e}")


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
    if not mas_config.can_self_modify:
        return ToolError("Self-modification is disabled by configuration.")

    name = template_name.strip()
    if not name:
        return "template_name must not be empty."
    if not _TEMPLATE_NAME_RE.match(name):
        return ToolError(
            f"Invalid template name {name!r}: only letters, digits, '_', '.', '-' are allowed (no path separators)."
        )
    if not name.endswith((".j2", ".jinja2")):
        name += ".jinja2"

    try:
        source_path = _resolve_template(name)
        content = source_path.read_text(encoding="utf-8")
        validate_template(content)
    except FileNotFoundError as exc:
        return ToolError(f"Template switch failed: {exc}")
    except OSError as exc:
        return ToolError(f"Template switch failed: cannot read {name}: {exc}")
    except SelfModError as exc:
        return ToolError(f"Template validation failed: {exc}")

    old_name = mas_config.persona_template
    mas_config.persona_template = name
    _persist_persona_to_env(name)
    logger.debug(f"[Persona] Switched persona template: {old_name} -> {name} (source: {source_path})")
    logger.info(f"Persona changed: {old_name} -> {name}")

    return "Persona template switched successfully.\n"


@on_function_call(
    "List all available persona templates (override layer + built-in), marking the currently active one. "
    "Use this before persona_switch to see what templates are available.",
    read_only=True,
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
