from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import Field

from muika.plugin.skills import get_skill_manager
from muika.utils.logger import logger

from ..schema import ActionOutput
from ._base import BaseTool

if TYPE_CHECKING:
    from muika.core.executor import Executor
    from muika.core.state import MuikaState

_MAX_SKILL_CHARS = 20000
"""SKILL.md 返回内容的最大字符数，超出部分截断"""


class LoadSkillTool(BaseTool):
    """Load the full instructions (SKILL.md) of a named skill."""

    name: Literal["load_skill"] = "load_skill"
    skill_name: str = Field(
        ...,
        description="Exact skill name as listed in the 'Available skills' section of the system prompt.",
    )

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        manager = get_skill_manager()
        skill = manager.get(self.skill_name)

        if skill is None:
            available = ", ".join(s.name for s in manager.skills) or "(none)"
            return ActionOutput(
                content=f"[LoadSkillTool] Skill {self.skill_name!r} not found. Available skills: {available}"
            )

        try:
            text = skill.location.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            # 扫描后文件被删除，等待监听触发重扫将其移出注册表
            return ActionOutput(
                content=f"[LoadSkillTool] Skill file was removed after scanning: {skill.location}. "
                "It will disappear from the registry on the next rescan."
            )
        except Exception as e:
            logger.error(f"[LoadSkillTool] Failed to read {skill.location}: {e}")
            return ActionOutput(content=f"[LoadSkillTool] Error reading skill: {e}")

        truncated = ""
        if len(text) > _MAX_SKILL_CHARS:
            text = text[:_MAX_SKILL_CHARS]
            truncated = f"\n\n[LoadSkillTool] Content truncated to {_MAX_SKILL_CHARS} characters."

        logger.debug(f"[LoadSkillTool] Loaded skill '{skill.name}' from {skill.location}")
        return ActionOutput(
            content=(
                f"Skill: {skill.name}\n"
                f"Skill file: {skill.location}\n"
                f"Files referenced by this skill live relative to: {skill.location.parent}\n"
                "Use the read_file tool with paths under that directory to read them "
                "(the directory must be included in fs_allowed_paths).\n\n"
                f"{text}{truncated}"
            )
        )
