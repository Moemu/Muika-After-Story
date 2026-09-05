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
        Subcommand("summarize", help_text="立即保存当前会话摘要", dest="summarize"),
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
    """保存当前摘要，并如实报告等待重试的状态。"""
    if not any(turn.role == "user" for turn in muika.memory.recent_turns):
        return "[System] 当前没有需要保存的对话"
    if not await muika.update_session_memory():
        return "[System] 摘要暂未保存，当前对话已保留，请稍后重试"
    return "[System] 会话摘要已保存"
