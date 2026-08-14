"""自我修改写入前的内容校验器：按文件类型分派。"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

from muika.config import mas_config

from .policy import SelfModError

_MAX_CONTENT_CHARS = 512 * 1024
"""单次自我写入的内容上限（字符数）。"""

_DANGEROUS_CALL_PATTERNS = ("os.system", "os.popen", "subprocess")
"""除 import 黑名单外，按属性链特征检测的危险调用。"""


def validate_content(path: Path, content: str) -> None:
    """按文件后缀分派校验，校验失败抛出 :class:`SelfModError`。

    :param path: 目标文件路径（仅用于选择校验器）
    :param content: 待写入的完整文件内容
    """
    if len(content) > _MAX_CONTENT_CHARS:
        raise SelfModError(f"Content too large ({len(content):,} chars, limit {_MAX_CONTENT_CHARS:,}).")

    suffix = path.suffix.lower()
    if suffix in (".jinja2", ".j2"):
        validate_template(content)
    elif suffix in (".yml", ".yaml"):
        _validate_yaml_syntax(content)
    elif suffix == ".py":
        validate_python(content)
    # .md 与其他文本类型直接放行


def validate_template(content: str) -> None:
    """校验 Jinja2 人格模板：语法检查 + 用最小数据试渲染。"""
    # 延迟导入，避免 loader <-> self_mod 模块级循环依赖

    from muika.core.state import MuikaState
    from muika.template.loader import SEARCH_PATH
    from muika.template.model import PromptTemplatesData

    env = Environment(loader=FileSystemLoader(SEARCH_PATH), autoescape=True)

    try:
        env.parse(content)
    except Exception as e:
        raise SelfModError(f"Jinja2 syntax error: {e}") from e

    try:
        data = PromptTemplatesData(event_type="self_check", state=MuikaState(), is_chat=True)
        env.from_string(content).render(data.model_dump())
    except SelfModError:
        raise
    except Exception as e:
        raise SelfModError(f"Template renders but fails with prompt data: {e}") from e


def _validate_yaml_syntax(content: str) -> dict:
    """仅做 YAML 语法解析，返回解析结果。"""
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise SelfModError(f"YAML syntax error: {e}") from e
    if not isinstance(data, dict):
        raise SelfModError("YAML root must be a mapping.")
    return data


def validate_topics(content: str) -> None:
    """校验 topics.yml 结构：顶层含 topics 列表，条目字段完整且 id 唯一。"""
    data = _validate_yaml_syntax(content)

    topics = data.get("topics")
    if not isinstance(topics, list) or not topics:
        raise SelfModError("topics.yml must contain a non-empty 'topics' list.")

    seen_ids: set[str] = set()
    for i, entry in enumerate(topics):
        if not isinstance(entry, dict):
            raise SelfModError(f"Topic #{i} is not a mapping.")
        topic_id = entry.get("id")
        concept = entry.get("concept", entry.get("content"))
        if not topic_id or not str(topic_id).strip():
            raise SelfModError(f"Topic #{i} is missing a non-empty 'id'.")
        if not concept or not str(concept).strip():
            raise SelfModError(f"Topic {topic_id!r} is missing a non-empty 'concept'.")
        if topic_id in seen_ids:
            raise SelfModError(f"Duplicate topic id: {topic_id!r}.")
        seen_ids.add(topic_id)


def validate_python(content: str) -> None:
    """校验插件源码：AST 语法检查 + 黑名单 import 扫描 + 危险调用特征检测。"""
    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        raise SelfModError(f"Python syntax error: line {e.lineno}: {e.msg}") from e

    blacklist = set(mas_config.plugin_import_blacklist or [])
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in blacklist:
                    violations.append(f"line {node.lineno}: blacklisted import '{alias.name}'")
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                if top in blacklist:
                    violations.append(f"line {node.lineno}: blacklisted import 'from {node.module}'")
        elif isinstance(node, ast.Call):
            call_text = _dotted_name(node.func)
            if call_text and (
                call_text in ("eval", "exec", "compile", "os.system", "os.popen", "os.execv")
                or call_text.startswith(_DANGEROUS_CALL_PATTERNS)
            ):
                violations.append(f"line {node.lineno}: dangerous call '{call_text}(...)'")

    if violations:
        raise SelfModError("Static check rejected this plugin:\n" + "\n".join(f"  - {v}" for v in violations))


def _dotted_name(node: ast.expr) -> str:
    """将 Name/Attribute 调用目标还原为点分名称，非名称调用返回空串。"""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""
