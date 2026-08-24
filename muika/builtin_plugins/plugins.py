""".plugins —— 查看 / 重载 / 卸载已加载插件。"""

from __future__ import annotations

from arclet.alconna import Alconna, Args, CommandMeta, Subcommand

from muika.plugin.command import on_alconna
from muika.plugin.manager import PluginManager, get_plugin_manager
from muika.plugin.models import PluginMetadata

metadata = PluginMetadata(
    name="plugins",
    description="查看 / 重载 / 卸载已加载插件",
    usage=".plugins [reload [name] | unload <name>]",
)

alc = Alconna(
    "plugins",
    Subcommand(
        "reload",
        Args["name", str, ""],
        help_text="重载指定插件；未指定名字则重载所有用户插件",
        dest="reload",
    ),
    Subcommand(
        "unload",
        Args["name", str],
        help_text="卸载指定插件（builtin 插件拒绝卸载）",
        dest="unload",
    ),
    meta=CommandMeta("管理 Muika 已加载的插件"),
)

plugins_cmd = on_alconna(alc)


@plugins_cmd.assign("reload")
async def _reload(name: str, manager: PluginManager) -> str:
    """重载指定插件；未指定名字则重载所有用户插件。"""
    if name:
        ok = manager.reload(name)
        return f"[System] {'已重载' if ok else '重载失败'}：{name}"
    reloaded = manager.reload_all_user_plugins()
    if not reloaded:
        return "[System] 没有需要重载的用户插件"
    return f"[System] 已重载 {len(reloaded)} 个用户插件：{', '.join(reloaded)}"


@plugins_cmd.assign("unload")
async def _unload(name: str, manager: PluginManager) -> str:
    """卸载指定插件（builtin 插件拒绝卸载）。"""
    ok = manager.unload(name)
    return f"[System] {'已卸载' if ok else '卸载失败'}：{name}"


@plugins_cmd.handle()
async def _list(manager: PluginManager) -> str:
    """列出所有已加载插件。"""
    loaded = manager.list_loaded()
    if not loaded:
        return "[System] 当前没有加载任何插件"

    lines = ["已加载插件："]
    for package_name, info in loaded.items():
        tag = "[builtin]" if info["is_builtin"] else "[user]"
        lines.append(
            f"- {info['name']} ({package_name}) {tag} " f"— cmds: {info['commands']}, tools: {info['func_calls']}"
        )
    return "\n".join(lines)


# 注入 DI 表：CommandDispatcher 需要能注入 PluginManager
# 插件被加载时（此时 CommandDispatcher 已 setup），立即注册到 DI 表
def _register_di() -> None:
    """确保 PluginManager 已注册到 CommandDispatcher DI 表。"""
    from muika.plugin.command import CommandDispatcher

    try:
        dispatcher = CommandDispatcher.get()
    except RuntimeError:
        return
    if PluginManager not in dispatcher._injections:
        dispatcher._injections[PluginManager] = get_plugin_manager()


_register_di()
