""".model —— 模型配置管理命令。"""

from arclet.alconna import Alconna, Args, Subcommand

from muika.config import get_model_config_manager
from muika.plugin.command import on_alconna
from muika.plugin.models import PluginMetadata

metadata = PluginMetadata(
    name="model",
    description="Muika 模型配置管理",
    usage=".model <help|load|reload|list>",
)

alc = Alconna(
    "model",
    Subcommand("help", help_text="显示指令帮助", dest="help"),
    Subcommand("load", Args["config_name?", str], help_text="切换指定模型配置", dest="load"),
    Subcommand("reload", help_text="重新加载模型配置文件", dest="reload"),
    Subcommand("list", help_text="列出所有可用模型配置", dest="list"),
)
model_cmd = on_alconna(alc)


@model_cmd.assign("help")
async def _help() -> str:
    return (
        "Model 命令指南:\n"
        "  - help: 显示此帮助信息\n"
        "  - load <config_name>: 加载模型配置\n"
        "  - reload: 重新加载模型配置文件\n"
        "  - list: 列出所有可用的模型配置"
    )


@model_cmd.assign("reload")
async def _reload() -> str:
    manager = get_model_config_manager()
    try:
        manager._on_config_changed()
    except Exception as e:
        return f"[System] 重新加载模型配置失败: {e}"
    return "[System] 模型配置已重新加载"


@model_cmd.assign("load")
async def _load(config_name: str | None = None) -> str:
    manager = get_model_config_manager()
    if not config_name:
        return "[System] 请指定要加载的配置名，使用 .model list 查看可用配置"

    if config_name not in manager.configs:
        return f"[System] 配置 '{config_name}' 不存在，使用 .model list 查看可用配置"

    try:
        manager.change_current_config(manager.configs[config_name])
    except Exception as e:
        return f"[System] 切换模型配置失败: {e}"
    cfg = manager.current_config
    return f"[System] 已切换到配置 '{config_name}' ({cfg.model_name}, {cfg.provider})"  # type:ignore


@model_cmd.assign("list")
async def _list() -> str:
    manager = get_model_config_manager()
    configs = manager.configs

    outputs = ["目前所有可用的模型配置列表:"]
    for name, config in configs.items():
        is_default = config == manager.current_config
        is_current = "[当前]" if is_default else ""
        outputs.append(
            f"- {name}({config.provider}){is_current}: {config.model_name}" f"({'多模态' if config.multimodal else ''})"
        )
    return "\n".join(outputs)
