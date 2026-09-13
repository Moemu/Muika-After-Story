"""自我修改沙箱策略：路径白名单 + deny-first 受保护清单。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from muika.config import mas_config

PROTECTED_PREFIXES: tuple[str, ...] = (
    "muika/",
    "muika_bot/",
    "bot.py",
    "core_main.py",
    "alembic.ini",
    ".env",
    "launcher/",
    "configs/models.yml",
    "muika/migrations/",
)
"""受保护路径清单（deny-first）。任何命中项无条件拒绝，优先级高于白名单。"""

_SANDBOX_PATHS: tuple[str, ...] = ("./templates", "./configs/skills")
"""自我编辑沙箱（文件或目录）"""

_READ_ONLY_ROOTS: tuple[Path, ...] = (
    Path(__file__).resolve().parents[2] / "builtin_templates",
    Path(__file__).resolve().parents[2] / "builtin_skills" / "muika-self",
    Path(__file__).resolve().parents[2] / "template" / "model.py",
)
"""自我修改指南可以观察的包内资源。"""

_EXCLUDED_PLUGIN_SUBDIRS = ("_quarantine", "_staging")
"""plugins/ 沙箱内不开放给自我编辑的子目录。"""


class SelfModError(Exception):
    """自我修改被策略或校验拒绝时抛出，消息可直接展示给 LLM。"""


SelfModLayer = Literal["template", "skill", "topic", "plugin", "other"]


def _project_root() -> Path:
    """返回当前工作目录（Core 进程的项目根）。"""
    return Path.cwd()


def _runtime_source_root() -> Path:
    """返回当前运行的 MAS 源码根目录。"""
    return Path(__file__).resolve().parents[3]


def is_protected_path(resolved: Path) -> bool:
    """判断解析后的路径是否命中受保护清单。

    同时拒绝"位于受保护路径内"与"包含受保护路径"（如项目根目录本身）两种情况。
    """
    data = mas_config.data_dir.resolve()
    controls = [data / name for name in ("reviews", "core_proposals", "restart.json", "agent_tasks", "agent_processes")]
    controls.append(Path(mas_config.self_mod_backup_dir).resolve())
    if any(resolved == path or path in resolved.parents or resolved in path.parents for path in controls):
        return True
    for root in {_project_root(), _runtime_source_root()}:
        for prefix in PROTECTED_PREFIXES:
            p = Path(prefix)
            protected = (p if p.is_absolute() else root / p).resolve()
            if resolved == protected or protected in resolved.parents or resolved in protected.parents:
                return True
    return False


def allowed_roots(include_read_only: bool = False) -> list[Path]:
    """解析当前生效的沙箱为绝对路径列表。"""
    paths: list[str] = list(_SANDBOX_PATHS)
    if mas_config.can_self_modify or include_read_only:
        paths.append(mas_config.plugins_dir)
    roots = [Path(p).resolve() for p in paths if p]
    if include_read_only:
        roots.extend(_READ_ONLY_ROOTS)
    return roots


def resolve_self_path(
    raw_path: str,
    require_write: bool = False,
) -> Path:
    """解析并校验自我编辑路径，拒绝越界访问。

    :param raw_path: LLM 传入的相对或绝对路径
    :param require_write: 是否要求自我修改权限
    :return: 解析后的绝对路径
    :raises SelfModError: 路径非法、命中保护清单或不在白名单内
    """
    if require_write and not mas_config.can_self_modify:
        raise SelfModError("Self-modification is disabled by configuration.")

    try:
        resolved = Path(raw_path).resolve()
    except Exception as e:
        raise SelfModError(f"Invalid path {raw_path!r}: {e}") from e

    read_only_match = not require_write and any(
        resolved == root or root in resolved.parents for root in _READ_ONLY_ROOTS
    )
    if is_protected_path(resolved) and not read_only_match:
        raise SelfModError(f"Access denied: {resolved} is protected core code and can never be self-modified.")

    roots = allowed_roots(include_read_only=not require_write)
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise SelfModError(
            f"Access denied: {resolved} is outside the self-edit sandbox. " f"Allowed: {[str(p) for p in roots]}"
        )

    for part in resolved.parts:
        if part in _EXCLUDED_PLUGIN_SUBDIRS:
            raise SelfModError(f"Access denied: {resolved} is inside the plugin {part} area.")

    return resolved


def is_self_path(resolved: Path) -> bool:
    """识别人格、技能、话题和插件目录及其父目录。"""
    roots = [Path(p).resolve() for p in (*_SANDBOX_PATHS, mas_config.plugins_dir, "configs/topics.yml")]
    return any(resolved == root or root in resolved.parents or resolved in root.parents for root in roots)


def display_path(resolved: Path) -> str:
    """返回用于审计与展示的相对路径（无法相对化时退回绝对路径）。"""
    try:
        return resolved.relative_to(_project_root()).as_posix()
    except ValueError:
        return str(resolved)


def infer_layer(resolved: Path) -> SelfModLayer:
    """根据路径推断自我修改所属层级。"""
    rel = display_path(resolved)
    if rel.startswith("templates/"):
        return "template"
    if rel.startswith("configs/skills/"):
        return "skill"
    if rel == "configs/topics.yml":
        return "topic"
    if rel.startswith("plugins/"):
        return "plugin"
    return "other"
