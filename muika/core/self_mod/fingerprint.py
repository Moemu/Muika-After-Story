"""运行代码指纹：为内核与插件内容计算可对比的哈希快照。

指纹描述"磁盘上的定义"，只在启动时与运行中的代码保证一致；运行期的
核心代码编辑在重启前对感知不可见。这是自我变更感知（``muika.core.self_change``）
的检测基础，与 Core 提案体系的 :meth:`CoreProposalManager.workspace_fingerprint`
互不复用——后者的范围包含测试工作区，且只输出整体摘要。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Dict, Optional

import muika

if TYPE_CHECKING:
    from muika.plugin.models import Plugin

_KERNEL_SUFFIXES = {".py"}
_TEMPLATE_DIR = "builtin_templates"
_TEMPLATE_SUFFIXES = {".jinja2"}
_PLUGIN_SUFFIXES = {".py", ".json", ".yaml", ".yml", ".toml"}


def kernel_root() -> Path:
    """返回实际被 import 的 muika 包根目录。"""
    return Path(muika.__file__).resolve().parent


def _sha256_file(path: Path) -> str:
    """计算单个文件的 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_excluded(rel_parts: tuple[str, ...]) -> bool:
    """排除缓存目录与隐藏目录。"""
    return any(part == "__pycache__" or part.startswith(".") for part in rel_parts)


def compute_kernel_files() -> Dict[str, str]:
    """计算内核指纹表：包内相对路径 -> 文件 SHA-256。

    范围为包内全部 ``.py`` 源码与 ``builtin_templates`` 下的人格模板；
    排除 ``__pycache__`` 等缓存产物。路径以包根为基准，venv 挪动不影响结果。
    """
    root = kernel_root()
    files: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root)
        if _is_excluded(rel.parts):
            continue
        suffix = path.suffix.lower()
        in_templates = rel.parts[0] == _TEMPLATE_DIR if rel.parts else False
        if suffix in _KERNEL_SUFFIXES or (in_templates and suffix in _TEMPLATE_SUFFIXES):
            files[rel.as_posix()] = _sha256_file(path)
    return files


def plugin_source_files(module: ModuleType) -> list[Path]:
    """返回插件包实际占据的源码与声明文件集合。

    包插件取 ``__path__`` 指向的目录扫描；单文件插件取 ``__file__``。
    扫描范围是包内源码集合，可能包含尚未被 import 的新文件——感知措辞
    以"重载成功"为限，不得据此声称未加载代码的行为已生效。
    """
    package_dirs = list(getattr(module, "__path__", None) or [])
    if package_dirs:
        root = Path(package_dirs[0]).resolve()
        files: list[Path] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if _is_excluded(path.relative_to(root).parts):
                continue
            if path.suffix.lower() in _PLUGIN_SUFFIXES:
                files.append(path)
        return files
    file_path = getattr(module, "__file__", None)
    return [Path(file_path)] if file_path else []


def compute_plugin_digest(plugins: Dict[str, "Plugin"]) -> Dict[str, str]:
    """计算已加载插件的内容聚合指纹：``package_name -> SHA-256``。

    :param plugins: ``package_name -> Plugin`` 映射，推荐传入公共
        :func:`muika.plugin.loader.get_plugins` 的结果
    """
    digests: Dict[str, str] = {}
    for package_name, plugin in plugins.items():
        digest = hashlib.sha256()
        files = plugin_source_files(plugin.module)
        if not files:
            continue
        base = files[0].parent
        for path in files:
            digest.update(path.relative_to(base).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        digests[package_name] = digest.hexdigest()
    return digests


def compare_versions(left: str, right: str) -> Optional[int]:
    """尽力比较两个点分版本号；无法比较时返回 ``None``。

    只解析前导的数字段（``1.5.5.dev13+g...`` 取 ``(1, 5, 5)``），
    任一侧没有数字前缀（如 ``Unknown``）时返回 ``None``，调用方应退回
    "更新"语域而非臆断方向。
    """

    def parse(version: str) -> Optional[tuple[int, ...]]:
        parts: list[int] = []
        for part in version.strip().split("."):
            if part.isdigit():
                parts.append(int(part))
            else:
                break
        return tuple(parts) if parts else None

    parsed_left, parsed_right = parse(left), parse(right)
    if parsed_left is None or parsed_right is None:
        return None
    width = max(len(parsed_left), len(parsed_right))
    left_padded = parsed_left + (0,) * (width - len(parsed_left))
    right_padded = parsed_right + (0,) * (width - len(parsed_right))
    return (left_padded > right_padded) - (left_padded < right_padded)
