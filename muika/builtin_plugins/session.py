""".session —— 会话管理命令。"""

from arclet.alconna import Alconna, CommandMeta, Subcommand

from muika.core.events import SessionEndEvent
from muika.core.loop import Muika
from muika.plugin.command import on_alconna
from muika.plugin.models import PluginMetadata

metadata = PluginMetadata(
    name="session",
    description="Muika 会话管理",
    usage=".session <new|summarize> / .new / .clear",
)

# .session 命令
session_cmd = on_alconna(
    Alconna(
        "session",
        Subcommand("new", help_text="结束当前会话并开始新会话", dest="new"),
        Subcommand("summarize", help_text="整理已有素材为日记", dest="summarize"),
    )
)
new_cmd = on_alconna(Alconna("new", meta=CommandMeta("结束当前会话并开始新会话")), aliases={"clear"})


@session_cmd.assign("new")
@new_cmd.handle()
async def _session_new(muika: Muika) -> str:
    await muika.create_event(SessionEndEvent())
    return "[System] 已发送新会话请求"


@session_cmd.assign("summarize")
async def _session_summarize(muika: Muika) -> str:
    """手动整理当前已有素材。"""
    await muika.reflection.force_reflect()
    return ""
