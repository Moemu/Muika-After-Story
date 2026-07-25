from __future__ import annotations

from pydantic import BaseModel, Field

from muika.plugin.func_call import on_function_call
from muika.plugin.skills import get_skill_manager
from muika.utils.logger import logger

_MAX_SKILL_CHARS = 20000
"""SKILL.md 返回内容的最大字符数，超出部分截断"""


class LoadSkillParams(BaseModel):
    skill_name: str = Field(
        ...,
        description="Exact skill name as listed in the 'Available skills' section of the system prompt.",
    )


@on_function_call(
    "Load the full instructions (SKILL.md) of a named skill.",
    params=LoadSkillParams,
)
async def load_skill(skill_name: str):
    manager = get_skill_manager()
    skill = manager.get(skill_name)

    if skill is None:
        available = ", ".join(s.name for s in manager.skills) or "(none)"
        return f"Skill {skill_name!r} not found. Available skills: {available}"

    try:
        text = skill.location.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return (
            f"Skill file was removed after scanning: {skill.location}. "
            "It will disappear from the registry on the next rescan."
        )
    except Exception as e:
        logger.error(f"[LoadSkill] Failed to read {skill.location}: {e}")
        return f"Error reading skill: {e}"

    truncated = ""
    if len(text) > _MAX_SKILL_CHARS:
        text = text[:_MAX_SKILL_CHARS]
        truncated = f"\n\n[LoadSkill] Content truncated to {_MAX_SKILL_CHARS} characters."

    logger.debug(f"[LoadSkill] Loaded skill '{skill.name}' from {skill.location}")
    return (
        f"Skill: {skill.name}\n"
        f"Skill file: {skill.location}\n"
        f"Files referenced by this skill live relative to: {skill.location.parent}\n"
        "Use the read_file tool with paths under that directory to read them "
        "(the directory must be included in fs_allowed_paths).\n\n"
        f"{text}{truncated}"
    )
