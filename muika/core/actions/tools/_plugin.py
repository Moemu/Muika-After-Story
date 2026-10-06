"""自写插件的手动激活工具与插件清单查看。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from muika.core.self_mod import SelfModError
from muika.core.self_mod.plugin_deployer import get_plugin_deployer
from muika.llm.utils.tools import ToolError
from muika.plugin.func_call import on_function_call
from muika.plugin.loader import get_plugins
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
        return ToolError(f"Plugin activation was rejected: {exc}")
    except Exception as exc:
        logger.error(f"[PluginTool] Unexpected activation error for {name!r}: {exc}")
        return ToolError(f"Unexpected plugin activation error: {exc}")


@on_function_call(
    "List Muika's currently loaded plugins with their names and descriptions. "
    "Use this when you want to see what plugins you have installed, "
    "or to inspect what a newly appeared plugin does.",
    read_only=True,
)
async def plugin_inspect() -> str:
    """列出已加载插件的名称、描述与来源。"""
    entries = []
    for package_name, plugin in sorted(get_plugins().items()):
        meta = plugin.meta
        name = meta.name if meta else package_name
        description = (meta.description if meta else "").strip()
        builtin = " [builtin]" if package_name.startswith("muika.builtin_plugins") else ""
        entries.append(
            f"- {name} ({package_name}){builtin}: {description}"
            if description
            else f"- {name} ({package_name}){builtin}"
        )
    if not entries:
        return "No plugins are currently loaded."
    return "Loaded plugins:\n" + "\n".join(entries)
