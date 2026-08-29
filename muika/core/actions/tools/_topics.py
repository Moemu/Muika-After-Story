"""话题库管理工具：Muika 以结构化方式查询/新增/修改/删除单条话题种子。

话题库文件（``configs/topics.yml``）由这些工具以文本块手术的方式维护：
LLM 只提供字段值，YAML 结构与其余内容绝不经过 LLM 转写，避免整文件覆写
造成的内容丢失与格式劣化。变更会校验、原子写入、重载并记录审计。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Optional

import yaml
from pydantic import BaseModel, Field

from muika.config import mas_config
from muika.core.self_mod import SelfModError
from muika.core.self_mod.manager import get_self_mod_manager
from muika.core.self_mod.validators import validate_topics
from muika.core.topic_manager import BUILTIN_TOPICS_PATH, TOPICS_PATH, get_topic_manager
from muika.plugin.func_call import on_function_call
from muika.utils.logger import logger

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
"""id 必须以小写字母开头，仅含小写字母/数字/下划线，最长 64 字符。"""
_NAME_RE = re.compile(r"^[a-z_]{1,32}$")
_TAG_RE = re.compile(r"^[\w\-]{1,32}$")
_ENTRY_ID_RE = re.compile(r"^  - id: (.+?)\s*$", re.MULTILINE)
"""话题条目起始行（topics.yml 采用两空格缩进的统一格式）。"""

_YAML_RESERVED = frozenset({"yes", "no", "true", "false", "on", "off", "null", "none", "nan", "inf", "y", "n"})
"""YAML 会将其解释为布尔/空/数值的标量，作为纯字符串 id 使用时必须用引号包裹。"""

_SPAN_LOOKUP_ERROR = (
    "Topic {id!r} was found in the YAML data but its entry block cannot be located"
    " — the file may have been hand-edited with non-standard indentation."
    " Please undo the manual edit and use topic_* tools instead."
)
"""YAML 解析成功但行号定位失败时的统一报错文案。"""

_TOPICS_LOCK = asyncio.Lock()
"""话题库文件的进程内读写锁，防止并发的 topic_add / topic_update / topic_delete 互相覆盖。"""

_DISABLED_MSG = "Self-modification is disabled by configuration."


def _read_topics_text() -> str:
    """读取话题库原文。"""
    path = TOPICS_PATH if TOPICS_PATH.is_file() else BUILTIN_TOPICS_PATH
    return path.read_text(encoding="utf-8")


def _parse_topics(text: str) -> list[dict]:
    """解析话题条目列表；文件结构异常时拒绝继续。"""
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or not isinstance(data.get("topics"), list):
        raise SelfModError("topics.yml is malformed; refusing to modify it until it is fixed.")
    return data["topics"]


def _entry_spans(text: str) -> list[tuple[str, int, int]]:
    """定位每个话题条目的文本区间（含到下一条目前为止的尾随空行）。

    :return: ``(topic_id, start, end)`` 列表
    """
    matches = list(_ENTRY_ID_RE.finditer(text))
    spans = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        raw_id = m.group(1).strip().strip('"')
        spans.append((raw_id, m.start(), end))
    return spans


def _yaml_scalar(text: str) -> str:
    """返回 YAML 标量表示：安全时用 plain 风格，否则用 JSON 双引号（合法 YAML）。"""
    stripped = text.strip()
    if stripped and "\n" not in stripped:
        try:
            if yaml.safe_load(stripped) == stripped:
                return stripped
        except yaml.YAMLError:
            pass
    return json.dumps(text, ensure_ascii=False)


def _format_entry(topic_id: str, category: str, concept: str, tags: list[str], cooldown_days: int) -> str:
    """按 topics.yml 统一格式生成单个话题条目的文本块。"""
    lines = [
        f"  - id: {_yaml_scalar(topic_id)}",
        f"    category: {category}",
        f"    concept: {_yaml_scalar(concept)}",
    ]
    if tags:
        lines.append("    tags:")
        lines.extend(f"      - {t}" for t in tags)
    lines.append(f"    cooldown_days: {int(cooldown_days)}")
    return "\n".join(lines) + "\n"


def _parse_tags(raw: str) -> list[str]:
    """把逗号/空格分隔的标签字符串解析并校验为列表。"""
    tags = [t.strip() for t in re.split(r"[,，\s]+", raw.strip()) if t.strip()]
    for tag in tags:
        if not _TAG_RE.match(tag):
            raise SelfModError(f"Invalid tag {tag!r}: only letters, digits, '_' and '-' are allowed (max 32 chars).")
    return tags


async def _apply_topics_change(new_text: str, reason: str, action: str) -> str:
    """校验并写入用户话题库，随后刷新索引并记录审计。"""
    validate_topics(new_text)
    _atomic_write_topics(new_text)
    topic_manager = get_topic_manager()
    if topic_manager is not None:
        topic_manager.reload_store()
    await get_self_mod_manager().record_event(str(TOPICS_PATH), action, reason, source="self")
    logger.info(f"[Topics] topics.yml updated: {reason[:80]}")
    return f"Topic library updated. Reason: {reason}"


def _atomic_write_topics(content: str) -> None:
    """先写临时文件再原子替换，避免半写状态。"""
    TOPICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOPICS_PATH.parent / (TOPICS_PATH.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, TOPICS_PATH)


def _disabled_or_reason_missing(reason: str) -> Optional[str]:
    """公共门控检查，返回错误提示或 None。"""
    if not mas_config.enable_self_modification:
        return _DISABLED_MSG
    if not reason or not reason.strip():
        return "A non-empty 'reason' is required: every change to your topic library is journaled."
    return None


class TopicListParams(BaseModel):
    category: str = Field(
        "",
        description="Filter by category (relationship / philosophy / trivia / story / meta). Empty = all.",
    )
    keyword: str = Field("", description="Substring to search for in id / concept / tags.")
    limit: int = Field(30, description="Max number of entries to return.")


@on_function_call(
    "List topic seeds in Muika's topic library, optionally filtered by category or keyword. "
    "Use this to see what topics she already has before adding or changing one.",
    params=TopicListParams,
)
async def topic_list(category: str = "", keyword: str = "", limit: int = 30) -> str:
    try:
        entries = _parse_topics(_read_topics_text())
    except SelfModError as e:
        return str(e)
    except Exception as e:
        logger.error(f"[Topics] Failed to read topic library: {e}")
        return f"Failed to read topic library: {e}"

    kw = keyword.strip().lower()
    lines = []
    for entry in entries:
        if category.strip() and entry.get("category") != category.strip():
            continue
        if kw:
            haystack = " ".join(
                [str(entry.get("id", "")), str(entry.get("concept", "")), " ".join(entry.get("tags") or [])]
            ).lower()
            if kw not in haystack:
                continue
        lines.append(f"- {entry.get('id')} [{entry.get('category')}] {str(entry.get('concept', ''))[:90]}")
        if len(lines) >= max(1, limit):
            break

    if not lines:
        return f"Topic library has {len(entries)} topics, but none match the filter."
    return f"Topic library: {len(entries)} topics total, showing {len(lines)}.\n" + "\n".join(lines)


class TopicAddParams(BaseModel):
    id: str = Field(..., description="Unique snake_case id, e.g. 'octopus_three_hearts'. Lowercase letters/digits/_.")
    category: str = Field(..., description="One of: relationship / philosophy / trivia / story / meta.")
    concept: str = Field(..., description="The topic seed itself — one evocative sentence Muika can grow into a topic.")
    tags: str = Field("", description="Comma-separated tags, e.g. 'nature,curiosity'.")
    cooldown_days: int = Field(14, description="Days before this topic may be used again (1-365).")
    reason: str = Field(..., description="Why Muika wants this topic. Written into her self-modification journal.")


@on_function_call(
    "Add ONE new topic seed to Muika's topic library. Only provide the fields; the library file itself "
    "is maintained by the system, so nothing else can be damaged. Check existing topics with topic_list "
    "first to avoid duplicates.",
    params=TopicAddParams,
)
async def topic_add(
    id: str,
    category: str,
    concept: str,
    tags: str = "",
    cooldown_days: int = 14,
    reason: str = "",
) -> str:
    if gate := _disabled_or_reason_missing(reason):
        return gate

    topic_id = id.strip()
    if not _ID_RE.match(topic_id):
        return (
            f"Invalid id {topic_id!r}: must start with a lowercase letter, then only letters/digits/'_' (1-64 chars)."
        )
    if topic_id.lower() in _YAML_RESERVED:
        return f"Invalid id {topic_id!r}: this word has a special meaning in YAML and cannot be used as a topic id."
    if not _NAME_RE.match(category.strip()):
        return f"Invalid category {category!r}: use lowercase letters and '_'."
    if not concept.strip():
        return "'concept' must not be empty."
    if not 1 <= int(cooldown_days) <= 365:
        return "cooldown_days must be between 1 and 365."
    try:
        tag_list = _parse_tags(tags)
    except SelfModError as e:
        return str(e)

    async with _TOPICS_LOCK:
        try:
            text = _read_topics_text()
            entries = _parse_topics(text)
        except SelfModError as e:
            return str(e)
        except Exception as e:
            logger.error(f"[Topics] Failed to read topic library: {e}")
            return f"Failed to read topic library: {e}"

        if any(entry.get("id") == topic_id for entry in entries):
            return f"Topic id {topic_id!r} already exists. Choose another id, or use topic_update to change it."

        block = _format_entry(topic_id, category.strip(), concept.strip(), tag_list, cooldown_days)
        new_text = text.rstrip("\n") + "\n\n" + block

        try:
            await _apply_topics_change(new_text, reason.strip(), "topic_add")
        except SelfModError as e:
            return f"The new topic was rejected: {e}"

    logger.info(f"[Topics] Added topic {topic_id!r}")
    return (
        f"Topic added to the library (now {len(entries) + 1} topics total). It is already available "
        f"for proactive conversations.\n{block.rstrip()}"
    )


class TopicUpdateParams(BaseModel):
    id: str = Field(..., description="The id of the topic to change.")
    concept: str = Field("", description="New concept text. Empty = keep the current one.")
    category: str = Field("", description="New category. Empty = keep the current one.")
    tags: str = Field("", description="New comma-separated tags. Empty = keep the current ones.")
    cooldown_days: Optional[int] = Field(None, description="New cooldown in days. Omit = keep the current one.")
    reason: str = Field(..., description="Why Muika wants this change. Written into her self-modification journal.")


@on_function_call(
    "Change ONE existing topic seed in Muika's topic library (its concept, category, tags or cooldown). "
    "Only the given fields are replaced; everything else stays untouched.",
    params=TopicUpdateParams,
)
async def topic_update(
    id: str,
    concept: str = "",
    category: str = "",
    tags: str = "",
    cooldown_days: Optional[int] = None,
    reason: str = "",
) -> str:
    if gate := _disabled_or_reason_missing(reason):
        return gate

    topic_id = id.strip()
    async with _TOPICS_LOCK:
        try:
            text = _read_topics_text()
            entries = _parse_topics(text)
        except SelfModError as e:
            return str(e)
        except Exception as e:
            logger.error(f"[Topics] Failed to read topic library: {e}")
            return f"Failed to read topic library: {e}"

        current = next((e for e in entries if e.get("id") == topic_id), None)
        if current is None:
            return f"Topic {topic_id!r} not found. Use topic_list to see available ids."

        new_concept = concept.strip() or str(current.get("concept", ""))
        new_category = (category.strip() or str(current.get("category", "misc"))).lower()
        new_cooldown = cooldown_days if cooldown_days is not None else int(current.get("cooldown_days", 7))
        try:
            new_tags = _parse_tags(tags) if tags.strip() else list(current.get("tags") or [])
        except SelfModError as e:
            return str(e)

        if not new_concept:
            return "'concept' cannot end up empty."
        if not _NAME_RE.match(new_category):
            return f"Invalid category {new_category!r}: use lowercase letters and '_'."
        if not 1 <= int(new_cooldown) <= 365:
            return "cooldown_days must be between 1 and 365."

        spans_dict = dict((tid, (start, end)) for tid, start, end in _entry_spans(text))
        if topic_id not in spans_dict:
            return _SPAN_LOOKUP_ERROR.format(id=topic_id)
        start, end = spans_dict[topic_id]
        block = _format_entry(topic_id, new_category, new_concept, new_tags, new_cooldown)
        separator = "\n" if end < len(text) else ""
        new_text = text[:start] + block + separator + text[end:]

        try:
            await _apply_topics_change(new_text, reason.strip(), "topic_update")
        except SelfModError as e:
            return f"The change was rejected: {e}"

    logger.info(f"[Topics] Updated topic {topic_id!r}")
    return f"Topic {topic_id!r} updated. The new version is already active.\n{block.rstrip()}"


class TopicDeleteParams(BaseModel):
    id: str = Field(..., description="The id of the topic to remove.")
    reason: str = Field(..., description="Why Muika wants to remove it. Written into her self-modification journal.")


@on_function_call(
    "Remove ONE topic seed from Muika's topic library. If she changes her mind later, "
    "she can simply topic_add it again.",
    params=TopicDeleteParams,
)
async def topic_delete(id: str, reason: str = "") -> str:
    if gate := _disabled_or_reason_missing(reason):
        return gate

    topic_id = id.strip()
    async with _TOPICS_LOCK:
        try:
            text = _read_topics_text()
            entries = _parse_topics(text)
        except SelfModError as e:
            return str(e)
        except Exception as e:
            logger.error(f"[Topics] Failed to read topic library: {e}")
            return f"Failed to read topic library: {e}"

        if not any(e.get("id") == topic_id for e in entries):
            return f"Topic {topic_id!r} not found. Use topic_list to see available ids."

        spans_dict = dict((tid, (start, end)) for tid, start, end in _entry_spans(text))
        if topic_id not in spans_dict:
            return _SPAN_LOOKUP_ERROR.format(id=topic_id)
        start, end = spans_dict[topic_id]
        new_text = (text[:start] + text[end:]).rstrip("\n") + "\n"

        try:
            await _apply_topics_change(new_text, reason.strip(), "topic_delete")
        except SelfModError as e:
            return f"The deletion was rejected: {e}"

    logger.info(f"[Topics] Deleted topic {topic_id!r}")
    return f"Topic {topic_id!r} removed from the library ({len(entries) - 1} topics remaining)."
