"""自写插件的手动激活工具。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from muika.core.self_mod import SelfModError
from muika.core.self_mod.plugin_deployer import get_plugin_deployer
from muika.plugin.func_call import on_function_call
from muika.utils.logger import logger


class PluginLoadParams(BaseModel):
    name: str = Field(..., description="Staged single-file plugin name without 'plugins/' or '.py'.")


@on_function_call(
    "Manually activate one validated plugin candidate from staging. "
    "Use this only after self_write or self_edit_confirm reports that the candidate is staged. "
    "Activation replaces the formal file. A failed activation restores the old plugin.",
    params=PluginLoadParams,
)
async def plugin_load(name: str) -> str:
    """手动激活已验证的单文件插件候选。"""
    try:
        return await get_plugin_deployer().activate(name.strip())
    except SelfModError as exc:
        return f"Plugin activation was rejected: {exc}"
    except Exception as exc:
        logger.error(f"[PluginTool] Unexpected activation error for {name!r}: {exc}")
        return f"Unexpected plugin activation error: {exc}"
