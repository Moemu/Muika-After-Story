"""自我修改沙箱策略：路径白名单 + deny-first 受保护清单。"""

from __future__ import annotations

from pathlib import Path

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

_SANDBOX_PATHS: tuple[str, ...] = ("./templates", "./skills")
"""自我编辑沙箱（文件或目录）"""

_EXCLUDED_PLUGIN_SUBDIRS = ("_quarantine", "_staging")
"""plugins/ 沙箱内不开放给自我编辑的子目录。"""


class SelfModError(Exception):
    """自我修改被策略或校验拒绝时抛出，消息可直接展示给 LLM。"""


def _project_root() -> Path:
    """返回当前工作目录（Core 进程的项目根）。"""
    return Path.cwd()


def is_protected_path(resolved: Path) -> bool:
    """判断解析后的路径是否命中受保护清单。

    同时拒绝"位于受保护路径内"与"包含受保护路径"（如项目根目录本身）两种情况。
    """
    root = _project_root()
    for prefix in PROTECTED_PREFIXES:
        p = Path(prefix)
        protected = (p if p.is_absolute() else root / p).resolve()
        if resolved == protected or protected in resolved.parents or resolved in protected.parents:
            return True
    return False


def allowed_roots() -> list[Path]:
    """解析当前生效的沙箱为绝对路径列表。"""
    paths: list[str] = list(_SANDBOX_PATHS)
    if mas_config.enable_plugin_self_modification:
        paths.append(mas_config.plugins_dir)
    return [Path(p).resolve() for p in paths if p]


def resolve_self_path(
    raw_path: str,
    require_write: bool = False,
) -> Path:
    """解析并校验自我编辑路径，拒绝越界访问。

    :param raw_path: LLM 传入的相对或绝对路径
    :param require_write: 是否要求写权限（当前沙箱读写同权，保留参数以镜像文件系统工具语义）
    :return: 解析后的绝对路径
    :raises SelfModError: 路径非法、命中保护清单或不在白名单内
    """
    if not mas_config.enable_self_modification:
        raise SelfModError("Self-modification is disabled by configuration.")

    try:
        resolved = Path(raw_path).resolve()
    except Exception as e:
        raise SelfModError(f"Invalid path {raw_path!r}: {e}") from e

    if is_protected_path(resolved):
        raise SelfModError(f"Access denied: {resolved} is protected core code and can never be self-modified.")

    roots = allowed_roots()
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise SelfModError(
            f"Access denied: {resolved} is outside the self-edit sandbox. " f"Allowed: {[str(p) for p in roots]}"
        )

    # plugins/ 沙箱内的管理子目录不开放
    for part in resolved.parts:
        if part in _EXCLUDED_PLUGIN_SUBDIRS:
            raise SelfModError(f"Access denied: {resolved} is inside the plugin {part} area.")

    return resolved


def display_path(resolved: Path) -> str:
    """返回用于审计与展示的相对路径（无法相对化时退回绝对路径）。"""
    try:
        return resolved.relative_to(_project_root()).as_posix()
    except ValueError:
        return str(resolved)


def infer_layer(resolved: Path) -> str:
    """根据路径推断自我修改所属层级。"""
    rel = display_path(resolved)
    if rel.startswith("templates/"):
        return "template"
    if rel.startswith("skills/"):
        return "skill"
    if rel.startswith("plugins/"):
        return "plugin"
    return "other"
