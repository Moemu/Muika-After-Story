""".help —— 列出所有可用命令。"""

from arclet.alconna import Alconna

from muika.plugin.command import get_commands, on_alconna
from muika.plugin.models import PluginMetadata

metadata = PluginMetadata(
    name="help",
    description="列出所有可用命令",
    usage=".help",
)

alc = Alconna("help")
help_cmd = on_alconna(alc)


@help_cmd.handle()
async def _list_commands() -> str:
    lines = ["可用命令:"]
    for cmd in get_commands():
        name = cmd.alc.command
        help_text = cmd.alc.help_text if cmd.alc.help_text != "Unknown" else "无用法说明"
        aliases_str = f" (别名: {', '.join(sorted(cmd.aliases))})" if cmd.aliases else ""
        dests = [d for d in cmd.handlers if d != "__default__"]
        subs = ", ".join(dests) if dests else ""
        lines.append(f".{name}{aliases_str}: {help_text}")
        if subs:
            lines.append(f"子命令: {subs}")
    return "\n".join(lines)
